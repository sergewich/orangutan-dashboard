# Orangutan Conservation Dashboard — Local Agent Instructions

## Project identity
A static website consolidating conservation data for the three orangutan
species (*Pongo abelii*, *P. pygmaeus*, *P. tapanuliensis*): range maps,
near-real-time deforestation and fire alerts, literature, threat/company
data, and social/news incident monitoring. No live backend — data is
pre-processed into versioned GeoJSON/CSV files in `data/`, refreshed on a
schedule via GitHub Actions, and the frontend just loads them. The site is
deployed to GitHub Pages at https://sergewich.github.io/orangutan-dashboard/,
rebuilt on every push to `master`. Full design doc:
`orangutan-dashboard-brief.md`.

## Current state of the repo (as of 2026-08-25)

```
Orangutan_dashboard/
  Modelfile                    # local Ollama model definition (see below)
  orangutan-dashboard-brief.md # full project brief / target architecture
  .env / .env.example          # GFW_API_KEY, FIRMS_API_KEY (gitignored, never commit .env)
  data/
    ranges/                    # pongo_abelii/pygmaeus/tapanuliensis.geojson (converted shapefiles)
    deforestation/               # pongo_{abelii,pygmaeus,tapanuliensis}_integrated_alerts.geojson
                                  # + summary.csv, all three species, monthly GFW refresh
    fire/                        # pongo_{abelii,pygmaeus,tapanuliensis} fire alerts (NASA
                                  # FIRMS), all three species, daily refresh
    literature/                  # literature.json — 980 real records (books/journals/news/
                                  # historic Dutch newspapers), see Literature tab below
    social/                      # raw/articles.json (scraped), drafts.json (LLM-extracted,
                                  # unreviewed), incidents.json (human-approved only —
                                  # the ONLY file the public Social tab reads)
    threats/                     # oil_palm.geojson (2527), mining_concessions.geojson (3017),
                                  # hydro_plants.geojson (62) — see fetch_threats.py
    archive/                      # permanent monthly snapshots of fire/deforestation alerts —
                                  # data/fire/ and data/deforestation/ are rolling windows that
                                  # get overwritten every refresh; see "Historic alert archive"
                                  # below. manifest.json lists what's archived per species/month
                                  # (and, for deforestation, whether that month's POINTS are
                                  # complete — the aggregate counts always are).
    wdpa_places.json              # hand-rebuilt reference: unique protected areas (deduped WDPA
                                  # site_pid groups) confirmed to overlap each species' range.
                                  # No WDPA geometry/bulk attributes stored (licensing, see
                                  # below) — just names/ids, same spirit as data/ngos/ngos.json.
                                  # Rebuild with scripts/build_wdpa_places.py, which also applies
                                  # a hand-curated EXCLUDED_IDS list for composite/landscape-scale
                                  # designations (e.g. a UNESCO World Heritage Site covering
                                  # ground already counted under a separately-listed national
                                  # park) that name-based dedup can't catch on its own, since the
                                  # two entries have genuinely different names. Removing a place
                                  # from the live JSON without also adding it to EXCLUDED_IDS just
                                  # means the next full rebuild brings it back.
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
    scrape_social.py            # finds orangutan articles on NGO sites (OIC/COP/YEL/BOSF/IAR/OFI/OF-UK/
                                 # TRAFFIC), news sites (ANTARA, Kompas, Prokal, Jakarta
                                 # Post, Tribunnews, Mongabay EN/ID), the ministry's KSDAE news (covers the
                                 # BKSDA offices) + a GDELT sweep (EN + ID), searching
                                 # "orangutan" and "orang utan". Appends url/title/date to
                                 # data/social/raw/articles.json — never article text
                                 # (public repo); extract_incidents.py fetches text locally.
    extract_incidents.py        # runs raw articles through the local Ollama model
                                 # (structured-output schema) to draft data/social/drafts.json
                                 # records (trade/translocation_rescue/killing). Every draft
                                 # is reviewed=false — see review policy below.
    review_server.py            # LOCAL ONLY, not deployed: threaded HTTP server + POST
                                 # /api/review endpoint backing site/tabs/review.html. Only
                                 # place that writes to data/social/incidents.json.
    fetch_threats.py             # oil palm + mining concessions from GFW Data API
                                  # (government registries, explicit SELECT columns — SELECT *
                                  # 500s on gfw_oil_palm's larger polygons), hydro plants from
                                  # OSM Overpass + Wikidata SPARQL (Wikidata fills real OSM
                                  # coverage gaps, e.g. Batang Toru/NSHE). Polygons simplified
                                  # with an in-file pure-Python Douglas-Peucker (no shapely dep).
                                  # Government/registry-sourced, so does NOT go through the
                                  # Social tab's review gate (see policy below) — but also isn't
                                  # independently verified by this project.
    build_wdpa_places.py         # rebuilds data/wdpa_places.json (occasional/manual — species
                                  # ranges rarely change, not wired into CI). See "Historic alert
                                  # archive & per-protected-area counts" below.
    archive_alerts.py             # freezes last month's fire/deforestation alerts into
                                  # data/archive/ before the next refresh overwrites the live
                                  # rolling-window files. Run monthly via monthly_refresh.yml.
    compute_pa_alerts.py          # complete fire + deforestation alert counts per protected
                                  # area in wdpa_places.json, for a given month. Run monthly via
                                  # monthly_refresh.yml. Needs GFW_API_KEY.
    backfill_archive_from_git.py  # one-time: mines old git commits of data/fire|deforestation
                                  # to recover months from before archive_alerts.py existed.
                                  # Already run once (2026-11-24) — no need to re-run.
  site/
    index.html                  # landing page, links to tabs/
    css/style.css
    js/map.js                   # Leaflet map: range + deforestation/fire alerts + Hansen
                                 # forest-loss tile overlay + WDPA protected areas (live tile
                                 # embed of UNEP-WCMC's own ArcGIS cache, not stored in data/ —
                                 # WDPA's terms forbid redistributing the data itself, see
                                 # "Protected areas (WDPA) layer" below) + oil palm/mining/
                                 # hydro (all off by default except range; oil palm/mining/
                                 # hydro lazy-load on first toggle, see THREAT_LAYERS in map.js).
                                 # A per-species month picker (reading data/archive/manifest.json)
                                 # swaps the deforestation/fire layers between the live rolling
                                 # window ("Current") and a permanent past-month snapshot from
                                 # data/archive/ — shows a warning banner when the selected
                                 # month's deforestation points are a partial sample (see
                                 # "Historic alert archive" below), never hides the gap.
    js/literature.js, js/social.js, js/review.js, js/threats.js, js/protected_areas.js
    tabs/map.html, literature.html, social.html, threats.html, protected-areas.html
                                 # all public, linked in nav
    tabs/review.html             # LOCAL ONLY — deliberately not linked from nav, see below
  .github/workflows/
    daily_fire_refresh.yml       # daily: re-run fetch_fire.py for all three species
    monthly_refresh.yml          # monthly (1st of month): fetch_deforestation.py +
                                  # fetch_literature.py + fetch_threats.py, all species
    weekly_social_scrape.yml     # weekly: re-run scrape_social.py (raw articles only —
                                  # does NOT run extract_incidents.py or touch
                                  # incidents.json; review is still a manual local step)
    social_review_reminder.yml   # monthly, 25th: check_review_backlog.py counts raw
                                  # articles never drafted + drafts awaiting human review;
                                  # emails the owner (Brevo) only if something is pending,
                                  # so the local draft+review step isn't missed before
                                  # monthly_digest.yml runs on the 2nd
    pages_deploy.yml             # on push to master: builds _site/ (index.html + site/ +
                                  # data/, minus data/ranges/raw) and deploys to GitHub
                                  # Pages via actions/deploy-pages
```

All four tabs from the original brief are now built (Map, Literature, Social,
Threats). Nothing structurally unbuilt remains, and every data pipeline
(deforestation/fire/literature/social/threats) is now on a GitHub Actions
schedule (see `.github/workflows/` above) — the only deliberate gap is
`extract_incidents.py` + `review_server.py`, which stay manual/local-only
since incident review must remain a human-in-the-loop step (see review
policy below). The site also auto-deploys to GitHub Pages on every push to
`master`.

Geometry/geospatial logic lives in `convert_ranges.py` (shapefile → WGS84
GeoJSON reprojection) and `fetch_deforestation.py`/`fetch_fire.py` (clipping
alert queries to range polygons — bbox + client-side point-in-polygon for
FIRMS, direct polygon query for GFW) — there is no separate "geometric math
utilities" module; that logic is inline in these scripts.

### Protected areas (WDPA) layer — live embed, deliberately not fetched/stored

`site/js/map.js`'s "Protected areas (WDPA)" overlay points straight at
UNEP-WCMC's own cached ArcGIS tile service
(`data-gis.unep-wcmc.org/server/rest/services/ProtectedSites/` ...
`The_World_Database_of_Protected_Areas/MapServer/tile/{z}/{y}/{x}`, confirmed
live 2026-11-24, no token needed, `maxNativeZoom: 14`) rather than following
the usual pattern of this repo (fetch → clip/simplify → commit a GeoJSON to
`data/`, as `fetch_threats.py` does). **This is deliberate, not an
oversight**: the WDPA terms (`protectedplanet.net/en/legal`) forbid
redistributing the data "by any means, including web downloads [and] web
services" without UNEP-WCMC's written permission — committing processed WDPA
geometry to this public repo (deployed as static files on GitHub Pages)
would be exactly that. Referencing their own live tile cache sidesteps this:
we never possess or redistribute the data ourselves, the same "reference,
don't store" approach already used for the Hansen forest-loss tile layer.
Don't add a `fetch_protected_areas.py`-style script that downloads and
commits WDPA polygons/attributes into `data/` without first getting
UNEP-WCMC's written permission (business-support@unep-wcmc.org) — ask the
owner before pursuing that path, since it's a live legal/licensing
condition they'd need to sign off on, not just a styling decision. If
UNEP-WCMC's tile service ever goes down or changes URL, that's the thing to
fix/replace, not a signal to switch to pre-fetched storage.

### Historic alert archive & per-protected-area counts

`data/fire/*.geojson` and `data/deforestation/*.geojson` are rolling
windows — every refresh **overwrites** them (FIRMS NRT only retains a few
weeks/months; the deforestation point file is deliberately capped to
`--points-days`, see `fetch_deforestation.py`). Nothing before that window
survived until `scripts/archive_alerts.py` started freezing each completed
month into `data/archive/{fire,deforestation}/<species>_<YYYY-MM>.geojson`
(+ a matching `_summary.csv`), run monthly via `monthly_refresh.yml`, right
after that day's deforestation fetch. `data/archive/manifest.json` lists
what's archived and drives the Map tab's month picker (GitHub Pages has no
directory-listing API, so a static file has to say what's available).

**Critical caveat discovered 2026-11-24, don't lose this**:
`fetch_deforestation.py`'s `--max-points` cap (100,000/run) means the
point-level deforestation file can be silently missing most or all of a
high-volume month — confirmed during a dry-season fire spike, Bornean
deforestation hit 2.86M alerts in a single month (August 2026) while the
point file caps at 100k. The **aggregate summary CSV is never affected** (a
separate, uncapped GFW query) — only the per-point file with coordinates.
`archive_alerts.py` cross-checks the two and records
`"points_complete": false` **and** `"points_count": N` in the manifest
whenever this happens — both fields matter, not just the boolean. 0 points
with a nonzero summary total is a *different* gap from a few thousand points
surviving out of a few million: it means the live rolling window never
reached back that far when the month was archived, i.e. no point-level data
exists for that month at all, not that "volume was too high." Found and
fixed 2026-11-24 after the Map tab's month picker showed a misleading
"volume was too high" banner for *Pongo tapanuliensis* (a low-volume
species!) on a month that actually had zero archived points for an unrelated
reason. **When extending anything that shows individual alert points on a
map: check both `points_complete` and `points_count`, and word the gap
accordingly — don't hide it, and don't misattribute it either.** Fire has no
such cap anywhere in `fetch_fire.py` and no known upstream row limit either
— the same investigation found no evidence of missing fire data. When
extending anything that shows *counts* (not points): prefer
`compute_pa_alerts.py`'s approach (a direct GFW count query per protected
area, never point-capped) over spatially joining the point file, which
inherits its gaps.

`scripts/compute_pa_alerts.py` produces `data/archive/pa_alerts/<YYYY-MM>.json`
— complete fire + deforestation counts per protected area (from
`data/wdpa_places.json`), run monthly alongside the archiving step. This is
what feeds the per-protected-area breakdown in the monthly digest
(`site_updates`-style, see the digest section below) — it exists precisely
*because* the point-file approach can't give reliable per-area counts for
high-volume regions like Borneo. Also surfaced on its own public tab,
`site/tabs/protected-areas.html` (+ `site/js/protected_areas.js`): a
sortable table per protected area, with a month picker reading
`manifest.json`'s `pa_alerts` list. The digest additionally carries a
per-species "Species overview" table (fire/deforestation totals with a
month-over-month trend, from `scripts/build_digest.py`'s
`compute_species_trends()`) and links out to both this tab and the Map
tab's month picker for anyone who wants to dig further.

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

## Literature Assistant (RAG chatbot)

A floating "Ask the literature" widget (`site/js/assistant.js`, on every
public page except `review.html`) backed by a **standalone Cloudflare
Worker** in `worker/`, deployed separately with wrangler (not part of the
GitHub Pages deploy). **Everything runs online** — the owner wants it to work
with their computer off, so don't add steps that need a local machine.
Setup, licensing rules, and refresh: `worker/README.md`.

Nightly `.github/workflows/assistant_index.yml`:
`sync_drive_library.py pull` (new PDFs from the owner's Google Drive library
folder + the catalogue Google Sheet) → `catalog_library.py` (per file:
`full` if the published version is openly licensed / PDF carries a CC
statement, else `abstract`; OCR for scans; the Sheet's `override` column
always wins) → `sync_drive_library.py push` → `build_rag_corpus.py` →
`ingest_rag.py --max-passages 4000` into Vectorize index `orangutan-lit-v2`.

Corpus (`build_rag_corpus.py` + `rag_sources.py`, ~16k passages): every
orangutan work in OpenAlex (~6,300, all years; abstracts filled from Europe
PMC, else Semantic Scholar TLDRs), full text of the ~500 CC-licensed papers
in Europe PMC (boilerplate sections dropped, methods capped), Delpher
newspapers, Open Library descriptions, per-species alert summaries, and the
PDF library.

Embeddings: `qwen3-embedding-0.6b` truncated to 256 dims ("qwen3-256"
profile) — beat bge-m3 at 1024 dims on `scripts/eval_assistant.py` (14/15 vs
13/15) and is what fits the corpus in Vectorize's free 5M stored dimensions
(~19k passages; currently ~4.1M used). Ingest is incremental with separate
embed/metadata fingerprints (`rag_state/<index>.json`), so metadata-only
changes (e.g. citation counts) cost no AI allowance.

Worker (`worker/`): question → vector search (raw-score cut-off
`MIN_SCORE` 0.60; below it, refuse with no LLM call) → 3 rewritten queries
(llama-3.1-8b) → merge, re-rank with a citations-per-year prior → 12
passages → answer model (`CHAT_MODEL`, Llama 3.3 70B) with a fallback when
output looks broken. Sources come from retrieval, never from model output.
`TESTING="1"` lets `eval_assistant.py answers --model/--debug` compare models
on the live pipeline — keep it `"0"` when public.

**Workers Free limits shape all of this**: ~10k AI neurons/day shared by
nightly embedding and answers (~95 neurons per answer with Llama 3.3);
DeepSeek V4 / Kimi K2.6 / GLM 5.3 are Paid-plan only. Tested and rejected on
Free: gpt-oss-120b (leaked reasoning, looped, summed figures across studies),
Gemma 4 (refused answerable questions), `bge-reranker-base` (dropped key
papers, near-zero scores on Dutch).

Rules when extending it:
- **Never index anything under `data/social/`**, not even approved
  `incidents.json`: an LLM paraphrase of an incident is a new, unreviewed
  claim about trade/killings (see review policy above).
- **Never put library PDFs, extracted text, or abstracts in the repo** —
  `data/` is published by the Pages deploy and the repo is public. They live
  in Drive and the Actions cache (`data/rag/` is gitignored).
- Full text only with an open licence on the published version; "free to
  read" (bronze OA) is abstract-only. Don't loosen this without the owner.
- The embedding profile in `scripts/ingest_rag.py` (`EMBEDDERS`) and
  `worker/wrangler.toml` (`EMBED_MODEL`/`EMBED_DIMS`/`QUERY_PREFIX`) must
  match; changing it needs a new index + `ingest_rag.py --rebuild`.
- Before changing retrieval or the prompt, run `scripts/eval_assistant.py`
  (retrieval mode is nearly free; answers mode spends neurons).
- The widget renders model output as text nodes only, never `innerHTML`.
- It's framed as a literature summariser, not a decision-maker — keep the
  system prompt's "inform, don't decide" and "no accusations against named
  companies unless a passage states it" rules.

## Monthly subscriber digest (Brevo)

`site/tabs/subscribe.html` embeds a Brevo sign-up form (iframe, double
opt-in, list "Monthly digest", optional ORGANISATION/ROLE/COUNTRY) — built in
the Brevo UI, so edit it there. `.github/workflows/monthly_digest.yml` (2nd of
each month) runs `build_digest.py` (approved incidents + new Literature tab
entries + hand-curated `data/site_updates.json` feature/content announcements
— whichever of the three aren't yet in `digest_state.json`) and
`send_digest.py`, which only creates a **draft** campaign and emails the
owner a test copy — the owner clicks Send in Brevo. Don't make it send
automatically without the owner. To announce a shipped feature in the next
digest, add an entry to `data/site_updates.json` (id/date/title/description)
— each entry is sent once, same dedup pattern as incidents/literature. Two
sections are NOT deduped against `digest_state.json` — they're a fresh
monthly snapshot every time, not a backlog: the per-species "Species
overview" trend table and the per-protected-area breakdown (both from
`data/archive/`, see "Historic alert archive & per-protected-area counts"
above).
Secrets: `BREVO_API_KEY` (restricted: Email campaigns + Contacts), optional
`DIGEST_TEST_TO`. Brevo free plan: 300 emails/day. Sender updates@orangutandashboard.org (domain bought in Cloudflare, DKIM/DMARC records for Brevo; Cloudflare Email Routing forwards updates@ to the site Gmail). Never send from the Gmail address: it fails DMARC at universities.

Since `extract_incidents.py` (drafting) and `review_server.py` (human
approval) both stay manual/local (see review policy above) and
`build_digest.py` only picks up *approved* incidents, forgetting to run
them before the 2nd means the digest silently skips new incidents.
`social_review_reminder.yml` (25th of each month) runs
`scripts/check_review_backlog.py`, which counts raw articles never drafted
plus drafts awaiting review and emails the owner (reusing `BREVO_API_KEY`
and the same updates@orangutandashboard.org sender, direct transactional
email via `/smtp/email`, not a campaign) only when that count is non-zero
— silent otherwise, so it doesn't nag every month. Recipient defaults to
the owner's own address; override with the optional `REMINDER_TO` secret.

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
  `python3 scripts/review_server.py --port 8124` instead of the plain
  `http.server` (port 8000 is taken on the Windows side of this machine, so
  localhost:8000 hits another program) —
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
(base `qwen3.6:27b`, `num_ctx 32000`, `num_gpu 99`). Ollama is a native
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

Note: at the full 32k context this loads at ~17GB VRAM (Q4_K_M, 100% GPU) —
this WSL instance only exposes ~24GB of the 5090's 32GB, so there's headroom
for a second concurrent GPU task now, unlike the old qwen2.5-coder:32b setup
which left almost none. `orangutan-dashboard-engineer` is a thinking model
(it emits a reasoning block before its answer, visible in Aider/CLI output)
— expect slightly higher latency per response than the old base model. If
VRAM ever gets tight, lower `num_ctx` in `Modelfile` and re-run
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
