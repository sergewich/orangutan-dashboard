#!/usr/bin/env python3
"""
export_library_refs.py — list the PDF library on the public Literature tab.

Reads the library catalogue (catalog.csv, as pulled from the Google Sheet by
sync_drive_library.py) and writes data/literature/library.json: references
only — title, authors, year, type, and a DOI/URL link. Never the PDFs, their
text, or abstracts: the repo and data/ are public, and most of the library
isn't licensed for reuse.

Which rows are listed (the Sheet's `show` column always wins):
  show = yes   listed
  show = no    hidden
  empty        listed only if the metadata is trustworthy: the DOI was found
               and OpenAlex supplied the authors, and the cached text mentions
               orangutans. Rows whose title was guessed from the PDF (often a
               file name or a garbled scan) stay hidden until someone fixes
               the title/authors/year in the Sheet and sets show = yes.
Works already in literature.json (same DOI or title) are skipped.

Usage:
    python scripts/export_library_refs.py --catalog /tmp/library/catalog.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from catalog_library import TEXT_CACHE  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
LITERATURE = REPO_ROOT / "data" / "literature" / "literature.json"
OUT = REPO_ROOT / "data" / "literature" / "library.json"
TYPES = {"journal", "book", "thesis", "report"}
ORANGUTAN_RE = re.compile(r"orang[\s-]?(utan|outan|oetan)|\bpongo\b", re.I)


def norm(s: str) -> str:
    return re.sub(r"\W+", " ", (s or "").lower()).strip()


def mentions_orangutans(md5: str) -> bool:
    cache = TEXT_CACHE / f"{md5}.json"
    if not cache.exists():
        return False
    d = json.loads(cache.read_text())
    return bool(ORANGUTAN_RE.search((d.get("abstract") or "") + " " + (d.get("full_text") or "")[:20000]))


def safe_url(row: dict) -> str | None:
    if row.get("doi"):
        return "https://doi.org/" + row["doi"].strip()
    url = (row.get("url") or "").strip()
    return url if url.startswith(("https://", "http://")) else None


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--catalog", required=True, help="catalog.csv from sync_drive_library.py")
    args = p.parse_args()

    with open(args.catalog, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    known = json.loads(LITERATURE.read_text()) if LITERATURE.exists() else []
    known_titles = {norm(r.get("title")) for r in known}
    known_urls = {(r.get("url") or "").lower() for r in known}

    out, seen, hidden = [], set(), 0
    for r in rows:
        show = (r.get("show") or "").strip().lower()
        if show in ("no", "n", "false", "0"):
            continue
        if show not in ("yes", "y", "true", "1"):
            if not (r.get("doi") and r.get("authors") and mentions_orangutans(r["md5"])):
                hidden += 1
                continue
        title, url = (r.get("title") or "").strip(), safe_url(r)
        key = norm(title)
        if not title or key in seen or key in known_titles or (url and url.lower() in known_urls):
            continue
        seen.add(key)
        year = (r.get("year") or "").strip()
        out.append({
            "type": r["doc_type"] if r.get("doc_type") in TYPES else "report",
            "title": title,
            "authors": (r.get("authors") or "").strip() or None,
            "year": int(year) if year.isdigit() else None,
            "source": None,
            "url": url,
            "open_access": (r.get("override") or r.get("access")) == "full",
            "language": None,
            "library": True,
        })

    out.sort(key=lambda x: (-(x["year"] or 0), x["title"].lower()))
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n")
    print(f"Wrote {len(out)} library references to {OUT.relative_to(REPO_ROOT)} "
          f"({hidden} hidden until checked in the Sheet: set show=yes)")


if __name__ == "__main__":
    main()
