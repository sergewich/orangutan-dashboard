#!/usr/bin/env python3
"""
simplify_ranges.py — one-time pass to shrink data/ranges/*.geojson for map
display. The IUCN range polygons convert_ranges.py produces keep full
shapefile precision, which is massively more detail than a web map outline
needs — pongo_pygmaeus.geojson was 39MB, far too heavy to fetch/render in a
browser. This simplifies in place; the original full-precision shapefiles
are untouched in data/ranges/raw/, so nothing authoritative is lost.

Usage:
    python scripts/simplify_ranges.py
    python scripts/simplify_ranges.py --tolerance 0.001
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_RANGES_DIR = REPO_ROOT / "data" / "ranges"
DEFAULT_TOLERANCE_DEG = 0.0015  # ~165m — visually fine for a range-outline overview map


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


COORD_PRECISION = 5  # ~1.1m — the source shapefiles carry 15-16 significant
# digits (sub-millimeter), absurd overkill for a species-range overview map.
# Rounding every coordinate matters as much as ring simplification here:
# pongo_pygmaeus's range is 6,533 separate ring fragments (heavily
# fragmented Bornean habitat), most already small (median 25 points), so
# per-ring point-count reduction alone has limited room — precision is
# repeated in every single coordinate regardless of ring size.


def simplify_geometry(geom: dict, tolerance: float) -> dict:
    def round_point(p):
        return [round(p[0], COORD_PRECISION), round(p[1], COORD_PRECISION)]

    def simplify_ring(ring):
        if len(ring) <= 4:
            return [round_point(p) for p in ring]
        simplified = douglas_peucker([tuple(p) for p in ring], tolerance)
        if len(simplified) < 4:
            return [round_point(p) for p in ring]
        if simplified[0] != simplified[-1]:
            simplified.append(simplified[0])
        return [round_point(p) for p in simplified]

    if geom["type"] == "Polygon":
        geom["coordinates"] = [simplify_ring(r) for r in geom["coordinates"]]
    elif geom["type"] == "MultiPolygon":
        geom["coordinates"] = [[simplify_ring(r) for r in poly] for poly in geom["coordinates"]]
    return geom


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ranges-dir", type=Path, default=DEFAULT_RANGES_DIR)
    parser.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE_DEG)
    args = parser.parse_args()

    for path in sorted(args.ranges_dir.glob("*.geojson")):
        before = path.stat().st_size
        fc = json.loads(path.read_text(encoding="utf-8"))
        for feat in fc.get("features", []):
            geom = feat.get("geometry")
            if geom and geom.get("type") in ("Polygon", "MultiPolygon"):
                feat["geometry"] = simplify_geometry(geom, args.tolerance)
        path.write_text(json.dumps(fc), encoding="utf-8")
        after = path.stat().st_size
        print(f"{path.name}: {before/1e6:.2f} MB -> {after/1e6:.2f} MB "
              f"({100 * (1 - after/before):.0f}% smaller)")


if __name__ == "__main__":
    main()
