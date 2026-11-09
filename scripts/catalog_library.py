#!/usr/bin/env python3
"""
catalog_library.py — scan a local folder of PDFs (papers, reports, theses) and
decide, per file, how much of it the Literature Assistant may index:

  full      the document carries an open licence (Creative Commons or public
            domain), so its full text is indexed
  abstract  no open licence found — only the abstract (or executive summary)
            is indexed. Covers paywalled papers AND "free to read" papers:
            being free to read is not a licence to reuse
  exclude   nothing usable (scanned with no text layer, or no abstract found)

Decisions are written to <library>/catalog.csv. Open it in Excel to check
them: the `override` column (full / abstract / exclude) always wins over the
automatic `access`, and re-running never touches your overrides, titles, or
notes. Just drop new PDFs in the folder (subfolders are fine) and re-run;
only new files are processed. Renamed or moved files are recognised by
content hash (MD5).

Normally this runs ONLINE, nightly, in .github/workflows/assistant_index.yml:
sync_drive_library.py downloads new PDFs from the Google Drive library folder
and the catalogue from its Google Sheet, this script decides, and the sheet
is updated. It also runs locally against any folder, for testing.

How the licence is found, in order:
  1. DOI in the PDF -> OpenAlex: licence of every known copy (publisher,
     repository). Any Creative Commons / public-domain copy -> full.
  2. A Creative Commons statement or licence URL on the first two pages or
     the last page of the PDF -> full.
  3. Otherwise -> abstract.
This is a cautious rule of thumb for a public, non-commercial site that
cites every source — not legal advice.

Extracted text is cached in data/rag/cache/library/ (gitignored), which
build_rag_corpus.py reads. The PDFs themselves never enter the repo: keep
the library folder OUTSIDE it (the Pages deploy publishes all of data/).

Library location: --library, or LIBRARY_DIR in .env, default
/mnt/c/AI_work/orangutan_library. Text cache: LIBRARY_TEXT_CACHE, default
data/rag/cache/library/.

Usage:
    python scripts/catalog_library.py
    python scripts/catalog_library.py --recheck     # redo automatic decisions (keeps overrides)
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("This script needs the 'requests' library.\nInstall it with:  pip install requests")
try:
    from pypdf import PdfReader
except ImportError:
    sys.exit("This script needs the 'pypdf' library.\n"
             "Install it with:  pip install --user --break-system-packages pypdf")

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
TEXT_CACHE = Path(os.environ.get("LIBRARY_TEXT_CACHE") or REPO_ROOT / "data" / "rag" / "cache" / "library")
DEFAULT_LIBRARY = "/mnt/c/AI_work/orangutan_library"
CONTACT_EMAIL = "sergewich@gmail.com"

COLUMNS = ["file", "md5", "title", "authors", "year", "doc_type", "doi", "url",
           "license", "access", "reason", "override", "notes"]
# Columns a person may edit; kept as-is on re-runs.
USER_COLUMNS = {"title", "authors", "year", "doc_type", "url", "override", "notes"}

OPEN_LICENSES = {"cc-by", "cc-by-sa", "cc-by-nc", "cc-by-nc-sa", "cc-by-nd",
                 "cc-by-nc-nd", "cc0", "public-domain"}
OPENALEX_TYPES = {"article": "journal", "review": "journal", "letter": "journal",
                  "dissertation": "thesis", "report": "report",
                  "book": "book", "book-chapter": "book"}

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)", re.I)
CC_URL_RE = re.compile(r"creativecommons\.org/(licenses/(by(?:-nc)?(?:-sa|-nd)?)|publicdomain/zero)", re.I)
CC_TEXT_RE = re.compile(r"\bCC[\s-]?(BY(?:[\s-]NC)?(?:[\s-](?:SA|ND))?)\b|Creative Commons Attribution", re.I)
# Many NGO/IUCN reports aren't CC-licensed but say e.g. "Reproduction of this
# publication for educational or other non-commercial purposes is authorised
# without prior written permission ... provided the source is fully
# acknowledged" — permission for exactly what this non-commercial, citing
# site does.
NONCOMMERCIAL_RE = re.compile(
    r"(reproduction|reproduce)[^.]{0,120}non-?commercial[^.]{0,150}(authori[sz]ed|permitted|without prior)"
    r"|only non-?commercial uses? of the work (is|are) permitted", re.I)
THESIS_RE = re.compile(r"\b(thesis|dissertation|doctor of philosophy|ph\.?\s?d\b|master of science|"
                       r"submitted in (partial )?fulfil+ment|skripsi|disertasi|tesis)\b", re.I)
ABSTRACT_HEAD_RE = re.compile(
    r"^\s*(abstract|summary|executive summary|ringkasan( eksekutif)?|abstrak|samenvatting)\s*:?\s*$",
    re.I | re.M)
NEXT_HEAD_RE = re.compile(
    r"^\s*(\d+\.?\s+)?(keywords?|kata kunci|introduction|pendahuluan|table of contents|contents|"
    r"acknowledg|background|1\.?\s+[A-Z])", re.I | re.M)

MIN_CHARS_PER_PAGE = 200   # below this on average, treat the PDF as scanned


def load_env() -> None:
    env = REPO_ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def norm(s: str | None) -> str:
    return re.sub(r"\W+", " ", (s or "").lower()).strip()


def md5_of(path: Path) -> str:
    # MD5 because Google Drive reports it for every file without a download
    # (see sync_drive_library.py); it is an identity check here, not security.
    h = hashlib.md5()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# PDF text
# --------------------------------------------------------------------------- #
def extract_pages(path: Path) -> tuple[list[str], dict]:
    logging.getLogger("pypdf").setLevel(logging.ERROR)  # malformed-PDF warnings are noise here
    reader = PdfReader(str(path))
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # one bad page shouldn't sink the document
            pages.append("")
    meta = reader.metadata or {}
    return pages, {"title": (meta.get("/Title") or "").strip()}


def ocr_pages(path: Path) -> list[str] | None:
    """OCR a scanned PDF with ocrmypdf (English, Indonesian, Dutch), if it's
    installed — it is in the nightly GitHub Actions run. Returns None if not."""
    if not shutil.which("ocrmypdf"):
        return None
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "ocr.pdf"
        result = subprocess.run(
            ["ocrmypdf", "--quiet", "--skip-text", "-l", "eng+ind+nld", str(path), str(out)],
            capture_output=True, timeout=1800)
        if result.returncode != 0 or not out.exists():
            print(f"  OCR failed: {result.stderr.decode(errors='replace')[-300:]}", file=sys.stderr)
            return None
        return extract_pages(out)[0]


def find_doi(text: str) -> str | None:
    m = DOI_RE.search(text)
    return m.group(1).rstrip(".,;)]}").lower() if m else None


def license_in_pdf(pages: list[str]) -> str | None:
    # Reports put the licence on the imprint page, often a few pages in.
    text = "\n".join(pages[:6] + pages[-2:])
    # Re-join words split across lines ("non-\ncommercial") and collapse
    # whitespace, or statements laid out over several lines never match.
    text = re.sub(r"\s+", " ", re.sub(r"-\s*\n\s*", "-", text))
    m = CC_URL_RE.search(text)
    if m:
        return f"cc-{m.group(2).lower()}" if m.group(2) else "cc0"
    m = CC_TEXT_RE.search(text)
    if m:
        return "cc-" + re.sub(r"[\s-]+", "-", m.group(1).lower()) if m.group(1) else "cc-by"
    if NONCOMMERCIAL_RE.search(text):
        return "non-commercial reproduction permitted"
    return None


def abstract_from_pdf(pages: list[str]) -> str | None:
    """The Abstract / (Executive) Summary section, if it can be found in the
    first ~15 pages (theses and reports bury it after front matter)."""
    text = "\n".join(pages[:15])
    m = ABSTRACT_HEAD_RE.search(text)
    if not m:
        # Many papers run the heading into the paragraph: "Abstract: Orangutans ..."
        m = re.search(r"\b(abstract|summary)\s*[:.—–-]\s*", text, re.I)
        if not m:
            return None
    rest = text[m.end():]
    end = NEXT_HEAD_RE.search(rest)
    body = rest[: end.start()] if end and end.start() > 300 else rest[:3500]
    body = re.sub(r"\s+", " ", body).strip()
    return body[:3500] if len(body) >= 300 else None


def full_text(pages: list[str]) -> str:
    text = "\n".join(pages)
    text = re.sub(r"-\n(\w)", r"\1", text)  # re-join hyphenated line breaks
    # Drop the reference list — it's mostly other papers' titles and pollutes retrieval.
    refs = list(re.finditer(r"^\s*(references|literature cited|bibliography|daftar pustaka|"
                            r"referensi|literatuur)\s*$", text, re.I | re.M))
    if refs and refs[-1].start() > len(text) * 0.5:
        text = text[: refs[-1].start()]
    return re.sub(r"\s+", " ", text).strip()


# --------------------------------------------------------------------------- #
# OpenAlex
# --------------------------------------------------------------------------- #
def openalex_request(session, params: dict):
    """GET /works, waiting out OpenAlex's short-term rate limit (429 with
    budget left) and only giving up when the daily budget is really gone."""
    for attempt in range(5):
        resp = session.get("https://api.openalex.org/works", params=params, timeout=30)
        if resp.status_code != 429:
            resp.raise_for_status()
            return resp
        remaining = resp.headers.get("X-RateLimit-Remaining-USD")
        if remaining is not None and float(remaining) <= 0:
            break
        time.sleep(float(resp.headers.get("Retry-After") or 2 ** attempt))
    raise RuntimeError("OpenAlex daily budget used up (or still rate-limited) — set OPENALEX_API_KEY "
                       "in .env, or re-run tomorrow; finished lookups are cached")


def openalex_get(session: requests.Session, **params) -> list[dict]:
    params.update({"mailto": CONTACT_EMAIL, "per-page": params.get("per-page", 5),
                   "select": "id,doi,title,publication_year,type,authorships,"
                             "abstract_inverted_index,locations,best_oa_location,primary_location"})
    if os.environ.get("OPENALEX_API_KEY"):
        params["api_key"] = os.environ["OPENALEX_API_KEY"]
    resp = openalex_request(session, params)
    time.sleep(0.15)
    return resp.json().get("results", [])


def lookup_openalex(session, doi: str | None, title_guess: str, first_page: str) -> dict | None:
    if doi:
        res = openalex_get(session, filter=f"doi:{doi}")
        if res:
            return res[0]
    # No DOI (or unknown DOI): title search, accepted only if the found title
    # actually appears on the first page — avoids attaching the wrong paper.
    q = norm(title_guess)
    if len(q) < 15:
        return None
    page = norm(first_page)
    for w in openalex_get(session, filter=f"title.search:{q[:200]}"):
        t = norm(w.get("title"))
        if len(t) >= 15 and t in page:
            return w
    return None


def first_line_title(first_page: str) -> str:
    """Best-effort title guess: the first reasonably long line on page one."""
    for line in first_page.splitlines():
        line = line.strip()
        if 25 <= len(line) <= 250 and not DOI_RE.search(line) and not re.search(r"journal|vol\.|http", line, re.I):
            return line
    return ""


def openalex_abstract(w: dict) -> str | None:
    inv = w.get("abstract_inverted_index")
    if not inv:
        return None
    return " ".join(word for _, word in sorted((p, word) for word, ps in inv.items() for p in ps))


def openalex_license(w: dict) -> tuple[str | None, str | None]:
    """(licence of the published version, note about other versions).

    Only a licence on the *published* version covers the PDF you most likely
    hold. Repository copies of submitted/accepted manuscripts are often CC
    licensed (and their metadata is sometimes wrong), but that licence covers
    that manuscript file, not the publisher's PDF."""
    other = None
    for loc in w.get("locations") or []:
        lic = loc.get("license")
        if lic not in OPEN_LICENSES:
            continue
        if loc.get("version") == "publishedVersion":
            return lic, None
        if not other:
            repo = (loc.get("source") or {}).get("display_name") or "a repository"
            version = (loc.get("version") or "manuscript").replace("Version", " version")
            other = (f"an openly licensed {version} ({lic}) exists in {repo} — put that file in the "
                     f"library instead to index full text")
    return None, other


# --------------------------------------------------------------------------- #
def catalog_file(path: Path, md5: str, session) -> dict:
    row = {c: "" for c in COLUMNS}
    row["md5"] = md5
    try:
        pages, meta = extract_pages(path)
    except Exception as exc:
        row.update(title=path.stem, access="exclude", reason=f"could not read PDF: {exc}")
        return row

    def is_scanned(pp: list[str]) -> bool:
        return not pp or sum(len(p) for p in pp) / len(pp) < MIN_CHARS_PER_PAGE

    ocr_note = ""
    if is_scanned(pages):
        print("  scanned PDF — running OCR")
        ocr = ocr_pages(path)
        if not ocr or is_scanned(ocr):
            row.update(title=meta["title"] or path.stem, access="exclude",
                       reason="no text layer (scanned) and OCR " +
                              ("unavailable" if ocr is None else "found too little text"))
            return row
        pages, ocr_note = ocr, " (text from OCR — check quality)"

    head = "\n".join(pages[:3])
    doi = find_doi(head)
    meta_title = meta["title"]
    if re.match(r"(microsoft (word|powerpoint)|untitled)\b", meta_title, re.I) or re.search(r"\.(docx?|pdf|indd)$", meta_title, re.I):
        meta_title = ""  # a leftover file name, not a title
    title_guess = meta_title if len(meta_title) > 15 else first_line_title(pages[0])
    work = lookup_openalex(session, doi, title_guess, pages[0])

    abstract = None
    if work:
        doi = (work.get("doi") or "").replace("https://doi.org/", "") or doi
        row["title"] = work.get("title") or title_guess or path.stem
        row["authors"] = ", ".join((a.get("author") or {}).get("display_name", "")
                                   for a in (work.get("authorships") or [])[:4])
        row["year"] = work.get("publication_year") or ""
        row["doc_type"] = OPENALEX_TYPES.get(work.get("type"), "report")
        oa = work.get("best_oa_location") or {}
        row["url"] = f"https://doi.org/{doi}" if doi else (oa.get("landing_page_url") or "")
        abstract = openalex_abstract(work)
    else:
        row["title"] = title_guess or path.stem
        row["url"] = f"https://doi.org/{doi}" if doi else ""
        y = re.findall(r"\b(19[5-9]\d|20[0-4]\d)\b", pages[0])
        row["year"] = max(y) if y else ""
    if not row["doc_type"]:
        row["doc_type"] = "thesis" if THESIS_RE.search(head) else ("journal" if doi else "report")
    row["doi"] = doi or ""

    lic, other_version = openalex_license(work) if work else (None, None)
    lic_source = "OpenAlex (published version)" if lic else None
    if not lic:
        lic = license_in_pdf(pages)
        lic_source = "statement in the PDF" if lic else None

    abstract = abstract or abstract_from_pdf(pages)
    if lic:
        row.update(license=lic, access="full", reason=f"open licence ({lic}) per {lic_source}{ocr_note}")
    elif abstract:
        row.update(access="abstract",
                   reason=(other_version or
                           "no open licence found — abstract only (free-to-read is not a licence to reuse)")
                   + ocr_note)
    else:
        row.update(access="exclude",
                   reason="no open licence and no abstract/summary section found — set override=full if "
                          "you know reuse is permitted")

    TEXT_CACHE.mkdir(parents=True, exist_ok=True)
    (TEXT_CACHE / f"{md5}.json").write_text(json.dumps(
        {"full_text": full_text(pages), "abstract": abstract}, ensure_ascii=False))
    return row


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--library", help="folder of PDFs (default: LIBRARY_DIR or %s)" % DEFAULT_LIBRARY)
    parser.add_argument("--recheck", action="store_true", help="redo automatic decisions for every file")
    parser.add_argument("--time-budget", type=float, default=0,
                        help="stop starting new files after this many minutes (OCR of a big batch can "
                             "outlast a CI job; files not reached are picked up next run)")
    parser.add_argument("--remote-listing", help="JSON list of {file, md5} for every file in the online "
                        "library (written by sync_drive_library.py). Catalogue rows for listed files "
                        "are kept even when the PDF wasn't downloaded this run.")
    args = parser.parse_args()
    load_env()

    library = Path(args.library or os.environ.get("LIBRARY_DIR") or DEFAULT_LIBRARY)
    if not library.is_dir():
        sys.exit(f"Library folder not found: {library}\nCreate it and put PDFs in it, or pass --library.")
    if REPO_ROOT in library.resolve().parents or library.resolve() == REPO_ROOT:
        sys.exit("Keep the library folder outside the repo — the Pages deploy publishes data/ publicly.")
    catalog_path = library / "catalog.csv"

    old = {}
    if catalog_path.exists():
        with catalog_path.open(newline="", encoding="utf-8-sig") as f:
            old = {r["md5"]: r for r in csv.DictReader(f) if r.get("md5")}

    session = requests.Session()
    session.headers["User-Agent"] = f"orangutan-dashboard/1.0 (mailto:{CONTACT_EMAIL})"
    rows, new, seen = [], [], set()

    pdfs = sorted(p for p in library.rglob("*") if p.suffix.lower() == ".pdf" and p.is_file())
    started, deferred = time.time(), 0
    for i, path in enumerate(pdfs, 1):
        md5 = md5_of(path)
        if md5 in seen:  # the same PDF twice in the library
            continue
        seen.add(md5)
        rel = path.relative_to(library).as_posix()
        prev = old.get(md5)
        if prev and not args.recheck and (TEXT_CACHE / f"{md5}.json").exists():
            prev["file"] = rel  # picks up renames/moves
            rows.append(prev)
            continue
        if args.time_budget and time.time() - started > args.time_budget * 60:
            deferred += 1  # not catalogued: no row, no cache, so the next run picks it up
            continue
        print(f"[{i}/{len(pdfs)}] {rel}")
        try:
            row = catalog_file(path, md5, session)
        except (requests.RequestException, RuntimeError) as exc:
            print(f"  WARNING: skipped for now ({exc}) — will retry next run", file=sys.stderr)
            if prev:
                rows.append(prev)
            continue
        row["file"] = rel
        cache = TEXT_CACHE / f"{md5}.json"
        if not cache.exists():  # excluded files too, so they aren't re-downloaded nightly
            TEXT_CACHE.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"full_text": "", "abstract": None}))
        if prev:  # --recheck: keep what the person edited
            row.update({k: prev[k] for k in USER_COLUMNS if prev.get(k)})
        rows.append(row)
        new.append(row)
        if len(new) % 10 == 0:  # checkpoint, so a crash doesn't lose progress
            write_catalog(catalog_path, rows)

    if args.remote_listing:
        for entry in json.loads(Path(args.remote_listing).read_text()):
            md5 = entry["md5"]
            if md5 not in seen and md5 in old:
                seen.add(md5)
                old[md5]["file"] = entry["file"]
                rows.append(old[md5])

    write_catalog(catalog_path, rows)

    for r in new:
        print(f"  {r['access'].upper():8} {r['file']}\n           {r['title'][:90]}\n           {r['reason']}")
    counts = {}
    for r in rows:
        eff = r.get("override") or r["access"]
        counts[eff] = counts.get(eff, 0) + 1
    if deferred:
        print(f"\nTime budget reached: {deferred} files left for the next run.")
    print(f"\n{len(rows)} files in {catalog_path} ({len(new)} new): "
          + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))


def write_catalog(path: Path, rows: list[dict]) -> None:
    # utf-8-sig so Excel opens accented author names correctly.
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: r["file"].lower()))


if __name__ == "__main__":
    main()
