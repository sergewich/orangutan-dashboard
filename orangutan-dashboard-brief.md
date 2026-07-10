# Orangutan Conservation Dashboard — Project Brief

## Purpose
A single website consolidating: (1) species range maps + deforestation + fire alerts,
(2) literature (open + paywalled links), (3) company/threat data in range areas,
(4) social media / news monitoring on orangutan-related incidents (killings, translocations, etc).

## Architecture
- Static frontend (Leaflet.js or MapLibre GL JS for maps; plain HTML/JS otherwise), hosted on
  GitHub Pages or Cloudflare Pages.
- Data refresh via scheduled GitHub Actions (cron), NOT a live backend server.
- Processed data lives as versioned files in the repo (GeoJSON/CSV/PNG), frontend just loads them.
- No GPU or heavy compute needed anywhere in this pipeline.

## Repo structure (proposed)
```
orangutan-dashboard/
  data/
    ranges/            # converted GeoJSON from your shapefiles, one per species/subspecies
    deforestation/      # monthly GFW integrated alert pulls, clipped to ranges
    fire/                # NASA FIRMS pulls, clipped to ranges
    literature/          # curated + auto-pulled paper metadata (OpenAlex/Crossref/Unpaywall)
    threats/             # concession/company polygons + sources (human-reviewed)
    social/              # news/GDELT monitoring output (human-reviewed before publish)
  scripts/
    fetch_deforestation.py
    fetch_fire.py
    fetch_literature.py
    generate_monthly_charts.py
    convert_ranges.py     # one-time: shapefile zips -> GeoJSON
  site/
    index.html
    tabs/
      map.html
      literature.html
      threats.html
      social.html
    js/ css/
  .github/workflows/
    monthly_refresh.yml
    daily_fire_refresh.yml
  README.md
```

## Phase 1 (proof of concept) — build this first
1. `convert_ranges.py`: unzip and convert your range shapefiles to GeoJSON (ogr2ogr).
2. `fetch_deforestation.py`: pull GFW integrated disturbance alerts via GFW Data API,
   clipped to each species range polygon, save as GeoJSON + summary CSV.
3. `fetch_fire.py`: pull NASA FIRMS active fire data (VIIRS/MODIS) via FIRMS API,
   clipped to ranges, save as GeoJSON + summary CSV.
4. `generate_monthly_charts.py`: matplotlib/plotly charts per species (alert counts,
   hectares lost, trend vs. prior months).
5. `map.html`: Leaflet/MapLibre map with layer toggles — range boundaries, deforestation
   alerts, fire alerts, OSM basemap, Esri satellite basemap.
6. `.github/workflows/monthly_refresh.yml`: runs steps 2–4 on a monthly schedule.
   `daily_fire_refresh.yml`: runs step 3 daily (fires are more time-sensitive).

## Later phases
- Literature tab: OpenAlex/Crossref pull + Unpaywall open-access flag, rendered as filterable table.
- Threats tab: concession/company polygons from GFW commodities data — flag for human review
  before publishing anything company-specific.
- Social tab: GDELT/RSS-based news monitoring (avoid paid X/Twitter API initially) — flag for
  human review before publishing incident claims.

## API keys needed
- GFW Data API key (free, register at globalforestwatch.org)
- NASA FIRMS API key (free, register at firms.modis.gov)
- No key needed for OpenAlex/Crossref; Unpaywall needs an email param only.

## Notes for Claude Code
- Use GitHub Actions secrets for API keys, never commit them.
- Keep each script runnable standalone (`python scripts/fetch_fire.py`) for local testing
  before wiring into Actions.
- Start with one orangutan species range as a test case before scaling to all three
  (P. abelii, P. pygmaeus, P. tapanuliensis).
