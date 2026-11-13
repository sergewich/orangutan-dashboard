#!/usr/bin/env python3
"""
check_review_backlog.py — email the owner a reminder if there are scraped
social articles still waiting on the local draft/review step, so the
monthly digest doesn't quietly skip incidents the owner forgot to process.

scrape_social.py already runs weekly on its own schedule (see
weekly_social_scrape.yml). extract_incidents.py (drafting) and
review_server.py (human approval) stay manual/local on purpose — drafting
uses the free local Ollama model only, and approval must be a human, per
the review policy in CLAUDE.md. This script doesn't touch either step; it
just counts what's still pending and nags by email if it's non-zero, run
on a schedule a few days before monthly_digest.yml so there's time to
catch up before the digest ships.

Pending = raw articles (data/social/raw/articles.json) whose sha256(url)
[:16] id (extract_incidents.py's draft_id()) is in neither drafts.json nor
not_relevant_ids.json (never drafted), plus drafts with reviewed=False
(drafted but not yet approved/rejected).

Needs BREVO_API_KEY (same restricted key send_digest.py uses) and sends to
REMINDER_TO (default: the owner's own address) only when something is
pending — silent otherwise, so it doesn't nag every month for nothing.

    python scripts/check_review_backlog.py
    python scripts/check_review_backlog.py --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

import requests

API = "https://api.brevo.com/v3"
REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_FILE = REPO_ROOT / "data" / "social" / "raw" / "articles.json"
DRAFTS_FILE = REPO_ROOT / "data" / "social" / "drafts.json"
NOT_RELEVANT_FILE = REPO_ROOT / "data" / "social" / "not_relevant_ids.json"
DEFAULT_TO = "sergewich@gmail.com"


def load_env() -> None:
    env = REPO_ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            k, _, v = line.strip().partition("=")
            if k and v and not k.startswith("#"):
                os.environ.setdefault(k, v.strip().strip('"'))


def draft_id(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def count_backlog() -> tuple[int, int]:
    raw = json.loads(RAW_FILE.read_text()) if RAW_FILE.exists() else []
    drafts = json.loads(DRAFTS_FILE.read_text()) if DRAFTS_FILE.exists() else []
    not_relevant = set(json.loads(NOT_RELEVANT_FILE.read_text())) if NOT_RELEVANT_FILE.exists() else set()

    seen = {draft_id(d["url"]) for d in drafts} | not_relevant
    never_drafted = sum(1 for a in raw if draft_id(a["url"]) not in seen)
    awaiting_review = sum(1 for d in drafts if not d.get("reviewed"))
    return never_drafted, awaiting_review


def call(method: str, path: str, **kw) -> dict:
    resp = requests.request(method, API + path, timeout=60, headers={
        "api-key": os.environ["BREVO_API_KEY"], "accept": "application/json"}, **kw)
    if resp.status_code >= 400:
        sys.exit(f"Brevo {method} {path} failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json() if resp.content else {}


def find_sender(domain: str) -> dict:
    senders = [s for s in call("GET", "/senders")["senders"] if s["active"]]
    sender = next((s for s in senders if s["email"].endswith("@" + domain)), None)
    if sender is None:
        sys.exit(f"No active Brevo sender @{domain} — add it under Settings -> Senders.")
    return sender


def send_reminder(never_drafted: int, awaiting_review: int, to: str) -> None:
    domain = os.environ.get("DIGEST_SENDER_DOMAIN", "orangutandashboard.org")
    sender = find_sender(domain)
    lines = []
    if never_drafted:
        lines.append(f"{never_drafted} scraped article(s) haven't been run through the local draft step yet.")
    if awaiting_review:
        lines.append(f"{awaiting_review} drafted incident(s) are waiting for your approve/reject review.")
    body = (
        "<p>" + "</p><p>".join(lines) + "</p>"
        "<p>The monthly digest runs on the 2nd and only picks up incidents already approved by then. "
        "Run <code>python scripts/extract_incidents.py</code>, then open the review queue "
        "(<code>python scripts/review_server.py --port 8124</code>) to catch up.</p>"
    )
    call("POST", "/smtp/email", json={
        "sender": {"name": sender["name"], "email": sender["email"]},
        "to": [{"email": to}],
        "subject": "Orangutan Dashboard: social review backlog before this month's digest",
        "htmlContent": f"<html><body>{body}</body></html>",
    })


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="print the backlog, never send an email")
    args = p.parse_args()
    load_env()

    never_drafted, awaiting_review = count_backlog()
    print(f"Never drafted: {never_drafted}; drafted but unreviewed: {awaiting_review}")

    if not (never_drafted or awaiting_review):
        print("Nothing pending; no reminder sent.")
        return
    if args.dry_run:
        print("--dry-run: would send a reminder email now.")
        return

    if not os.environ.get("BREVO_API_KEY"):
        sys.exit("Set BREVO_API_KEY (GitHub secret / .env).")
    to = os.environ.get("REMINDER_TO") or DEFAULT_TO
    send_reminder(never_drafted, awaiting_review, to)
    print(f"Reminder sent to {to}.")


if __name__ == "__main__":
    main()
