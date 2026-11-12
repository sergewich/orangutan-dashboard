#!/usr/bin/env python3
"""
send_digest.py — turn build_digest.py's output into a Brevo email campaign
DRAFT for the "Monthly digest" list, and email a test copy to the owner.

Nothing goes to subscribers from here: the owner opens Brevo -> Campaigns,
checks the draft and clicks Send (draft-then-send, by the owner's choice).

Needs BREVO_API_KEY (restricted key: Email campaigns = All, Contacts =
read/write). Optional: BREVO_LIST_NAME (default "Monthly digest"),
DIGEST_TEST_TO (comma-separated; default: the verified sender address).

    python scripts/send_digest.py --digest /tmp/digest
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests

API = "https://api.brevo.com/v3"


def load_env() -> None:
    env = Path(__file__).resolve().parent.parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            k, _, v = line.strip().partition("=")
            if k and v and not k.startswith("#"):
                os.environ.setdefault(k, v.strip().strip('"'))


def call(method: str, path: str, **kw) -> dict:
    resp = requests.request(method, API + path, timeout=60, headers={
        "api-key": os.environ["BREVO_API_KEY"], "accept": "application/json"}, **kw)
    if resp.status_code >= 400:
        sys.exit(f"Brevo {method} {path} failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json() if resp.content else {}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--digest", required=True, help="folder written by build_digest.py --out")
    args = p.parse_args()
    load_env()
    if not os.environ.get("BREVO_API_KEY"):
        sys.exit("Set BREVO_API_KEY (GitHub secret / .env).")

    d = Path(args.digest)
    summary = json.loads((d / "digest.json").read_text())
    if not summary["incidents"] and not summary["literature"]:
        print("Nothing new since the last digest; no draft created.")
        return
    head, _, tail = (d / "digest.html").read_text().rpartition("</div>")
    # before the last closing tag: the unsubscribe link Brevo fills in
    body = head + '<p style="font-size:12px;color:#888"><a href="{{ unsubscribe }}">Unsubscribe</a></p></div>' + tail

    list_name = os.environ.get("BREVO_LIST_NAME", "Monthly digest")
    lists = call("GET", "/contacts/lists", params={"limit": 50})["lists"]
    match = next((l for l in lists if l["name"] == list_name), None)
    if match is None:
        sys.exit(f"No Brevo contact list named {list_name!r}.")
    if not match.get("uniqueSubscribers"):
        print(f"The {list_name!r} list has no subscribers yet; no draft created.")
        return
    list_id = match["id"]
    sender = next(s for s in call("GET", "/senders")["senders"] if s["active"])

    campaign = call("POST", "/emailCampaigns", json={
        "name": f"Monthly digest — {summary['month']}",
        "subject": summary["subject"],
        "sender": {"id": sender["id"]},
        "htmlContent": f"<html><body>{body}</body></html>",
        # no scheduledAt: the campaign stays a draft until the owner sends it
    })
    # Brevo refuses recipients on create ("no contacts associated") but
    # accepts the same list on an update.
    call("PUT", f"/emailCampaigns/{campaign['id']}", json={"recipients": {"listIds": [list_id]}})
    test_to = [e.strip() for e in (os.environ.get("DIGEST_TEST_TO") or sender["email"]).split(",") if e.strip()]
    call("POST", f"/emailCampaigns/{campaign['id']}/sendTest", json={"emailTo": test_to})
    print(f"Draft campaign {campaign['id']} created for list {list_name!r}; test copy sent to "
          f"{len(test_to)} address(es). Send it from Brevo -> Campaigns.")


if __name__ == "__main__":
    main()
