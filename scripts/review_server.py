#!/usr/bin/env python3
"""
review_server.py — local-only server for the human review gate on the
Social/Threats incident pipeline. NOT part of the deployed static site
(nothing here should run on GitHub Pages/Cloudflare Pages) — it's a
workstation tool for approving/rejecting LLM-drafted incident records
before they can appear anywhere public.

Serves the whole repo as static files (same as `python3 -m http.server`
run from the repo root — see CLAUDE.md) plus one POST endpoint:

    POST /api/review
    {"id": "<draft id>", "decision": "approve" | "reject", "edits": {...}}

On "approve": the draft (with any edits merged in) is appended/updated in
data/social/incidents.json — the file a future public Social/Threats tab
would read — and marked reviewed=true in drafts.json.
On "reject": marked reviewed=true, decision="rejected" in drafts.json.
Rejected records are never written to incidents.json.

Usage:
    python scripts/review_server.py [--port 8000]
    Then open http://localhost:8000/site/tabs/review.html
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DRAFTS_FILE = REPO_ROOT / "data" / "social" / "drafts.json"
INCIDENTS_FILE = REPO_ROOT / "data" / "social" / "incidents.json"

EDITABLE_FIELDS = {"category", "summary", "date", "location", "species_mentioned"}
WRITE_LOCK = threading.Lock()  # server is now threaded — avoid racing on file read-modify-write


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=None), encoding="utf-8")


class ReviewHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(REPO_ROOT), **kwargs)

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{self.address_string()} - {fmt % args}\n")

    def do_POST(self):
        if self.path != "/api/review":
            self.send_error(404, "Unknown endpoint")
            return

        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            self.send_error(400, "Invalid JSON body")
            return

        draft_id = body.get("id")
        decision = body.get("decision")
        edits = body.get("edits") or {}
        if not draft_id or decision not in ("approve", "reject"):
            self.send_error(400, "Missing id or invalid decision")
            return

        with WRITE_LOCK:
            drafts = load_json(DRAFTS_FILE, [])
            target = next((d for d in drafts if d["id"] == draft_id), None)
            if target is None:
                self.send_error(404, "Draft not found")
                return

            for k, v in edits.items():
                if k in EDITABLE_FIELDS:
                    target[k] = v

            target["reviewed"] = True
            target["decision"] = "approved" if decision == "approve" else "rejected"
            save_json(DRAFTS_FILE, drafts)

            if decision == "approve":
                incidents = load_json(INCIDENTS_FILE, [])
                incidents = [i for i in incidents if i["id"] != draft_id]
                incidents.append(target)
                incidents.sort(key=lambda r: r.get("date") or "", reverse=True)
                save_json(INCIDENTS_FILE, incidents)

        self._send_json({"status": "ok", "id": draft_id, "decision": target["decision"]})

    def _send_json(self, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        # Local-only tool — CORS isn't a real concern, but keep same-origin
        # fetches from the review page frictionless during development.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    server = ThreadingHTTPServer(("0.0.0.0", args.port), ReviewHandler)
    print(f"Review server running at http://localhost:{args.port}/")
    print(f"Open http://localhost:{args.port}/site/tabs/review.html to review drafts.")
    print("Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
