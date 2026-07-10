#!/usr/bin/env python3
"""
fetch_literature.py — pull real orangutan-related references from four free,
keyless public APIs and write one combined JSON file for the Literature tab.

Sources:
  - OpenAlex (api.openalex.org)       -> type "journal"   (scientific articles;
    OpenAlex reports each work's open-access status/URL directly)
  - Open Library (openlibrary.org)    -> type "book"
  - GDELT Doc API (gdeltproject.org)  -> type "news"       (recent online media
    coverage; GDELT's free doc API only indexes ~2017-present)
  - KB/Delpher SRU (jsru.kb.nl)       -> type "historic_newspaper" (digitized
    Dutch newspapers, filtered to the colonial era: everything before Dutch
    recognition of Indonesian independence, 1949-12-27)

All four are public, keyless APIs — nothing here is fabricated; every record
in the output carries a real, resolvable source URL. Coverage is necessarily
partial (each API is queried once per run with a capped result count), not
an exhaustive bibliography.

Usage:
    python scripts/fetch_literature.py
    python scripts/fetch_literature.py --openalex-limit 80 --books-limit 40
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit(
        "This script needs the 'requests' library.\n"
        "Install it with:  pip install requests"
    )

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_OUT_DIR = REPO_ROOT / "data" / "literature"

CONTACT_EMAIL = "sergewich@gmail.com"  # polite-pool identifier for OpenAlex
COLONIAL_END_DATE = "1949-12-27"  # Dutch recognition of Indonesian independence

SRU_NS = {
    "srw": "http://www.loc.gov/zing/srw/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcx": "http://krait.kb.nl/coop/tel/handbook/telterms.html",
}


# --------------------------------------------------------------------------- #
# OpenAlex — scientific journal articles
# --------------------------------------------------------------------------- #
def fetch_openalex(query: str, limit: int) -> list[dict]:
    """Search title+abstract (not full text) and restrict to actual articles —
    a plain fulltext search surfaces GBIF "Occurrence Download" dataset-citation
    stubs and papers that only mention the term in passing. Cursor-paginates
    to pull well beyond a single 100-result page."""
    out = []
    url = "https://api.openalex.org/works"
    cursor = "*"
    page_size = 200
    while len(out) < limit and cursor:
        params = {
            "filter": f"title_and_abstract.search:{query},type:article",
            "per-page": min(page_size, limit - len(out)),
            "sort": "publication_year:desc",
            "cursor": cursor,
            "mailto": CONTACT_EMAIL,
        }
        try:
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            print(f"  WARNING: OpenAlex fetch failed: {exc}", file=sys.stderr)
            break

        results = data.get("results", [])
        if not results:
            break

        for w in results:
            loc = w.get("primary_location") or {}
            oa_info = w.get("open_access") or {}
            authors = ", ".join(
                (a.get("author") or {}).get("display_name", "")
                for a in (w.get("authorships") or [])[:4]
                if (a.get("author") or {}).get("display_name")
            )
            source_name = (loc.get("source") or {}).get("display_name") or "Unknown venue"
            best_url = oa_info.get("oa_url") or loc.get("landing_page_url") or w.get("ids", {}).get("doi")
            if not best_url:
                continue
            out.append({
                "type": "journal",
                "title": w.get("title") or w.get("display_name") or "Untitled",
                "authors": authors or None,
                "year": w.get("publication_year"),
                "source": source_name,
                "url": best_url,
                "open_access": bool(oa_info.get("is_oa")),
                "language": None,
            })

        cursor = (data.get("meta") or {}).get("next_cursor")
        time.sleep(0.15)

    return out[:limit]


# --------------------------------------------------------------------------- #
# Open Library — books
# --------------------------------------------------------------------------- #
def fetch_books(query: str, limit: int) -> list[dict]:
    """Page through Open Library search results (100 per page) up to `limit`."""
    out = []
    url = "https://openlibrary.org/search.json"
    page = 1
    page_size = 100
    while len(out) < limit:
        params = {
            "q": query,
            "fields": "title,author_name,first_publish_year,key,ebook_access",
            "limit": page_size,
            "page": page,
            "sort": "new",
        }
        try:
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            print(f"  WARNING: Open Library fetch failed: {exc}", file=sys.stderr)
            break

        docs = data.get("docs", [])
        if not docs:
            break

        for d in docs:
            if len(out) >= limit:
                break
            key = d.get("key")
            if not key:
                continue
            out.append({
                "type": "book",
                "title": d.get("title") or "Untitled",
                "authors": ", ".join(d.get("author_name", [])[:4]) or None,
                "year": d.get("first_publish_year"),
                "source": "Open Library",
                "url": f"https://openlibrary.org{key}",
                "open_access": d.get("ebook_access") == "public",
                "language": None,
            })

        page += 1
        time.sleep(0.15)

    return out[:limit]


# --------------------------------------------------------------------------- #
# GDELT — recent online news / media
# --------------------------------------------------------------------------- #
def fetch_news(query: str, limit: int) -> list[dict]:
    out = []
    url = "https://api.gdeltproject.org/api/v2/doc/doc"
    params = {
        "query": f"{query} sourcelang:english",
        "mode": "artlist",
        "format": "json",
        "maxrecords": min(limit, 250),
        "sort": "hybridrel",
    }
    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        text = resp.text.strip()
        data = json.loads(text) if text else {"articles": []}
    except (requests.RequestException, ValueError) as exc:
        print(f"  WARNING: GDELT fetch failed: {exc}", file=sys.stderr)
        return out

    for a in data.get("articles", [])[:limit]:
        seendate = a.get("seendate", "")  # e.g. "20260530T061500Z"
        year = int(seendate[:4]) if len(seendate) >= 4 and seendate[:4].isdigit() else None
        out.append({
            "type": "news",
            "title": a.get("title") or "Untitled",
            "authors": None,
            "year": year,
            "source": a.get("domain") or "Unknown source",
            "url": a.get("url"),
            "open_access": True,
            "language": a.get("language"),
        })
    return out


# --------------------------------------------------------------------------- #
# KB / Delpher SRU — historic Dutch newspapers (colonial era)
# --------------------------------------------------------------------------- #
def fetch_historic_newspapers(query: str, limit: int, before_date: str) -> list[dict]:
    out = []
    url = "https://jsru.kb.nl/sru/sru"
    params = {
        "operation": "searchRetrieve",
        "version": "1.2",
        "x-collection": "DDD_artikel",
        "query": f"{query} and dc.date < {before_date}",
        "maximumRecords": min(limit, 50),
    }
    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except (requests.RequestException, ET.ParseError) as exc:
        print(f"  WARNING: Delpher/KB fetch failed: {exc}", file=sys.stderr)
        return out

    for rec in root.findall(".//srw:record", SRU_NS):
        data_el = rec.find("srw:recordData", SRU_NS)
        if data_el is None:
            continue

        def text(tag: str, ns: str = "dc") -> str | None:
            el = data_el.find(f"{ns}:{tag}", SRU_NS)
            return el.text if el is not None else None

        date_str = text("date")  # "1925/11/27 00:00:00"
        year = int(date_str[:4]) if date_str and date_str[:4].isdigit() else None
        identifier = text("identifier")  # http://resolver.kb.nl/resolve?urn=...
        if not identifier:
            continue
        out.append({
            "type": "historic_newspaper",
            "title": text("title") or "Untitled",
            "authors": None,
            "year": year,
            "source": text("publisher") or "Delpher (KB, National Library of the Netherlands)",
            "url": identifier.replace("http://", "https://"),
            "open_access": True,
            "language": "Dutch",
        })
    return out


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--openalex-limit", type=int, default=600)
    parser.add_argument("--books-limit", type=int, default=300)
    parser.add_argument("--news-limit", type=int, default=40)
    parser.add_argument("--historic-limit", type=int, default=40)
    parser.add_argument("--colonial-end-date", default=COLONIAL_END_DATE,
                        help="Only include historic newspaper records dated before this "
                             "(default: 1949-12-27, Dutch recognition of Indonesian independence).")
    args = parser.parse_args()

    print("Fetching journal articles from OpenAlex...")
    journals = fetch_openalex("orangutan", args.openalex_limit)
    print(f"  got {len(journals)}")

    print("Fetching books from Open Library...")
    books = fetch_books("orangutan", args.books_limit)
    print(f"  got {len(books)}")

    print("Fetching recent online media from GDELT...")
    news = fetch_news("orangutan", args.news_limit)
    print(f"  got {len(news)}")

    print(f"Fetching historic Dutch newspapers from Delpher/KB (before {args.colonial_end_date})...")
    historic = fetch_historic_newspapers("orang-oetan", args.historic_limit, args.colonial_end_date)
    print(f"  got {len(historic)}")

    combined = journals + books + news + historic
    combined.sort(key=lambda r: (r["year"] is not None, r["year"] or 0), reverse=True)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.out_dir / "literature.json"
    out_path.write_text(json.dumps(combined, ensure_ascii=False, indent=None), encoding="utf-8")

    print(f"\nWrote {len(combined)} record(s) -> {out_path.relative_to(REPO_ROOT)}")
    by_type: dict[str, int] = {}
    for r in combined:
        by_type[r["type"]] = by_type.get(r["type"], 0) + 1
    for t, n in sorted(by_type.items()):
        print(f"  {t}: {n}")


if __name__ == "__main__":
    main()
