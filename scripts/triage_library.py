#!/usr/bin/env python3
"""
triage_library.py — go through a big local folder of PDFs and copy only the
ones worth adding to the Literature Assistant's Google Drive library into a
separate folder, ready to drag into Drive. Never changes, moves or deletes
anything in the source folder.

Worth uploading:
  - openly licensed papers whose full text isn't indexed yet (adds full text)
  - anything not in the index at all: reports, theses, older or obscure papers
    (adds at least its abstract/summary)
  - papers in the index without an abstract (the PDF's own abstract fills it)
  - scanned PDFs with no text layer (the nightly job OCRs them)
Skipped:
  - papers already indexed in full text (Europe PMC)
  - non-openly-licensed papers whose abstract is already indexed (only the
    abstract could be used, and it's already there)
  - files already in the Drive library (same content)

"Already indexed" is judged against data/rag/corpus.jsonl, so build the
corpus first (scripts/build_rag_corpus.py). Licences are decided exactly as
the nightly cataloguer does (catalog_library.py), so what this predicts is
what the library will index.

Usage (Windows paths are fine):
    python3 scripts/triage_library.py "C:\\Users\\serge\\Documents\\Papers"
    python3 scripts/triage_library.py "C:\\...\\Papers" --out "C:\\AI_work\\orangutan_upload" --dry-run

Writes triage_report.csv (one row per PDF: decision, reason, estimated
passages) into the output folder.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))
from catalog_library import (  # noqa: E402
    MIN_CHARS_PER_PAGE, abstract_from_pdf, find_doi, first_line_title, license_in_pdf,
    load_env, md5_of, norm, openalex_license,
)

import logging  # noqa: E402
logging.disable(logging.WARNING)  # pypdf font/encoding warnings are noise here

import requests  # noqa: E402
from pypdf import PdfReader  # noqa: E402

CORPUS = REPO_ROOT / "data" / "rag" / "corpus.jsonl"
CHARS_PER_PAGE_EST = 3200
CHARS_PER_PASSAGE = 2200


def win_to_wsl(p: str) -> Path:
    if re.match(r"^[A-Za-z]:\\", p):
        return Path("/mnt/" + p[0].lower() + p[2:].replace("\\", "/"))
    return Path(p)


def load_index() -> tuple[dict, dict]:
    """doi -> {"fulltext", "abstract"} and normalised title -> same, from the corpus."""
    by_doi, by_title = {}, {}
    for line in CORPUS.open():
        p = json.loads(line)
        key = p.get("source", "")
        doi = key[4:] if key.startswith("doi:") else ""
        if p["id"].startswith("lib-"):  # library copies replace the catalogue entry in the corpus
            m = re.search(r"doi\.org/(10\.\S+)", p.get("url") or "")
            doi = m.group(1).lower() if m else ""
        state = {"fulltext": p.get("kind") == "fulltext", "abstract": p.get("kind") in ("abstract", "text")}
        for d, k in ((by_doi, doi), (by_title, norm(p.get("title")))):
            if not k:
                continue
            cur = d.setdefault(k, {"fulltext": False, "abstract": False})
            cur["fulltext"] |= state["fulltext"]
            cur["abstract"] |= state["abstract"]
    return by_doi, by_title


def sample_pages(path: Path) -> tuple[list[str], int, str]:
    """First 6 and last 2 pages only (enough for DOI, licence and abstract) —
    reading every page of thousands of PDFs would take hours."""
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    reader = PdfReader(str(path))
    n = len(reader.pages)
    idx = sorted(set(list(range(min(6, n))) + list(range(max(0, n - 2), n))))
    pages = []
    for i in idx:
        try:
            pages.append(reader.pages[i].extract_text() or "")
        except Exception:
            pages.append("")
    title = ((reader.metadata or {}).get("/Title") or "").strip()
    return pages, n, title


def inspect(path: Path) -> dict:
    """Pass 1, local only: what can be read from the PDF itself."""
    info = {"file": str(path), "doi": "", "title": "", "licence": "", "pages": 0,
            "scanned": False, "has_abstract": False, "error": "", "head": ""}
    try:
        pages, n_pages, meta_title = sample_pages(path)
    except Exception as exc:
        info["error"] = exc.__class__.__name__
        return info
    info["pages"] = n_pages
    if not pages or sum(len(p) for p in pages) / len(pages) < MIN_CHARS_PER_PAGE:
        info["scanned"] = True
        return info
    if re.match(r"(microsoft (word|powerpoint)|untitled)\b", meta_title, re.I) or re.search(r"\.(docx?|pdf)$", meta_title, re.I):
        meta_title = ""
    info["doi"] = find_doi("\n".join(pages[:3])) or ""
    info["title"] = meta_title if len(meta_title) > 15 else first_line_title(pages[0])
    info["licence"] = license_in_pdf(pages) or ""
    info["has_abstract"] = bool(abstract_from_pdf(pages))
    info["head"] = " " + norm(" ".join(pages[:2])) + " "  # for matching known titles (dropped from the report)
    return info


def openalex_by_doi(dois: list[str], session) -> dict:
    """Pass 2: one OpenAlex request per 50 DOIs (a DOI filter costs $0.0001;
    title searches cost 10x that, so the triage never does them)."""
    import os
    out = {}
    key = os.environ.get("OPENALEX_API_KEY")
    for i in range(0, len(dois), 50):
        batch = dois[i:i + 50]
        params = {"filter": "doi:" + "|".join(batch), "per-page": 100,
                  "select": "doi,title,locations,best_oa_location,abstract_inverted_index",
                  "mailto": "sergewich@gmail.com"}
        if key:
            params["api_key"] = key
        try:
            resp = session.get("https://api.openalex.org/works", params=params, timeout=60)
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"  WARNING: OpenAlex batch failed ({exc}); those PDFs are judged from the PDF alone")
            continue
        for w in resp.json().get("results", []):
            out[(w.get("doi") or "").replace("https://doi.org/", "").lower()] = w
    return out


_TITLES: list[str] = []


def title_index(by_title: dict) -> list[str]:
    """Known titles long enough to be distinctive, longest first."""
    if not _TITLES:
        _TITLES.extend(sorted((t for t in by_title if len(t) >= 30 and len(t.split()) >= 5),
                              key=len, reverse=True))
    return _TITLES


def decide(info: dict, index, oa: dict) -> dict:
    by_doi, by_title = index
    row = {**info, "decision": "", "reason": "", "est_passages": 0}
    if info["error"]:
        row.update(decision="skip", reason=f"unreadable PDF ({info['error']})")
        return row
    if info["scanned"]:
        # Old scans are rarely openly licensed, so usually only an abstract gets indexed.
        row.update(decision="upload", reason="scanned, no text layer — the nightly job OCRs it; can't check "
                   "if it's already indexed", est_passages=2)
        return row

    work = oa.get(info["doi"]) if info["doi"] else None
    if work and work.get("title"):
        row["title"] = work["title"]
    known = (by_doi.get(info["doi"]) if info["doi"] else None) or by_title.get(norm(row["title"]))
    if not known:
        # Older PDFs often have no DOI and a useless title in their metadata,
        # so look for any known (reasonably distinctive) title on the first pages.
        for t in title_index(by_title):
            if f" {t} " in info["head"]:
                known, row["title"] = by_title[t], t
                break
    if known and known["fulltext"]:
        row.update(decision="skip", reason="already indexed in full text")
        return row

    lic = (openalex_license(work)[0] if work else None) or info["licence"]
    row["licence"] = lic or ""
    full_est = max(1, min(info["pages"] * CHARS_PER_PAGE_EST // CHARS_PER_PASSAGE, 400))
    if lic:
        row.update(decision="upload", est_passages=full_est,
                   reason="openly licensed — adds full text" + (" (only the abstract is indexed now)" if known else ""))
    elif known and known["abstract"]:
        row.update(decision="skip", reason="not openly licensed and its abstract is already indexed")
    elif known:
        row.update(decision="upload", est_passages=2,
                   reason="in the index without an abstract — the PDF's own abstract fills the gap")
    elif info["has_abstract"] or (work and work.get("abstract_inverted_index")):
        row.update(decision="upload", est_passages=2, reason="not in the index yet — adds its abstract/summary")
    else:
        row.update(decision="upload", est_passages=0,
                   reason="not in the index; no licence or abstract found — will be excluded unless you set "
                   "override=full in the sheet (do so if you know reuse is permitted)")
    return row


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="folder with your PDFs (searched recursively; never modified)")
    ap.add_argument("--out", default=r"C:\AI_work\orangutan_upload", help="where to copy PDFs worth uploading")
    ap.add_argument("--dry-run", action="store_true", help="write the report only, copy nothing")
    args = ap.parse_args()
    load_env()

    src, out = win_to_wsl(args.source), win_to_wsl(args.out)
    if not src.is_dir():
        sys.exit(f"Folder not found: {src}")
    if not CORPUS.exists():
        sys.exit("data/rag/corpus.jsonl not found — run scripts/build_rag_corpus.py first.")
    out.mkdir(parents=True, exist_ok=True)

    index = load_index()
    in_drive = set()
    try:  # what's already in the Drive library (read-only listing via the robot account)
        import os
        from sync_drive_library import list_pdfs, session as drive_session
        if os.environ.get("DRIVE_FOLDER_ID"):
            in_drive = {f["md5"] for f in list_pdfs(drive_session(), os.environ["DRIVE_FOLDER_ID"])}
            print(f"{len(in_drive)} PDFs already in the Drive library")
    except (Exception, SystemExit) as exc:
        print(f"  (couldn't list the Drive library, so duplicates of it won't be skipped: {exc})")
    session = requests.Session()

    pdfs = sorted(p for p in src.rglob("*") if p.suffix.lower() == ".pdf" and p.is_file())
    print(f"{len(pdfs)} PDFs in {src} — reading them (cloud-only files download first, so this can take a while)")
    rows, infos, seen = [], [], set()
    for i, path in enumerate(pdfs, 1):
        md5 = md5_of(path)
        if md5 in seen:
            rows.append({"file": str(path), "decision": "skip", "reason": "duplicate of another file here"})
        elif md5 in in_drive:
            rows.append({"file": str(path), "decision": "skip", "reason": "already in the Drive library"})
        else:
            infos.append((path, inspect(path)))
        seen.add(md5)
        if i % 50 == 0:
            print(f"  ...read {i}/{len(pdfs)}")

    dois = sorted({info["doi"] for _, info in infos if info["doi"]})
    print(f"Looking up {len(dois)} DOIs in OpenAlex ({(len(dois) + 49) // 50} requests)")
    oa = openalex_by_doi(dois, session)

    for path, info in infos:
        row = decide(info, index, oa)
        rows.append(row)
        if row["decision"] == "upload" and not args.dry_run:
            dest = out / path.name
            k = 1
            while dest.exists():  # same file name from different subfolders
                dest = out / f"{path.stem}_{k}{path.suffix}"
                k += 1
            shutil.copy2(path, dest)

    report = out / "triage_report.csv"
    cols = ["decision", "reason", "est_passages", "title", "doi", "licence", "pages", "file"]
    with report.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: (r["decision"], r.get("reason", ""))))

    reasons = {}
    for r in rows:
        reasons[(r["decision"], r["reason"].split(" — ")[0])] = reasons.get((r["decision"], r["reason"].split(" — ")[0]), 0) + 1
    print()
    for (d, why), n in sorted(reasons.items()):
        print(f"  {d.upper():6} {n:5}  {why}")
    up = [r for r in rows if r["decision"] == "upload"]
    est = sum(int(r.get("est_passages") or 0) for r in up)
    print(f"\n{len(up)} PDFs to upload (~{est} passages ≈ {est * 256 / 1e6:.2f}M of the free 5M stored dimensions)")
    print(f"{'Would copy' if args.dry_run else 'Copied'} them to {out}; full list in {report}")


if __name__ == "__main__":
    main()
