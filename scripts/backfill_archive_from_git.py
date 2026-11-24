#!/usr/bin/env python3
"""
backfill_archive_from_git.py — one-time, best-effort recovery of months that
rolled off data/fire/*.geojson and data/deforestation/*.geojson's rolling
windows BEFORE scripts/archive_alerts.py started archiving them (2026-11-24).

Not needed going forward — monthly_refresh.yml now archives each month
before it's lost. This is purely to reach a bit further back than that start
date, by mining old git commits of these files (each refresh historically
overwrote them in place, so an old commit is a snapshot of what the rolling
window looked like back then). Quality degrades the further back you go:
fire has (roughly) daily commits so there's a commit close to most month
boundaries; deforestation only has monthly commits, so a target month's
point-level data can only be as complete as whatever the *next* monthly
commit's 60-day window still happened to contain (often not very, for
high-volume months/species — see CLAUDE.md's completeness note). The
aggregate (summary CSV) side isn't git-mined at all here: the *current*
summary CSVs already cover ~12 full months with complete counts, and
scripts/archive_alerts.py already extracted all of those directly (no git
needed) before this script runs.

For each month between the earliest useful commit and whatever
archive_alerts.py already covers, picks the EARLIEST commit dated after that
month's end (closest to the month boundary = least risk of the window
having moved on) and extracts data/fire/<species>_fire_alerts.geojson or
data/deforestation/<species>_integrated_alerts.geojson from it via
`git show`, then reuses the same filter/write/completeness logic as
archive_alerts.py.

    python scripts/backfill_archive_from_git.py            # report + backfill everything findable
    python scripts/backfill_archive_from_git.py --dry-run   # just report what commits would be used
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SPECIES_LIST = ["pongo_abelii", "pongo_pygmaeus", "pongo_tapanuliensis"]
ARCHIVE_DIR = REPO_ROOT / "data" / "archive"
MANIFEST = ARCHIVE_DIR / "manifest.json"
COMPLETENESS_SLACK = 0.95


def git_commits(path: str) -> list[tuple[str, dt.date]]:
    """[(hash, commit_date)] for a path, oldest first."""
    out = subprocess.run(
        ["git", "log", "--format=%H %cI", "--follow", "--", path],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout.strip()
    commits = []
    for line in out.splitlines():
        h, iso = line.split(" ", 1)
        commits.append((h, dt.date.fromisoformat(iso[:10])))
    commits.reverse()  # oldest first
    return commits


def git_show_json(commit: str, path: str) -> dict | None:
    proc = subprocess.run(["git", "show", f"{commit}:{path}"], cwd=REPO_ROOT,
                           capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def month_end(month: str) -> dt.date:
    year, mon = (int(x) for x in month.split("-"))
    return dt.date(year + (mon == 12), mon % 12 + 1, 1) - dt.timedelta(days=1)


def months_between(start: str, end: str) -> list[str]:
    y, m = (int(x) for x in start.split("-"))
    ey, em = (int(x) for x in end.split("-"))
    out = []
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def best_commit_for_month(commits: list[tuple[str, dt.date]], month: str) -> tuple[str, dt.date] | None:
    """Earliest commit dated strictly after the month's end."""
    end = month_end(month)
    for h, d in commits:
        if d > end:
            return h, d
    return None


def load_manifest() -> dict:
    if MANIFEST.exists():
        m = json.loads(MANIFEST.read_text())
    else:
        m = {"fire": {}, "deforestation": {}}
    m.setdefault("fire", {})
    m.setdefault("deforestation", {})
    return m


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


def backfill_fire(species: str, manifest: dict, dry_run: bool) -> None:
    path = f"data/fire/{species}_fire_alerts.geojson"
    commits = git_commits(path)
    if not commits:
        print(f"fire {species}: no commit history found")
        return
    already = set(manifest["fire"].get(species, []))
    earliest_month = commits[0][1].replace(day=1).strftime("%Y-%m")
    target_months = [m for m in months_between(earliest_month, "2026-08") if m not in already]

    for month in target_months:
        best = best_commit_for_month(commits, month)
        if not best:
            print(f"fire   {species} {month}: no commit found after month end, skipping")
            continue
        commit, commit_date = best
        if dry_run:
            print(f"fire   {species} {month}: would use commit {commit[:10]} ({commit_date})")
            continue
        fc = git_show_json(commit, path)
        if not fc:
            print(f"fire   {species} {month}: could not read {path} at {commit[:10]}, skipping")
            continue
        feats = filter_features(fc, "date_acq", month)
        if not feats:
            print(f"fire   {species} {month}: 0 alerts at commit {commit[:10]} ({commit_date}) — window didn't reach back that far, skipping")
            continue
        out_geo = ARCHIVE_DIR / "fire" / f"{species}_{month}.geojson"
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
        manifest["fire"].setdefault(species, [])
        if month not in manifest["fire"][species]:
            manifest["fire"][species].append(month)
            manifest["fire"][species].sort()
        print(f"fire   {species} {month}: {len(feats)} alert(s) recovered from commit {commit[:10]} ({commit_date})")


def backfill_deforestation_points(species: str, manifest: dict, dry_run: bool) -> None:
    path = f"data/deforestation/{species}_integrated_alerts.geojson"
    commits = git_commits(path)
    if not commits:
        print(f"defor  {species}: no commit history found")
        return
    existing = manifest["deforestation"].get(species, {})
    # Only attempt months that are already archived (aggregate-only, from the
    # live summary CSV) but still missing points — there's nowhere else to
    # look for a month with no archive entry at all (no complete count exists).
    target_months = sorted(m for m, info in existing.items() if not info.get("points_complete"))

    for month in target_months:
        best = best_commit_for_month(commits, month)
        if not best:
            continue
        commit, commit_date = best
        out_geo = ARCHIVE_DIR / "deforestation" / f"{species}_{month}.geojson"
        existing_feats = json.loads(out_geo.read_text())["features"] if out_geo.exists() else []
        if dry_run:
            print(f"defor  {species} {month}: would try commit {commit[:10]} ({commit_date}), currently {len(existing_feats)} point(s) archived")
            continue
        fc = git_show_json(commit, path)
        if not fc:
            continue
        feats = filter_features(fc, "date", month)
        if len(feats) <= len(existing_feats):
            print(f"defor  {species} {month}: commit {commit[:10]} has {len(feats)} point(s), no better than what's archived ({len(existing_feats)}), skipping")
            continue
        write_geojson(feats, out_geo)

        summary_csv = ARCHIVE_DIR / "deforestation" / f"{species}_{month}_summary.csv"
        summary_total = 0
        if summary_csv.exists():
            summary_total = sum(int(r["alert_count"]) for r in csv.DictReader(summary_csv.open()))
        complete = summary_total == 0 or len(feats) >= COMPLETENESS_SLACK * summary_total
        manifest["deforestation"].setdefault(species, {})[month] = {"points_complete": complete}
        flag = "" if complete else "  (still incomplete, but better than before)"
        print(f"defor  {species} {month}: {len(feats)} point(s) recovered from commit {commit[:10]} ({commit_date}){flag}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--species", nargs="+", default=SPECIES_LIST, choices=SPECIES_LIST)
    args = p.parse_args()

    manifest = load_manifest()
    for species in args.species:
        backfill_fire(species, manifest, args.dry_run)
    for species in args.species:
        backfill_deforestation_points(species, manifest, args.dry_run)

    if not args.dry_run:
        save_manifest(manifest)
        print(f"Manifest updated: {MANIFEST.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
