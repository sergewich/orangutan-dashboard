#!/usr/bin/env python3
"""
fetch_fire.py — pull NASA FIRMS VIIRS active fire data, clipped to
each orangutan range polygon, and save as GeoJSON (points) + summary CSV.

Data source: NASA FIRMS "area" API (see the project brief). This is a
GET, bounding-box-only, CSV-returning API — it does NOT accept an arbitrary
polygon like the GFW Data API does, and each request covers at most 5 days
(https://firms.modaps.eosdis.nasa.gov/api/area/). So for every
FeatureCollection in data/ranges/*.geojson this script:
  1. merges the range polygons into one geometry and takes its bounding box,
  2. GETs the FIRMS area/csv endpoint in <=5-day chunks covering the
     requested date window, clipped server-side to that bounding box,
  3. filters the returned points to the actual polygon client-side (a
     bounding box is coarser than the range shape), since FIRMS itself
     cannot clip to a polygon,
  4. writes:
       data/fire/<species>_fire_alerts.geojson   (one Point per detection)
       data/fire/<species>_fire_alerts_summary.csv (counts by date)

The API key is read from the FIRMS_API_KEY environment variable, or from a `.env`
file at the repo root (KEY=VALUE lines). It is never written to any output.

Usage:
    # test with a single small range first (recommended)
    python scripts/fetch_fire.py --species pongo_tapanuliensis

    # then all ranges found in data/ranges/
    python scripts/fetch_fire.py

    # custom window / sensor
    python scripts/fetch_fire.py --species pongo_abelii \
        --start-date 2024-01-01 --end-date 2024-12-31 --source VIIRS_NOAA20_NRT

Register for a free key at https://firms.modaps.eosdis.nasa.gov/api/ (FIRMS API).
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import os
import sys
import time
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

# NASA FIRMS "area" API: GET, bounding-box only (no arbitrary polygon), CSV
# response, at most 5 days per request. https://firms.modaps.eosdis.nasa.gov/api/area/
API_BASE = "https://firms.modaps.eosdis.nasa.gov/api/area/csv"
MAX_DAY_RANGE = 5
DEFAULT_SOURCE = "VIIRS_SNPP_NRT"
VALID_SOURCES = {
    "LANDSAT_NRT", "MODIS_NRT", "MODIS_SP",
    "VIIRS_NOAA20_NRT", "VIIRS_NOAA20_SP",
    "VIIRS_NOAA21_NRT",
    "VIIRS_SNPP_NRT", "VIIRS_SNPP_SP",
}


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
    into a single MultiPolygon usable for client-side point-in-polygon
    filtering (FIRMS itself only accepts a bounding box, see run_query).

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


def bbox_of_multipolygon(geometry: dict) -> tuple[float, float, float, float]:
    """Return (west, south, east, north) covering every ring in a MultiPolygon."""
    lons: list = []
    lats: list = []
    for polygon in geometry["coordinates"]:
        for ring in polygon:
            for lon, lat in ring:
                lons.append(lon)
                lats.append(lat)
    return min(lons), min(lats), max(lons), max(lats)


def _point_in_ring(lon: float, lat: float, ring: list) -> bool:
    """Even-odd ray-casting test."""
    inside = False
    n = len(ring)
    x1, y1 = ring[0]
    for i in range(1, n + 1):
        x2, y2 = ring[i % n]
        if ((y1 > lat) != (y2 > lat)) and (
            lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1
        ):
            inside = not inside
        x1, y1 = x2, y2
    return inside


def point_in_multipolygon(lon: float, lat: float, geometry: dict) -> bool:
    """True if (lon, lat) falls inside any polygon's exterior ring and outside
    all of that polygon's hole rings."""
    for polygon in geometry["coordinates"]:
        if not polygon:
            continue
        if not _point_in_ring(lon, lat, polygon[0]):
            continue
        if any(_point_in_ring(lon, lat, hole) for hole in polygon[1:]):
            continue
        return True
    return False


# --------------------------------------------------------------------------- #
# FIRMS API query
# --------------------------------------------------------------------------- #
def _daterange_chunks(start: dt.date, end: dt.date, chunk_days: int):
    """Yield (chunk_start, day_count) covering [start, end] inclusive in
    <=chunk_days steps, matching the area API's DATE + DAY_RANGE semantics
    (DATE is the start of the window, not the end)."""
    cur = start
    while cur <= end:
        remaining = (end - cur).days + 1
        day_count = min(chunk_days, remaining)
        yield cur, day_count
        cur += dt.timedelta(days=day_count)


def run_query(
    bbox: tuple[float, float, float, float],
    api_key: str,
    source: str,
    start_date: str,
    end_date: str,
) -> list[dict]:
    """GET the FIRMS area/csv endpoint in <=MAX_DAY_RANGE-day chunks covering
    [start_date, end_date], clipped server-side to `bbox`. Returns the
    combined list of CSV rows (as dicts) across all chunks."""
    start = dt.date.fromisoformat(start_date)
    end = dt.date.fromisoformat(end_date)
    west, south, east, north = bbox
    area = f"{west},{south},{east},{north}"

    rows: list[dict] = []
    for chunk_start, day_count in _daterange_chunks(start, end, MAX_DAY_RANGE):
        url = f"{API_BASE}/{api_key}/{source}/{area}/{day_count}/{chunk_start.isoformat()}"
        try:
            resp = requests.get(url, timeout=60)
        except requests.RequestException as exc:
            sys.exit(f"  Could not reach FIRMS API: {exc}")

        if resp.status_code != 200:
            sys.exit(f"  FIRMS API error {resp.status_code} {resp.reason}:\n{resp.text[:1000]}")

        text = resp.text.strip()
        # The API returns a plain-text one-line error (e.g. bad key / no data
        # for source) instead of CSV when something's wrong.
        if not text or "\n" not in text:
            if text and "error" in text.lower():
                sys.exit(f"  FIRMS query failed: {text[:500]}")
            continue  # no detections in this window

        reader = csv.DictReader(io.StringIO(text))
        rows.extend(reader)

        # Stay well under the 5000-transactions/10-minute key limit.
        time.sleep(0.2)

    return rows


# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #
def rows_to_geojson(rows: list[dict], species: str, geometry: dict) -> dict:
    """Convert FIRMS CSV rows to a GeoJSON FeatureCollection, keeping only
    points that fall inside the actual range polygon (the FIRMS query itself
    only clipped to the bounding box)."""
    features = []
    for r in rows:
        try:
            lat = float(r["latitude"])
            lon = float(r["longitude"])
        except (KeyError, ValueError):
            continue
        if not point_in_multipolygon(lon, lat, geometry):
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {
                    "species": species,
                    "date_acq": f"{r.get('acq_date', '')}T{r.get('acq_time', '')}",
                    "confidence": r.get("confidence"),
                    "brightness": r.get("bright_ti4") or r.get("brightness"),
                    "frp": r.get("frp"),
                    "daynight": r.get("daynight"),
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}


def write_summary_csv(features: list[dict], species: str, out_path: Path) -> int:
    """Aggregate feature count by acquisition date and write to CSV.
    Returns the total alert count."""
    counts: dict[str, int] = {}
    for f in features:
        date_acq = f["properties"].get("date_acq", "")
        date_only = date_acq.split("T", 1)[0]
        counts[date_only] = counts.get(date_only, 0) + 1

    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["species", "date_acq", "alert_count"])
        for date_only in sorted(counts):
            writer.writerow([species, date_only, counts[date_only]])

    return sum(counts.values())


# --------------------------------------------------------------------------- #
# Per-species driver
# --------------------------------------------------------------------------- #
def process_species(
    geojson_path: Path,
    out_dir: Path,
    api_key: str,
    source: str,
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
    bbox = bbox_of_multipolygon(geometry)
    print(f"  bbox (west,south,east,north): {bbox}")
    print(f"  window {start} .. {end} ({source})")

    rows = run_query(bbox, api_key, source, start, end)
    print(f"  API returned {len(rows)} row(s) in bbox")

    out_dir.mkdir(parents=True, exist_ok=True)
    geojson_out = out_dir / f"{species}_fire_alerts.geojson"
    csv_out = out_dir / f"{species}_fire_alerts_summary.csv"

    fc = rows_to_geojson(rows, species, geometry)
    geojson_out.write_text(json.dumps(fc), encoding="utf-8")
    total_alerts = write_summary_csv(fc["features"], species, csv_out)

    print(f"  wrote {len(fc['features'])} point(s) inside range -> data/fire/{geojson_out.name}")
    print(f"  wrote summary          -> data/fire/{csv_out.name}")
    print(f"  totals: {total_alerts} alerts")
    if len(rows) > 0 and len(fc["features"]) == 0:
        print(
            "  NOTE: bbox query returned rows but none fell inside the range "
            "polygon — check the range geometry if this looks wrong.",
            file=sys.stderr,
        )
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
    parser.add_argument("--source", default=DEFAULT_SOURCE, choices=sorted(VALID_SOURCES),
                        help=f"FIRMS sensor/source (default: {DEFAULT_SOURCE}). "
                             "*_NRT sources only retain a recent rolling window (order of "
                             "weeks/months) — use the matching *_SP source for older windows.")
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

    if args.source.endswith("_NRT") and (
        dt.date.today() - dt.date.fromisoformat(args.start_date)
    ).days > 60:
        print(
            f"  WARNING: --source {args.source} is a near-real-time feed and typically "
            "doesn't retain data that far back. If you get 0 results for the older part "
            f"of the window, retry with --source {args.source.replace('_NRT', '_SP')}.",
            file=sys.stderr,
        )

    presence = None if args.presence == [0] else set(args.presence)
    targets = resolve_targets(args.species, args.ranges_dir)

    print(f"Processing {len(targets)} range file(s) into {args.out_dir}")

    ok = 0
    for path in targets:
        if process_species(
            path, args.out_dir, api_key, args.source,
            args.start_date, args.end_date,
            presence,
        ):
            ok += 1

    print(f"\nDone: {ok}/{len(targets)} range(s) processed.")
    if ok < len(targets):
        sys.exit(1)


if __name__ == "__main__":
    main()
