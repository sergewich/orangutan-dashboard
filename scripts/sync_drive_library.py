#!/usr/bin/env python3
"""
sync_drive_library.py — bridge between the online literature library (a
Google Drive folder of PDFs + a Google Sheet catalogue) and catalog_library.py.
Runs nightly in .github/workflows/assistant_index.yml, so adding a paper is
just: drop the PDF in the Drive folder (from any device), and the next night's
run catalogues and indexes it. No local computer involved.

  pull  — list every PDF in the Drive folder (recursively), copy the Sheet
          into <workdir>/catalog.csv, and download only the PDFs that still
          need processing: new files, files whose cached text is gone, or
          (with --all) everything. Writes <workdir>/drive_files.json, the full
          listing, so catalog_library.py keeps rows for files it didn't see.
  push  — write <workdir>/catalog.csv back to the Sheet, so the person sees
          the decisions and can set the `override` column.

Google setup (one-time, details in worker/README.md):
  - a Google Cloud service account with the Drive and Sheets APIs enabled;
    its JSON key goes in GOOGLE_SERVICE_ACCOUNT_JSON (the JSON text itself)
  - share the Drive folder with the service account's email as Viewer, and
    the catalogue Sheet as Editor (the Sheet is the only thing it writes)
  - create an empty Google Sheet in that folder for the catalogue
  - DRIVE_FOLDER_ID and CATALOG_SHEET_ID: the IDs from the folder/sheet URLs

Usage:
    python scripts/sync_drive_library.py pull --workdir /tmp/library
    python scripts/catalog_library.py --library /tmp/library --remote-listing /tmp/library/drive_files.json
    python scripts/sync_drive_library.py push --workdir /tmp/library
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from pathlib import Path

try:
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2 import service_account
except ImportError:
    sys.exit("This script needs the 'google-auth' library.\n"
             "Install it with:  pip install google-auth requests")

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))
from catalog_library import COLUMNS, TEXT_CACHE, USER_COLUMNS, load_env  # noqa: E402

SCOPES = ["https://www.googleapis.com/auth/drive.readonly",
          "https://www.googleapis.com/auth/spreadsheets"]
DRIVE = "https://www.googleapis.com/drive/v3/files"
SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"
FOLDER_MIME = "application/vnd.google-apps.folder"


def session() -> AuthorizedSession:
    raw = (os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or "").strip()
    if not raw:
        sys.exit("Set GOOGLE_SERVICE_ACCOUNT_JSON (the service account's JSON key text, "
                 "or locally the path to the downloaded key file).")
    if not raw.startswith("{"):  # a path to the key file (handy in .env)
        path = Path(raw)
        if re.match(r"^[A-Za-z]:\\", raw):  # Windows path -> WSL path
            path = Path("/mnt/" + raw[0].lower() + raw[2:].replace("\\", "/"))
        raw = path.read_text()
    creds = service_account.Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)
    return AuthorizedSession(creds)


def require(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.exit(f"Set {name} (see this script's docstring).")
    return v


def list_pdfs(s: AuthorizedSession, folder_id: str, prefix: str = "") -> list[dict]:
    """Every PDF under the folder, with its path relative to the folder."""
    out, token = [], None
    while True:
        resp = s.get(DRIVE, params={
            "q": f"'{folder_id}' in parents and trashed = false",
            "fields": "nextPageToken, files(id, name, mimeType, md5Checksum)",
            "pageSize": 1000,
            "pageToken": token,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        for f in data.get("files", []):
            if f["mimeType"] == FOLDER_MIME:
                out += list_pdfs(s, f["id"], f"{prefix}{f['name']}/")
            elif f["mimeType"] == "application/pdf" and f.get("md5Checksum"):
                out.append({"id": f["id"], "file": prefix + f["name"], "md5": f["md5Checksum"]})
        token = data.get("nextPageToken")
        if not token:
            return out


def read_sheet(s: AuthorizedSession, sheet_id: str) -> list[dict]:
    resp = s.get(f"{SHEETS}/{sheet_id}/values/A:Z", timeout=60)
    resp.raise_for_status()
    values = resp.json().get("values", [])
    if not values:
        return []
    header = values[0]
    return [dict(zip(header, row + [""] * (len(header) - len(row)))) for row in values[1:]]


def write_sheet(s: AuthorizedSession, sheet_id: str, rows: list[dict]) -> None:
    values = [COLUMNS] + [[str(r.get(c, "")) for c in COLUMNS] for r in rows]
    s.post(f"{SHEETS}/{sheet_id}/values/A:Z:clear", timeout=60).raise_for_status()
    resp = s.put(f"{SHEETS}/{sheet_id}/values/A1", params={"valueInputOption": "RAW"},
                 json={"values": values}, timeout=60)
    resp.raise_for_status()


def pull(args) -> None:
    s = session()
    folder_id, sheet_id = require("DRIVE_FOLDER_ID"), require("CATALOG_SHEET_ID")
    work = Path(args.workdir)
    work.mkdir(parents=True, exist_ok=True)

    files = list_pdfs(s, folder_id)
    rows = read_sheet(s, sheet_id)
    with (work / "catalog.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    (work / "drive_files.json").write_text(json.dumps(
        [{"file": f["file"], "md5": f["md5"]} for f in files], ensure_ascii=False))

    known = {r.get("md5") for r in rows}
    todo = [f for f in files
            if args.all or f["md5"] not in known or not (TEXT_CACHE / f"{f['md5']}.json").exists()]
    print(f"{len(files)} PDFs in Drive, {len(rows)} catalogue rows; downloading {len(todo)}")
    for f in todo:
        dest = work / f["file"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        resp = s.get(f"{DRIVE}/{f['id']}", params={"alt": "media", "supportsAllDrives": "true"},
                     timeout=300)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
        print(f"  {f['file']}")


def push(args) -> None:
    s = session()
    sheet_id = require("CATALOG_SHEET_ID")
    with (Path(args.workdir) / "catalog.csv").open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    # The person may have edited the sheet while this run was going; their
    # columns win over what was pulled at the start (picked up next run).
    current = {r.get("md5"): r for r in read_sheet(s, sheet_id)}
    for r in rows:
        edited = current.get(r["md5"]) or {}
        r.update({k: edited[k] for k in USER_COLUMNS if edited.get(k)})
    write_sheet(s, sheet_id, rows)
    # Keep the local copy in step, for export_library_refs.py.
    with (Path(args.workdir) / "catalog.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {len(rows)} rows to the catalogue sheet")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["pull", "push"])
    parser.add_argument("--workdir", required=True, help="scratch folder for PDFs + catalog.csv (outside the repo)")
    parser.add_argument("--all", action="store_true", help="pull: download every PDF, not just new ones")
    args = parser.parse_args()
    load_env()
    (pull if args.command == "pull" else push)(args)


if __name__ == "__main__":
    main()
