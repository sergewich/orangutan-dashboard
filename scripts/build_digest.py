#!/usr/bin/env python3
"""
build_digest.py — the monthly subscriber email: what was added to the site
since the last digest.

Lists news incidents from data/social/incidents.json (human-approved only —
the same file the public News tab reads, never drafts.json) and new Literature
tab entries (literature.json + library.json). "New" means not in
digest_state.json, which records every incident id and literature link/title
already sent. The state file lives at the repo root, outside the published
data/ folder.

  python scripts/build_digest.py --out /tmp/digest     # writes digest.html, digest.txt, digest.json
  python scripts/build_digest.py --seed                # mark everything current as already sent
  python scripts/build_digest.py --out /tmp/digest --update-state
                                                       # also record this digest's items as sent

Only data that's already public goes in the email: titles, links, dates and
the reviewed incident summaries shown on the News tab.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INCIDENTS = REPO_ROOT / "data" / "social" / "incidents.json"
LITERATURE = [REPO_ROOT / "data" / "literature" / "literature.json",
              REPO_ROOT / "data" / "literature" / "library.json"]
STATE = REPO_ROOT / "digest_state.json"
SITE = "https://sergewich.github.io/orangutan-dashboard/"
CATEGORIES = {"translocation_rescue": "Rescues and translocations",
              "trade": "Wildlife trade", "killing": "Killings"}
TYPE_LABEL = {"journal": "Article", "book": "Book", "thesis": "Thesis", "report": "Report",
              "historic_newspaper": "Historic newspaper"}
MAX_LITERATURE = 40  # beyond this the email links to the tab instead


def lit_key(rec: dict) -> str:
    return (rec.get("url") or "").lower() or "t:" + re.sub(r"\W+", " ", (rec.get("title") or "").lower()).strip()


def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"last_digest": None, "incidents": [], "literature": []}


def save_state(state: dict, incidents: list[dict], literature: list[dict]) -> None:
    state["incidents"] = sorted(set(state["incidents"]) | {r["id"] for r in incidents})
    state["literature"] = sorted(set(state["literature"]) | {lit_key(r) for r in literature})
    state["last_digest"] = dt.date.today().isoformat()
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=0) + "\n")


def current() -> tuple[list[dict], list[dict]]:
    incidents = [r for r in json.loads(INCIDENTS.read_text())
                 if r.get("reviewed") and r.get("decision") == "approved"]
    literature = [r for p in LITERATURE if p.exists() for r in json.loads(p.read_text())]
    return incidents, literature


def date_of(rec: dict) -> str:
    return rec.get("date_hint") or ""


def esc(s) -> str:
    return html.escape(str(s or ""))


def render(incidents: list[dict], literature: list[dict], month: str) -> tuple[str, str]:
    lit = sorted(literature, key=lambda r: (-(r.get("year") or 0), (r.get("title") or "").lower()))
    shown = lit[:MAX_LITERATURE]
    h = [f'<div style="font-family:Arial,Helvetica,sans-serif;max-width:640px;margin:0 auto;color:#222;line-height:1.45">',
         f'<h1 style="font-size:22px;color:#8a4b14">Orangutan Conservation Dashboard — {esc(month)}</h1>',
         f'<p>New on the <a href="{SITE}">dashboard</a> since the last update: '
         f'{len(incidents)} news report{"s" * (len(incidents) != 1)} and '
         f'{len(literature)} literature entr{"ies" if len(literature) != 1 else "y"}.</p>']
    t = [f"Orangutan Conservation Dashboard — {month}", "",
         f"New since the last update: {len(incidents)} news reports, {len(literature)} literature entries.",
         f"Dashboard: {SITE}", ""]

    if incidents:
        h.append('<h2 style="font-size:18px;border-bottom:2px solid #d9a066">News</h2>'
                 '<p style="font-size:13px;color:#666">Reports found in news, NGO and government sources '
                 'and checked by a person before publishing. Summaries are short paraphrases — follow the '
                 'link for the original report.</p>')
        t += ["NEWS", ""]
        for cat, label in CATEGORIES.items():
            recs = sorted((r for r in incidents if r.get("category") == cat), key=date_of, reverse=True)
            if not recs:
                continue
            h.append(f'<h3 style="font-size:15px;margin-bottom:4px">{esc(label)} ({len(recs)})</h3><ul style="padding-left:18px">')
            t.append(f"{label} ({len(recs)})")
            for r in recs:
                meta = " · ".join(x for x in (date_of(r), r.get("location"), r.get("org")) if x)
                h.append(f'<li style="margin-bottom:8px"><a href="{esc(r["url"])}">{esc(r["title"])}</a>'
                         f'<br><span style="font-size:12px;color:#666">{esc(meta)}</span>'
                         f'<br><span style="font-size:13px">{esc(r.get("summary"))}</span></li>')
                t += [f"- {r['title']}", f"  {meta}", f"  {r.get('summary') or ''}", f"  {r['url']}"]
            h.append("</ul>")
            t.append("")
        h.append(f'<p><a href="{SITE}site/tabs/social.html">All news on the dashboard</a></p>')

    if literature:
        h.append('<h2 style="font-size:18px;border-bottom:2px solid #d9a066">Literature</h2><ul style="padding-left:18px">')
        t += ["LITERATURE", ""]
        for r in shown:
            bits = " · ".join(str(x) for x in (TYPE_LABEL.get(r.get("type"), "Work"), r.get("year"), r.get("authors")) if x)
            title = esc(r.get("title"))
            if (r.get("url") or "").startswith(("https://", "http://")):
                title = f'<a href="{esc(r["url"])}">{title}</a>'
            h.append(f'<li style="margin-bottom:6px">{title}<br><span style="font-size:12px;color:#666">{esc(bits)}</span></li>')
            t += [f"- {r.get('title')}", f"  {bits}"] + ([f"  {r['url']}"] if r.get("url") else [])
        h.append("</ul>")
        if len(lit) > len(shown):
            h.append(f"<p>…and {len(lit) - len(shown)} more.</p>")
            t.append(f"...and {len(lit) - len(shown)} more.")
        h.append(f'<p><a href="{SITE}site/tabs/literature.html">Browse the Literature tab</a></p>')
        t += ["", f"Literature tab: {SITE}site/tabs/literature.html"]

    h.append('<p style="font-size:12px;color:#888;margin-top:24px">You receive this because you signed up on '
             'the Orangutan Conservation Dashboard.</p></div>')
    return "\n".join(h), "\n".join(t) + "\n"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", help="folder for digest.html / digest.txt / digest.json")
    p.add_argument("--seed", action="store_true", help="record everything current as sent; no digest")
    p.add_argument("--update-state", action="store_true", help="record this digest's items as sent")
    args = p.parse_args()

    state = load_state()
    incidents, literature = current()
    if args.seed:
        save_state(state, incidents, literature)
        print(f"Seeded {STATE.name}: {len(incidents)} incidents, {len(literature)} literature entries")
        return
    seen_inc, seen_lit = set(state["incidents"]), set(state["literature"])
    new_inc = [r for r in incidents if r["id"] not in seen_inc]
    new_lit, keys = [], set()
    for r in literature:
        k = lit_key(r)
        if k not in seen_lit and k not in keys:
            keys.add(k)
            new_lit.append(r)

    month = dt.date.today().strftime("%B %Y")
    body_html, body_text = render(new_inc, new_lit, month)
    summary = {"month": month, "incidents": len(new_inc), "literature": len(new_lit),
               "subject": f"Orangutan dashboard update, {month}: {len(new_inc)} news reports, "
                          f"{len(new_lit)} new publications"}
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "digest.html").write_text(body_html)
        (out / "digest.txt").write_text(body_text)
        (out / "digest.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary))
    if args.update_state:
        save_state(state, new_inc, new_lit)


if __name__ == "__main__":
    main()
