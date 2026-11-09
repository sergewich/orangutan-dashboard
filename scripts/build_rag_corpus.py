#!/usr/bin/env python3
"""
build_rag_corpus.py — turn the dashboard's literature list (plus a small
snapshot of the dashboard's own alert data) into text passages for the
Literature Assistant chatbot. Writes data/rag/corpus.jsonl; ingest_rag.py then
embeds and uploads it to Cloudflare Vectorize.

Why this step exists: data/literature/literature.json is metadata only (title,
authors, year, url). A title alone is too little to answer questions from, so
this script fetches the real text that can legally be indexed for each record:

  - journal             -> abstract from OpenAlex (rebuilt from
                           abstract_inverted_index). Full text is NOT fetched:
                           many "open access" PDFs aren't licensed for reuse.
  - book                -> Open Library work description, where one exists
  - historic_newspaper  -> article OCR text from the KB/Delpher resolver
                           (pre-1950 Dutch newspapers, public domain)
  - news                -> skipped (GDELT gives no article text)

Records with no usable text are dropped rather than indexed as title-only
stubs, since those pollute retrieval with weak matches.

Plus one "dashboard_data" passage per species, summarising the deforestation
and fire alert CSVs, so the assistant can answer "how many alerts recently"
from the same numbers the Map tab shows.

Plus your own library (PDF papers, reports, theses) if --library / LIBRARY_DIR
points at a folder catalogued by catalog_library.py: full text where the
catalogue says `full`, abstract only where it says `abstract`. When a paper is
in both the library and literature.json, only the library copy is indexed.

Deliberately NOT included: anything under data/social/. Approved incidents
are human-reviewed, but an LLM paraphrasing them is a new, unreviewed claim
about trade/killings — see the review policy in CLAUDE.md.

Lookups are cached in data/rag/cache/ (gitignored, like the rest of
data/rag/), so monthly re-runs only fetch new records.

OpenAlex: works without a key for single-page lookups, but the keyless budget
is small and shared per IP. Set OPENALEX_API_KEY in .env (free, from
https://openalex.org/settings/api) for reliable runs.

Usage:
    python scripts/build_rag_corpus.py
    python scripts/build_rag_corpus.py --limit 20     # quick smoke test
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("This script needs the 'requests' library.\nInstall it with:  pip install requests")

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
LIT_PATH = REPO_ROOT / "data" / "literature" / "literature.json"
OUT_DIR = REPO_ROOT / "data" / "rag"
CACHE_DIR = OUT_DIR / "cache"

CONTACT_EMAIL = "sergewich@gmail.com"  # polite-pool identifier for OpenAlex
SITE_MAP_URL = "https://sergewich.github.io/orangutan-dashboard/site/tabs/map.html"

SPECIES = {
    "pongo_abelii": "Sumatran orangutan (Pongo abelii)",
    "pongo_pygmaeus": "Bornean orangutan (Pongo pygmaeus)",
    "pongo_tapanuliensis": "Tapanuli orangutan (Pongo tapanuliensis)",
}
# Keyword tags let the Worker nudge retrieval toward the species the user is
# looking at on the map. Matching is on lowercased title + text.
SPECIES_KEYWORDS = {
    "pongo_abelii": ["abelii", "sumatra", "leuser", "aceh"],
    "pongo_pygmaeus": ["pygmaeus", "borneo", "bornean", "kalimantan", "sabah", "sarawak", "tanjung puting", "sebangau"],
    "pongo_tapanuliensis": ["tapanuli", "batang toru"],
}

MIN_TEXT_CHARS = 200     # below this a passage is too thin to ground an answer
CHUNK_CHARS = 2400       # ~600 tokens; most abstracts fit in one chunk
CHUNK_OVERLAP = 400      # ~100 tokens


def load_env() -> None:
    env = REPO_ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def record_id(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:16]


def norm_title(s: str | None) -> str:
    return re.sub(r"\W+", " ", (s or "").lower()).strip()


def clean_text(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s)          # stray JATS/HTML tags in abstracts
    return re.sub(r"\s+", " ", s).strip()


def load_cache(name: str) -> dict:
    p = CACHE_DIR / f"{name}.json"
    return json.loads(p.read_text()) if p.exists() else {}


def save_cache(name: str, data: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False))


# --------------------------------------------------------------------------- #
# Text fetchers. Each returns text or None; None is cached too ("" sentinel)
# so a record with no abstract isn't re-requested every month.
# --------------------------------------------------------------------------- #
def openalex_abstract(rec: dict, session: requests.Session) -> str | None:
    params = {
        "per-page": 5,
        "select": "title,abstract_inverted_index",
        "mailto": CONTACT_EMAIL,
    }
    if os.environ.get("OPENALEX_API_KEY"):
        params["api_key"] = os.environ["OPENALEX_API_KEY"]
    doi = re.search(r"doi\.org/(10\.\S+)", rec["url"])
    if doi:
        params["filter"] = f"doi:{doi.group(1)}"
    else:
        # Filter values can't contain , : | — strip punctuation before searching.
        params["filter"] = f"title.search:{norm_title(rec['title'])[:250]}"
    resp = session.get("https://api.openalex.org/works", params=params, timeout=30)
    if resp.status_code == 429:
        raise RuntimeError("OpenAlex rate limit/budget hit — set OPENALEX_API_KEY in .env and re-run")
    resp.raise_for_status()
    want = norm_title(rec["title"])
    for w in resp.json().get("results", []):
        if doi or norm_title(w.get("title")) == want:
            inv = w.get("abstract_inverted_index")
            if not inv:
                return None
            words = sorted((pos, word) for word, positions in inv.items() for pos in positions)
            return " ".join(word for _, word in words)
    return None


def openlibrary_description(rec: dict, session: requests.Session) -> str | None:
    resp = session.get(rec["url"].rstrip("/") + ".json", timeout=30)
    resp.raise_for_status()
    desc = resp.json().get("description")
    if isinstance(desc, dict):
        desc = desc.get("value")
    return desc or None


def delpher_ocr(rec: dict, session: requests.Session) -> str | None:
    resp = session.get(rec["url"], timeout=30)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    return " ".join("".join(p.itertext()) for p in root.iter("p")) or None


FETCHERS = {
    "journal": ("openalex", openalex_abstract),
    "book": ("openlibrary", openlibrary_description),
    "historic_newspaper": ("delpher", delpher_ocr),
}


# --------------------------------------------------------------------------- #
# Chunking — a plain sentence-aware character splitter. Abstracts are short,
# so LangChain's RecursiveCharacterTextSplitter would be a heavy dependency
# for what is usually a single chunk.
# --------------------------------------------------------------------------- #
def chunk_text(text: str) -> list[str]:
    if len(text) <= CHUNK_CHARS:
        return [text]
    sentences = []
    for s in re.split(r"(?<=[.!?])\s+", text):
        # Tables and OCR text can run for pages without a full stop.
        while len(s) > CHUNK_CHARS:
            cut = s.rfind(" ", 0, CHUNK_CHARS) if " " in s[:CHUNK_CHARS] else CHUNK_CHARS
            sentences.append(s[:cut])
            s = s[cut:].lstrip()
        sentences.append(s)
    chunks, cur = [], ""
    for s in sentences:
        if cur and len(cur) + len(s) + 1 > CHUNK_CHARS:
            chunks.append(cur)
            cur = cur[-CHUNK_OVERLAP:].split(" ", 1)[-1]  # overlap on a word boundary
        cur = f"{cur} {s}".strip()
    if cur:
        chunks.append(cur)
    return chunks


def species_tags(text: str) -> list[str]:
    t = text.lower()
    return [sp for sp, kws in SPECIES_KEYWORDS.items() if any(k in t for k in kws)]


# --------------------------------------------------------------------------- #
# Dashboard-data passages (one per species)
# --------------------------------------------------------------------------- #
def read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open() as f:
        return list(csv.DictReader(f))


def dashboard_passages() -> list[dict]:
    """Windows count back from the latest date in each CSV, not from today —
    GFW alerts lag by weeks, so "last 30 days from today" would read as zero."""
    out = []
    for sp, label in SPECIES.items():
        parts = []
        defor = read_rows(REPO_ROOT / "data" / "deforestation" / f"{sp}_integrated_alerts_summary.csv")
        if defor:
            latest = date.fromisoformat(max(r["date"] for r in defor))
            for days in (30, 90):
                since = (latest - timedelta(days=days)).isoformat()
                area = {"high": 0.0, "nominal": 0.0}
                for r in defor:
                    if r["date"] > since:
                        area[r["confidence"]] = area.get(r["confidence"], 0.0) + float(r["area_ha"] or 0)
                parts.append(
                    f"In the {days} days up to {latest.isoformat()}, GFW integrated deforestation alerts "
                    f"within the {label} range covered about {area['high']:.1f} ha at high confidence "
                    f"and {area['nominal']:.1f} ha at nominal (lower) confidence.")
        fire = read_rows(REPO_ROOT / "data" / "fire" / f"{sp}_fire_alerts_summary.csv")
        if fire:
            latest = date.fromisoformat(max(r["date_acq"] for r in fire))
            since = (latest - timedelta(days=30)).isoformat()
            n = sum(int(float(r["alert_count"] or 0)) for r in fire if r["date_acq"] > since)
            parts.append(f"NASA FIRMS recorded {n} active-fire detections within the range "
                         f"in the 30 days up to {latest.isoformat()}.")
        else:
            parts.append("NASA FIRMS has recorded no active-fire detections within the range "
                         "in the dashboard's current fire data window.")
        text = " ".join(parts) + (
            " These are satellite alerts, not field-verified forest loss. The range polygon is the IUCN"
            " range map, so some alerts fall in non-forest or unoccupied parts of the range.")
        out.append({
            "id": f"dash-{sp}",
            "source": f"dash-{sp}",
            "type": "dashboard_data",
            "title": f"Dashboard alert summary — {label}",
            "authors": "Orangutan Conservation Dashboard (GFW / NASA FIRMS data)",
            "year": date.today().year,
            "url": SITE_MAP_URL,
            "species": [sp],
            "text": text,
        })
    return out


def library_passages(library: Path) -> list[dict]:
    """Passages from the catalogued PDF library (see catalog_library.py)."""
    from catalog_library import TEXT_CACHE  # same cache location the cataloguer wrote

    catalog = library / "catalog.csv"
    if not catalog.exists():
        sys.exit(f"{catalog} not found — run catalog_library.py (or sync_drive_library.py) first. "
                 "Refusing to continue: an index built without the library would delete it.")
    out, missing = [], 0
    with catalog.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        access = (r.get("override") or r.get("access") or "").strip().lower()
        if access not in ("full", "abstract"):
            continue
        cache = TEXT_CACHE / f"{r['md5']}.json"
        if not cache.exists():
            missing += 1
            continue
        cached = json.loads(cache.read_text())
        text = cached["full_text"] if access == "full" else (cached.get("abstract") or "")
        text = clean_text(text)
        if len(text) < MIN_TEXT_CHARS:
            continue
        try:
            year = int(float(r.get("year") or 0)) or None
        except ValueError:
            year = None
        title = r.get("title") or r["file"]
        tags = species_tags(f"{title} {text}")
        for n, chunk in enumerate(chunk_text(text)):
            out.append({
                "id": f"lib-{r['md5'][:16]}-{n}",
                "source": f"lib-{r['md5'][:16]}",
                "type": r.get("doc_type") or "report",
                "title": title,
                "authors": r.get("authors") or None,
                "year": year,
                "url": r.get("url") or "",
                "species": tags,
                "text": chunk,
            })
    if missing:
        print(f"  WARNING: {missing} catalogued library files have no cached text "
              "(re-run the sync/catalogue step)", file=sys.stderr)
    kinds = {}
    for r in rows:
        k = (r.get("override") or r.get("access") or "?").strip().lower()
        kinds[k] = kinds.get(k, 0) + 1
    print(f"  library: {len(rows)} files ({', '.join(f'{v} {k}' for k, v in sorted(kinds.items()))}) "
          f"-> {len(out)} passages")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=0, help="only process the first N literature records")
    parser.add_argument("--library", help="catalogued PDF library folder (default: LIBRARY_DIR, if set)")
    args = parser.parse_args()
    load_env()

    records = json.loads(LIT_PATH.read_text())
    if args.limit:
        records = records[: args.limit]

    caches = {name: load_cache(name) for name, _ in FETCHERS.values()}
    session = requests.Session()
    session.headers["User-Agent"] = f"orangutan-dashboard/1.0 (mailto:{CONTACT_EMAIL})"

    passages, stats = [], {}
    try:
        for i, rec in enumerate(records, 1):
            kind = rec.get("type")
            if kind not in FETCHERS:
                continue
            cache_name, fetch = FETCHERS[kind]
            cache = caches[cache_name]
            if rec["url"] not in cache:
                try:
                    cache[rec["url"]] = fetch(rec, session) or ""
                except (requests.RequestException, ET.ParseError, ValueError) as exc:
                    print(f"  WARNING: {kind} lookup failed for {rec['url']}: {exc}", file=sys.stderr)
                    continue  # not cached — retried next run
                time.sleep(0.15)
                if i % 50 == 0:
                    print(f"  ...{i}/{len(records)} records")
            text = clean_text(cache[rec["url"]])
            s = stats.setdefault(kind, [0, 0])
            s[1] += 1
            if len(text) < MIN_TEXT_CHARS:
                continue
            s[0] += 1
            tags = species_tags(f"{rec['title']} {text}")
            for n, chunk in enumerate(chunk_text(text)):
                passages.append({
                    "id": f"lit-{record_id(rec['url'])}-{n}",
                    "source": f"lit-{record_id(rec['url'])}",
                    "type": kind,
                    "title": rec["title"],
                    "authors": rec.get("authors"),
                    "year": rec.get("year"),
                    "url": rec["url"],
                    "species": tags,
                    "text": chunk,
                })
    finally:
        for name, data in caches.items():
            save_cache(name, data)

    library = args.library or os.environ.get("LIBRARY_DIR")
    if library:
        lib = library_passages(Path(library))
        # Same paper in both? Keep the library copy (it may be full text).
        lib_keys = {norm_title(p["title"]) for p in lib} | {p["url"].lower() for p in lib if p["url"]}
        before = len(passages)
        passages = [p for p in passages
                    if norm_title(p["title"]) not in lib_keys and p["url"].lower() not in lib_keys]
        if before - len(passages):
            print(f"  dropped {before - len(passages)} literature.json passages duplicated in the library")
        passages += lib

    passages += dashboard_passages()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / "corpus.jsonl"
    with out.open("w") as f:
        for p in passages:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    for kind, (kept, seen) in stats.items():
        print(f"  {kind}: {kept}/{seen} records had usable text")
    print(f"Wrote {len(passages)} passages to {out.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
