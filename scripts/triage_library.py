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
    DOI_RE, MIN_CHARS_PER_PAGE, abstract_from_pdf, find_doi, first_line_title, license_in_pdf,
    load_env, lookup_openalex, md5_of, norm, openalex_license,
)

import logging  # noqa: E402

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


def decide(path: Path, index, session) -> dict:
    by_doi, by_title = index
    row = {"file": str(path), "decision": "", "reason": "", "title": "", "doi": "", "licence": "", "est_passages": 0}
    try:
        pages, n_pages, meta_title = sample_pages(path)
    except Exception as exc:
        row.update(decision="skip", reason=f"unreadable PDF ({exc.__class__.__name__})")
        return row

    if not pages or sum(len(p) for p in pages) / len(pages) < MIN_CHARS_PER_PAGE:
        row.update(decision="upload", reason="scanned, no text layer — the nightly job OCRs it; can't check "
                   "if it's already indexed", est_passages=max(1, n_pages * CHARS_PER_PAGE_EST // CHARS_PER_PASSAGE // 3))
        return row

    head = "\n".join(pages[:3])
    doi = find_doi(head)
    if re.match(r"(microsoft (word|powerpoint)|untitled)\b", meta_title, re.I) or re.search(r"\.(docx?|pdf)$", meta_title, re.I):
        meta_title = ""
    title_guess = meta_title if len(meta_title) > 15 else first_line_title(pages[0])

    known = by_doi.get(doi) if doi else None
    if not known and title_guess:
        known = by_title.get(norm(title_guess))
    if known and known["fulltext"]:
        row.update(decision="skip", reason="already indexed in full text", doi=doi or "", title=title_guess)
        return row

    try:
        work = lookup_openalex(session, doi, title_guess, pages[0])
    except (requests.RequestException, RuntimeError):
        work = None
    if work:
        doi = (work.get("doi") or "").replace("https://doi.org/", "").lower() or doi
        title_guess = work.get("title") or title_guess
        known = known or by_doi.get(doi) or by_title.get(norm(title_guess))
        if known and known["fulltext"]:
            row.update(decision="skip", reason="already indexed in full text", doi=doi or "", title=title_guess)
            return row
    lic, _ = openalex_license(work) if work else (None, None)
    lic = lic or license_in_pdf(pages)
    full_est = max(1, min(n_pages * CHARS_PER_PAGE_EST // CHARS_PER_PASSAGE, 400))
    row.update(title=title_guess, doi=doi or "", licence=lic or "")

    if lic:
        row.update(decision="upload", est_passages=full_est,
                   reason="openly licensed — adds full text" + (" (only the abstract is indexed now)" if known else ""))
    elif known and known["abstract"]:
        row.update(decision="skip", reason="not openly licensed and its abstract is already indexed")
    elif known:
        row.update(decision="upload", est_passages=2,
                   reason="in the index without an abstract — the PDF's own abstract fills the gap")
    else:
        has_abs = bool(abstract_from_pdf(pages))
        row.update(decision="upload", est_passages=2 if has_abs else 0,
                   reason="not in the index yet — adds its abstract/summary" if has_abs else
                   "not in the index; no licence or abstract found — will be excluded unless you set "
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
    print(f"{len(pdfs)} PDFs in {src}")
    rows, seen = [], set()
    for i, path in enumerate(pdfs, 1):
        md5 = md5_of(path)
        if md5 in seen:
            rows.append({"file": str(path), "decision": "skip", "reason": "duplicate of another file here"})
            continue
        seen.add(md5)
        if md5 in in_drive:
            rows.append({"file": str(path), "decision": "skip", "reason": "already in the Drive library"})
            continue
        row = decide(path, index, session)
        rows.append(row)
        if row["decision"] == "upload" and not args.dry_run:
            dest = out / path.name
            k = 1
            while dest.exists():  # same file name from different subfolders
                dest = out / f"{path.stem}_{k}{path.suffix}"
                k += 1
            shutil.copy2(path, dest)
        if i % 25 == 0:
            print(f"  ...{i}/{len(pdfs)}")

    report = out / "triage_report.csv"
    cols = ["decision", "reason", "est_passages", "title", "doi", "licence", "file"]
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
