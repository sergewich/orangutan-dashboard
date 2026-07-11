# Orangutan Conservation Dashboard — Local Agent Instructions

## Project identity
A static website consolidating conservation data for the three orangutan
species (*Pongo abelii*, *P. pygmaeus*, *P. tapanuliensis*): range maps,
near-real-time deforestation and fire alerts, literature, threat/company
data, and social/news incident monitoring. No live backend — data is
pre-processed into versioned GeoJSON/CSV files in `data/`, refreshed on a
schedule (GitHub Actions, not yet wired up), and the frontend just loads
them. Full design doc: `orangutan-dashboard-brief.md`.

## Current state of the repo (as of 2026-07-11)

```
Orangutan_dashboard/
  Modelfile                    # local Ollama model definition (see below)
  orangutan-dashboard-brief.md # full project brief / target architecture
  .env / .env.example          # GFW_API_KEY, FIRMS_API_KEY (gitignored, never commit .env)
  data/
    ranges/                    # pongo_abelii/pygmaeus/tapanuliensis.geojson (converted shapefiles)
    deforestation/              # pongo_tapanuliensis_integrated_alerts.geojson + summary.csv
                                 # (only Tapanuli pulled so far — Phase 1 test case)
    fire/                       # pongo_tapanuliensis fire alerts (NASA FIRMS)
    literature/                  # literature.json — 980 real records (books/journals/news/
                                  # historic Dutch newspapers), see Literature tab below
    social/                      # raw/articles.json (scraped), drafts.json (LLM-extracted,
                                  # unreviewed), incidents.json (human-approved only —
                                  # the ONLY file the public Social tab reads)
    threats/                     # created, empty — not built yet
  scripts/
    convert_ranges.py          # one-time: shapefile zips (data/ranges/raw/) -> GeoJSON.
                                # Prefers ogr2ogr if on PATH, falls back to pyshp+pyproj.
    fetch_deforestation.py     # pulls GFW `gfw_integrated_alerts`, clipped to each range
                                # polygon, via GFW Data API. Needs GFW_API_KEY.
    fetch_fire.py               # pulls NASA FIRMS area/csv (GET, bbox-only, <=5 days per
                                 # request — NOT the same contract as the GFW Data API,
                                 # see docstring), client-side point-in-polygon filtered.
                                 # Needs FIRMS_API_KEY.
    fetch_literature.py         # OpenAlex (journal articles) + Open Library (books) +
                                 # GDELT (recent news) + KB/Delpher SRU API (historic Dutch
                                 # colonial-era newspapers). All keyless public APIs.
    scrape_social.py            # scrapes OIC/COP/YEL/SOCP/TRAFFIC news pages + a GDELT
                                 # sweep into data/social/raw/articles.json. Raw text only —
                                 # does not classify or publish anything.
    extract_incidents.py        # runs raw articles through the local Ollama model
                                 # (structured-output schema) to draft data/social/drafts.json
                                 # records (trade/translocation_rescue/killing). Every draft
                                 # is reviewed=false — see review policy below.
    review_server.py            # LOCAL ONLY, not deployed: threaded HTTP server + POST
                                 # /api/review endpoint backing site/tabs/review.html. Only
                                 # place that writes to data/social/incidents.json.
  site/
    index.html                  # landing page, links to tabs/
    css/style.css
    js/map.js                   # Leaflet map: range + deforestation/fire alerts + Hansen
                                 # forest-loss tile overlay (all off by default except range)
    js/literature.js, js/social.js, js/review.js
    tabs/map.html, literature.html, social.html   # public, linked in nav
    tabs/review.html             # LOCAL ONLY — deliberately not linked from nav, see below
    tabs/threats.html            # still just a disabled nav placeholder, not built
  .github/workflows/             # empty — scheduled refresh workflows not yet created
```

Geometry/geospatial logic lives in `convert_ranges.py` (shapefile → WGS84
GeoJSON reprojection) and `fetch_deforestation.py`/`fetch_fire.py` (clipping
alert queries to range polygons — bbox + client-side point-in-polygon for
FIRMS, direct polygon query for GFW) — there is no separate "geometric math
utilities" module; that logic is inline in these scripts.

## Mandatory human review for Social/Threats content

**No scraped or LLM-extracted claim about wildlife trade, translocations, or
killings may be published without explicit human review and approval.** This
is a firm project rule (confirmed by the project owner), not a style
preference. The pipeline enforces it structurally:

`scrape_social.py` (raw text only) → `extract_incidents.py` (drafts,
`reviewed: false`, written to `data/social/drafts.json`) →
`review_server.py` + `site/tabs/review.html` (human clicks Approve/Reject
per record) → only approved records land in `data/social/incidents.json`,
which is the *only* file `site/js/social.js` reads.

When extending this pipeline: never make `extract_incidents.py` (or
anything else) write directly to `incidents.json`. The review step is the
whole point — an LLM classification of a scraped article is not a verified
fact, regardless of how confident it looks. `site/tabs/review.html` is
intentionally **not** linked from the public nav (`site/tabs/review.html`
is a workstation-only tool) and `review_server.py` must never run as part
of a public deployment (GitHub Pages/Cloudflare Pages can't run a live
Python process anyway, so this is structurally impossible, not just a
convention — but don't accidentally document or link it as if it were
public-facing).

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
- To use the review queue (`site/tabs/review.html`), serve with
  `python3 scripts/review_server.py` instead of the plain `http.server` —
  it serves the same static files plus the `POST /api/review` endpoint the
  page needs to persist approve/reject decisions. It's a
  `ThreadingHTTPServer`; the plain single-threaded `HTTPServer` stalls on
  *all* requests the moment one browser tab holds a connection open, which
  looks like the server hung when it's really just serializing on one
  client.
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

**If Ollama stops responding** (a request hangs indefinitely, `ollama ps`
claims the model is loaded but generate calls never return) — this has
happened once after the machine slept overnight with a connection open.
`nvidia-smi` will still show the GPU idle/responsive; the Ollama *service*
is what's wedged. Needs `sudo systemctl restart ollama` (requires the
user's password, cannot be done non-interactively).

Ollama's `format` parameter (JSON schema, not just `format: "json"`)
reliably forces structured output — used in `extract_incidents.py`. Prefer
this over free-text-then-parse for anything needing reliable JSON from the
local model.
