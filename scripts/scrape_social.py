#!/usr/bin/env python3
"""
scrape_social.py — find orangutan news on NGO sites, Indonesian and English
news sites, the forestry ministry's conservation news, and a GDELT keyword
sweep, as the first stage of the Social/Threats incident pipeline.

THIS SCRIPT ONLY COLLECTS LINKS. It does not classify or publish anything.
See extract_incidents.py for the next stage (LLM-drafted structured records,
marked unreviewed, run locally) and review_server.py for the human-approval
gate that must happen before anything reaches a public-facing data file.
Nothing here writes to a file the live site reads.

It stores url/title/date only — no article text. data/ is published by the
Pages deploy and the repo is public, and news articles aren't ours to
republish; extract_incidents.py fetches the text itself when drafting.
data/social/raw/articles.json accumulates across runs (new links are
appended), so extraction only sees each article once.

Sources (all public pages, fetched with a standard browser User-Agent —
some of these sites block the default requests/curl UA outright). Search
pages and tag pages are queried for both "orangutan" and "orang utan".
  NGOs
  - Orangutan Information Centre (OIC / YOSL-OIC)   orangutancentre.org
  - Centre for Orangutan Protection (COP)            orangutanprotection.com
  - Yayasan Ekosistem Lestari / SOCP (YEL)            yel.or.id
  - TRAFFIC (wildlife trade monitoring network)       traffic.org
  News
  - ANTARA (English + Indonesian)                     antaranews.com
  - Kompas                                            kompas.com
  - Prokal (Kalimantan)                               prokal.co
  - The Jakarta Post                                  thejakartapost.com
  - Tribunnews                                        tribunnews.com
  Government
  - Ditjen KSDAE (Ministry of Forestry) — carries the regional BKSDA/BBKSDA
    offices' news too                                  ksdae.kehutanan.go.id
  Sweep
  - GDELT keyword sweep, English + Indonesian         api.gdeltproject.org

Checked and left out (2026-11): bbksdasumut.com now redirects to a gambling
site; bksdakaltim.menlhk.go.id and ksdae.menlhk.go.id no longer resolve
(the ministry moved to kehutanan.go.id); the BKSDA West and Central
Kalimantan blogs stopped posting in 2008 and 2014; Google search can't be
scripted (GDELT covers the same news).

Usage:
    python scripts/scrape_social.py
    python scripts/scrape_social.py --per-source-limit 20
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import date, datetime, timezone
from html import unescape
from pathlib import Path
from urllib.parse import urljoin

try:
    import requests
except ImportError:
    sys.exit(
        "This script needs the 'requests' library.\n"
        "Install it with:  pip install requests"
    )

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_OUT_DIR = REPO_ROOT / "data" / "social" / "raw"

# A default requests/curl UA gets a flat 403 from at least one of these
# sites (orangutanprotection.com) — use a normal browser UA everywhere.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
REQUEST_DELAY = 1.0  # be a polite scraper
ORANGUTAN_RE = re.compile(r"orang[\s-]?(utan|outan|oetan)|\bpongo\b", re.I)

SOURCES = [
    {
        "org": "OIC",
        "kind": "ngo_site",
        "listing_urls": ["https://orangutancentre.org/news/"],
        "base": "https://orangutancentre.org",
        "article_re": re.compile(r"^https://orangutancentre\.org/\d{4}/\d{2}/\d{2}/[^/]+/$"),
    },
    {
        "org": "COP",
        "kind": "ngo_site",
        "listing_urls": ["https://orangutanprotection.com/category/cop-news/"],
        "base": "https://orangutanprotection.com",
        "article_re": re.compile(r"^https://orangutanprotection\.com/\d{4}/\d{2}/[^/]+/$"),
    },
    {
        "org": "YEL",
        "kind": "ngo_site",
        "listing_urls": ["https://www.yel.or.id/media/news/"],
        "base": "https://www.yel.or.id",
        "article_re": re.compile(r"^https://www\.yel\.or\.id/news/[^/]+/$"),
    },
    {
        "org": "TRAFFIC",
        # TRAFFIC covers all wildlife trade globally — use their site search
        # to scope to orangutan-relevant results instead of scraping everything.
        "kind": "ngo_site",
        "listing_urls": ["https://www.traffic.org/search/?q=orangutan"],
        "base": "https://www.traffic.org",
        "article_re": re.compile(
            r"^https://www\.traffic\.org/(news|publications/reports)/[^/]+/$"
        ),
    },
    {
        "org": "ANTARA",
        "kind": "news_site",
        "listing_urls": ["https://en.antaranews.com/search?q=orangutan",
                         "https://en.antaranews.com/search?q=orang+utan"],
        "base": "https://en.antaranews.com",
        "article_re": re.compile(r"^https://en\.antaranews\.com/news/\d+/[^/?#]+$"),
    },
    {
        "org": "ANTARA (id)",
        "kind": "news_site",
        "listing_urls": ["https://www.antaranews.com/tag/orangutan",
                         "https://www.antaranews.com/tag/orang-utan"],
        "base": "https://www.antaranews.com",
        "article_re": re.compile(r"^https://www\.antaranews\.com/berita/\d+/[^/?#]+$"),
    },
    {
        "org": "Kompas",
        "kind": "news_site",
        "listing_urls": ["https://www.kompas.com/tag/orangutan",
                         "https://www.kompas.com/tag/orang-utan"],
        "base": "https://www.kompas.com",
        "article_re": re.compile(r"^https://[a-z]+\.kompas\.com/read/\d{4}/\d{2}/\d{2}/\d+/[^/?#]+$"),
    },
    {
        "org": "Prokal",
        "kind": "news_site",
        "listing_urls": ["https://www.prokal.co/tag/orangutan",
                         "https://www.prokal.co/tag/orang-utan"],
        "base": "https://www.prokal.co",
        "article_re": re.compile(r"^https://www\.prokal\.co/[a-z-]+/\d{10}/[^/?#]+$"),
    },
    {
        "org": "Jakarta Post",
        "kind": "news_site",
        "listing_urls": ["https://www.thejakartapost.com/tag/orangutan"],
        "base": "https://www.thejakartapost.com",
        "article_re": re.compile(r"^https://www\.thejakartapost\.com/[a-z-]+/\d{4}/\d{2}/\d{2}/[^/?#]+$"),
    },
    {
        "org": "Tribunnews",
        "kind": "news_site",
        "listing_urls": ["https://www.tribunnews.com/tag/orangutan",
                         "https://www.tribunnews.com/tag/orang-utan"],
        "base": "https://www.tribunnews.com",
        "article_re": re.compile(
            r"^https://www\.tribunnews\.com/[a-z-]+/(\d{4}/\d{2}/\d{2}/)?\d{6,}/[^/?#]+$"
            r"|^https://www\.tribunnews\.com/[a-z-]+/\d{4}/\d{2}/\d{2}/[^/?#]+$"
        ),
    },
    {
        "org": "KSDAE",
        "kind": "government",
        "listing_urls": ["https://ksdae.kehutanan.go.id/pencarian/?q=orangutan",
                         "https://ksdae.kehutanan.go.id/pencarian/?q=orang+utan"],
        "base": "https://ksdae.kehutanan.go.id",
        "article_re": re.compile(
            r"^https://ksdae\.kehutanan\.go\.id/publikasi/(berita|artikel|siaran-pers)/[^/]+/$"
        ),
    },
]


def fetch(url: str) -> str | None:
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=30)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as exc:
        print(f"    WARNING: failed to fetch {url}: {exc}", file=sys.stderr)
        return None


def extract_article_links(html: str, base: str, article_re: re.Pattern) -> list[str]:
    """Article links in page order (search/tag pages list newest first)."""
    links = []
    for href in re.findall(r'href=["\']([^"\']+)["\']', html):
        full = urljoin(base + "/", unescape(href))
        if article_re.match(full) and full not in links:
            links.append(full)
    return links


TAG_BLOCK_RE = re.compile(
    r"<(script|style|nav|header|footer|aside|form|noscript)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"[ \t\r\f\v]+")
BLANKLINES_RE = re.compile(r"\n\s*\n+")
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
OG_TITLE_RE = re.compile(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', re.I)
DATE_RES = [
    re.compile(r'<meta[^>]+(?:property|name)=["\'](?:article:published_time|pubdate|publishdate|'
               r'content_PublishedDate)["\'][^>]+content=["\'](\d{4}-\d{2}-\d{2})', re.I),
    re.compile(r'"datePublished"\s*:\s*"(\d{4}-\d{2}-\d{2})'),
    re.compile(r'<time[^>]*datetime="(\d{4}-\d{2}-\d{2})'),
]
URL_DATE_RE = re.compile(r"/(\d{4})/(\d{2})(?:/(\d{2}))?/")
MONTHS = {m: i % 12 + 1 for i, m in enumerate(
    "januari februari maret april mei juni juli agustus september oktober november desember "
    "january february march april may june july august september october november december".split())}
TEXT_DATE_RE = re.compile(r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+((?:19|20)\d{2})\b", re.I)
PARA_RE = re.compile(r"<p\b[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)


def _clean(fragment: str) -> str:
    return WHITESPACE_RE.sub(" ", unescape(TAG_RE.sub(" ", fragment))).strip()


def extract_text(html: str, url: str) -> dict:
    og = OG_TITLE_RE.search(html)
    title_match = TITLE_RE.search(html)
    title = unescape(og.group(1) if og else title_match.group(1) if title_match else "Untitled").strip()
    # Most themes suffix the site name after a delimiter — trim it.
    for sep in (" | ", " – ", " — ", " - "):
        if sep in title:
            title = title.split(sep)[0].strip()
            break

    date = next((m.group(1) for r in DATE_RES if (m := r.search(html))), None)
    if not date:
        url_date = URL_DATE_RE.search(url)
        if url_date:
            y, m, d = url_date.group(1), url_date.group(2), url_date.group(3) or "01"
            date = f"{y}-{m}-{d}"

    # KSDAE (ministry) pages carry the story as Editor.js JSON in a <script>.
    editorjs = re.search(r'<script[^>]*id="editorjs-data"[^>]*>(.*?)</script>', html, re.S)
    if editorjs:
        try:
            doc = json.loads(editorjs.group(1))
            story = "\n".join(_clean(str(b.get("data", {}).get("text", ""))) for b in doc.get("blocks", []))
            if not date and doc.get("time"):
                date = datetime.fromtimestamp(doc["time"] / 1000, timezone.utc).date().isoformat()
            if len(story) > 200:
                return {"title": title, "date_hint": date, "text": story[:4000]}
        except (ValueError, AttributeError):
            pass

    body = TAG_BLOCK_RE.sub(" ", html)
    if not date:  # last resort: the first written-out date ("15 September 2026")
        m = TEXT_DATE_RE.search(_clean(body))
        if m:
            date = f"{m.group(3)}-{MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
    # News pages bury the story in menus and widgets; the <p> paragraphs are
    # usually just the story. Fall back to all text if there are too few.
    paras = [p for p in (_clean(x) for x in PARA_RE.findall(body)) if len(p) > 60]
    if sum(map(len, paras)) >= 400:
        text = "\n".join(paras)
    else:
        text = BLANKLINES_RE.sub("\n", _clean(body))
    return {"title": title, "date_hint": date, "text": text.strip()[:4000]}


def scrape_source(source: dict, per_source_limit: int, known: set[str]) -> list[dict]:
    org = source["org"]
    print(f"\n=== {org} ===")
    links: list[str] = []
    for listing_url in source["listing_urls"]:
        html = fetch(listing_url)
        time.sleep(REQUEST_DELAY)
        if html is None:
            continue
        links += [l for l in extract_article_links(html, source["base"], source["article_re"])
                  if l not in links]
    new = [l for l in links if l not in known][:per_source_limit]
    print(f"  found {len(links)} article link(s), {len(new)} new")

    out = []
    for url in new:
        html = fetch(url)
        time.sleep(REQUEST_DELAY)
        if html is None:
            continue
        ex = extract_text(html, url)
        # Search results can match "orang" + "utan" anywhere on the page.
        if source["kind"] != "ngo_site" and not ORANGUTAN_RE.search(ex["title"] + " " + ex["text"]):
            continue
        out.append({"org": org, "source_kind": source["kind"], "url": url,
                    "title": ex["title"], "date_hint": ex["date_hint"], "text": None})
    print(f"  kept {len(out)} new article(s)")
    return out


GDELT_QUERIES = [
    '("orangutan trafficking" OR "orangutan trade" OR "orangutan poaching" '
    'OR "orangutan smuggled" OR "orangutan killed" OR "orangutan seized") sourcelang:english',
    # GDELT matches Indonesian articles on their original words.
    '(orangutan OR "orang utan") sourcelang:indonesian',
]


def scrape_gdelt_sweep(limit: int, known: set[str]) -> list[dict]:
    print("\n=== GDELT keyword sweep ===")
    out = []
    for query in GDELT_QUERIES:
        params = {"query": query, "mode": "artlist", "format": "json",
                  "maxrecords": min(limit, 100), "sort": "datedesc", "timespan": "3months"}
        try:
            for attempt in range(2):
                resp = requests.get("https://api.gdeltproject.org/api/v2/doc/doc", params=params,
                                    headers={"User-Agent": UA}, timeout=30)
                if resp.status_code != 429:
                    break
                time.sleep(20)  # rate limited: one request per 5 s, stricter when busy
            resp.raise_for_status()
            text = resp.text.strip()
            data = json.loads(text) if text.startswith("{") else {"articles": []}
        except (requests.RequestException, ValueError) as exc:
            print(f"  WARNING: GDELT sweep failed: {exc}", file=sys.stderr)
            continue
        for a in data.get("articles", [])[:limit]:
            if not a.get("url") or a["url"] in known:
                continue
            seendate = a.get("seendate", "")
            out.append({
                "org": a.get("domain") or "unknown",
                "source_kind": "gdelt_news",
                "url": a["url"],
                "title": a.get("title") or "Untitled",
                "date_hint": f"{seendate[:4]}-{seendate[4:6]}-{seendate[6:8]}" if len(seendate) >= 8 else None,
                "text": None,
            })
            known.add(a["url"])
        time.sleep(5)  # GDELT asks for at most one request every 5 seconds
    print(f"  got {len(out)} new candidate article(s)")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--per-source-limit", type=int, default=20,
                        help="Max new articles per source per run (default: 20).")
    parser.add_argument("--gdelt-limit", type=int, default=50)
    parser.add_argument("--skip-gdelt", action="store_true")
    parser.add_argument("--only", help="comma-separated source names to run (e.g. Kompas,KSDAE)")
    args = parser.parse_args()

    out_path = args.out_dir / "articles.json"
    existing = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else []
    known = {a["url"] for a in existing}

    only = {s.strip().lower() for s in args.only.split(",")} if args.only else None
    new: list[dict] = []
    for source in SOURCES:
        if only and source["org"].lower() not in only:
            continue
        new.extend(scrape_source(source, args.per_source_limit, known))
    if not args.skip_gdelt and not only:
        new.extend(scrape_gdelt_sweep(args.gdelt_limit, known))

    today = date.today().isoformat()
    for a in new:
        a["found"] = today
    # Article text never goes in the repo (see docstring) — older runs stored it.
    articles = [{**a, "text": None} for a in existing + new]
    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(articles, ensure_ascii=False, indent=None), encoding="utf-8")

    print(f"\n{len(new)} new article link(s); {len(articles)} total in {out_path}")
    by_org: dict[str, int] = {}
    for a in new:
        by_org[a["org"]] = by_org.get(a["org"], 0) + 1
    for org, n in sorted(by_org.items()):
        print(f"  {org}: {n}")


if __name__ == "__main__":
    main()
