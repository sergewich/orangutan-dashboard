#!/usr/bin/env python3
"""
archive_cf_analytics.py — snapshot Cloudflare Web Analytics (visits/page
views) into data/archive/ before it ages out, since Cloudflare's free Web
Analytics only *retains* 30 days of RUM data (confirmed 2026-11-26: the
dashboard's date picker refuses anything older, and this is the underlying
data actually being gone, not just a display limit) — nothing before that
survives unless we copy it out first, same problem and same fix as
data/fire/data/deforestation (see archive_alerts.py).

Reuses fetch_cloudflare_analytics.py's GraphQL query helpers (same
dataset/credentials/schema) rather than re-deriving them.

Writes two things each run:
  1. data/archive/cf_analytics/visits_daily.json — a single cumulative
     {date: {visits, page_views}} dict, upserted (never truncated) from the
     last LOOKBACK_DAYS of daily totals. The 35-day lookback (vs. the
     ~30-day retention and the ~30-day gap between monthly_refresh.yml
     runs) is deliberate slack: a run landing a few days late, or one
     missed run, still won't lose a day's data to the retention window
     closing first. A gap longer than that (e.g. two consecutive missed
     runs) will still lose whatever aged out in between — this isn't
     trying to survive extended outages, just the normal monthly cadence.
  2. data/archive/cf_analytics/<YYYY-MM>.json — a best-effort snapshot of
     the last *completed* calendar month's top pages/countries (not
     gap-sensitive the way visits_daily.json is — this is a "what did a
     typical month look like" snapshot, not a trend line). Idempotent,
     like archive_alerts.py: skipped if already archived unless --force.

Updates data/archive/manifest.json's "cf_analytics" list (which months have
a snapshot file) the same way archive_alerts.py does for fire/deforestation.

Needs CF_API_TOKEN, CF_ACCOUNT_ID, CF_SITE_TAG — see
fetch_cloudflare_analytics.py's docstring for where to find each.

    python scripts/archive_cf_analytics.py
    python scripts/archive_cf_analytics.py --month 2026-10 --force
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))
from fetch_cloudflare_analytics import fetch_rows, get_credentials, load_dotenv  # noqa: E402

ARCHIVE_DIR = REPO_ROOT / "data" / "archive"
CF_DIR = ARCHIVE_DIR / "cf_analytics"
MANIFEST = ARCHIVE_DIR / "manifest.json"
DAILY_FILE = CF_DIR / "visits_daily.json"

LOOKBACK_DAYS = 35
TOP_N = 15


def prev_month(today: dt.date) -> str:
    first_of_this_month = today.replace(day=1)
    return (first_of_this_month - dt.timedelta(days=1)).strftime("%Y-%m")


def month_bounds(month: str) -> tuple[dt.date, dt.date]:
    start = dt.date.fromisoformat(f"{month}-01")
    if start.month == 12:
        end = dt.date(start.year, 12, 31)
    else:
        end = dt.date(start.year, start.month + 1, 1) - dt.timedelta(days=1)
    return start, end


def load_manifest() -> dict:
    if MANIFEST.exists():
        m = json.loads(MANIFEST.read_text())
    else:
        m = {"fire": {}, "deforestation": {}}
    m.setdefault("cf_analytics", [])
    return m


def save_manifest(manifest: dict) -> None:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")


def update_daily_totals(token: str, account_id: str, site_tag: str) -> int:
    end = dt.date.today()
    start = end - dt.timedelta(days=LOOKBACK_DAYS)
    rows = fetch_rows(token, account_id, site_tag, start.isoformat(), end.isoformat(),
                       include_bots=False, dimension_field="date")

    daily = json.loads(DAILY_FILE.read_text()) if DAILY_FILE.exists() else {}
    touched = set()
    for r in rows:
        date = r["dimensions"]["date"]
        bucket = daily.setdefault(date, {"visits": 0, "page_views": 0})
        if date not in touched:
            bucket["visits"] = 0
            bucket["page_views"] = 0
            touched.add(date)
        bucket["visits"] += r["sum"]["visits"]
        bucket["page_views"] += r["count"]

    CF_DIR.mkdir(parents=True, exist_ok=True)
    DAILY_FILE.write_text(json.dumps(daily, indent=1, sort_keys=True) + "\n")
    return len(touched)


def archive_month_breakdown(token: str, account_id: str, site_tag: str, month: str, force: bool) -> str:
    out_path = CF_DIR / f"{month}.json"
    if out_path.exists() and not force:
        return "skipped (already archived)"

    start, end = month_bounds(month)
    path_rows = fetch_rows(token, account_id, site_tag, start.isoformat(), end.isoformat(),
                            include_bots=False, dimension_field="requestPath")
    country_rows = fetch_rows(token, account_id, site_tag, start.isoformat(), end.isoformat(),
                               include_bots=False, dimension_field="countryName")

    def rank(rows: list[dict], field: str) -> list[dict]:
        grouped: dict[str, dict[str, int]] = {}
        for r in rows:
            key = r["dimensions"].get(field) or "(none)"
            bucket = grouped.setdefault(key, {"visits": 0, "page_views": 0})
            bucket["visits"] += r["sum"]["visits"]
            bucket["page_views"] += r["count"]
        ranked = sorted(grouped.items(), key=lambda kv: -kv[1]["visits"])[:TOP_N]
        return [{"key": k, **v} for k, v in ranked]

    top_paths = rank(path_rows, "requestPath")
    top_countries = rank(country_rows, "countryName")
    total_views = sum(r["count"] for r in path_rows)
    total_visits = sum(r["sum"]["visits"] for r in path_rows)

    CF_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "month": month,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "total_visits": total_visits,
        "total_page_views": total_views,
        "top_paths": top_paths,
        "top_countries": top_countries,
    }, indent=1))
    return f"{total_visits} visit(s), {total_views} page view(s)"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--month", default=None, help="YYYY-MM (default: last completed calendar month)")
    p.add_argument("--force", action="store_true", help="re-archive this month's breakdown even if it already exists")
    args = p.parse_args()

    load_dotenv(REPO_ROOT)
    token, account_id, site_tag = get_credentials()

    month = args.month or prev_month(dt.date.today())

    days_updated = update_daily_totals(token, account_id, site_tag)
    print(f"daily totals: upserted {days_updated} day(s) from the last {LOOKBACK_DAYS} days into {DAILY_FILE.relative_to(REPO_ROOT)}")

    result = archive_month_breakdown(token, account_id, site_tag, month, args.force)
    print(f"month breakdown {month}: {result}")

    manifest = load_manifest()
    if month not in manifest["cf_analytics"]:
        manifest["cf_analytics"].append(month)
        manifest["cf_analytics"].sort()
    save_manifest(manifest)


if __name__ == "__main__":
    main()
