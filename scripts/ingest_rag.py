#!/usr/bin/env python3
"""
ingest_rag.py — embed data/rag/corpus.jsonl (from build_rag_corpus.py) with
Workers AI and upsert it into the Cloudflare Vectorize index that the
Literature Assistant Worker (worker/) queries.

The embedding model MUST match the one the Worker uses for queries
(EMBED_MODEL in worker/wrangler.toml). bge-m3 is multilingual, which matters
here: the corpus mixes English, Indonesian, and colonial-era Dutch.

Incremental: a manifest in rag_state/ingested.json records a hash of
each uploaded passage, so only new/changed passages are re-embedded, and
passages that disappeared from the corpus are deleted from the index.

Needs in .env (or the environment):
  CF_ACCOUNT_ID       — same account as the analytics script
  CF_RAG_API_TOKEN    — a SEPARATE scoped token with "Workers AI: Read" and
                        "Vectorize: Edit" (don't widen the analytics token)

One-time index setup (run from worker/):
  npx wrangler vectorize create orangutan-literature --dimensions=1024 --metric=cosine

Usage:
    python scripts/ingest_rag.py --dry-run     # show what would change
    python scripts/ingest_rag.py
    python scripts/ingest_rag.py --rebuild     # re-embed everything (e.g. after a model change)
    python scripts/ingest_rag.py --allow-mass-delete   # if you really mean to remove >25% of the index
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("This script needs the 'requests' library.\nInstall it with:  pip install requests")

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
CORPUS = REPO_ROOT / "data" / "rag" / "corpus.jsonl"
# Committed to the repo (ids + hashes only, no text) so it survives between
# GitHub Actions runs; deliberately outside data/, which the Pages deploy publishes.
MANIFEST = REPO_ROOT / "rag_state" / "ingested.json"

INDEX_NAME = "orangutan-literature"
EMBED_MODEL = "@cf/baai/bge-m3"      # keep in sync with worker/wrangler.toml
EMBED_DIMS = 1024
BATCH = 50
MAX_META_TEXT = 3000  # Vectorize caps metadata at 10 KiB/vector; leave room for title etc.


def load_env() -> None:
    env = REPO_ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def passage_hash(p: dict) -> str:
    return hashlib.sha1(json.dumps(p, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def embed_text(p: dict) -> str:
    # Title is embedded with the body so title-only matches still score.
    return f"{p['title']}\n\n{p['text']}"


class Cloudflare:
    def __init__(self, account: str, token: str):
        self.base = f"https://api.cloudflare.com/client/v4/accounts/{account}"
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"

    def _check(self, resp: requests.Response) -> dict:
        try:
            body = resp.json()
        except ValueError:
            resp.raise_for_status()
            raise
        if not body.get("success", resp.ok):
            raise RuntimeError(f"Cloudflare API error {resp.status_code}: {body.get('errors')}")
        return body.get("result") or {}

    def embed(self, texts: list[str]) -> list[list[float]]:
        result = self._check(self.s.post(f"{self.base}/ai/run/{EMBED_MODEL}", json={"text": texts}, timeout=120))
        vectors = result.get("data") or result.get("response")
        if not vectors or len(vectors) != len(texts) or len(vectors[0]) != EMBED_DIMS:
            raise RuntimeError(f"Unexpected embedding response shape: {str(result)[:300]}")
        return vectors

    def upsert(self, rows: list[dict]) -> None:
        ndjson = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
        self._check(self.s.post(
            f"{self.base}/vectorize/v2/indexes/{INDEX_NAME}/upsert",
            data=ndjson.encode(), headers={"Content-Type": "application/x-ndjson"}, timeout=120))

    def delete(self, ids: list[str]) -> None:
        self._check(self.s.post(
            f"{self.base}/vectorize/v2/indexes/{INDEX_NAME}/delete_by_ids", json={"ids": ids}, timeout=60))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--rebuild", action="store_true", help="ignore the manifest and re-embed everything")
    parser.add_argument("--allow-mass-delete", action="store_true",
                        help="permit deleting more than 25%% of indexed passages in one run")
    args = parser.parse_args()
    load_env()

    if not CORPUS.exists():
        sys.exit(f"{CORPUS.relative_to(REPO_ROOT)} not found — run scripts/build_rag_corpus.py first.")
    passages = [json.loads(line) for line in CORPUS.read_text().splitlines() if line.strip()]
    manifest = {} if args.rebuild or not MANIFEST.exists() else json.loads(MANIFEST.read_text())

    current = {p["id"]: passage_hash(p) for p in passages}
    todo = [p for p in passages if manifest.get(p["id"]) != current[p["id"]]]
    stale = [i for i in manifest if i not in current]
    print(f"{len(passages)} passages: {len(todo)} to embed/upsert, {len(stale)} to delete, "
          f"{len(passages) - len(todo)} unchanged")
    if args.dry_run or not (todo or stale):
        return
    # A failed upstream step (e.g. the Drive sync) can produce a corpus that's
    # missing a whole source; don't let that silently wipe the index.
    if manifest and len(stale) > 0.25 * len(manifest) and not args.allow_mass_delete:
        sys.exit(f"Refusing to delete {len(stale)} of {len(manifest)} indexed passages — "
                 "check the corpus build, or pass --allow-mass-delete if this is intended.")

    account, token = os.environ.get("CF_ACCOUNT_ID"), os.environ.get("CF_RAG_API_TOKEN")
    if not account or not token:
        sys.exit("Set CF_ACCOUNT_ID and CF_RAG_API_TOKEN in .env (see this script's docstring).")
    cf = Cloudflare(account, token)

    try:
        for start in range(0, len(todo), BATCH):
            batch = todo[start:start + BATCH]
            vectors = cf.embed([embed_text(p) for p in batch])
            cf.upsert([{
                "id": p["id"],
                "values": v,
                "metadata": {
                    "type": p["type"],
                    "title": p["title"][:300],
                    "authors": (p.get("authors") or "")[:300],
                    "year": p.get("year") or 0,
                    "url": p["url"],
                    "source": p["source"],
                    "species": ",".join(p.get("species") or []),
                    "text": p["text"][:MAX_META_TEXT],
                },
            } for p, v in zip(batch, vectors)])
            for p in batch:
                manifest[p["id"]] = current[p["id"]]
            print(f"  upserted {min(start + BATCH, len(todo))}/{len(todo)}")
        for start in range(0, len(stale), 100):
            ids = stale[start:start + 100]
            cf.delete(ids)
            for i in ids:
                manifest.pop(i, None)
        if stale:
            print(f"  deleted {len(stale)} stale passages")
    finally:
        MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST.write_text(json.dumps(manifest))
    print("Done. Vectorize indexes asynchronously — new passages can take a minute to become queryable.")


if __name__ == "__main__":
    main()
