#!/usr/bin/env python3
"""
ingest_rag.py — embed data/rag/corpus.jsonl (from build_rag_corpus.py) with
Workers AI and upsert it into the Cloudflare Vectorize index that the
Literature Assistant Worker (worker/) queries.

The embedding model MUST match the one the Worker uses for queries
(EMBED_MODEL in worker/wrangler.toml). bge-m3 is multilingual, which matters
here: the corpus mixes English, Indonesian, and colonial-era Dutch.

Incremental: a manifest in rag_state/<index>.json records a hash of
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
import time
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
STATE_DIR = REPO_ROOT / "rag_state"

# Embedding profiles: (model, stored dims, query prefix). Documents are
# embedded without the prefix. qwen3 vectors are Matryoshka-trained, so the
# first 256 dims (re-normalised) keep most of the quality at a quarter of the
# storage — that's what fits ~19k passages in Vectorize's free 5M dimensions.
# The Worker's EMBED_MODEL / EMBED_DIMS / QUERY_PREFIX must match the profile
# used for its index.
EMBEDDERS = {
    "bge-m3": ("@cf/baai/bge-m3", 1024, ""),
    "qwen3-256": ("@cf/qwen/qwen3-embedding-0.6b", 256,
                  "Instruct: Given a question, retrieve passages from orangutan research literature "
                  "that answer it\nQuery: "),
}
DEFAULT_INDEX, DEFAULT_EMBEDDER = "orangutan-lit-v2", "qwen3-256"
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
    # Title (and full-text section) are embedded with the body so a passage
    # is findable by what the paper is about, not only by its own wording.
    head = p["title"] + (f" — {p['section']}" if p.get("section") else "")
    return f"{head}\n\n{p['text']}"


def hash_of(obj) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def embed_hash(p: dict) -> str:
    return hash_of(embed_text(p))


def metadata(p: dict) -> dict:
    """What the Worker reads back with each match (Vectorize caps it at 10 KiB)."""
    return {k: v for k, v in {
        "type": p["type"],
        "kind": p.get("kind", ""),
        "title": p["title"][:300],
        "authors": (p.get("authors") or "")[:300],
        "year": p.get("year") or 0,
        "venue": (p.get("venue") or "")[:200],
        "section": (p.get("section") or "")[:120],
        "abstract_source": p.get("abstract_source", ""),
        "cites": p.get("cites") or 0,
        "url": p["url"],
        "source": p["source"],
        "species": ",".join(p.get("species") or []),
        "text": p["text"][:MAX_META_TEXT],
    }.items() if v not in ("", None)}


def normalise(v: list[float], dims: int) -> list[float]:
    v = v[:dims]
    n = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / n for x in v]


class DailyLimit(RuntimeError):
    pass


class Cloudflare:
    def __init__(self, account: str, token: str, index: str, embedder: str):
        self.index = index
        self.model, self.dims, _ = EMBEDDERS[embedder]
        self.base = f"https://api.cloudflare.com/client/v4/accounts/{account}"
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"

    def _post(self, url: str, **kw) -> requests.Response:
        """POST with retries: Cloudflare's API occasionally times out or 5xxs."""
        for attempt in range(5):
            try:
                resp = self.s.post(url, timeout=180, **kw)
            except (requests.ConnectionError, requests.Timeout):
                if attempt == 4:
                    raise
                time.sleep(5 * (attempt + 1))
                continue
            if resp.status_code >= 500 and attempt < 4:
                time.sleep(5 * (attempt + 1))
                continue
            return resp
        return resp

    def _check(self, resp: requests.Response) -> dict:
        try:
            body = resp.json()
        except ValueError:
            resp.raise_for_status()
            raise
        if not body.get("success", resp.ok):
            msg = str(body.get("errors"))
            if "daily" in msg.lower() or "neurons" in msg.lower() or resp.status_code == 429:
                raise DailyLimit(msg)
            raise RuntimeError(f"Cloudflare API error {resp.status_code}: {msg}")
        return body.get("result") or {}

    def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            return self._embed(texts)
        except RuntimeError as exc:
            # Workers AI caps the total input per request ("input too big");
            # a batch with several long passages trips it, so halve and retry.
            if "input too big" not in str(exc) or len(texts) == 1:
                raise
            mid = len(texts) // 2
            return self.embed(texts[:mid]) + self.embed(texts[mid:])

    def _embed(self, texts: list[str]) -> list[list[float]]:
        result = self._check(self._post(f"{self.base}/ai/run/{self.model}", json={"text": texts}))
        vectors = result.get("data") or result.get("response")
        if not vectors or len(vectors) != len(texts) or len(vectors[0]) < self.dims:
            raise RuntimeError(f"Unexpected embedding response shape: {str(result)[:300]}")
        return [normalise(v, self.dims) for v in vectors]

    def get_values(self, ids: list[str]) -> dict[str, list[float]]:
        """Stored vectors by id (no AI cost) — for metadata-only updates."""
        out = {}
        for i in range(0, len(ids), 20):  # the API takes at most 20 ids per call
            result = self._check(self._post(f"{self.base}/vectorize/v2/indexes/{self.index}/get_by_ids",
                                            json={"ids": ids[i:i + 20]}))
            rows = result if isinstance(result, list) else result.get("vectors", [])
            out.update({r["id"]: r["values"] for r in rows if r.get("values")})
        return out

    def upsert(self, rows: list[dict]) -> None:
        ndjson = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
        self._check(self._post(
            f"{self.base}/vectorize/v2/indexes/{self.index}/upsert",
            data=ndjson.encode(), headers={"Content-Type": "application/x-ndjson"}))

    def delete(self, ids: list[str]) -> None:
        self._check(self._post(
            f"{self.base}/vectorize/v2/indexes/{self.index}/delete_by_ids", json={"ids": ids}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--rebuild", action="store_true", help="ignore the manifest and re-embed everything")
    parser.add_argument("--allow-mass-delete", action="store_true",
                        help="permit deleting more than 25%% of indexed passages in one run")
    parser.add_argument("--index", default=DEFAULT_INDEX)
    parser.add_argument("--embed", default=DEFAULT_EMBEDDER, choices=sorted(EMBEDDERS))
    parser.add_argument("--corpus", type=Path, default=CORPUS)
    parser.add_argument("--metadata-only", action="store_true",
                        help="only apply metadata updates and deletions; embed nothing")
    parser.add_argument("--max-passages", type=int, default=0,
                        help="embed at most N passages this run (stays inside the Workers Free daily "
                             "AI allowance; the next run continues)")
    args = parser.parse_args()
    load_env()

    if not args.corpus.exists():
        sys.exit(f"{args.corpus} not found — run scripts/build_rag_corpus.py first.")
    passages = [json.loads(line) for line in args.corpus.read_text().splitlines() if line.strip()]
    manifest_path = STATE_DIR / f"{args.index}.json"  # one manifest per index
    manifest = {} if args.rebuild or not manifest_path.exists() else json.loads(manifest_path.read_text())

    # Two fingerprints per passage: "e" covers what gets embedded, "m" the
    # stored metadata. A metadata-only change (e.g. updated citation counts)
    # reuses the stored vector instead of spending AI allowance on re-embedding.
    current = {p["id"]: {"e": embed_hash(p), "m": hash_of(metadata(p))} for p in passages}

    def state(pid: str, p: dict) -> str:
        old = manifest.get(pid)
        if old is None:
            return "embed"
        if isinstance(old, str):  # manifest written before the e/m split
            legacy = passage_hash({k: v for k, v in p.items() if k != "cites"})
            return "meta" if old == legacy else "embed"
        if old.get("e") != current[pid]["e"]:
            return "embed"
        return "meta" if old.get("m") != current[pid]["m"] else "same"

    status = {p["id"]: state(p["id"], p) for p in passages}
    todo = [p for p in passages if status[p["id"]] == "embed"]
    meta_only = [p for p in passages if status[p["id"]] == "meta"]
    # Most useful first, so a run cut short by the daily allowance still
    # leaves the index broadly useful: abstracts and short texts before the
    # long tail of full-text chunks.
    todo.sort(key=lambda p: (p.get("kind") == "fulltext", p["id"]))
    if args.max_passages:
        todo = todo[:args.max_passages]
    if args.metadata_only:
        todo = []
    stale = [i for i in manifest if i not in current]
    print(f"{len(passages)} passages: {len(todo)} to embed, {len(meta_only)} metadata-only updates, "
          f"{len(stale)} to delete")
    if args.dry_run or not (todo or meta_only or stale):
        return
    # A failed upstream step (e.g. the Drive sync) can produce a corpus that's
    # missing a whole source; don't let that silently wipe the index.
    if manifest and len(stale) > 0.25 * len(manifest) and not args.allow_mass_delete:
        sys.exit(f"Refusing to delete {len(stale)} of {len(manifest)} indexed passages — "
                 "check the corpus build, or pass --allow-mass-delete if this is intended.")

    account, token = os.environ.get("CF_ACCOUNT_ID"), os.environ.get("CF_RAG_API_TOKEN")
    if not account or not token:
        sys.exit("Set CF_ACCOUNT_ID and CF_RAG_API_TOKEN in .env (see this script's docstring).")
    cf = Cloudflare(account, token, args.index, args.embed)

    try:
        for start in range(0, len(meta_only), 100):
            batch = meta_only[start:start + 100]
            values = cf.get_values([p["id"] for p in batch])
            rows = [{"id": p["id"], "values": values[p["id"]], "metadata": metadata(p)}
                    for p in batch if p["id"] in values]
            if rows:
                cf.upsert(rows)
            for p in batch:
                if p["id"] in values:
                    manifest[p["id"]] = current[p["id"]]
                else:  # not actually in the index: embed it next run
                    manifest.pop(p["id"], None)
            if (start // 100) % 10 == 0 or start + 100 >= len(meta_only):
                print(f"  metadata updated {min(start + 100, len(meta_only))}/{len(meta_only)}")
        for start in range(0, len(todo), BATCH):
            batch = todo[start:start + BATCH]
            vectors = cf.embed([embed_text(p) for p in batch])
            cf.upsert([{"id": p["id"], "values": v, "metadata": metadata(p)} for p, v in zip(batch, vectors)])
            for p in batch:
                manifest[p["id"]] = current[p["id"]]
            if (start // BATCH) % 10 == 0 or start + BATCH >= len(todo):
                print(f"  embedded {min(start + BATCH, len(todo))}/{len(todo)}")
        for start in range(0, len(stale), 100):
            ids = stale[start:start + 100]
            cf.delete(ids)
            for i in ids:
                manifest.pop(i, None)
        if stale:
            print(f"  deleted {len(stale)} stale passages")
    except DailyLimit as exc:
        # Workers Free: the daily AI allowance ran out. Progress is saved in
        # the manifest; the next run (e.g. tomorrow night) carries on.
        print(f"  Stopped: Workers AI daily allowance used up ({exc}). "
              f"Re-run tomorrow to continue — progress is saved.", file=sys.stderr)
    finally:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest))
    print("Done. Vectorize indexes asynchronously — new passages can take a minute to become queryable.")


if __name__ == "__main__":
    main()
