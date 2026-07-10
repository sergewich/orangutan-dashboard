# Orangutan Conservation Dashboard — Local Agent Instructions

## Project identity
A static website consolidating conservation data for the three orangutan
species (*Pongo abelii*, *P. pygmaeus*, *P. tapanuliensis*): range maps,
near-real-time deforestation and fire alerts, literature, threat/company
data, and social/news incident monitoring. No live backend — data is
pre-processed into versioned GeoJSON/CSV files in `data/`, refreshed on a
schedule (GitHub Actions, not yet wired up), and the frontend just loads
them. Full design doc: `orangutan-dashboard-brief.md`.

## Current state of the repo (as of 2026-07-10)

```
Orangutan_dashboard/
  Modelfile                    # local Ollama model definition (see below)
  orangutan-dashboard-brief.md # full project brief / target architecture
  .env / .env.example          # GFW_API_KEY, FIRMS_API_KEY (gitignored, never commit .env)
  data/
    ranges/                    # pongo_abelii.geojson, pongo_pygmaeus.geojson,
                                # pongo_tapanuliensis.geojson (converted from shapefiles)
    deforestation/              # pongo_tapanuliensis_integrated_alerts.geojson + summary.csv
                                 # (only Tapanuli pulled so far — Phase 1 test case)
    fire/ literature/ social/ threats/   # created, empty — later phases, not built yet
  scripts/
    convert_ranges.py          # one-time: shapefile zips (data/ranges/raw/) -> GeoJSON.
                                # Prefers ogr2ogr if on PATH, falls back to pyshp+pyproj.
    fetch_deforestation.py     # pulls GFW `gfw_integrated_alerts` (GLAD-L+S2+RADD),
                                # clipped to each range polygon, via GFW Data API.
                                # Needs GFW_API_KEY (env or .env). Uses `requests`.
    # fetch_fire.py, fetch_literature.py, generate_monthly_charts.py — planned
    # in the brief, not yet written.
  site/
    index.html                 # landing page, links to tabs/
    css/style.css
    js/map.js                  # Leaflet/MapLibre map logic
    tabs/map.html               # only tab built so far; shows Tapanuli range +
                                 # deforestation alerts. literature/threats/social
                                 # tabs are linked but marked disabled — not built.
  .github/workflows/            # empty — monthly_refresh.yml / daily_fire_refresh.yml
                                 # from the brief not yet created.
```

Geometry/geospatial logic lives in `convert_ranges.py` (shapefile → WGS84
GeoJSON reprojection) and `fetch_deforestation.py` (clipping alert queries to
range polygons) — there is no separate "geometric math utilities" module;
that logic is inline in these two scripts.

## Build / test / run

- No build step — `site/` is plain static HTML/CSS/JS.
- Serve locally **from the repo root, not from `site/`** — `site/js/map.js`
  fetches data via relative paths (`../../data/...`) that assume the page is
  reached through the repo root:
  `python3 -m http.server 8000` (run from the repo root), then open
  `http://localhost:8000/site/index.html`. Serving with `--directory site`
  breaks all data fetches (`../../data/...` has nowhere to climb to) and the
  map will show "Could not load data: Failed to fetch". (A VS Code launch
  config at `.claude/launch.json` also serves the whole repo root on port
  8123 via a Windows-side conda Python — same idea, just a different port.)
- **Never open the HTML files directly (double-click / `file://...` in the
  address bar / dragging the file into the browser).** Browsers block
  `fetch()` from `file://` pages (CORS origin `'null'`, `net::ERR_FAILED`),
  which looks identical to the wrong-serve-root error above but has a
  different fix. The address bar must read `http://localhost:8000/...` —
  always go through the running server above, not the filesystem path.
- Run a data script standalone, e.g.:
  `python3 scripts/fetch_deforestation.py --species pongo_tapanuliensis`
  `python3 scripts/convert_ranges.py`
- No automated test suite exists yet.
- `requests` is available system-wide in this WSL Python. Any new script
  should stay standalone-runnable like the existing two before being wired
  into a GitHub Actions workflow.
- Never commit `.env` or API keys — `.gitignore` already excludes `.env`.

## Local model policy

This workstation runs fully offline via Ollama on the local RTX 5090. A
custom model `orangutan-dashboard-engineer` is defined in `./Modelfile`
(base `qwen2.5-coder:32b`, `num_ctx 32000`, `num_gpu 99`). Ollama is a native
systemd-managed install (`/usr/local/bin/ollama`, not the snap package — the
snap build can't see the GPU in this WSL setup and silently falls back
toward CPU/system RAM instead of VRAM).

**Any local coding agent (Aider, etc.) working in this repo should target
`orangutan-dashboard-engineer` via the local Ollama endpoint
(`http://127.0.0.1:11434`) by default, instead of a cloud API**, to keep this
workflow zero-token-cost and private. Only fall back to a cloud model if the
user explicitly asks for one.

### Launching the local agent

Aider is installed via `uv` under an isolated Python 3.12 (the system Python
is 3.14, too new for some of Aider's pinned dependencies like `numpy==1.24.3`
to build against). `OLLAMA_API_BASE` is already exported in `~/.bashrc`.

```bash
cd /mnt/c/AI_work/Orangutan_dashboard
aider --model ollama_chat/orangutan-dashboard-engineer
```

Note: VRAM is nearly maxed at the full 32k context (~24.1/24.5 GB used) —
this WSL instance only exposes ~24GB of the 5090's 32GB. Fine for a single
agent session; a second concurrent GPU task will likely OOM it. If that
happens, lower `num_ctx` in `Modelfile` and re-run
`ollama create orangutan-dashboard-engineer -f Modelfile`.
