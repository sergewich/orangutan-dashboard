#!/usr/bin/env python3
"""
eval_assistant.py — regression test for the Literature Assistant.

Each question in EVAL lists title fragments of the paper(s) that should be
found. Two modes:

  retrieval  embed each question and query a Vectorize index directly;
             reports whether an expected paper is in the top K (recall@K)
             and at what rank. Use it to compare embedding models / corpora
             without spending LLM calls.
  answers    ask the deployed Worker each question and print the answer and
             cited sources, for reading by a person (and flags questions
             where no expected paper was cited).

Needs CF_ACCOUNT_ID + CF_RAG_API_TOKEN in .env for retrieval mode.

Usage:
    python scripts/eval_assistant.py retrieval
    python scripts/eval_assistant.py retrieval --index orangutan-lit-v2 --embed qwen3-256
    python scripts/eval_assistant.py answers
"""

from __future__ import annotations

import argparse
import math
import os
import re
import sys
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKER_URL = "https://orangutan-assistant.orangutan-dashboard.workers.dev/api/chat"

EVAL = [
    ("When was the Tapanuli orangutan described as a new species?", ["evidence for a new orangutan species"]),
    ("How many orangutans are left in the wild?", ["population status of sumatran and tapanuli", "distribution and conservation status"]),
    ("How many Bornean orangutans were lost between 1999 and 2015?", ["eliminated more than 100 000 bornean"]),
    ("Why are orangutans killed in Kalimantan?", ["quantifying killing of orangutans", "not just conflict",
                                                  "killing of orangutans in kalimantan"]),
    ("Do orangutans have culture?", ["orangutan cultures and the evolution"]),
    ("How long do orangutan mothers wait between births?", ["slow ape", "life history of wild sumatran", "interbirth", "reproductive"]),
    ("Do wild orangutans use tools?", ["tools in wild sumatran", "tool use"]),
    ("Can orangutans survive in oil palm plantations?", ["of pongo palms and perceptions", "oil palm"]),
    ("Do orangutans produce speech-like sounds?", ["speech like rhythm", "vocal"]),
    ("How successful is orangutan reintroduction?", ["reintroduc", "release", "rehabilitat"]),
    ("What threatens the Tapanuli orangutan?", ["tapanuli"]),
    ("How much deforestation has there been recently in the Bornean orangutan range?",
     ["dashboard alert summary bornean"]),
    ("Wat schreven Nederlandse kranten over orang-oetans in dierentuinen?", ["diergaarde", "orang oetan"]),
    ("Apa ancaman utama bagi orangutan Tapanuli?", ["tapanuli"]),
    ("When was the first orangutan born in a Dutch zoo?", ["geboren in de rotterdamsche diergaarde"]),
]

# Embedding configs: model, output dims, query prefix. Must match ingest.
EMBEDDERS = {
    "bge-m3": ("@cf/baai/bge-m3", 1024, ""),
    "qwen3-256": ("@cf/qwen/qwen3-embedding-0.6b", 256,
                  "Instruct: Given a question, retrieve passages from orangutan research literature "
                  "that answer it\nQuery: "),
}


def norm(s: str) -> str:
    return re.sub(r"\W+", " ", (s or "").lower()).strip()


def load_env() -> None:
    env = REPO_ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def embed(session, base, cfg, texts, is_query=True):
    model, dims, prefix = EMBEDDERS[cfg]
    body = {"text": [(prefix if is_query else "") + t for t in texts]}
    r = session.post(f"{base}/ai/run/{model}", json=body, timeout=120).json()
    if not r.get("success"):
        raise RuntimeError(r.get("errors"))
    vecs = r["result"].get("data") or r["result"].get("response")
    out = []
    for v in vecs:
        v = v[:dims]
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        out.append([x / n for x in v])
    return out


def retrieval(args) -> None:
    account, token = os.environ["CF_ACCOUNT_ID"], os.environ["CF_RAG_API_TOKEN"]
    base = f"https://api.cloudflare.com/client/v4/accounts/{account}"
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {token}"
    vecs = embed(s, base, args.embed, [q for q, _ in EVAL])
    hits = 0
    for (q, expected), v in zip(EVAL, vecs):
        r = s.post(f"{base}/vectorize/v2/indexes/{args.index}/query",
                   json={"vector": v, "topK": args.k, "returnMetadata": "all"}, timeout=60).json()
        titles = [norm(m["metadata"].get("title", "")) for m in r["result"]["matches"]]
        rank = next((i + 1 for i, t in enumerate(titles) if any(norm(e) in t for e in expected)), None)
        hits += rank is not None
        print(f"{'✓' if rank else '✗'} rank {rank or '-':>2}  {q[:70]}")
        if args.verbose or not rank:
            for i, m in enumerate(r["result"]["matches"][:5]):
                md = m["metadata"]
                print(f"        {i+1}. {m['score']:.3f} {md.get('year')} {md.get('title','')[:70]}")
    print(f"\nrecall@{args.k}: {hits}/{len(EVAL)}  (index {args.index}, embedder {args.embed})")


def answers(args) -> None:
    missed = 0
    picks = [EVAL[int(i) - 1] for i in args.only.split(",")] if args.only else EVAL
    for q, expected in picks:
        t0 = time.time()
        body = {"question": q}
        if args.model:
            body["model"] = args.model
        if args.debug:
            body["debug"] = True
        resp = requests.post(args.url, json=body,
                             headers={"Origin": "https://sergewich.github.io"}, timeout=180)
        d = resp.json()
        dt = time.time() - t0
        print("=" * 100 + f"\nQ: {q}   ({dt:.1f}s)")
        if "error" in d:
            print("ERROR:", d["error"])
            missed += 1
            continue
        if d.get("debug"):
            print("   rewrites:", d["debug"]["rewritten"])
            for r in d["debug"]["retrieved"]:
                print("   ·", r)
        print(d["answer"])
        cited = [s for s in d["sources"] if s["cited"]]
        ok = any(any(norm(e) in norm(s["title"]) for e in expected) for s in cited)
        missed += not ok
        for s in cited:
            print(f"   [{s['n']}] {s.get('year')} {s['title'][:80]} ({s.get('kind','')})")
        print("   " + ("✓ expected source cited" if ok else "✗ expected source NOT cited"))
        time.sleep(args.pause)
    print(f"\nexpected source cited for {len(picks) - missed}/{len(picks)} questions")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=["retrieval", "answers"])
    p.add_argument("--index", default="orangutan-literature")
    p.add_argument("--embed", default="bge-m3", choices=sorted(EMBEDDERS))
    p.add_argument("-k", type=int, default=8)
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--url", default=WORKER_URL)
    p.add_argument("--pause", type=float, default=8.0, help="seconds between questions (rate limit is 8/min)")
    p.add_argument("--model", help="answers mode: answer model to test (Worker must have TESTING=1)")
    p.add_argument("--debug", action="store_true", help="answers mode: show rewrites and retrieved passages")
    p.add_argument("--only", help="answers mode: comma-separated question numbers (1-based) to run")
    args = p.parse_args()
    load_env()
    (retrieval if args.mode == "retrieval" else answers)(args)


if __name__ == "__main__":
    sys.exit(main())
