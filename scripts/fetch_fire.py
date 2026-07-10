#!/usr/bin/env python3
"""
fetch_fire.py — pull NASA FIRMS VIIRS active fire data, clipped to
each orangutan range polygon, and save as GeoJSON (points) + summary CSV.

Data source: NASA FIRMS API. See the project brief.

For every FeatureCollection in data/ranges/*.geojson this script:
  1. merges the range polygons into one geometry,
  2. POSTs a query to the FIRMS API, clipped to that geometry and a date window,
  3. writes:
       data/fire/<species>_fire_alerts.geojson   (one Point per alert pixel)
       data/fire/<species>_fire_alerts_summary.csv (counts by date)

The API key is read from the FIRMS_API_KEY environment variable, or from a `.env`
file at the repo root (KEY=VALUE lines). It is never written to any output.

Usage:
    # test with a single small range first (recommended)
    python scripts/fetch_fire.py --species pongo_tapanuliensis

    # then all ranges found in data/ranges/
    python scripts/fetch_fire.py

    # custom window
    python scripts/fetch_fire.py --species pongo_abelii \
        --start-date 2024-01-01 --end-date 2024-12-31

Register for a free key at https://firms.modaps.eosdis.nasa.gov/ (FIRMS API).
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit(
        "This script needs the 'requests' library.\n"
        "Install it with:  pip install requests\n"
        "(urllib is not used because FIRMS API resets its connections.)"
    )

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_RANGES_DIR = REPO_ROOT / "data" / "ranges"
DEFAULT_OUT_DIR = REPO_ROOT / "data" / "fire"

API_BASE = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"

# Inline geometries much larger than this tend to be rejected or time out on the
# synchronous API endpoint; warn and suggest the async path.
GEOMETRY_WARN_BYTES = 3_000_000


# --------------------------------------------------------------------------- #
# Environment / API key
# --------------------------------------------------------------------------- #
def load_dotenv(repo_root: Path) -> None:
    """Populate os.environ from a repo-root .env (does not overwrite existing
    vars). Deliberately dependency-free so the script stays standalone."""
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


def get_api_key() -> str:
    key = os.environ.get("FIRMS_API_KEY", "").strip()
    if not key:
        sys.exit(
            "FIRMS_API_KEY not set.\n"
            "Add it to a .env file at the repo root:\n"
            "  FIRMS_API_KEY=your-key-here\n"
            "or export it in your shell before running. "
            "Register for a free key at https://firms.modaps.eosdis.nasa.gov/ (FIRMS API)."
        )
    return key


# --------------------------------------------------------------------------- #
# Range geometry
# --------------------------------------------------------------------------- #
def merge_range_geometry(geojson_path: Path, presence: set[int] | None) -> tuple[dict, int]:
    """Load a range FeatureCollection and merge every (Multi)Polygon feature
    into a single MultiPolygon suitable for the API `geometry` field.

    Returns (geometry, n_features_used). Optionally keep only features whose
    PRESENCE attribute is in `presence` (IUCN presence codes; 1 == Extant)."""
    fc = json.loads(geojson_path.read_text(encoding="utf-8"))
    features = fc.get("features", []) if fc.get("type") == "FeatureCollection" else [fc]

    polygons: list = []
    used = 0
    for feat in features:
        if presence is not None:
            code = (feat.get("properties") or {}).get("PRESENCE")
            if code is not None and int(code) not in presence:
                continue
        geom = feat.get("geometry") or {}
        gtype = geom.get("type")
        coords = geom.get("coordinates")
        if not coords:
            continue
        if gtype == "Polygon":
            polygons.append(coords)
            used += 1
        elif gtype == "MultiPolygon":
            polygons.extend(coords)
            used += 1
        else:
            print(f"  skipping unsupported geometry type: {gtype}", file=sys.stderr)

    if not polygons:
        raise ValueError(f"no polygon geometry found in {geojson_path.name}")
    return {"type": "MultiPolygon", "coordinates": polygons}, used


# --------------------------------------------------------------------------- #
# FIRMS API query
# --------------------------------------------------------------------------- #
class PayloadTooLarge(Exception):
    """Raised when the API rejects a query because its JSON response would exceed
    the synchronous endpoint's ~6 MB cap. The caller narrows the date range and
    retries."""


def run_query(geometry: dict, api_key: str, start_date: str, end_date: str) -> list[dict]:
    """POST a query clipped to `geometry`, return the list of result rows."""
    url = API_BASE
    body = {
        "format": "json",
        "latitude": geometry["coordinates"][0][0][1],
        "longitude": geometry["coordinates"][0][0][0],
        "startDate": start_date,
        "endDate": end_date,
        "polygon": json.dumps(geometry),
        "key": api_key
    }

    payload_bytes = len(json.dumps(body).encode("utf-8"))
    if payload_bytes > GEOMETRY_WARN_BYTES:
        print(
            f"  WARNING: query payload is {payload_bytes / 1e6:.1f} MB — large/complex ranges "
            "may be rejected or time out on the synchronous endpoint.\n"
            "  If this fails, simplify the range polygon.",
            file=sys.stderr,
        )

    headers = {
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(url, data=json.dumps(body), headers=headers)
    except requests.RequestException as exc:
        sys.exit(f"  Could not reach FIRMS API: {exc}")

    if resp.status_code != 200:
        sys.exit(f"  FIRMS API error {resp.status_code} {resp.reason}:\n{resp.text[:1000]}")

    try:
        data = resp.json()
    except ValueError:
        sys.exit(f"  FIRMS API returned non-JSON response:\n{resp.text[:1000]}")

    if "message" in data and "error" in data["message"].lower():
        sys.exit(f"  FIRMS query failed: {json.dumps(data)[:1000]}")

    return data.get("data", []) or []


# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #
def rows_to_geojson(rows: list[dict], species: str) -> dict:
    features = []
    for r in rows:
        lon, lat = r.get("longitude"), r.get("latitude")
        if lon is None or lat is None:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {
                    "species": species,
                    "date_acq": r.get("acquisition_time"),
                    "confidence": r.get("confidence"),
                    "brightness": r.get("brightness"),
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}


def write_summary_csv(summary_rows: list[dict], species: str, out_path: Path) -> tuple[int]:
    """Write the aggregate (date) rows from the summary query to CSV.
    Returns (total_alerts)."""
    total_alerts = 0
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["species", "date_acq", "alert_count"])
        for r in summary_rows:
            count = int(r.get("count") or 0)
            total_alerts += count
            writer.writerow([
                species,
                r.get("acquisition_time"),
                count,
            ])
    return total_alerts


# --------------------------------------------------------------------------- #
# Per-species driver
# --------------------------------------------------------------------------- #
def process_species(
    geojson_path: Path,
    out_dir: Path,
    api_key: str,
    start: str,
    end: str,
    presence: set[int] | None,
) -> bool:
    species = geojson_path.stem
    print(f"\n=== {species} ===")
    try:
        geometry, n_feats = merge_range_geometry(geojson_path, presence)
    except ValueError as exc:
        print(f"  SKIP: {exc}", file=sys.stderr)
        return False
    print(f"  range: {n_feats} polygon feature(s) merged")
    print(f"  window {start} .. {end}")

    # Point query over the full window — one feature per alert pixel for
    # the map.
    point_rows = run_query(geometry, api_key, start, end)
    print(f"  API returned {len(point_rows)} point row(s)")

    out_dir.mkdir(parents=True, exist_ok=True)
    geojson_out = out_dir / f"{species}_fire_alerts.geojson"
    csv_out = out_dir / f"{species}_fire_alerts_summary.csv"

    fc = rows_to_geojson(point_rows, species)
    geojson_out.write_text(json.dumps(fc), encoding="utf-8")
    total_alerts = write_summary_csv(point_rows, species, csv_out)

    print(f"  wrote {len(fc['features'])} point(s) -> data/fire/{geojson_out.name}")
    print(f"  wrote summary          -> data/fire/{csv_out.name}")
    print(f"  totals: {total_alerts} alerts")
    return True


def default_start() -> str:
    return (dt.date.today() - dt.timedelta(days=365)).isoformat()


def resolve_targets(species: str | None, ranges_dir: Path) -> list[Path]:
    if species:
        cand = Path(species)
        if not cand.exists():
            cand = ranges_dir / (species if species.endswith(".geojson") else f"{species}.geojson")
        if not cand.exists():
            sys.exit(f"Range file not found for '{species}' (looked for {cand}).")
        return [cand]
    targets = sorted(ranges_dir.glob("*.geojson"))
    if not targets:
        sys.exit(f"No range GeoJSON files in {ranges_dir}. Run convert_ranges.py first.")
    return targets


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--species",
                        help="Single range to process: a species name (e.g. pongo_tapanuliensis), "
                             "a .geojson filename, or a path. Default: all in --ranges-dir.")
    parser.add_argument("--ranges-dir", type=Path, default=DEFAULT_RANGES_DIR)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--start-date", default=default_start(),
                        help="Inclusive YYYY-MM-DD (default: 365 days ago).")
    parser.add_argument("--end-date", default=dt.date.today().isoformat(),
                        help="Inclusive YYYY-MM-DD (default: today).")
    parser.add_argument("--presence", type=int, nargs="+", default=[1],
                        help="IUCN PRESENCE codes to include (default: 1 = Extant). "
                             "Pass 0 to disable filtering.")
    args = parser.parse_args()

    load_dotenv(REPO_ROOT)
    api_key = get_api_key()

    presence = None if args.presence == [0] else set(args.presence)
    targets = resolve_targets(args.species, args.ranges_dir)

    print(f"Processing {len(targets)} range file(s) into {args.out_dir}")

    ok = 0
    for path in targets:
        if process_species(
            path, args.out_dir, api_key,
            args.start_date, args.end_date,
            presence,
        ):
            ok += 1

    print(f"\nDone: {ok}/{len(targets)} range(s) processed.")
    if ok < len(targets):
        sys.exit(1)


if __name__ == "__main__":
    main()
