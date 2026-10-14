#!/usr/bin/env python3
"""
fetch_cloudflare_analytics.py — read visits/page views from Cloudflare Web
Analytics (the free RUM beacon embedded in every site/*.html page) via
Cloudflare's GraphQL Analytics API, and print a totals + by-country summary.

LOCAL ONLY, not deployed and not wired into any GitHub Actions workflow —
this is an operator tool for checking site traffic, not part of the public
dashboard. It reads credentials, never writes them anywhere.

This site isn't a Cloudflare-proxied zone (it's plain GitHub Pages with just
the JS beacon snippet added), so its Web Analytics data is keyed by a
`siteTag` under the account, not a zone. The relevant GraphQL field is
`viewer.accounts(filter: {accountTag}).rumPageloadEventsAdaptiveGroups`,
grouped by `dimensions { countryName, date }` with a `bot: 0` filter to match
the dashboard's default "Exclude bots" view. This isn't documented clearly
on Cloudflare's public docs pages as of writing — found via GraphQL schema
introspection (see git history / ask for the probe script if this ever
breaks and needs rediscovering).

Reads CF_API_TOKEN, CF_ACCOUNT_ID, CF_SITE_TAG from the environment or a
`.env` file at the repo root (KEY=VALUE lines):
  - CF_API_TOKEN: a scoped Cloudflare API token (Account > Account Analytics >
    Read). Create one at https://dash.cloudflare.com/profile/api-tokens —
    do NOT use the Global API Key.
  - CF_ACCOUNT_ID: your Cloudflare account ID (visible in the dashboard URL).
  - CF_SITE_TAG: the Web Analytics site's tag (also visible in the dashboard
    URL, as `siteTag=~in=<value>` on the Web Analytics overview page).

Usage:
    python scripts/fetch_cloudflare_analytics.py                # last 30 days
    python scripts/fetch_cloudflare_analytics.py --days 7
    python scripts/fetch_cloudflare_analytics.py --start-date 2026-09-01 --end-date 2026-09-30
    python scripts/fetch_cloudflare_analytics.py --include-bots
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("This script needs the 'requests' library.\nInstall it with:  pip install requests")

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
API_URL = "https://api.cloudflare.com/client/v4/graphql"

QUERY = """
query ($accountTag: string, $siteTag: string, $start: Date, $end: Date, $filter: AccountRumPageloadEventsAdaptiveGroupsFilter_InputObject) {
  viewer {
    accounts(filter: {accountTag: $accountTag}) {
      rumPageloadEventsAdaptiveGroups(
        filter: $filter
        limit: 10000
        orderBy: [date_ASC]
      ) {
        count
        sum { visits }
        dimensions { countryName date }
      }
    }
  }
}
"""


def load_dotenv(repo_root: Path) -> None:
    env_path = repo_root / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def get_credentials() -> tuple[str, str, str]:
    token = os.environ.get("CF_API_TOKEN", "").strip()
    account_id = os.environ.get("CF_ACCOUNT_ID", "").strip()
    site_tag = os.environ.get("CF_SITE_TAG", "").strip()
    missing = [n for n, v in [("CF_API_TOKEN", token), ("CF_ACCOUNT_ID", account_id), ("CF_SITE_TAG", site_tag)] if not v]
    if missing:
        sys.exit(
            f"Missing {', '.join(missing)}.\n"
            "Add them to a .env file at the repo root:\n"
            "  CF_API_TOKEN=...\n  CF_ACCOUNT_ID=...\n  CF_SITE_TAG=...\n"
            "See the script's docstring for where to find each value."
        )
    return token, account_id, site_tag


def fetch_rows(token: str, account_id: str, site_tag: str, start: str, end: str, include_bots: bool) -> list[dict]:
    filt = {"siteTag": site_tag, "date_geq": start, "date_leq": end}
    if not include_bots:
        filt["bot"] = 0
    body = {
        "query": QUERY,
        "variables": {"accountTag": account_id, "siteTag": site_tag, "start": start, "end": end, "filter": filt},
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    resp = requests.post(API_URL, headers=headers, json=body, timeout=30)
    if resp.status_code != 200:
        sys.exit(f"Cloudflare API error {resp.status_code}:\n{resp.text[:1000]}")
    data = resp.json()
    if data.get("errors"):
        sys.exit(f"GraphQL errors:\n{json.dumps(data['errors'], indent=2)}")
    accounts = data["data"]["viewer"]["accounts"]
    if not accounts:
        sys.exit(
            "No account matched CF_ACCOUNT_ID — double-check it against the "
            "Cloudflare dashboard URL, and that the API token has Account "
            "Analytics: Read on that account."
        )
    return accounts[0]["rumPageloadEventsAdaptiveGroups"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=30, help="Look back this many days (default: 30). Ignored if --start-date is given.")
    parser.add_argument("--start-date", help="Inclusive YYYY-MM-DD. Default: --days ago.")
    parser.add_argument("--end-date", default=dt.date.today().isoformat(), help="Inclusive YYYY-MM-DD (default: today).")
    parser.add_argument("--include-bots", action="store_true", help="Include bot traffic (dashboard default is to exclude it).")
    parser.add_argument("--json", action="store_true", help="Print raw per-row JSON instead of the summary.")
    args = parser.parse_args()

    load_dotenv(REPO_ROOT)
    token, account_id, site_tag = get_credentials()

    end = dt.date.fromisoformat(args.end_date)
    start = dt.date.fromisoformat(args.start_date) if args.start_date else end - dt.timedelta(days=args.days)

    rows = fetch_rows(token, account_id, site_tag, start.isoformat(), end.isoformat(), args.include_bots)

    if args.json:
        print(json.dumps(rows, indent=2))
        return

    if not rows:
        print(f"No data for {start} .. {end}.")
        if not args.include_bots:
            print(
                "At low traffic volumes, Cloudflare's adaptive sampling can return "
                "zero rows for a narrow window once the bot:0 filter is applied, "
                "even when data exists — try --include-bots or a wider --days range."
            )
        return

    total_visits = sum(r["sum"]["visits"] for r in rows)
    total_views = sum(r["count"] for r in rows)
    bot_note = " (including bots)" if args.include_bots else " (bots excluded)"
    print(f"Window: {start} .. {end}{bot_note}")
    print(f"Total visits: {total_visits}")
    print(f"Total page views: {total_views}")

    by_country: dict[str, dict[str, int]] = {}
    for r in rows:
        country = r["dimensions"]["countryName"] or "unknown"
        bucket = by_country.setdefault(country, {"visits": 0, "views": 0})
        bucket["visits"] += r["sum"]["visits"]
        bucket["views"] += r["count"]

    print("\nBy country:")
    for country, counts in sorted(by_country.items(), key=lambda kv: -kv[1]["visits"]):
        print(f"  {country}: visits={counts['visits']}, page views={counts['views']}")


if __name__ == "__main__":
    main()
