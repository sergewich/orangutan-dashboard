#!/usr/bin/env python3
"""
fetch_threats.py — pull spatially explicit oil palm concessions, mining
concessions, and hydroelectric plant locations covering the combined
orangutan range area, and save as GeoJSON for the map's Threats layers.

Sources:
  - Oil palm concessions:  GFW Data API `gfw_oil_palm` (v2025)
  - Mining concessions:    GFW Data API `gfw_mining_concessions` (v2025)
    Both need GFW_API_KEY (same key already used by fetch_deforestation.py).
    Real, source-attributed concession polygons (company, source agency,
    source year) — not derived/inferred, straight from the dataset.
  - Hydroelectric plants:  OpenStreetMap Overpass API (power=plant,
    plant:source=hydro), keyless, point locations with names.
    Checked first: GFW's `intl_rivers_dam_hotspots` dataset — it only
    covers ~40 major global river basins (Amazon, Nile, Mekong, ...), none
    in Sumatra/Borneo, so it's not used here.

Queries a single bounding box padded around the union of all three range
files (not per-species) since concession/plant coverage isn't naturally
species-partitioned the way alerts are. Threats near a range boundary are
relevant even if just outside the strict polygon, hence the padding.

Usage:
    python scripts/fetch_threats.py
    python scripts/fetch_threats.py --pad-degrees 0.5
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit(
        "This script needs the 'requests' library.\n"
        "Install it with:  pip install requests"
    )

SIMPLIFY_TOLERANCE_DEG = 0.0005  # ~55m — plenty for a country-scale overview map


def _perpendicular_distance(pt, start, end) -> float:
    if start == end:
        return ((pt[0] - start[0]) ** 2 + (pt[1] - start[1]) ** 2) ** 0.5
    x1, y1 = start
    x2, y2 = end
    x0, y0 = pt
    num = abs((y2 - y1) * x0 - (x2 - x1) * y0 + x2 * y1 - y2 * x1)
    den = ((y2 - y1) ** 2 + (x2 - x1) ** 2) ** 0.5
    return num / den


def douglas_peucker(points: list, tolerance: float) -> list:
    """Pure-Python Douglas-Peucker line simplification — no new dependency
    (shapely) for what's otherwise a `requests`-only, standalone-runnable
    script, consistent with the rest of scripts/."""
    if len(points) < 3:
        return points
    start, end = points[0], points[-1]
    max_dist = 0.0
    max_idx = 0
    for i in range(1, len(points) - 1):
        d = _perpendicular_distance(points[i], start, end)
        if d > max_dist:
            max_dist, max_idx = d, i
    if max_dist <= tolerance:
        return [start, end]
    left = douglas_peucker(points[:max_idx + 1], tolerance)
    right = douglas_peucker(points[max_idx:], tolerance)
    return left[:-1] + right


def simplify_geometry(geom: dict, tolerance: float = SIMPLIFY_TOLERANCE_DEG) -> dict:
    """Simplify each ring of a Polygon/MultiPolygon; always keeps >=4 points
    per ring (a valid closed ring needs at least that many)."""
    def simplify_ring(ring):
        if len(ring) <= 4:
            return ring
        simplified = douglas_peucker([tuple(p) for p in ring], tolerance)
        if len(simplified) < 4:
            return ring
        if simplified[0] != simplified[-1]:
            simplified.append(simplified[0])
        return [list(p) for p in simplified]

    if geom["type"] == "Polygon":
        geom["coordinates"] = [simplify_ring(r) for r in geom["coordinates"]]
    elif geom["type"] == "MultiPolygon":
        geom["coordinates"] = [[simplify_ring(r) for r in poly] for poly in geom["coordinates"]]
    return geom

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_RANGES_DIR = REPO_ROOT / "data" / "ranges"
DEFAULT_OUT_DIR = REPO_ROOT / "data" / "threats"

GFW_API_BASE = "https://data-api.globalforestwatch.org"
GFW_USER_AGENT = "orangutan-dashboard/1.0 (threats fetcher)"
GFW_ORIGIN = "https://globalforestwatch.org"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"


# --------------------------------------------------------------------------- #
# Environment / API key
# --------------------------------------------------------------------------- #
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


def get_gfw_api_key() -> str:
    key = os.environ.get("GFW_API_KEY", "").strip()
    if not key:
        sys.exit(
            "GFW_API_KEY not set.\nAdd it to a .env file at the repo root:\n"
            "  GFW_API_KEY=your-key-here\n"
            "Register for a free key at https://www.globalforestwatch.org/ (Data API)."
        )
    return key


# --------------------------------------------------------------------------- #
# Combined, padded bounding box across all range files
# --------------------------------------------------------------------------- #
def combined_padded_bbox(ranges_dir: Path, pad_degrees: float) -> tuple[float, float, float, float]:
    lons: list = []
    lats: list = []
    for path in sorted(ranges_dir.glob("*.geojson")):
        fc = json.loads(path.read_text(encoding="utf-8"))
        for feat in fc.get("features", []):
            geom = feat.get("geometry") or {}
            polys = geom.get("coordinates")
            if not polys:
                continue
            rings = polys if geom.get("type") == "MultiPolygon" else [polys]
            for poly in rings:
                for ring in poly:
                    for lon, lat in ring:
                        lons.append(lon)
                        lats.append(lat)
    if not lons:
        sys.exit(f"No range geometry found in {ranges_dir}. Run convert_ranges.py first.")
    return (min(lons) - pad_degrees, min(lats) - pad_degrees,
            max(lons) + pad_degrees, max(lats) + pad_degrees)


# --------------------------------------------------------------------------- #
# GFW concession datasets (oil palm, mining)
# --------------------------------------------------------------------------- #
# Schemas aren't identical between the two datasets — comp_group only
# exists on gfw_oil_palm, mineral only on gfw_mining_concessions.
GFW_CONCESSION_COLUMNS = {
    "gfw_oil_palm": ["conc_type", "conc_name", "company", "comp_group",
                      "source", "source_yr", "gfw_area__ha", "gfw_geojson"],
    "gfw_mining_concessions": ["conc_type", "conc_name", "company", "mineral",
                                "source", "source_yr", "gfw_area__ha", "gfw_geojson"],
}


def fetch_gfw_concessions(dataset: str, version: str, bbox: tuple, api_key: str) -> dict:
    w, s, e, n = bbox
    bbox_geom = {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}
    url = f"{GFW_API_BASE}/dataset/{dataset}/{version}/query/json"
    headers = {
        "x-api-key": api_key,
        "Content-Type": "application/json",
        "User-Agent": GFW_USER_AGENT,
        "Origin": GFW_ORIGIN,
    }
    # Explicit column list, NOT SELECT * — pulling the raw geom/geom_wm EWKB
    # columns causes a server-side 500 for gfw_oil_palm's more complex
    # polygons (gfw_mining_concessions tolerates SELECT * fine, oil palm
    # doesn't — asymmetric, but the explicit list sidesteps it for both).
    columns = GFW_CONCESSION_COLUMNS[dataset]
    body = {"sql": f"SELECT {', '.join(columns)} FROM data", "geometry": bbox_geom}
    try:
        resp = requests.post(url, json=body, headers=headers, timeout=60)
    except requests.RequestException as exc:
        print(f"  WARNING: could not reach GFW API for {dataset}: {exc}", file=sys.stderr)
        return {"type": "FeatureCollection", "features": []}

    if resp.status_code != 200:
        print(f"  WARNING: GFW API error {resp.status_code} for {dataset}: {resp.text[:300]}",
              file=sys.stderr)
        return {"type": "FeatureCollection", "features": []}

    rows = resp.json().get("data", []) or []
    features = []
    for r in rows:
        geom_raw = r.pop("gfw_geojson", None)
        if not geom_raw:
            continue
        geom = json.loads(geom_raw) if isinstance(geom_raw, str) else geom_raw
        geom = simplify_geometry(geom)
        properties = {k: v for k, v in r.items() if v not in (None, "")}
        features.append({"type": "Feature", "geometry": geom, "properties": properties})
    return {"type": "FeatureCollection", "features": features}


# --------------------------------------------------------------------------- #
# OpenStreetMap Overpass — hydroelectric plants
# --------------------------------------------------------------------------- #
def fetch_osm_hydro(bbox: tuple) -> dict:
    w, s, e, n = bbox
    # Overpass bbox order is (south, west, north, east).
    query = f"""
    [out:json][timeout:60];
    (
      node["power"="plant"]["plant:source"="hydro"]({s},{w},{n},{e});
      way["power"="plant"]["plant:source"="hydro"]({s},{w},{n},{e});
      relation["power"="plant"]["plant:source"="hydro"]({s},{w},{n},{e});
    );
    out center tags;
    """
    # The public overpass-api.de instance is occasionally overloaded (504) —
    # worth a couple of retries before giving up.
    resp = None
    for attempt in range(3):
        try:
            resp = requests.post(
                OVERPASS_URL, data={"data": query},
                headers={"User-Agent": GFW_USER_AGENT},  # Overpass 406s requests with no real UA
                timeout=90,
            )
        except requests.RequestException as exc:
            print(f"  WARNING: could not reach Overpass API (attempt {attempt + 1}): {exc}", file=sys.stderr)
            continue
        if resp.status_code == 200:
            break
        print(f"  WARNING: Overpass API error {resp.status_code} (attempt {attempt + 1}), retrying...",
              file=sys.stderr)
        time.sleep(5)

    if resp is None or resp.status_code != 200:
        print("  WARNING: Overpass API still failing after retries, giving up.", file=sys.stderr)
        return {"type": "FeatureCollection", "features": []}

    elements = resp.json().get("elements", []) or []
    features = []
    for el in elements:
        if el.get("type") == "node":
            lon, lat = el.get("lon"), el.get("lat")
        else:
            center = el.get("center") or {}
            lon, lat = center.get("lon"), center.get("lat")
        if lon is None or lat is None:
            continue
        tags = el.get("tags", {})
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "name": tags.get("name") or "Unnamed hydroelectric plant",
                "operator": tags.get("operator"),
                "output_mw": tags.get("plant:output:electricity"),
                "osm_type": el.get("type"),
                "osm_id": el.get("id"),
                "data_source": "OpenStreetMap",
            },
        })
    return {"type": "FeatureCollection", "features": features}


# --------------------------------------------------------------------------- #
# Wikidata — hydroelectric plants OSM is missing (e.g. under-construction
# projects like Batang Toru/NSHE, which as of this writing has zero OSM
# coverage at all despite being one of the most significant threats to
# Pongo tapanuliensis specifically — it sits inside/adjacent to the range).
# --------------------------------------------------------------------------- #
WIKIDATA_SPARQL_URL = "https://query.wikidata.org/sparql"
WIKIDATA_HYDRO_CLASS = "wd:Q15911738"  # "hydroelectric power station"


def fetch_wikidata_hydro(bbox: tuple) -> dict:
    w, s, e, n = bbox
    # NOTE: the wikibase:box SERVICE clause must come before the class-filter
    # triple, or the query silently returns zero results (Blazegraph query
    # planner quirk — confirmed empirically, not documented anywhere obvious).
    query = f"""
    SELECT ?item ?itemLabel ?coord ?capacity ?operatorLabel WHERE {{
      SERVICE wikibase:box {{
        ?item wdt:P625 ?coord .
        bd:serviceParam wikibase:cornerWest "Point({w} {s})"^^geo:wktLiteral .
        bd:serviceParam wikibase:cornerEast "Point({e} {n})"^^geo:wktLiteral .
      }}
      ?item wdt:P31/wdt:P279* {WIKIDATA_HYDRO_CLASS} .
      OPTIONAL {{ ?item wdt:P2109 ?capacity }}
      OPTIONAL {{ ?item wdt:P127 ?operator }}
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
    }}
    """
    try:
        resp = requests.get(
            WIKIDATA_SPARQL_URL,
            params={"query": query},
            headers={"User-Agent": GFW_USER_AGENT, "Accept": "application/sparql-results+json"},
            timeout=60,
        )
    except requests.RequestException as exc:
        print(f"  WARNING: could not reach Wikidata: {exc}", file=sys.stderr)
        return {"type": "FeatureCollection", "features": []}

    if resp.status_code != 200:
        print(f"  WARNING: Wikidata query error {resp.status_code}: {resp.text[:300]}", file=sys.stderr)
        return {"type": "FeatureCollection", "features": []}

    bindings = resp.json().get("results", {}).get("bindings", [])
    point_re = re.compile(r"Point\(([-\d.]+) ([-\d.]+)\)")
    features = []
    for b in bindings:
        m = point_re.match(b["coord"]["value"])
        if not m:
            continue
        lon, lat = float(m.group(1)), float(m.group(2))
        label = b.get("itemLabel", {}).get("value")
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "name": label or b["item"]["value"].rsplit("/", 1)[-1],
                "operator": b.get("operatorLabel", {}).get("value"),
                "output_mw": b.get("capacity", {}).get("value"),
                "wikidata_id": b["item"]["value"].rsplit("/", 1)[-1],
                "data_source": "Wikidata",
            },
        })
    return {"type": "FeatureCollection", "features": features}


def merge_hydro_sources(osm: dict, wikidata: dict, dedup_degrees: float = 0.02) -> dict:
    """Combine OSM + Wikidata hydro points, dropping Wikidata entries that
    are near-duplicates of an existing OSM point (~2.2km at this latitude)
    rather than showing the same plant twice under two source labels."""
    osm_features = osm["features"]
    osm_coords = [f["geometry"]["coordinates"] for f in osm_features]

    added = 0
    for wf in wikidata["features"]:
        wlon, wlat = wf["geometry"]["coordinates"]
        is_dup = any(
            abs(wlon - olon) < dedup_degrees and abs(wlat - olat) < dedup_degrees
            for olon, olat in osm_coords
        )
        if not is_dup:
            osm_features.append(wf)
            added += 1
    print(f"  +{added} additional plant(s) from Wikidata not already in OSM "
          f"({len(wikidata['features']) - added} were duplicates)")
    return {"type": "FeatureCollection", "features": osm_features}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ranges-dir", type=Path, default=DEFAULT_RANGES_DIR)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--pad-degrees", type=float, default=0.4,
                        help="Padding around the combined range bbox, in degrees "
                             "(~44km at the equator; default: 0.4). Threats just outside "
                             "the strict range boundary are still relevant pressure.")
    args = parser.parse_args()

    load_dotenv(REPO_ROOT)
    api_key = get_gfw_api_key()

    bbox = combined_padded_bbox(args.ranges_dir, args.pad_degrees)
    print(f"Combined padded bbox (west,south,east,north): {bbox}")

    args.out_dir.mkdir(parents=True, exist_ok=True)

    print("\nFetching oil palm concessions (GFW)...")
    oil_palm = fetch_gfw_concessions("gfw_oil_palm", "v2025", bbox, api_key)
    (args.out_dir / "oil_palm.geojson").write_text(json.dumps(oil_palm), encoding="utf-8")
    print(f"  {len(oil_palm['features'])} concession(s)")

    print("\nFetching mining concessions (GFW)...")
    mining = fetch_gfw_concessions("gfw_mining_concessions", "v2025", bbox, api_key)
    (args.out_dir / "mining_concessions.geojson").write_text(json.dumps(mining), encoding="utf-8")
    print(f"  {len(mining['features'])} concession(s)")

    print("\nFetching hydroelectric plants (OpenStreetMap)...")
    hydro_osm = fetch_osm_hydro(bbox)
    print(f"  {len(hydro_osm['features'])} plant(s)")

    print("\nFetching hydroelectric plants (Wikidata, fills OSM gaps e.g. Batang Toru/NSHE)...")
    hydro_wikidata = fetch_wikidata_hydro(bbox)
    print(f"  {len(hydro_wikidata['features'])} plant(s)")

    hydro = merge_hydro_sources(hydro_osm, hydro_wikidata)
    (args.out_dir / "hydro_plants.geojson").write_text(json.dumps(hydro), encoding="utf-8")
    print(f"  {len(hydro['features'])} plant(s) total after merge")

    print(f"\nWrote 3 file(s) -> {args.out_dir.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
