#!/usr/bin/env python3
"""
scrape_social.py — pull raw article text from orangutan rescue/rehab NGOs,
the wildlife-trade monitoring network TRAFFIC, and a GDELT keyword sweep, as
the first stage of the Social/Threats incident pipeline.

THIS SCRIPT ONLY COLLECTS RAW TEXT. It does not classify or publish anything.
See extract_incidents.py for the next stage (LLM-drafted structured records,
marked unreviewed) and review_server.py for the human-approval gate that
must happen before anything reaches a public-facing data file. Nothing here
writes to a file the live site reads.

Sources (all public pages, fetched with a standard browser User-Agent —
some of these sites block the default requests/curl UA outright):
  - Orangutan Information Centre (OIC / YOSL-OIC)   orangutancentre.org
  - Centre for Orangutan Protection (COP)            orangutanprotection.com
  - Yayasan Ekosistem Lestari / SOCP (YEL)            yel.or.id
  - TRAFFIC (wildlife trade monitoring network)       traffic.org
  - GDELT keyword sweep (broader net, more noise)     api.gdeltproject.org

Usage:
    python scripts/scrape_social.py
    python scripts/scrape_social.py --per-source-limit 15
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
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

SOURCES = [
    {
        "org": "OIC",
        "listing_urls": ["https://orangutancentre.org/news/"],
        "base": "https://orangutancentre.org",
        "article_re": re.compile(r"^https://orangutancentre\.org/\d{4}/\d{2}/\d{2}/[^/]+/$"),
    },
    {
        "org": "COP",
        "listing_urls": ["https://orangutanprotection.com/category/cop-news/"],
        "base": "https://orangutanprotection.com",
        "article_re": re.compile(r"^https://orangutanprotection\.com/\d{4}/\d{2}/[^/]+/$"),
    },
    {
        "org": "YEL",
        "listing_urls": ["https://www.yel.or.id/media/news/"],
        "base": "https://www.yel.or.id",
        "article_re": re.compile(r"^https://www\.yel\.or\.id/news/[^/]+/$"),
    },
    {
        "org": "TRAFFIC",
        # TRAFFIC covers all wildlife trade globally — use their site search
        # to scope to orangutan-relevant results instead of scraping everything.
        "listing_urls": ["https://www.traffic.org/search/?q=orangutan"],
        "base": "https://www.traffic.org",
        "article_re": re.compile(
            r"^https://www\.traffic\.org/(news|publications/reports)/[^/]+/$"
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
    links = set()
    for href in re.findall(r'href="([^"]+)"', html):
        full = urljoin(base, href)
        if article_re.match(full):
            links.add(full)
    return sorted(links)


TAG_BLOCK_RE = re.compile(
    r"<(script|style|nav|header|footer|aside|form)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"[ \t\r\f\v]+")
BLANKLINES_RE = re.compile(r"\n\s*\n+")
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
TIME_RE = re.compile(r'<time[^>]*datetime="([^"]+)"')
URL_DATE_RE = re.compile(r"/(\d{4})/(\d{2})(?:/(\d{2}))?/")


def extract_text(html: str, url: str) -> dict:
    title_match = TITLE_RE.search(html)
    title = unescape(title_match.group(1)).strip() if title_match else "Untitled"
    # Most WP themes suffix the site name after a delimiter — trim it.
    for sep in (" | ", " – ", " — ", " - "):
        if sep in title:
            title = title.split(sep)[0].strip()
            break

    time_match = TIME_RE.search(html)
    date = time_match.group(1)[:10] if time_match else None
    if not date:
        url_date = URL_DATE_RE.search(url)
        if url_date:
            y, m, d = url_date.group(1), url_date.group(2), url_date.group(3) or "01"
            date = f"{y}-{m}-{d}"

    body = TAG_BLOCK_RE.sub(" ", html)
    body = TAG_RE.sub(" ", body)
    body = unescape(body)
    body = WHITESPACE_RE.sub(" ", body)
    body = BLANKLINES_RE.sub("\n", body)
    body = body.strip()[:4000]  # plenty for an LLM classification pass

    return {"title": title, "date_hint": date, "text": body}


def scrape_ngo_source(source: dict, per_source_limit: int) -> list[dict]:
    org = source["org"]
    print(f"\n=== {org} ===")
    links: list[str] = []
    for listing_url in source["listing_urls"]:
        html = fetch(listing_url)
        if html is None:
            continue
        links.extend(extract_article_links(html, source["base"], source["article_re"]))
        time.sleep(REQUEST_DELAY)
    links = sorted(set(links))[:per_source_limit]
    print(f"  found {len(links)} article link(s)")

    out = []
    for url in links:
        html = fetch(url)
        time.sleep(REQUEST_DELAY)
        if html is None:
            continue
        extracted = extract_text(html, url)
        out.append({
            "org": org,
            "source_kind": "ngo_site",
            "url": url,
            **extracted,
        })
    print(f"  scraped {len(out)} article(s)")
    return out


def scrape_gdelt_sweep(limit: int) -> list[dict]:
    print("\n=== GDELT keyword sweep ===")
    url = "https://api.gdeltproject.org/api/v2/doc/doc"
    query = (
        '("orangutan trafficking" OR "orangutan trade" OR "orangutan poaching" '
        'OR "orangutan smuggled" OR "orangutan killed" OR "orangutan seized") '
        "sourcelang:english"
    )
    params = {
        "query": query,
        "mode": "artlist",
        "format": "json",
        "maxrecords": min(limit, 100),
        "sort": "hybridrel",
    }
    try:
        resp = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=30)
        resp.raise_for_status()
        text = resp.text.strip()
        data = json.loads(text) if text else {"articles": []}
    except (requests.RequestException, ValueError) as exc:
        print(f"  WARNING: GDELT sweep failed: {exc}", file=sys.stderr)
        return []

    out = []
    for a in data.get("articles", [])[:limit]:
        seendate = a.get("seendate", "")
        date = f"{seendate[:4]}-{seendate[4:6]}-{seendate[6:8]}" if len(seendate) >= 8 else None
        out.append({
            "org": a.get("domain") or "unknown",
            "source_kind": "gdelt_news",
            "url": a.get("url"),
            "title": a.get("title") or "Untitled",
            "date_hint": date,
            # GDELT's doc API doesn't return article bodies, only metadata —
            # the extraction stage fetches full text from the URL itself for
            # these, same as the NGO sources.
            "text": None,
        })
    print(f"  got {len(out)} candidate article(s)")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--per-source-limit", type=int, default=15,
                        help="Max articles to scrape per NGO/TRAFFIC source (default: 15).")
    parser.add_argument("--gdelt-limit", type=int, default=30)
    parser.add_argument("--skip-gdelt", action="store_true")
    args = parser.parse_args()

    all_articles: list[dict] = []
    for source in SOURCES:
        all_articles.extend(scrape_ngo_source(source, args.per_source_limit))

    if not args.skip_gdelt:
        gdelt_articles = scrape_gdelt_sweep(args.gdelt_limit)
        # GDELT articles need their body text fetched separately (the doc
        # API only returns metadata).
        for a in gdelt_articles:
            if not a["url"]:
                continue
            html = fetch(a["url"])
            time.sleep(REQUEST_DELAY)
            if html:
                a["text"] = extract_text(html, a["url"])["text"]
        all_articles.extend([a for a in gdelt_articles if a.get("text")])

    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.out_dir / "articles.json"
    out_path.write_text(json.dumps(all_articles, ensure_ascii=False, indent=None), encoding="utf-8")

    print(f"\nWrote {len(all_articles)} raw article(s) -> {out_path.relative_to(REPO_ROOT)}")
    by_org: dict[str, int] = {}
    for a in all_articles:
        by_org[a["org"]] = by_org.get(a["org"], 0) + 1
    for org, n in sorted(by_org.items()):
        print(f"  {org}: {n}")


if __name__ == "__main__":
    main()
