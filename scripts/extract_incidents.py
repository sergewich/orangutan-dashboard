#!/usr/bin/env python3
"""
extract_incidents.py — run scraped raw article text through the local
orangutan-dashboard-engineer Ollama model to draft structured incident
records (trade, translocation/rescue, killing, or not relevant).

THIS SCRIPT ONLY PRODUCES DRAFTS. Every record is written with
reviewed=False and decision=None. Nothing here is fit to publish — an LLM
classification of a scraped news article is not a verified fact. The
review_server.py step (human approval, one record at a time) is mandatory
before anything reaches a public-facing data file. This is a firm project
rule, not a suggestion — see MEMORY.md / project notes if unclear on why.

Runs entirely on the local Ollama instance (http://127.0.0.1:11434) — no
cloud API calls, no cost, nothing leaves this machine.

Usage:
    python scripts/extract_incidents.py
    python scripts/extract_incidents.py --raw-file data/social/raw/articles.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
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
DEFAULT_RAW_FILE = REPO_ROOT / "data" / "social" / "raw" / "articles.json"
DEFAULT_DRAFTS_FILE = REPO_ROOT / "data" / "social" / "drafts.json"

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
MODEL = "orangutan-dashboard-engineer"

CATEGORIES = ["trade", "translocation_rescue", "killing", "other_not_relevant"]

SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": CATEGORIES},
        "summary": {"type": "string"},
        "date": {"type": "string"},
        "location": {"type": "string"},
        "species_mentioned": {"type": "string"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["category", "summary", "confidence"],
}

PROMPT_TEMPLATE = """You are drafting a candidate record for a human reviewer — you are \
NOT publishing anything, and the reviewer will discard anything wrong or unclear. Be \
conservative: if the article isn't really about orangutan trade/trafficking, a \
translocation or rescue/rehabilitation, or an orangutan being killed, classify it as \
"other_not_relevant" rather than stretching to fit a category.

Categories:
- trade: poaching, trafficking, illegal pet trade, seizures/confiscations of orangutans
- translocation_rescue: an orangutan being rescued, translocated, released, or rehabilitated
- killing: an orangutan being killed or found dead due to human causes
- other_not_relevant: anything else (general conservation news, habitat/deforestation \
  stories with no specific incident, unrelated topics)

Article title: {title}
Article text (may include site navigation noise — ignore that):
{text}

Extract: category, a one-two sentence neutral summary of the specific incident (empty \
string if other_not_relevant), the date if stated in the text (any format you find it \
in), the location if stated, the orangutan species if named, and your confidence."""


def draft_id(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def extract_one(article: dict) -> dict | None:
    text = (article.get("text") or "")[:3500]
    if not text:
        return None
    prompt = PROMPT_TEMPLATE.format(title=article.get("title", "Untitled"), text=text)
    try:
        resp = requests.post(
            OLLAMA_URL,
            json={"model": MODEL, "prompt": prompt, "stream": False, "format": SCHEMA},
            timeout=180,
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "")
        parsed = json.loads(raw)
    except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
        print(f"    WARNING: extraction failed for {article.get('url')}: {exc}", file=sys.stderr)
        return None

    if parsed.get("category") not in CATEGORIES:
        return None

    return {
        "id": draft_id(article["url"]),
        "org": article.get("org"),
        "source_kind": article.get("source_kind"),
        "url": article["url"],
        "title": article.get("title"),
        "date_hint": article.get("date_hint"),
        "category": parsed["category"],
        "summary": parsed.get("summary", ""),
        "date": parsed.get("date") or article.get("date_hint"),
        "location": parsed.get("location") or None,
        "species_mentioned": parsed.get("species_mentioned") or None,
        "confidence": parsed.get("confidence", "low"),
        "reviewed": False,
        "decision": None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--raw-file", type=Path, default=DEFAULT_RAW_FILE)
    parser.add_argument("--drafts-file", type=Path, default=DEFAULT_DRAFTS_FILE)
    parser.add_argument("--skip-not-relevant", action="store_true", default=True,
                        help="Drop other_not_relevant drafts instead of keeping them "
                             "for review (default: on, since they add review-queue noise).")
    parser.add_argument("--keep-not-relevant", dest="skip_not_relevant", action="store_false")
    args = parser.parse_args()

    if not args.raw_file.exists():
        sys.exit(f"{args.raw_file} not found — run scripts/scrape_social.py first.")
    articles = json.loads(args.raw_file.read_text(encoding="utf-8"))

    existing: dict[str, dict] = {}
    if args.drafts_file.exists():
        for d in json.loads(args.drafts_file.read_text(encoding="utf-8")):
            existing[d["id"]] = d

    print(f"Extracting from {len(articles)} article(s) via local model ({MODEL})...")
    new_count = 0
    skipped_existing = 0
    for i, article in enumerate(articles, 1):
        did = draft_id(article["url"])
        if did in existing:
            skipped_existing += 1
            continue
        print(f"  [{i}/{len(articles)}] {article.get('org')}: {article.get('title', '')[:60]}")
        draft = extract_one(article)
        if draft is None:
            continue
        if args.skip_not_relevant and draft["category"] == "other_not_relevant":
            continue
        existing[draft["id"]] = draft
        new_count += 1

    args.drafts_file.parent.mkdir(parents=True, exist_ok=True)
    all_drafts = list(existing.values())
    args.drafts_file.write_text(json.dumps(all_drafts, ensure_ascii=False, indent=None), encoding="utf-8")

    print(f"\n{new_count} new draft(s), {skipped_existing} already extracted, "
          f"{len(all_drafts)} total in {args.drafts_file.relative_to(REPO_ROOT)}")
    by_cat: dict[str, int] = {}
    for d in all_drafts:
        by_cat[d["category"]] = by_cat.get(d["category"], 0) + 1
    for cat, n in sorted(by_cat.items()):
        print(f"  {cat}: {n}")
    print("\nAll drafts are unreviewed. Run scripts/review_server.py to approve/reject "
          "before anything is published.")


if __name__ == "__main__":
    main()
