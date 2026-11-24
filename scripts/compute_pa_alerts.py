#!/usr/bin/env python3
"""
compute_pa_alerts.py — for each protected area in data/wdpa_places.json,
compute complete fire + deforestation alert counts for a given month.

Why this exists rather than just spatially joining the existing per-species
alert files: data/deforestation/*.geojson is deliberately capped at
--max-points rows (see fetch_deforestation.py and the 2026-11-24
investigation in CLAUDE.md) — for a high-volume region like Borneo during a
bad fire season, that point file can be missing most or all of a month's
alerts, even though the aggregate count is accurate. Spatially joining
*points* to protected areas would silently inherit that gap.

Instead, for deforestation this queries the GFW Data API directly, once per
protected area, for a COUNT aggregate clipped to that area's own (much
smaller than a species range) polygon — the same kind of query
fetch_deforestation.py's summary CSV already uses and which is NOT subject
to the point cap, so these counts are always complete regardless of volume.
Fire has no such cap (see the investigation above), so fire counts come from
a straightforward point-in-polygon join against the already-stored fire
GeoJSON (the archived month if it exists, else the live file).

Protected area geometry is fetched live from UNEP-WCMC per place and used
only in memory for this computation — never written out (same "don't
redistribute WDPA data" rule as the map's live tile layer).

    python scripts/compute_pa_alerts.py --month 2026-10
    python scripts/compute_pa_alerts.py --month 2026-10 --species pongo_tapanuliensis
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.request
import urllib.parse
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("This script needs 'requests'. Install with: pip install requests")

REPO_ROOT = Path(__file__).resolve().parent.parent
PLACES_PATH = REPO_ROOT / "data" / "wdpa_places.json"
FIRE_DIR = REPO_ROOT / "data" / "fire"
ARCHIVE_DIR = REPO_ROOT / "data" / "archive"

WDPA_Q_URL = ("https://data-gis.unep-wcmc.org/server/rest/services/ProtectedSites/"
              "The_World_Database_of_Protected_Areas/MapServer/1/query")

GFW_API_BASE = "https://data-api.globalforestwatch.org"
GFW_DATASET = "gfw_integrated_alerts"
GFW_USER_AGENT = "orangutan-dashboard/1.0 (protected-area alert counts)"
GFW_ORIGIN = "https://globalforestwatch.org"
SIMPLIFY_TOLERANCE_DEG = 0.0005  # ~55m, same as fetch_threats.py


# --------------------------------------------------------------------------- #
# Small helpers duplicated on purpose (see fetch_threats.py / fetch_fire.py —
# this project keeps each data script standalone rather than sharing a module)
# --------------------------------------------------------------------------- #
def load_dotenv() -> None:
    env = REPO_ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        k, _, v = line.strip().partition("=")
        if k and v and not k.startswith("#"):
            os.environ.setdefault(k, v.strip().strip('"').strip("'"))


def perp_dist(pt, a, b) -> float:
    (x, y), (ax, ay), (bx, by) = pt, a, b
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return ((x - ax) ** 2 + (y - ay) ** 2) ** 0.5
    t = ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)
    px, py = ax + t * dx, ay + t * dy
    return ((x - px) ** 2 + (y - py) ** 2) ** 0.5


def douglas_peucker(points: list, tol: float) -> list:
    if len(points) < 3:
        return points
    dmax, idx = 0.0, 0
    for i in range(1, len(points) - 1):
        d = perp_dist(points[i], points[0], points[-1])
        if d > dmax:
            dmax, idx = d, i
    if dmax > tol:
        left = douglas_peucker(points[:idx + 1], tol)
        right = douglas_peucker(points[idx:], tol)
        return left[:-1] + right
    return [points[0], points[-1]]


def point_in_ring(x: float, y: float, ring: list) -> bool:
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xint = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xint:
                inside = not inside
    return inside


def point_in_rings(x: float, y: float, rings_with_bbox: list[dict]) -> bool:
    count = 0
    for r in rings_with_bbox:
        xmin, ymin, xmax, ymax = r["bbox"]
        if xmin <= x <= xmax and ymin <= y <= ymax and point_in_ring(x, y, r["pts"]):
            count += 1
    return count % 2 == 1


# --------------------------------------------------------------------------- #
# WDPA geometry (live, in-memory only)
# --------------------------------------------------------------------------- #
def fetch_place_rings(site_pid: str) -> list:
    params = {
        "f": "json", "where": f"site_pid='{site_pid}'",
        "outFields": "site_pid", "returnGeometry": "true",
        "geometryPrecision": "6", "outSR": "4326",
    }
    body = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(WDPA_Q_URL, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())
    rings = []
    for f in data.get("features", []):
        rings.extend(f.get("geometry", {}).get("rings", []))
    return rings


def rings_with_bbox(rings: list) -> list[dict]:
    out = []
    for ring in rings:
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        out.append({"pts": ring, "bbox": (min(xs), min(ys), max(xs), max(ys))})
    return out


def rings_to_gfw_geometry(rings: list) -> dict:
    """Esri rings -> a GeoJSON geometry GFW's query API accepts, simplified
    to stay well under its request-size limits (these are single protected
    areas, not whole species ranges, so this is a formality, not usually
    load-bearing)."""
    simplified = []
    for ring in rings:
        simp = douglas_peucker([tuple(p) for p in ring], SIMPLIFY_TOLERANCE_DEG)
        if len(simp) >= 4:
            simplified.append([list(p) for p in simp])
    if len(simplified) == 1:
        return {"type": "Polygon", "coordinates": simplified}
    return {"type": "MultiPolygon", "coordinates": [[ring] for ring in simplified]}


# --------------------------------------------------------------------------- #
# GFW deforestation count (complete, not point-capped)
# --------------------------------------------------------------------------- #
def gfw_count(geometry: dict, start: str, end: str, api_key: str) -> dict:
    sql = (
        "SELECT gfw_integrated_alerts__confidence, count(*), sum(area__ha) AS area_ha "
        "FROM results "
        f"WHERE gfw_integrated_alerts__date >= '{start}' AND gfw_integrated_alerts__date <= '{end}' "
        "GROUP BY gfw_integrated_alerts__confidence"
    )
    url = f"{GFW_API_BASE}/dataset/{GFW_DATASET}/latest/query/json"
    headers = {"x-api-key": api_key, "Content-Type": "application/json",
               "User-Agent": GFW_USER_AGENT, "Origin": GFW_ORIGIN}
    for attempt in range(1, 4):
        resp = requests.post(url, data=json.dumps({"sql": sql, "geometry": geometry}),
                              headers=headers, timeout=60)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "success":
                rows = data.get("data", []) or []
                total = sum(r.get("count", 0) for r in rows)
                area = sum(r.get("area_ha") or 0 for r in rows)
                by_conf = {r["gfw_integrated_alerts__confidence"]: r.get("count", 0) for r in rows}
                return {"count": total, "area_ha": round(area, 4), "by_confidence": by_conf}
            raise RuntimeError(f"GFW query failed: {json.dumps(data)[:500]}")
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < 3:
            time.sleep(2 * attempt)
            continue
        raise RuntimeError(f"GFW API error {resp.status_code}: {resp.text[:300]}")
    raise RuntimeError("GFW query failed after retries")


# --------------------------------------------------------------------------- #
# Fire count (never capped — see fetch_fire.py, no row limit exists there)
# --------------------------------------------------------------------------- #
def fire_features_for_month(species: str, month: str) -> list[dict]:
    archived = ARCHIVE_DIR / "fire" / f"{species}_{month}.geojson"
    if archived.exists():
        fc = json.loads(archived.read_text())
        return fc.get("features", [])
    live = FIRE_DIR / f"{species}_fire_alerts.geojson"
    if not live.exists():
        return []
    fc = json.loads(live.read_text())
    return [f for f in fc.get("features", [])
            if (f.get("properties") or {}).get("date_acq", "")[:7] == month]


def count_fire_in_place(fire_feats: list[dict], place_rings: list[dict]) -> int:
    n = 0
    for f in fire_feats:
        lon, lat = f["geometry"]["coordinates"]
        if point_in_rings(lon, lat, place_rings):
            n += 1
    return n


# --------------------------------------------------------------------------- #
def prev_month(today: dt.date) -> str:
    first = today.replace(day=1)
    last_of_prev = first - dt.timedelta(days=1)
    return last_of_prev.strftime("%Y-%m")


def month_bounds(month: str) -> tuple[str, str]:
    year, mon = (int(x) for x in month.split("-"))
    start = dt.date(year, mon, 1)
    end = (dt.date(year + (mon == 12), mon % 12 + 1, 1) - dt.timedelta(days=1))
    return start.isoformat(), end.isoformat()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--month", default=None, help="YYYY-MM (default: last completed calendar month)")
    p.add_argument("--species", nargs="+", default=None,
                   choices=["pongo_abelii", "pongo_pygmaeus", "pongo_tapanuliensis"])
    args = p.parse_args()

    load_dotenv()
    gfw_key = os.environ.get("GFW_API_KEY", "").strip()
    if not gfw_key:
        sys.exit("GFW_API_KEY not set (.env or environment).")

    month = args.month or prev_month(dt.date.today())
    start, end = month_bounds(month)

    places = json.loads(PLACES_PATH.read_text())["places"]
    if args.species:
        places = [pl for pl in places if pl["species"] in args.species]

    fire_cache: dict[str, list[dict]] = {}
    results = []
    for i, place in enumerate(places, 1):
        species = place["species"]
        if species not in fire_cache:
            fire_cache[species] = fire_features_for_month(species, month)

        print(f"[{i}/{len(places)}] {place['name']} ({species})...", file=sys.stderr)
        rings = fetch_place_rings(place["representative_site_pid"])
        if not rings:
            print(f"  WARNING: no geometry returned for site_pid {place['representative_site_pid']}, skipping",
                  file=sys.stderr)
            continue
        rwb = rings_with_bbox(rings)

        fire_n = count_fire_in_place(fire_cache[species], rwb)

        try:
            gfw_geom = rings_to_gfw_geometry(rings)
            defor = gfw_count(gfw_geom, start, end, gfw_key)
        except RuntimeError as exc:
            print(f"  WARNING: GFW query failed for {place['name']}: {exc}", file=sys.stderr)
            defor = None
        time.sleep(0.15)

        results.append({
            "id": place["id"], "name": place["name"], "species": species,
            "designations": place["designations"], "iucn_cat": place.get("iucn_cat"),
            "range_categories": place.get("range_categories", []),
            "fire_count": fire_n,
            "deforestation_count": defor["count"] if defor else None,
            "deforestation_area_ha": defor["area_ha"] if defor else None,
            "deforestation_by_confidence": defor["by_confidence"] if defor else None,
        })

    out_dir = ARCHIVE_DIR / "pa_alerts"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{month}.json"
    out_path.write_text(json.dumps({"month": month, "generated": dt.date.today().isoformat(),
                                     "places": results}, indent=1, ensure_ascii=False) + "\n")
    print(f"Wrote {len(results)} place(s) -> {out_path.relative_to(REPO_ROOT)}")

    if not args.species:  # only a full (all-species) run counts as a real snapshot for the manifest
        manifest_path = ARCHIVE_DIR / "manifest.json"
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"fire": {}, "deforestation": {}}
        manifest.setdefault("pa_alerts", [])
        if month not in manifest["pa_alerts"]:
            manifest["pa_alerts"].append(month)
            manifest["pa_alerts"].sort()
        manifest_path.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
