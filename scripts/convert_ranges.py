#!/usr/bin/env python3
"""
convert_ranges.py — one-time step: shapefile zips -> GeoJSON.

Looks in data/ranges/raw/ for range shapefiles (either zipped, e.g.
"pongo_abelii.zip" containing .shp/.shx/.dbf/.prj, or loose .shp sets already
unzipped there) and writes one WGS84 (EPSG:4326) GeoJSON FeatureCollection per
input into data/ranges/, named after the source file
(e.g. data/ranges/pongo_abelii.geojson).

Conversion strategy (in order of preference):
  1. ogr2ogr, if installed on the system — most robust, handles any CRS/
     encoding edge case. This is what the project brief assumes.
  2. Pure-Python fallback (pyshp + pyproj) — used automatically if ogr2ogr
     isn't on PATH, so this script also runs on machines without GDAL
     installed. Reprojects to EPSG:4326 using the shapefile's .prj if present.

Usage:
    python scripts/convert_ranges.py
    python scripts/convert_ranges.py --raw-dir data/ranges/raw --out-dir data/ranges

Drop your range shapefile zip(s) into data/ranges/raw/ and run this script.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_RAW_DIR = REPO_ROOT / "data" / "ranges" / "raw"
DEFAULT_OUT_DIR = REPO_ROOT / "data" / "ranges"


def find_ogr2ogr() -> str | None:
    return shutil.which("ogr2ogr")


def extract_zips_to_tmp(raw_dir: Path, tmp_dir: Path) -> list[Path]:
    """Unzip every .zip in raw_dir into its own subfolder of tmp_dir.
    Returns list of directories that may contain .shp files to process
    (the extracted subfolders, plus raw_dir itself for any loose .shp sets)."""
    search_dirs = [raw_dir]
    for zip_path in sorted(raw_dir.glob("*.zip")):
        dest = tmp_dir / zip_path.stem
        dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(dest)
        print(f"  unzipped {zip_path.name} -> {dest}")
        search_dirs.append(dest)
    return search_dirs


def find_shapefiles(dirs: list[Path]) -> list[Path]:
    shp_files: list[Path] = []
    seen = set()
    for d in dirs:
        for shp in sorted(d.rglob("*.shp")):
            if shp not in seen:
                shp_files.append(shp)
                seen.add(shp)
    return shp_files


def guess_output_name(shp_path: Path) -> str:
    """Prefer the species name from the attribute table (e.g. IUCN Red List
    shapefiles are all literally named data_0.shp, so the filename is
    useless for distinguishing species) falling back to the file stem."""
    try:
        import shapefile  # pyshp

        reader = shapefile.Reader(str(shp_path))
        fields = [f[0] for f in reader.fields[1:]]
        name_field = next(
            (f for f in fields if f.upper() in ("SCI_NAME", "SCIENTIFIC", "BINOMIAL", "SPECIES")),
            None,
        )
        if name_field:
            rec = dict(zip(fields, reader.record(0)))
            value = str(rec[name_field]).strip()
            if value:
                return value.lower().replace(" ", "_").replace(".", "") + ".geojson"
    except Exception:
        pass
    return shp_path.stem.lower().replace(" ", "_") + ".geojson"


def convert_with_ogr2ogr(ogr2ogr: str, shp_path: Path, out_path: Path) -> None:
    cmd = [
        ogr2ogr,
        "-f", "GeoJSON",
        "-t_srs", "EPSG:4326",
        str(out_path),
        str(shp_path),
    ]
    subprocess.run(cmd, check=True)


def convert_with_pure_python(shp_path: Path, out_path: Path) -> None:
    try:
        import shapefile  # pyshp
    except ImportError:
        sys.exit(
            "ogr2ogr not found and 'pyshp' isn't installed.\n"
            "Install fallback dependencies with:\n"
            "  pip install --break-system-packages pyshp pyproj\n"
            "or install GDAL (ogr2ogr) instead."
        )

    transformer = None
    prj_path = shp_path.with_suffix(".prj")
    if prj_path.exists():
        try:
            from pyproj import CRS, Transformer

            src_crs = CRS.from_wkt(prj_path.read_text())
            if not src_crs.equals(CRS.from_epsg(4326)):
                transformer = Transformer.from_crs(src_crs, CRS.from_epsg(4326), always_xy=True)
        except ImportError:
            print(
                f"  WARNING: {prj_path.name} found but 'pyproj' isn't installed, "
                "so coordinates will be written as-is (NOT reprojected to EPSG:4326).\n"
                "  Install it with: pip install --break-system-packages pyproj",
                file=sys.stderr,
            )
        except Exception as exc:  # malformed .prj, unsupported CRS, etc.
            print(f"  WARNING: could not parse {prj_path.name} ({exc}); coordinates left as-is.",
                  file=sys.stderr)
    else:
        print(f"  WARNING: no .prj alongside {shp_path.name}; assuming it's already EPSG:4326.",
              file=sys.stderr)

    def reproject(coords):
        # Recursively walk nested coordinate lists (points / lines / polygons / multi-*)
        if isinstance(coords[0], (int, float)):
            x, y = transformer.transform(coords[0], coords[1])
            return [x, y]
        return [reproject(c) for c in coords]

    reader = shapefile.Reader(str(shp_path))
    fields = [f[0] for f in reader.fields[1:]]  # skip deletion flag field
    features = []
    for sr in reader.iterShapeRecords():
        geom = sr.shape.__geo_interface__
        if transformer is not None and geom.get("coordinates"):
            geom = dict(geom)
            geom["coordinates"] = reproject(geom["coordinates"])
        props = dict(zip(fields, sr.record))
        features.append({"type": "Feature", "geometry": geom, "properties": props})

    fc = {"type": "FeatureCollection", "features": features}
    out_path.write_text(json.dumps(fc))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR,
                         help="Folder containing shapefile zips / loose .shp sets "
                              f"(default: {DEFAULT_RAW_DIR})")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                         help=f"Folder to write GeoJSON output (default: {DEFAULT_OUT_DIR})")
    args = parser.parse_args()

    raw_dir: Path = args.raw_dir
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    if not raw_dir.exists() or not any(raw_dir.iterdir()):
        sys.exit(
            f"No input found in {raw_dir}.\n"
            "Drop your range shapefile(s) there — either as .zip archives "
            "(containing .shp/.shx/.dbf/.prj) or as a loose set of those files — "
            "then re-run this script."
        )

    ogr2ogr = find_ogr2ogr()
    print(f"Conversion backend: {'ogr2ogr (' + ogr2ogr + ')' if ogr2ogr else 'pure-Python fallback (pyshp + pyproj)'}")

    converted = 0
    with tempfile.TemporaryDirectory(prefix="convert_ranges_") as tmp:
        tmp_dir = Path(tmp)
        print(f"Scanning {raw_dir} ...")
        search_dirs = extract_zips_to_tmp(raw_dir, tmp_dir)
        shp_files = find_shapefiles(search_dirs)

        if not shp_files:
            sys.exit(f"No .shp files found in or under {raw_dir}.")

        for shp_path in shp_files:
            out_name = guess_output_name(shp_path)
            out_path = out_dir / out_name
            if out_path.exists():
                # avoid clobbering a distinct file that happened to guess the same name
                n = 2
                stem, suffix = out_name[:-len(".geojson")], ".geojson"
                while out_path.exists():
                    out_path = out_dir / f"{stem}_{n}{suffix}"
                    n += 1
            print(f"Converting {shp_path.name} -> data/ranges/{out_name}")
            try:
                if ogr2ogr:
                    convert_with_ogr2ogr(ogr2ogr, shp_path, out_path)
                else:
                    convert_with_pure_python(shp_path, out_path)
                converted += 1
            except Exception as exc:
                print(f"  FAILED: {exc}", file=sys.stderr)

    print(f"\nDone: {converted}/{len(shp_files)} shapefile(s) converted into {out_dir}")
    if converted < len(shp_files):
        sys.exit(1)


if __name__ == "__main__":
    main()
