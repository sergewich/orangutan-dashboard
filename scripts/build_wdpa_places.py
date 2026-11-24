#!/usr/bin/env python3
"""
build_wdpa_places.py — build data/wdpa_places.json, a deduplicated reference
list of protected areas (by WDPA site_pid) confirmed to overlap each
orangutan species' range.

WDPA's terms forbid redistributing its data (see the "Protected areas (WDPA)
layer" note in CLAUDE.md) — so, like the live map tile layer, this script
never stores WDPA geometry or bulk attributes. It queries UNEP-WCMC's own
live ArcGIS service each run and writes out only a small cross-reference
(names + site_pid numbers + which species), the same kind of "which
real-world place is this" reference this project already keeps for NGOs
(data/ngos/ngos.json) — not a copy of the WDPA dataset itself.

Why dedup is needed: WDPA commonly carries more than one record for the same
physical place (e.g. Gunung Leuser is both a National Park record and a
separate UNESCO-MAB Biosphere Reserve record over the same ground) — see the
2026-11-24 investigation this script formalizes. This groups records by
normalized name per species and keeps one "representative" record per group
(the largest-area one) to use for later geometry/alert-count queries, while
still recording every underlying designation for transparency.

Method: for each species, bbox-filter candidate protected areas per range
feature (cheap, always-valid query geometry), then confirm real overlap by
sampling each candidate's vertices against the species' actual (unsimplified)
range polygon with a plain point-in-polygon test — no WDPA geometry is ever
written out, only used in memory to decide inclusion.

    python scripts/build_wdpa_places.py
    python scripts/build_wdpa_places.py --species pongo_abelii
"""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.request
import urllib.parse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SPECIES_LIST = ["pongo_abelii", "pongo_pygmaeus", "pongo_tapanuliensis"]
OUT_PATH = REPO_ROOT / "data" / "wdpa_places.json"

Q_URL = ("https://data-gis.unep-wcmc.org/server/rest/services/ProtectedSites/"
         "The_World_Database_of_Protected_Areas/MapServer/1/query")
OUT_FIELDS = "site_pid,name_eng,desig_eng,iucn_cat,status,gis_area"


def ring_points(geom_type: str, coords) -> list:
    polys = [coords] if geom_type == "Polygon" else coords
    for poly in polys:
        for ring in poly:
            yield ring


def species_rings(species: str) -> list[dict]:
    """All rings for a species, from the original (unsimplified) range file,
    each with a precomputed bbox for fast rejection in point_in_species."""
    d = json.loads((REPO_ROOT / "data" / "ranges" / f"{species}.geojson").read_text())
    rings = []
    for ft in d["features"]:
        g = ft.get("geometry")
        if not g:
            continue
        for ring in ring_points(g["type"], g["coordinates"]):
            xs = [p[0] for p in ring]
            ys = [p[1] for p in ring]
            rings.append({"pts": ring, "bbox": (min(xs), min(ys), max(xs), max(ys))})
    return rings


def feature_bboxes(species: str) -> list[tuple]:
    d = json.loads((REPO_ROOT / "data" / "ranges" / f"{species}.geojson").read_text())
    boxes = []
    for ft in d["features"]:
        g = ft.get("geometry")
        if not g:
            continue
        xs, ys = [], []
        for ring in ring_points(g["type"], g["coordinates"]):
            for lon, lat in ring:
                xs.append(lon)
                ys.append(lat)
        if xs:
            boxes.append((min(xs), min(ys), max(xs), max(ys)))
    return boxes


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


def point_in_species(x: float, y: float, rings: list[dict]) -> bool:
    count = 0
    for r in rings:
        xmin, ymin, xmax, ymax = r["bbox"]
        if xmin <= x <= xmax and ymin <= y <= ymax and point_in_ring(x, y, r["pts"]):
            count += 1
    return count % 2 == 1


def http_post(params: dict) -> dict:
    body = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(Q_URL, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


def query_bbox_candidates(bbox: tuple) -> dict:
    """outFields only, no geometry — cheap. Returns {site_pid: attrs}."""
    xmin, ymin, xmax, ymax = bbox
    geometry = json.dumps({"xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax,
                            "spatialReference": {"wkid": 4326}})
    ids: dict[str, dict] = {}
    offset = 0
    while True:
        params = {
            "f": "json", "geometry": geometry, "geometryType": "esriGeometryEnvelope",
            "inSR": "4326", "spatialRel": "esriSpatialRelIntersects",
            "outFields": OUT_FIELDS, "returnGeometry": "false",
            "resultOffset": str(offset), "resultRecordCount": "2000",
        }
        data = http_post(params)
        if "error" in data:
            raise RuntimeError(data["error"])
        feats = data.get("features", [])
        for f in feats:
            a = f["attributes"]
            ids[a["site_pid"]] = a
        if not data.get("exceededTransferLimit"):
            break
        offset += len(feats)
        time.sleep(0.2)
    return ids


def fetch_geometry_rings(site_pid: str) -> list:
    params = {
        "f": "json", "where": f"site_pid='{site_pid}'",
        "outFields": "site_pid", "returnGeometry": "true",
        "geometryPrecision": "6", "outSR": "4326",
    }
    data = http_post(params)
    rings = []
    for f in data.get("features", []):
        rings.extend(f.get("geometry", {}).get("rings", []))
    return rings


def confirm_overlap(candidates: dict, rings: list[dict]) -> dict:
    confirmed = {}
    for pid, attrs in candidates.items():
        geoms = fetch_geometry_rings(pid)
        hit = False
        for ring in geoms:
            step = max(1, len(ring) // 30)
            for (x, y) in ring[::step]:
                if point_in_species(x, y, rings):
                    hit = True
                    break
            if hit:
                break
        if hit:
            confirmed[pid] = attrs
        time.sleep(0.05)
    return confirmed


def normalize_name(name: str) -> str:
    n = (name or "").lower().strip()
    n = re.sub(r"\s+", " ", n)
    n = re.sub(r"\b(national park|nature reserve|wildlife reserve|forest reserve|"
               r"nature recreation park|grand forest park|hunting park|sanctuary|"
               r"marine protected area|biosphere reserve|world heritage site.*|"
               r"wetland.*ramsar.*|state park)\b", "", n)
    return re.sub(r"\s+", " ", n).strip(" -,")


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "place"


def build_places(species_filter: list[str]) -> dict:
    places_by_key: dict[tuple, dict] = {}
    for species in species_filter:
        rings = species_rings(species)
        candidates: dict[str, dict] = {}
        for bb in feature_bboxes(species):
            candidates.update(query_bbox_candidates(bb))
        confirmed = confirm_overlap(candidates, rings)
        print(f"{species}: {len(candidates)} bbox candidates -> {len(confirmed)} confirmed overlapping", flush=True)

        groups: dict[str, list[dict]] = {}
        for pid, a in confirmed.items():
            key = normalize_name(a.get("name_eng") or a.get("site_pid"))
            groups.setdefault(key, []).append(a)

        for key, recs in groups.items():
            recs.sort(key=lambda a: (a.get("gis_area") or 0), reverse=True)
            rep = recs[0]
            place_key = (species, key)
            places_by_key[place_key] = {
                "id": f"{species}-{slugify(key or rep['name_eng'])}",
                "name": rep.get("name_eng") or rep.get("name") or key,
                "species": species,
                "representative_site_pid": rep["site_pid"],
                "site_pids": sorted({r["site_pid"] for r in recs}),
                "designations": sorted({r.get("desig_eng") for r in recs if r.get("desig_eng")}),
                "iucn_cat": rep.get("iucn_cat"),
                "status": rep.get("status"),
            }

    places = [places_by_key[k] for k in sorted(places_by_key, key=lambda k: (k[0], k[1]))]
    return {
        "updated": time.strftime("%Y-%m-%d"),
        "note": ("Reference list of protected areas (WDPA site_pid numbers) confirmed to "
                 "overlap each orangutan species' range, deduplicated to one record per "
                 "physical place. No WDPA geometry or bulk attribute data is stored here "
                 "(see CLAUDE.md's WDPA licensing note) — this is a cross-reference for "
                 "querying UNEP-WCMC's and GFW's live services, same spirit as "
                 "data/ngos/ngos.json. Rebuild with scripts/build_wdpa_places.py."),
        "places": places,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--species", nargs="+", default=SPECIES_LIST, choices=SPECIES_LIST)
    args = p.parse_args()

    result = build_places(args.species)
    if set(args.species) == set(SPECIES_LIST):
        OUT_PATH.write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n")
        print(f"Wrote {len(result['places'])} places -> {OUT_PATH.relative_to(REPO_ROOT)}")
    else:
        print(json.dumps(result, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
