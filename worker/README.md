# Literature Assistant

The "Ask the literature" chat widget (`site/js/assistant.js`) and everything
behind it. **All of it runs online.** Answering happens in a Cloudflare
Worker, and indexing runs as a nightly GitHub Action, so nothing depends on a
local computer being on.

```
Google Drive folder (your PDFs) ─┐
literature.json (Literature tab) ├─ nightly GitHub Action ─▶ Vectorize index
alert summaries (Map tab)       ─┘   (assistant_index.yml)        │
                                                                   ▼
browser widget ──POST /api/chat──▶ Cloudflare Worker: embed question → top passages → LLM → answer + sources
```

## Adding literature (the everyday part)

1. Drop PDFs into the **Drive library folder**. Any device works and
   subfolders are fine.
2. Overnight, the Action downloads only the new files and adds a row for each
   to the **catalogue Google Sheet** (in the same folder). Each row says what
   will be indexed (`full`, `abstract` or `exclude`) and why.
3. Look over the new rows when convenient. To change a decision, type `full`,
   `abstract` or `exclude` in the `override` column. You can also correct
   `title`, `authors`, `year` or `url` there, since those appear in citations.
   Your edits are never overwritten, and they take effect the next night.

To run it immediately rather than overnight: GitHub → Actions → *Literature
Assistant index refresh* → *Run workflow*.

### What gets indexed: the rules

| The file is… | Indexed | Why |
|---|---|---|
| Published under an open licence (CC BY, BY-SA, BY-NC, BY-ND, CC0, public domain) | Full text | The licence allows reuse with attribution, and every answer cites the source. Non-commercial (NC) licences are fine because the site is non-commercial. |
| Paywalled | Abstract only | Copyright stays with the publisher. |
| **Free to read but not openly licensed** (e.g. Cell, PNAS and Science papers that are free after a period) | Abstract only | Free to read is not the same as permission to reuse. |
| Paywalled, but an openly licensed accepted manuscript exists in a repository | Abstract only, and the reason column says where the licensed copy is | The licence covers that manuscript file, not the publisher's PDF. Put the repository version in the folder instead to get full text. |
| A report or thesis with a CC statement in the PDF | Full text | The licence statement on the document itself. |
| A report or thesis with no licence statement | Its own abstract or executive summary | Same caution as for papers. If you know the publisher allows reuse (many NGO and IUCN reports do), set `override` to `full`. |
| A scanned PDF | OCR'd automatically (English, Indonesian, Dutch), then the rules above | The reason column marks OCR'd text so you can spot-check it. |

How the licence is found: if the PDF contains a DOI, OpenAlex reports the
licence of the *published version*. Otherwise the script looks for a Creative
Commons statement on the first two pages or the last page. Treat this as a
cautious rule of thumb, not legal advice. Your university's copyright
librarian can confirm edge cases.

Indexing full text means the bot can summarise and quote from it, with a
citation. It never hands out the PDF: the files stay in your private Drive,
and the Worker returns only the answer plus title, authors, year and link.

## One-time setup

### 1. Cloudflare (the chatbot itself)

```
cd worker
npx wrangler login
npx wrangler vectorize create orangutan-literature --dimensions=1024 --metric=cosine
npx wrangler deploy
```
`deploy` prints a URL such as `https://orangutan-assistant.<subdomain>.workers.dev`.
Set `ASSISTANT_ENDPOINT` at the top of `site/js/assistant.js` to
`<that URL>/api/chat`, then commit and push. Until this is set, the widget
stays hidden.

Create an API token for the nightly indexing at
https://dash.cloudflare.com/profile/api-tokens with *Account → Workers AI →
Read* and *Account → Vectorize → Edit*. Keep it separate from the analytics token.

### 2. Google Drive (the library)

Use a **dedicated Google account** for this, not your personal one. The
site's own address (orangutanconservationsite@gmail.com) is a natural fit.
Your personal Drive then never connects to GitHub. To add papers from your
personal account, share the library folder with yourself.

1. In the dedicated account, create a Drive folder, e.g. *Orangutan library*,
   and inside it an empty Google Sheet, e.g. *Library catalogue*.
2. At https://console.cloud.google.com, signed in as the dedicated account:
   create a project, enable the **Google Drive API** and **Google Sheets
   API**, and create a **service account** (no roles needed). Under *Keys*,
   add a JSON key and download it.
3. Share the **folder** with the service account's email address
   (`…@….iam.gserviceaccount.com`) as **Viewer**, then share the **Sheet**
   with it as **Editor**. The bot can then read the PDFs but never change or
   delete them, and the Sheet is the only thing it can write to. It sees
   nothing outside that folder.

### 3. GitHub repository secrets

Settings → Secrets and variables → Actions → *New repository secret*:

| Secret | Value |
|---|---|
| `CF_ACCOUNT_ID` | Cloudflare account ID |
| `CF_RAG_API_TOKEN` | the token from step 1 |
| `OPENALEX_API_KEY` | free key from https://openalex.org/settings/api (strongly recommended: keyless OpenAlex has a tiny shared budget) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | the **entire contents** of the downloaded JSON key file |
| `DRIVE_FOLDER_ID` | from the folder URL: `drive.google.com/drive/folders/<THIS>` |
| `CATALOG_SHEET_ID` | from the sheet URL: `docs.google.com/spreadsheets/d/<THIS>/edit` |

Without the three Google secrets, the Action still indexes the Literature tab
and alert summaries; it just skips the library.

### 4. First run

Actions → *Literature Assistant index refresh* → *Run workflow*. The first
run downloads, OCRs and embeds everything, so allow up to an hour for a few
hundred PDFs. Later runs only handle what changed.

## Test it

```
curl -s https://orangutan-assistant.<subdomain>.workers.dev/api/chat \
  -H 'Origin: https://sergewich.github.io' -H 'Content-Type: application/json' \
  -d '{"question":"What threatens the Tapanuli orangutan?"}'
```
Also try off-topic questions ("best pizza in Naples?"). These should return
the refusal with no sources. If on-topic questions get refused, or off-topic
ones get answers, adjust `MIN_SCORE` in `wrangler.toml` and redeploy.
`npx wrangler tail` streams the Worker's logs.

## Safety nets

- `ingest_rag.py` refuses to delete more than 25% of the index in one run.
  This stops a broken Drive sync from wiping the library. Use
  `--allow-mass-delete` deliberately.
- `build_rag_corpus.py` aborts if the library is enabled but its catalogue is
  missing.
- `rag_state/ingested.json` (committed; IDs and hashes only) records what's in
  the index. Lookup caches and extracted text live in the Actions cache, never
  in the public repo. If GitHub evicts the cache, the next run re-fetches.

## Cost and abuse

- On the Workers **Free** plan, Workers AI has a daily free allocation and
  stops serving when it runs out (users see "unavailable"), so there is no
  surprise bill. On **Paid**, overage is billed; set a budget alert.
  Vectorize's free tier caps total stored vectors. A few hundred full-text
  PDFs can exceed it, in which case ingest fails with a quota error and the
  Paid plan (low cost at this scale) is needed. Check current limits in the
  Cloudflare docs.
- Per-IP limit: 8 questions per minute. CORS restricts browsers to the site
  and localhost. If scripted abuse appears, add Cloudflare Turnstile.
- No conversations are stored.

## Running pieces locally (optional, for testing)

Each script runs standalone with `.env` values (see `.env.example`):
```
python3 scripts/catalog_library.py --library /path/to/test/pdfs
python3 scripts/build_rag_corpus.py --library /path/to/test/pdfs --limit 20
python3 scripts/ingest_rag.py --dry-run
```
Local runs write the same `rag_state/ingested.json` as CI. Don't commit a
local one over CI's unless you intend to.

## Changing models

`CHAT_MODEL` can be any Workers AI text-generation model; edit it and
redeploy. `EMBED_MODEL` must match `scripts/ingest_rag.py`. After changing
it, run the workflow with `ingest_rag.py --rebuild` (and recreate the index
if the dimensions change).
