#!/usr/bin/env python3
"""
fetch_deforestation.py — pull GFW integrated deforestation alerts, clipped to
each orangutan range polygon, and save as GeoJSON (points) + summary CSV.

Data source: Global Forest Watch Data API, dataset `gfw_integrated_alerts`
(GLAD-L + GLAD-S2 + RADD combined, 10 m, daily). See the project brief.

For every FeatureCollection in data/ranges/*.geojson this script:
  1. merges the range polygons into one geometry,
  2. POSTs a SQL query to the GFW Data API /query/json endpoint, clipped to
     that geometry and a date window,
  3. writes:
       data/deforestation/<species>_integrated_alerts.geojson   (one Point per alert pixel)
       data/deforestation/<species>_integrated_alerts_summary.csv (counts + hectares by date/confidence)

The API key is read from the GFW_API_KEY environment variable, or from a `.env`
file at the repo root (KEY=VALUE lines). It is never written to any output.

Usage:
    # test with a single small range first (recommended)
    python scripts/fetch_deforestation.py --species pongo_tapanuliensis

    # then all ranges found in data/ranges/
    python scripts/fetch_deforestation.py

    # custom window / confidence filter
    python scripts/fetch_deforestation.py --species pongo_abelii \
        --start-date 2024-01-01 --end-date 2024-12-31 --confidence high highest

Register for a free key at https://www.globalforestwatch.org/ (Data API).
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
        "(urllib is not used because GFW's CDN resets its connections.)"
    )

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_RANGES_DIR = REPO_ROOT / "data" / "ranges"
DEFAULT_OUT_DIR = REPO_ROOT / "data" / "deforestation"

DATASET = "gfw_integrated_alerts"
API_BASE = "https://data-api.globalforestwatch.org"

# GFW's CloudFront edge resets connections from bare API clients (default urllib/
# requests User-Agents) and rejects requests with no Origin. A browser-like
# User-Agent plus an Origin header makes the request go through; the /latest alias
# also 307-redirects to a concrete version, which requests follows while keeping
# the x-api-key header (urllib drops it on redirect).
USER_AGENT = "orangutan-dashboard/1.0 (deforestation fetcher)"
ORIGIN = "https://globalforestwatch.org"

# GFW integrated-alerts confidence categories (strings stored in the dataset).
VALID_CONFIDENCE = ("nominal", "high", "highest")

# Inline geometries much larger than this tend to be rejected or time out on the
# synchronous /query/json endpoint; warn and suggest the async / geostore path.
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
    key = os.environ.get("GFW_API_KEY", "").strip()
    if not key:
        sys.exit(
            "GFW_API_KEY not set.\n"
            "Add it to a .env file at the repo root:\n"
            "  GFW_API_KEY=your-key-here\n"
            "or export it in your shell before running. "
            "Register for a free key at https://www.globalforestwatch.org/ (Data API)."
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
# GFW Data API query
# --------------------------------------------------------------------------- #
class PayloadTooLarge(Exception):
    """Raised when the API rejects a query because its JSON response would exceed
    the synchronous endpoint's ~6 MB cap. The caller narrows the date range and
    retries."""


def run_query(sql: str, geometry: dict, api_key: str, version: str, timeout: int) -> list[dict]:
    """POST a SQL query clipped to `geometry`, return the list of result rows."""
    url = f"{API_BASE}/dataset/{DATASET}/{version}/query/json"
    body = {"sql": sql, "geometry": geometry}

    payload_bytes = len(json.dumps(body).encode("utf-8"))
    if payload_bytes > GEOMETRY_WARN_BYTES:
        print(
            f"  WARNING: query payload is {payload_bytes / 1e6:.1f} MB — large/complex ranges "
            "may be rejected or time out on the synchronous endpoint.\n"
            "  If this fails, simplify the range polygon or switch to the geostore / "
            "async batch query path.",
            file=sys.stderr,
        )

    headers = {
        "x-api-key": api_key,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "Origin": ORIGIN,
    }
    try:
        resp = requests.post(url, data=json.dumps(body), headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        sys.exit(f"  Could not reach GFW API: {exc}")

    if resp.status_code != 200:
        if "payload size exceeded" in resp.text.lower() or "ResponseSizeTooLarge" in resp.text:
            raise PayloadTooLarge(resp.text[:300])
        sys.exit(f"  GFW API error {resp.status_code} {resp.reason}:\n{resp.text[:1000]}")

    try:
        data = resp.json()
    except ValueError:
        sys.exit(f"  GFW API returned non-JSON response:\n{resp.text[:1000]}")

    if data.get("status") != "success":
        sys.exit(f"  GFW query failed: {json.dumps(data)[:1000]}")

    return data.get("data", []) or []


def _where(start: str, end: str, confidence: list[str] | None) -> str:
    clauses = [
        f"gfw_integrated_alerts__date >= '{start}'",
        f"gfw_integrated_alerts__date <= '{end}'",
    ]
    if confidence:
        quoted = ", ".join(f"'{c}'" for c in confidence)
        clauses.append(f"gfw_integrated_alerts__confidence IN ({quoted})")
    return " AND ".join(clauses)


def build_points_sql(start: str, end: str, confidence: list[str] | None, limit: int | None) -> str:
    """One row per alert pixel. NOTE: area__ha is an aggregation-only field and
    cannot be selected per-pixel (the API 500s if you try), so it is omitted here
    and reported via the summary query instead."""
    sql = (
        "SELECT latitude, longitude, "
        "gfw_integrated_alerts__date, "
        "gfw_integrated_alerts__confidence, "
        "gfw_integrated_alerts__intensity "
        "FROM results "
        f"WHERE {_where(start, end, confidence)} "
        "ORDER BY gfw_integrated_alerts__date"
    )
    if limit:
        sql += f" LIMIT {limit}"
    return sql


def build_summary_sql(start: str, end: str, confidence: list[str] | None) -> str:
    """Alert counts and hectares aggregated by date and confidence."""
    # count(*) comes back as the column "count" (alias ignored by the API);
    # sum(area__ha) keeps its alias.
    return (
        "SELECT gfw_integrated_alerts__date, "
        "gfw_integrated_alerts__confidence, "
        "count(*), "
        "sum(area__ha) AS area_ha "
        "FROM results "
        f"WHERE {_where(start, end, confidence)} "
        "GROUP BY gfw_integrated_alerts__date, gfw_integrated_alerts__confidence "
        "ORDER BY gfw_integrated_alerts__date"
    )


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
                    "date": r.get("gfw_integrated_alerts__date"),
                    "confidence": r.get("gfw_integrated_alerts__confidence"),
                    "intensity": r.get("gfw_integrated_alerts__intensity"),
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}


def write_summary_csv(summary_rows: list[dict], species: str, out_path: Path) -> tuple[int, float]:
    """Write the aggregate (date, confidence) rows from the summary query to CSV.
    Returns (total_alerts, total_area_ha)."""
    total_alerts = 0
    total_area = 0.0
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["species", "date", "confidence", "alert_count", "area_ha"])
        for r in summary_rows:
            # NOTE: the API ignores the alias on count(*) and always returns the
            # column as "count"; sum(...) aliases are honoured.
            count = int(r.get("count") or 0)
            area = float(r.get("area_ha") or 0.0)
            total_alerts += count
            total_area += area
            writer.writerow([
                species,
                r.get("gfw_integrated_alerts__date"),
                r.get("gfw_integrated_alerts__confidence"),
                count,
                round(area, 4),
            ])
    return total_alerts, total_area


# --------------------------------------------------------------------------- #
# Per-species driver
# --------------------------------------------------------------------------- #
def fetch_points(geometry: dict, api_key: str, start: str, end: str,
                 confidence: list[str] | None, version: str, limit: int,
                 timeout: int) -> tuple[list[dict], bool, list[str]]:
    """Fetch alert points over [start, end], respecting the API's ~6 MB response
    cap by adaptively bisecting the date range whenever a response is too large.
    A single day that is still too large is skipped (its alerts remain counted in
    the summary CSV). Stops once `limit` rows are collected.

    Returns (rows, truncated, skipped_days)."""
    rows: list[dict] = []
    skipped: list[str] = []
    state = {"truncated": False}

    def recurse(d0: dt.date, d1: dt.date) -> None:
        if len(rows) >= limit:
            state["truncated"] = True
            return
        remaining = limit - len(rows)
        try:
            chunk = run_query(build_points_sql(d0.isoformat(), d1.isoformat(), confidence, remaining),
                              geometry, api_key, version, timeout)
        except PayloadTooLarge:
            if d0 >= d1:
                skipped.append(d0.isoformat())
                return
            mid = d0 + (d1 - d0) // 2
            recurse(d0, mid)
            recurse(mid + dt.timedelta(days=1), d1)
            return
        rows.extend(chunk)
        if len(rows) >= limit:
            state["truncated"] = True

    recurse(dt.date.fromisoformat(start), dt.date.fromisoformat(end))
    return rows[:limit], state["truncated"], skipped


def process_species(
    geojson_path: Path,
    out_dir: Path,
    api_key: str,
    start: str,
    end: str,
    points_start: str,
    confidence: list[str] | None,
    presence: set[int] | None,
    version: str,
    limit: int,
    timeout: int,
) -> bool:
    species = geojson_path.stem
    print(f"\n=== {species} ===")
    try:
        geometry, n_feats = merge_range_geometry(geojson_path, presence)
    except ValueError as exc:
        print(f"  SKIP: {exc}", file=sys.stderr)
        return False
    print(f"  range: {n_feats} polygon feature(s) merged")
    print(f"  summary window {start} .. {end}; point window {points_start} .. {end}")

    # Summary (aggregate) query over the full window — reliable counts + hectares.
    summary_rows = run_query(build_summary_sql(start, end, confidence),
                             geometry, api_key, version, timeout)
    # Point query over the (shorter) point window — one feature per alert pixel for
    # the map. area__ha is aggregation-only so it isn't included per point. Fetched
    # with adaptive date-bisection to stay under the API's ~6 MB response cap.
    point_rows, truncated, skipped = fetch_points(geometry, api_key, points_start, end,
                                                  confidence, version, limit, timeout)
    print(f"  API returned {len(point_rows)} point row(s), "
          f"{len(summary_rows)} summary row(s)")
    if truncated:
        print(
            f"  WARNING: point query hit the row limit ({limit}); the GeoJSON is "
            "truncated (the summary CSV is unaffected). Raise --max-points or shorten "
            "--points-days.",
            file=sys.stderr,
        )
    if skipped:
        print(
            f"  WARNING: {len(skipped)} day(s) had too many alerts to return as points "
            f"({', '.join(skipped)}); they are still counted in the summary CSV. "
            "Use the async batch endpoint for per-point data on those days.",
            file=sys.stderr,
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    geojson_out = out_dir / f"{species}_integrated_alerts.geojson"
    csv_out = out_dir / f"{species}_integrated_alerts_summary.csv"

    fc = rows_to_geojson(point_rows, species)
    geojson_out.write_text(json.dumps(fc), encoding="utf-8")
    total_alerts, total_area = write_summary_csv(summary_rows, species, csv_out)

    print(f"  wrote {len(fc['features'])} point(s) -> data/deforestation/{geojson_out.name}")
    print(f"  wrote summary          -> data/deforestation/{csv_out.name}")
    print(f"  totals: {total_alerts} alerts, {total_area:.2f} ha")
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
    parser.add_argument("--points-days", type=int, default=60,
                        help="Point GeoJSON covers the last N days of the window "
                             "(default: 60). The summary CSV always covers the full "
                             "window. Use 0 to fetch points for the whole window.")
    parser.add_argument("--confidence", nargs="+", choices=VALID_CONFIDENCE, default=None,
                        help="Keep only these confidence levels (default: all).")
    parser.add_argument("--presence", type=int, nargs="+", default=[1],
                        help="IUCN PRESENCE codes to include (default: 1 = Extant). "
                             "Pass 0 to disable filtering.")
    parser.add_argument("--version", default="latest",
                        help="GFW dataset version (default: latest).")
    parser.add_argument("--max-points", type=int, default=100_000,
                        help="Row cap for the point query (default: 100000).")
    parser.add_argument("--timeout", type=int, default=120,
                        help="Per-request timeout in seconds (default: 120).")
    args = parser.parse_args()

    load_dotenv(REPO_ROOT)
    api_key = get_api_key()

    presence = None if args.presence == [0] else set(args.presence)
    targets = resolve_targets(args.species, args.ranges_dir)

    # Point window: last N days of the summary window (clamped to start-date).
    if args.points_days and args.points_days > 0:
        end_date = dt.date.fromisoformat(args.end_date)
        pstart = (end_date - dt.timedelta(days=args.points_days)).isoformat()
        points_start = max(pstart, args.start_date)
    else:
        points_start = args.start_date

    print(f"Dataset: {DATASET} (version: {args.version})")
    print(f"Processing {len(targets)} range file(s) into {args.out_dir}")

    ok = 0
    for path in targets:
        if process_species(
            path, args.out_dir, api_key,
            args.start_date, args.end_date, points_start, args.confidence,
            presence, args.version, args.max_points, args.timeout,
        ):
            ok += 1

    print(f"\nDone: {ok}/{len(targets)} range(s) processed.")
    if ok < len(targets):
        sys.exit(1)


if __name__ == "__main__":
    main()
