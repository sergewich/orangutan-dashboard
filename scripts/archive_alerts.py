#!/usr/bin/env python3
"""
archive_alerts.py — freeze last month's fire + deforestation alerts into a
permanent per-month archive, since both data/fire/*.geojson and
data/deforestation/*.geojson are rolling windows that get overwritten on
every refresh (FIRMS NRT retains only a few weeks; the deforestation point
GeoJSON is deliberately windowed to --points-days, see fetch_deforestation.py)
— nothing before that window survives unless we copy it out first.

For a given month (default: the most recently completed calendar month) and
each species, this script:
  1. filters the CURRENT data/fire/<species>_fire_alerts.geojson to that
     month's dates and writes data/archive/fire/<species>_<YYYY-MM>.geojson
     (+ a matching _summary.csv, aggregated from the filtered features).
  2. does the same for data/deforestation/<species>_integrated_alerts.geojson
     -> data/archive/deforestation/<species>_<YYYY-MM>.geojson, BUT also
     cross-checks completeness against the (always-complete, never
     point-capped) deforestation summary CSV: if --max-points was hit during
     the live refresh, the point GeoJSON can be silently missing whole days
     for a volume-heavy month, even though the summary CSV's counts are
     always correct — this records the fact via `"points_complete": false`
     in the manifest, so the archive is still honest even when it can't
     show every point on a map. The archived summary CSV
     (data/archive/deforestation/<species>_<YYYY-MM>_summary.csv) is always
     taken from the current summary CSV, so it never inherits that gap.
  3. updates data/archive/manifest.json, which the Map tab reads to build
     the month picker (GitHub Pages has no directory-listing API, so a
     static file has to tell the frontend what's available).

Idempotent — an already-archived month is left alone unless --force.

    python scripts/archive_alerts.py                  # last completed month, all species
    python scripts/archive_alerts.py --month 2026-10
    python scripts/archive_alerts.py --month 2026-10 --species pongo_abelii --force
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SPECIES_LIST = ["pongo_abelii", "pongo_pygmaeus", "pongo_tapanuliensis"]
FIRE_DIR = REPO_ROOT / "data" / "fire"
DEFOR_DIR = REPO_ROOT / "data" / "deforestation"
ARCHIVE_DIR = REPO_ROOT / "data" / "archive"
MANIFEST = ARCHIVE_DIR / "manifest.json"

# How much a month's archived point count may fall short of the (always-
# complete) summary CSV's total before we flag it as truncated rather than
# just rounding/race-condition noise.
COMPLETENESS_SLACK = 0.95


def prev_month(today: dt.date) -> str:
    first_of_this_month = today.replace(day=1)
    last_of_prev = first_of_this_month - dt.timedelta(days=1)
    return last_of_prev.strftime("%Y-%m")


def load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text())


def load_manifest() -> dict:
    return load_json(MANIFEST) or {"fire": {}, "deforestation": {}}


def save_manifest(manifest: dict) -> None:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")


def filter_features(fc: dict, date_field: str, month: str) -> list[dict]:
    out = []
    for f in fc.get("features", []):
        d = (f.get("properties") or {}).get(date_field, "")
        if d[:7] == month:
            out.append(f)
    return out


def write_geojson(features: list[dict], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))


def archive_fire(species: str, month: str, force: bool) -> str | None:
    live = load_json(FIRE_DIR / f"{species}_fire_alerts.geojson")
    if not live:
        return None
    out_geo = ARCHIVE_DIR / "fire" / f"{species}_{month}.geojson"
    if out_geo.exists() and not force:
        return "skipped (already archived)"

    live_dates = [(f.get("properties") or {}).get("date_acq", "")[:10]
                  for f in live.get("features", [])]
    live_dates = [d for d in live_dates if d]
    if live_dates and not (min(live_dates)[:7] <= month <= max(live_dates)[:7]):
        # The live rolling window never covered this month at all — writing
        # an empty archive here would misrepresent "we don't know" as "we
        # checked and there were zero fires." Leave it unarchived instead.
        return None

    feats = filter_features(live, "date_acq", month)
    write_geojson(feats, out_geo)

    counts: dict[str, int] = {}
    for f in feats:
        d = f["properties"]["date_acq"][:10]
        counts[d] = counts.get(d, 0) + 1
    out_csv = ARCHIVE_DIR / "fire" / f"{species}_{month}_summary.csv"
    with out_csv.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["species", "date_acq", "alert_count"])
        for d in sorted(counts):
            w.writerow([species, d, counts[d]])
    return f"{len(feats)} alert(s)"


def archive_deforestation(species: str, month: str, force: bool) -> tuple[str | None, bool, int]:
    """Returns (log message, points_complete, points_count). `points_count`
    matters on its own, not just as a boolean: 0 points with a nonzero
    summary total means NO point-level data survives for this month at all
    (the live window never reached back that far when this ran) — a
    different, more total gap than a high-volume month where most points
    were truncated but some remain. The frontend (map.js) uses this
    distinction to avoid saying "too high volume" when the real reason is
    "no point data was ever captured for this month." """
    live = load_json(DEFOR_DIR / f"{species}_integrated_alerts.geojson")
    summary_csv = DEFOR_DIR / f"{species}_integrated_alerts_summary.csv"
    if not live or not summary_csv.exists():
        return None, True, 0

    out_geo = ARCHIVE_DIR / "deforestation" / f"{species}_{month}.geojson"
    out_csv = ARCHIVE_DIR / "deforestation" / f"{species}_{month}_summary.csv"
    if out_geo.exists() and not force:
        existing = json.loads(out_geo.read_text())
        n = len(existing.get("features", []))
        existing_total = (sum(int(r["alert_count"]) for r in csv.DictReader(out_csv.open()))
                           if out_csv.exists() else 0)
        complete = existing_total == 0 or n >= COMPLETENESS_SLACK * existing_total
        return "skipped (already archived)", complete, n

    feats = filter_features(live, "date", month)
    write_geojson(feats, out_geo)

    rows = [r for r in csv.DictReader(summary_csv.open()) if r["date"][:7] == month]
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["species", "date", "confidence", "alert_count", "area_ha"])
        for r in rows:
            w.writerow([r["species"], r["date"], r["confidence"], r["alert_count"], r["area_ha"]])

    summary_total = sum(int(r["alert_count"]) for r in rows)
    complete = summary_total == 0 or len(feats) >= COMPLETENESS_SLACK * summary_total
    return f"{len(feats)} point(s), {summary_total} per summary CSV", complete, len(feats)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--month", default=None, help="YYYY-MM (default: last completed calendar month)")
    p.add_argument("--species", nargs="+", default=SPECIES_LIST, choices=SPECIES_LIST)
    p.add_argument("--force", action="store_true", help="re-archive even if this month already exists")
    args = p.parse_args()

    month = args.month or prev_month(dt.date.today())
    manifest = load_manifest()

    for species in args.species:
        fire_result = archive_fire(species, month, args.force)
        if fire_result:
            print(f"fire   {species} {month}: {fire_result}")
            manifest["fire"].setdefault(species, [])
            if month not in manifest["fire"][species]:
                manifest["fire"][species].append(month)
                manifest["fire"][species].sort()
        else:
            print(f"fire   {species} {month}: no live data to archive, skipped")

        defor_result, complete, points_count = archive_deforestation(species, month, args.force)
        if defor_result:
            if complete:
                flag = ""
            elif points_count == 0:
                flag = "  ** NO POINT DATA: the live window never reached this far back, only aggregate counts are available **"
            else:
                flag = "  ** INCOMPLETE: point data was truncated by --max-points during the live refresh, summary counts are still accurate **"
            print(f"defor  {species} {month}: {defor_result}{flag}")
            manifest["deforestation"].setdefault(species, {})
            manifest["deforestation"][species][month] = {"points_complete": complete, "points_count": points_count}
        else:
            print(f"defor  {species} {month}: no live data to archive, skipped")

    save_manifest(manifest)
    print(f"Manifest updated: {MANIFEST.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
