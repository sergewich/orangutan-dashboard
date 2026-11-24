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
import csv
import datetime as dt
import html
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INCIDENTS = REPO_ROOT / "data" / "social" / "incidents.json"
LITERATURE = [REPO_ROOT / "data" / "literature" / "literature.json",
              REPO_ROOT / "data" / "literature" / "library.json"]
SITE_UPDATES = REPO_ROOT / "data" / "site_updates.json"
ARCHIVE_DIR = REPO_ROOT / "data" / "archive"
PA_ALERTS_DIR = ARCHIVE_DIR / "pa_alerts"
STATE = REPO_ROOT / "digest_state.json"
MAX_PA_ALERTS = 20  # beyond this the email links to the dashboard instead
SITE = "https://sergewich.github.io/orangutan-dashboard/"
SPECIES_LIST = ["pongo_abelii", "pongo_pygmaeus", "pongo_tapanuliensis"]
SPECIES_LABEL = {"pongo_abelii": "Sumatran orangutan", "pongo_pygmaeus": "Bornean orangutan",
                  "pongo_tapanuliensis": "Tapanuli orangutan"}
CATEGORIES = {"translocation_rescue": "Rescues and translocations",
              "trade": "Wildlife trade", "killing": "Killings"}
TYPE_LABEL = {"journal": "Article", "book": "Book", "thesis": "Thesis", "report": "Report",
              "historic_newspaper": "Historic newspaper"}
MAX_LITERATURE = 40  # beyond this the email links to the tab instead


def lit_key(rec: dict) -> str:
    return (rec.get("url") or "").lower() or "t:" + re.sub(r"\W+", " ", (rec.get("title") or "").lower()).strip()


def load_state() -> dict:
    if STATE.exists():
        state = json.loads(STATE.read_text())
    else:
        state = {"last_digest": None, "incidents": [], "literature": []}
    state.setdefault("site_updates", [])
    return state


def save_state(state: dict, incidents: list[dict], literature: list[dict], site_updates: list[dict]) -> None:
    state["incidents"] = sorted(set(state["incidents"]) | {r["id"] for r in incidents})
    state["literature"] = sorted(set(state["literature"]) | {lit_key(r) for r in literature})
    state["site_updates"] = sorted(set(state["site_updates"]) | {r["id"] for r in site_updates})
    state["last_digest"] = dt.date.today().isoformat()
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=0) + "\n")


def current() -> tuple[list[dict], list[dict], list[dict], dict | None, list[dict]]:
    incidents = [r for r in json.loads(INCIDENTS.read_text())
                 if r.get("reviewed") and r.get("decision") == "approved"]
    literature = [r for p in LITERATURE if p.exists() for r in json.loads(p.read_text())]
    site_updates = json.loads(SITE_UPDATES.read_text())["updates"] if SITE_UPDATES.exists() else []
    pa_alerts = latest_pa_alerts()
    species_trends = []
    if pa_alerts:
        species_trends = compute_species_trends(pa_alerts["month"])
    return incidents, literature, site_updates, pa_alerts, species_trends


def prev_month_str(month: str) -> str:
    year, mon = (int(x) for x in month.split("-"))
    year, mon = (year - 1, 12) if mon == 1 else (year, mon - 1)
    return f"{year:04d}-{mon:02d}"


def month_fire_total(species: str, month: str) -> int | None:
    p = ARCHIVE_DIR / "fire" / f"{species}_{month}_summary.csv"
    if not p.exists():
        return None
    return sum(int(r["alert_count"]) for r in csv.DictReader(p.open()))


def month_defor_totals(species: str, month: str) -> tuple[int, float] | None:
    p = ARCHIVE_DIR / "deforestation" / f"{species}_{month}_summary.csv"
    if not p.exists():
        return None
    rows = list(csv.DictReader(p.open()))
    count = sum(int(r["alert_count"]) for r in rows)
    area = sum(float(r["area_ha"]) for r in rows)
    return count, round(area, 1)


def compute_species_trends(month: str) -> list[dict]:
    """Per-species fire + deforestation totals for `month`, with a
    month-over-month comparison where the previous month was also archived
    (see scripts/archive_alerts.py) — a raw count alone doesn't tell a
    subscriber whether things are getting better or worse."""
    prev = prev_month_str(month)
    out = []
    for species in SPECIES_LIST:
        fire_now = month_fire_total(species, month)
        fire_prev = month_fire_total(species, prev)
        defor_now = month_defor_totals(species, month)
        defor_prev = month_defor_totals(species, prev)
        out.append({
            "species": species,
            "fire_now": fire_now, "fire_prev": fire_prev,
            "defor_count_now": defor_now[0] if defor_now else None,
            "defor_area_now": defor_now[1] if defor_now else None,
            "defor_count_prev": defor_prev[0] if defor_prev else None,
        })
    return out


def trend_str(now: int | None, prev: int | None) -> str:
    if now is None:
        return "no data"
    if prev is None or prev == 0:
        return f"{now:,}"
    pct = (now - prev) / prev * 100
    arrow = "▲" if pct > 5 else ("▼" if pct < -5 else "→")
    return f"{now:,} ({arrow} {abs(pct):.0f}% vs last month)"


def latest_pa_alerts() -> dict | None:
    """The most recently computed data/archive/pa_alerts/<YYYY-MM>.json, if
    any. Unlike incidents/literature/site_updates this is NOT deduped against
    digest_state.json — it's inherently a fresh monthly snapshot, not a
    backlog of "new since last time" items, so it's included every digest
    regardless of whether a previous digest already covered a past month."""
    if not PA_ALERTS_DIR.exists():
        return None
    files = sorted(PA_ALERTS_DIR.glob("*.json"))
    if not files:
        return None
    return json.loads(files[-1].read_text())


def date_of(rec: dict) -> str:
    return rec.get("date_hint") or ""


def esc(s) -> str:
    return html.escape(str(s or ""))


def render(incidents: list[dict], literature: list[dict], site_updates: list[dict],
           pa_alerts: dict | None, species_trends: list[dict], month: str) -> tuple[str, str]:
    lit = sorted(literature, key=lambda r: (-(r.get("year") or 0), (r.get("title") or "").lower()))
    shown = lit[:MAX_LITERATURE]
    updates_clause = f", and {len(site_updates)} site update{'s' if len(site_updates) != 1 else ''}" if site_updates else ""
    h = [f'<div style="font-family:Arial,Helvetica,sans-serif;max-width:640px;margin:0 auto;color:#222;line-height:1.45">',
         f'<h1 style="font-size:22px;color:#8a4b14">Orangutan Conservation Dashboard — {esc(month)}</h1>',
         f'<p>New on the <a href="{SITE}">dashboard</a> since the last update: '
         f'{len(incidents)} news report{"s" * (len(incidents) != 1)} and '
         f'{len(literature)} literature entr{"ies" if len(literature) != 1 else "y"}'
         f'{esc(updates_clause)}.</p>']
    t = [f"Orangutan Conservation Dashboard — {month}", "",
         f"New since the last update: {len(incidents)} news reports, {len(literature)} literature entries"
         f"{updates_clause}.",
         f"Dashboard: {SITE}", ""]

    if site_updates:
        h.append('<h2 style="font-size:18px;border-bottom:2px solid #d9a066">Site updates</h2><ul style="padding-left:18px">')
        t += ["SITE UPDATES", ""]
        for r in site_updates:
            h.append(f'<li style="margin-bottom:8px"><b>{esc(r.get("title"))}</b>'
                     f'<br><span style="font-size:13px">{esc(r.get("description"))}</span></li>')
            t += [f"- {r.get('title')}", f"  {r.get('description') or ''}"]
        h.append("</ul>")
        t.append("")

    if species_trends:
        trend_month = pa_alerts.get("month", "") if pa_alerts else ""
        h.append(f'<h2 style="font-size:18px;border-bottom:2px solid #d9a066">Species overview — {esc(trend_month)}</h2>'
                 '<table style="width:100%;border-collapse:collapse;font-size:13px">'
                 '<tr style="text-align:left;border-bottom:1px solid #ccc">'
                 '<th style="padding:4px 6px 4px 0">Species</th>'
                 '<th style="padding:4px 6px">Fire alerts</th>'
                 '<th style="padding:4px 6px">Deforestation alerts</th>'
                 '<th style="padding:4px 6px">Area affected (ha)</th></tr>')
        t += [f"SPECIES OVERVIEW — {trend_month}", ""]
        for s in species_trends:
            label = SPECIES_LABEL.get(s["species"], s["species"])
            fire_cell = trend_str(s["fire_now"], s["fire_prev"])
            defor_cell = trend_str(s["defor_count_now"], s["defor_count_prev"])
            area_cell = f'{s["defor_area_now"]:,}' if s["defor_area_now"] is not None else "no data"
            h.append(f'<tr style="border-bottom:1px solid #eee">'
                     f'<td style="padding:4px 6px 4px 0">{esc(label)}</td>'
                     f'<td style="padding:4px 6px">{esc(fire_cell)}</td>'
                     f'<td style="padding:4px 6px">{esc(defor_cell)}</td>'
                     f'<td style="padding:4px 6px">{esc(area_cell)}</td></tr>')
            t.append(f"- {label}: {fire_cell} fire, {defor_cell} deforestation, {area_cell} ha")
        h.append('</table>'
                 f'<p><a href="{SITE}site/tabs/map.html">Browse past months on the map</a> '
                 '(pick a species, then a month, from the panel in the top right) — '
                 f'or see the <a href="{SITE}site/tabs/protected-areas.html">full per-protected-area breakdown</a>.</p>')
        t += ["", f"Browse past months: {SITE}site/tabs/map.html",
              f"Per-protected-area breakdown: {SITE}site/tabs/protected-areas.html", ""]

    if pa_alerts and pa_alerts.get("places"):
        pa_month = pa_alerts.get("month", "")
        active = [pl for pl in pa_alerts["places"]
                  if (pl.get("fire_count") or 0) > 0 or (pl.get("deforestation_count") or 0) > 0]
        active.sort(key=lambda pl: (pl.get("fire_count") or 0) + (pl.get("deforestation_count") or 0), reverse=True)
        shown_pa = active[:MAX_PA_ALERTS]
        h.append(f'<h2 style="font-size:18px;border-bottom:2px solid #d9a066">Protected areas — {esc(pa_month)}</h2>'
                 '<p style="font-size:13px;color:#666">Fire and deforestation alerts for that month, counted '
                 'inside each protected area\'s boundary (World Database on Protected Areas). Deforestation '
                 'counts come from a complete query per area, not from the map\'s point layer, which can be '
                 'an incomplete sample in very high-alert months — see the dashboard for details.</p>')
        t += [f"PROTECTED AREAS — {pa_month}", ""]
        if shown_pa:
            h.append('<table style="width:100%;border-collapse:collapse;font-size:13px">'
                      '<tr style="text-align:left;border-bottom:1px solid #ccc">'
                      '<th style="padding:4px 6px 4px 0">Protected area</th>'
                      '<th style="padding:4px 6px">Fire alerts</th>'
                      '<th style="padding:4px 6px">Deforestation alerts</th></tr>')
            for pl in shown_pa:
                h.append('<tr style="border-bottom:1px solid #eee">'
                         f'<td style="padding:4px 6px 4px 0">{esc(pl.get("name"))}</td>'
                         f'<td style="padding:4px 6px">{(pl.get("fire_count") or 0):,}</td>'
                         f'<td style="padding:4px 6px">{(pl.get("deforestation_count") or 0):,}</td></tr>')
                t.append(f"- {pl.get('name')}: {pl.get('fire_count') or 0} fire, "
                         f"{pl.get('deforestation_count') or 0} deforestation")
            h.append("</table>")
            if len(active) > len(shown_pa):
                h.append(f"<p>…and {len(active) - len(shown_pa)} more protected area(s) with alerts this month.</p>")
                t.append(f"...and {len(active) - len(shown_pa)} more.")
        else:
            h.append("<p>No fire or deforestation alerts were recorded inside a tracked protected area this month.</p>")
            t.append("No fire or deforestation alerts recorded this month.")
        h.append(f'<p><a href="{SITE}site/tabs/map.html">See the map</a></p>')
        t += ["", f"Map: {SITE}site/tabs/map.html", ""]

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
    incidents, literature, site_updates, pa_alerts, species_trends = current()
    if args.seed:
        save_state(state, incidents, literature, site_updates)
        print(f"Seeded {STATE.name}: {len(incidents)} incidents, {len(literature)} literature entries, "
              f"{len(site_updates)} site updates")
        return
    seen_inc, seen_lit, seen_upd = set(state["incidents"]), set(state["literature"]), set(state["site_updates"])
    new_inc = [r for r in incidents if r["id"] not in seen_inc]
    new_lit, keys = [], set()
    for r in literature:
        k = lit_key(r)
        if k not in seen_lit and k not in keys:
            keys.add(k)
            new_lit.append(r)
    new_upd = [r for r in site_updates if r["id"] not in seen_upd]

    month = dt.date.today().strftime("%B %Y")
    body_html, body_text = render(new_inc, new_lit, new_upd, pa_alerts, species_trends, month)
    summary = {"month": month, "incidents": len(new_inc), "literature": len(new_lit),
               "site_updates": len(new_upd),
               "pa_alerts_month": (pa_alerts or {}).get("month"),
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
        save_state(state, new_inc, new_lit, new_upd)


if __name__ == "__main__":
    main()
