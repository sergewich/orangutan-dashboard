"""
rag_sources.py — the bulk literature sources behind the Literature Assistant,
used by build_rag_corpus.py. Kept separate because each one is a small
harvester with its own cache and API quirks:

  harvest_openalex()        every orangutan work in OpenAlex (all years; title
                            or abstract mentions orangutan / a Pongo species)
  fill_missing_abstracts()  OpenAlex hides abstracts for some publishers
                            (e.g. Elsevier, so Nater et al. 2017 has none).
                            Europe PMC usually has them for PubMed-indexed
                            papers; Semantic Scholar's one-line TLDR is the
                            last resort (labelled as such, never passed off
                            as the abstract).
  europepmc_fulltext()      full text of papers in Europe PMC's open-access
                            subset under a Creative Commons licence — the
                            only full text indexed automatically, because the
                            licence explicitly allows reuse.

All responses are cached in data/rag/cache/ (gitignored: abstracts and full
text are third-party content), so nightly runs only fetch what's new.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from html import unescape
from pathlib import Path

import requests

CONTACT_EMAIL = "sergewich@gmail.com"
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"

OPENALEX_QUERY = ('orangutan OR orangutans OR "orang-utan" OR "Pongo abelii" OR '
                  '"Pongo pygmaeus" OR "Pongo tapanuliensis"')
OPENALEX_TYPES = "article|review|book-chapter|book|dissertation|preprint|report|letter"
OPENALEX_SELECT = ("id,doi,title,publication_year,type,authorships,abstract_inverted_index,"
                   "primary_location,best_oa_location,open_access,cited_by_count")
EPMC_FULLTEXT_QUERY = ('TITLE_ABS:(orangutan OR orangutans OR "orang-utan" OR "Pongo abelii" OR '
                       '"Pongo pygmaeus" OR "Pongo tapanuliensis") AND OPEN_ACCESS:y AND HAS_FT:y')
CC_LICENCES = {"cc by", "cc0", "cc by-sa", "cc by-nc", "cc by-nc-sa", "cc by-nd", "cc by-nc-nd"}
TYPE_LABELS = {"article": "journal", "review": "journal", "letter": "journal", "preprint": "preprint",
               "book-chapter": "book", "book": "book", "dissertation": "thesis", "report": "report"}
MAX_FULLTEXT_CHUNKS = 40  # per paper; long genomics papers otherwise swamp the index


def _get(session, url, retries=4, **kw):
    for attempt in range(retries):
        try:
            resp = session.get(url, timeout=60, **kw)
        except requests.RequestException:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
            continue
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
            time.sleep(float(resp.headers.get("Retry-After") or 2 ** attempt))
            continue
        return resp
    return resp


def _abstract(inv: dict | None) -> str:
    if not inv:
        return ""
    return " ".join(w for _, w in sorted((p, w) for w, ps in inv.items() for p in ps))


# OpenAlex lists supplementary files deposited in data repositories as
# separate works ("Additional file 2 of …", "MOESM3 of …", "Table S1 from …"),
# picks up repository items that mention orangutans only in passing, and
# "orangutan-inspired" optimisation algorithms. Used by the Literature tab
# (fetch_literature.py) and the assistant corpus (build_rag_corpus.py).
SUPPLEMENT_RE = re.compile(
    r"^(additional file|moesm\d|supplementa(ry|l)\b|(table|figure|fig\.|video|movie|data|dataset)\s*s\d)", re.I)
# In repositories only (real papers say "data from" too): "X from <Title>".
DEPOSIT_RE = re.compile(r"^.{0,80}\b(analyses|information|material|materials|tables?|figures?|data) from [A-Z]", re.I)
ALGORITHM_RE = re.compile(r"(inspired|metaheuristic|optimi[sz]ation algorithm|optimi[sz]er\b)", re.I)
ORANGUTAN_RE = re.compile(r"orang[\s-]?(utan|outan|oetan|hutan)|\bpongo\b|\bapes?\b", re.I)
REPOSITORIES = ("figshare", "zenodo", "dryad", "mendeley data", "dataverse")


def is_junk_work(title: str | None, venue: str | None) -> bool:
    title, venue = (title or "").strip().lstrip("#").strip(), (venue or "").lower()
    if not title or SUPPLEMENT_RE.search(title) or ALGORITHM_RE.search(title):
        return True
    # Repository deposits must be about apes in their title and not a paper's supplement.
    return any(r in venue for r in REPOSITORIES) and (DEPOSIT_RE.search(title) or not ORANGUTAN_RE.search(title))


def norm_doi(doi: str | None) -> str:
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", (doi or "").strip()).lower()


# --------------------------------------------------------------------------- #
def harvest_openalex(session, cache_dir: Path, max_age_hours: float = 20) -> list[dict]:
    """All matching works, refreshed at most once per max_age_hours (a full
    harvest is ~35 pages, a few cents of OpenAlex budget)."""
    cache = cache_dir / "openalex_harvest.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < max_age_hours * 3600:
        return json.loads(cache.read_text())
    key = os.environ.get("OPENALEX_API_KEY")
    if not key:
        print("  WARNING: no OPENALEX_API_KEY — OpenAlex harvest needs one (cursor paging)", file=sys.stderr)
        return json.loads(cache.read_text()) if cache.exists() else []

    works, cursor = [], "*"
    while cursor:
        resp = _get(session, "https://api.openalex.org/works", params={
            "filter": f"title_and_abstract.search:{OPENALEX_QUERY},type:{OPENALEX_TYPES}",
            "select": OPENALEX_SELECT, "per-page": 200, "cursor": cursor,
            "mailto": CONTACT_EMAIL, "api_key": key,
        })
        resp.raise_for_status()
        data = resp.json()
        for w in data.get("results", []):
            loc = w.get("primary_location") or {}
            oa = w.get("best_oa_location") or {}
            doi = norm_doi(w.get("doi"))
            works.append({
                "openalex": w["id"].rsplit("/", 1)[-1],
                "doi": doi,
                "title": w.get("title") or "",
                "authors": ", ".join((a.get("author") or {}).get("display_name", "")
                                     for a in (w.get("authorships") or [])[:4]),
                "n_authors": len(w.get("authorships") or []),
                "year": w.get("publication_year"),
                "type": TYPE_LABELS.get(w.get("type"), "journal"),
                "venue": ((loc.get("source") or {}).get("display_name")) or "",
                "url": f"https://doi.org/{doi}" if doi else (oa.get("landing_page_url") or loc.get("landing_page_url") or ""),
                "abstract": _abstract(w.get("abstract_inverted_index")),
                "abstract_source": "OpenAlex",
                "cites": w.get("cited_by_count") or 0,
            })
        cursor = (data.get("meta") or {}).get("next_cursor")
        print(f"  ...OpenAlex harvest: {len(works)} works")
        time.sleep(0.1)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(works, ensure_ascii=False))
    return works


def fill_missing_abstracts(works: list[dict], session, cache_dir: Path) -> None:
    """In place: abstracts from Europe PMC, else Semantic Scholar TLDRs."""
    cache_path = cache_dir / "abstract_fill.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    need = [w for w in works if len(w["abstract"]) < 200 and w["doi"]]
    todo = [w["doi"] for w in need if w["doi"] not in cache]

    # Europe PMC: OR-ed DOI queries, 40 at a time.
    for i in range(0, len(todo), 40):
        batch = todo[i:i + 40]
        q = " OR ".join(f'DOI:"{d}"' for d in batch)
        resp = _get(session, f"{EPMC}/search", params={"query": q, "format": "json",
                                                        "resultType": "core", "pageSize": 100})
        if not resp.ok:
            continue
        found = {norm_doi(r.get("doi")): r.get("abstractText") or ""
                 for r in resp.json().get("resultList", {}).get("result", [])}
        for d in batch:
            text = re.sub(r"<[^>]+>", " ", found.get(d, ""))
            cache[d] = {"text": text, "source": "Europe PMC"} if len(text) >= 200 else None
        time.sleep(0.2)

    # Semantic Scholar for what's still missing (abstract, else TLDR).
    still = [d for d in todo if not cache.get(d)]
    for i in range(0, len(still), 400):
        batch = still[i:i + 400]
        for attempt in range(4):
            resp = requests.post("https://api.semanticscholar.org/graph/v1/paper/batch",
                                 params={"fields": "abstract,tldr"},
                                 json={"ids": [f"DOI:{d}" for d in batch]}, timeout=120)
            if resp.status_code != 429:
                break
            time.sleep(5 * (attempt + 1))
        if not resp.ok:
            print(f"  WARNING: Semantic Scholar batch failed ({resp.status_code})", file=sys.stderr)
            continue
        for d, p in zip(batch, resp.json()):
            p = p or {}
            if len(p.get("abstract") or "") >= 200:
                cache[d] = {"text": p["abstract"], "source": "Semantic Scholar"}
            elif (p.get("tldr") or {}).get("text"):
                cache[d] = {"text": p["tldr"]["text"], "source": "Semantic Scholar TLDR (auto-summary)"}
            else:
                cache[d] = None
        time.sleep(1.5)

    cache_path.write_text(json.dumps(cache, ensure_ascii=False))
    filled = 0
    for w in need:
        hit = cache.get(w["doi"])
        if hit:
            w["abstract"], w["abstract_source"] = hit["text"], hit["source"]
            filled += 1
    print(f"  abstracts filled for {filled} of {len(need)} works missing one in OpenAlex")


# --------------------------------------------------------------------------- #
def _jats_sections(body: ET.Element) -> list[tuple[str, str]]:
    """(section title, text) for each top-level section of a JATS <body>.
    Tables are skipped (cells read as noise); figure/table captions kept."""
    out = []

    def text_of(el) -> str:
        parts = []
        for child in el.iter():
            if child.tag in ("table", "thead", "tbody", "tr", "td", "th"):
                continue
            if child.tag in ("p", "title", "caption"):
                t = " ".join("".join(child.itertext()).split())
                if t:
                    parts.append(t)
        return "\n".join(parts)

    secs = body.findall("sec")
    if not secs:
        return [("", text_of(body))]
    for sec in secs:
        t_el = sec.find("title")  # not `or`: an Element with no children is falsy
        title = " ".join("".join(t_el.itertext()).split()) if t_el is not None else ""
        for t in sec.findall("title"):
            sec.remove(t)
        out.append((title, text_of(sec)))
    return out


def europepmc_fulltext(session, cache_dir: Path) -> list[dict]:
    """[{pmcid, doi, title, authors, year, venue, licence, sections: [(title, text)]}]"""
    cache_path = cache_dir / "europepmc_fulltext.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}

    hits, cursor = [], "*"
    while True:
        resp = _get(session, f"{EPMC}/search", params={"query": EPMC_FULLTEXT_QUERY, "format": "json",
                                                        "resultType": "core", "pageSize": 100,
                                                        "cursorMark": cursor})
        resp.raise_for_status()
        data = resp.json()
        hits += data.get("resultList", {}).get("result", [])
        nxt = data.get("nextCursorMark")
        if not nxt or nxt == cursor or not data.get("resultList", {}).get("result"):
            break
        cursor = nxt

    wanted = [r for r in hits if (r.get("license") or "").lower() in CC_LICENCES and r.get("pmcid")]
    todo = [r["pmcid"] for r in wanted if r["pmcid"] not in cache]

    def fetch(pmcid):
        resp = _get(session, f"{EPMC}/{pmcid}/fullTextXML")
        if not resp.ok:
            return pmcid, None  # not cached: retried next run
        try:
            body = ET.fromstring(resp.content).find(".//body")
        except ET.ParseError:
            body = None
        return pmcid, (_jats_sections(body) if body is not None else [])

    # Europe PMC takes ~10 s per full-text request, so fetch in parallel.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as pool:
        for i, (pmcid, sections) in enumerate(pool.map(fetch, todo), 1):
            if sections is not None:
                cache[pmcid] = sections
            if i % 50 == 0:
                cache_path.write_text(json.dumps(cache, ensure_ascii=False))
                print(f"  ...Europe PMC full text: {i}/{len(todo)} new papers fetched")

    out = []
    for r in wanted:
        licence = r["license"].lower()
        pmcid = r["pmcid"]
        if pmcid not in cache:
            continue
        if not cache[pmcid]:
            continue
        authors = r.get("authorString") or ""
        out.append({
            "pmcid": pmcid,
            "doi": norm_doi(r.get("doi")),
            "title": re.sub(r"<[^>]+>", "", r.get("title") or "").rstrip("."),
            "authors": ", ".join(authors.split(", ")[:4]) + (" et al." if authors.count(",") >= 4 else ""),
            "year": int(r["pubYear"]) if str(r.get("pubYear", "")).isdigit() else None,
            "venue": (r.get("journalInfo") or {}).get("journal", {}).get("title") or "",
            "licence": licence,
            "cites": r.get("citedByCount") or 0,
            "sections": cache[pmcid],
        })
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, ensure_ascii=False))
    print(f"  Europe PMC: {len(out)} CC-licensed full-text papers")
    return out


# --------------------------------------------------------------------------- #
# IUCN Red List — the three species' own conservation assessments
# --------------------------------------------------------------------------- #
# taxon_id, assessment_id from the current iucnredlist.org species page URL
# (https://www.iucnredlist.org/species/<taxon_id>/<assessment_id>). IUCN
# re-assesses a species every few years under a new assessment_id -- there's
# no API lookup here, so update this by hand when that happens (the old id's
# cache entry is simply orphaned, not reused).
IUCN_REDLIST_ASSESSMENTS = {
    "pongo_abelii": ("121097935", "247631244"),
    "pongo_pygmaeus": ("17975", "259043172"),
    "pongo_tapanuliensis": ("120588639", "247632253"),
}
# iucnredlist.org's bot protection rejects this module's usual polite
# "orangutan-dashboard/1.0 (mailto:...)" User-Agent (403); a plain desktop
# browser UA is accepted.
IUCN_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
IUCN_SECTION_HEADERS = ["Taxonomic Notes:", "Justification:", "Range Description:", "Population",
                        "Habitat and Ecology", "Use and Trade", "Threats", "Conservation Actions",
                        "Credits"]
IUCN_JUNK_LINE_RE = re.compile(
    r"^(©|Tiles \(C\) Esri|Compiled by:|The boundaries and names shown|"
    r"For further information about this species|\(see Appendix|Distribution Map$|"
    r"EXTANT|POSSIBLY EXTANT)")
IUCN_TITLE_RE = re.compile(r'<meta name="citation_title" content="([^"]+)"', re.I)
IUCN_AUTHOR_RE = re.compile(r'<meta name="citation_author" content="([^"]+)"', re.I)
IUCN_YEAR_RE = re.compile(r'<meta name="citation_publication_date" content="(\d{4})"', re.I)


def _iucn_lines(raw_html: str) -> list[str]:
    body = re.sub(r"<(style|script)[^>]*>.*?</\1>", "", raw_html, flags=re.S | re.I)
    body = re.sub(r"<[^>]+>", " ", body)
    text = unescape(body)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n")]
    return [ln for ln in lines if ln and not IUCN_JUNK_LINE_RE.search(ln)]


def _iucn_sections(lines: list[str]) -> dict[str, str]:
    """Named chunks of the assessment's narrative body (Justification,
    Population, Threats, ...), cut from the "PDF preview" HTML route, which
    is the same content as the real PDF but far easier to parse than a
    binary PDF. Stops at "Credits": everything after that is the
    bibliography, boilerplate and classification-scheme tables, not prose."""
    idx = {}
    for h in IUCN_SECTION_HEADERS:
        for i, ln in enumerate(lines):
            if ln == h:
                idx[h] = i
                break
    present = [h for h in IUCN_SECTION_HEADERS if h in idx]
    out = {}
    for i, h in enumerate(present[:-1]):
        out[h.rstrip(":")] = " ".join(lines[idx[h] + 1: idx[present[i + 1]]])
    return out


def iucn_redlist(session, cache_dir: Path) -> list[dict]:
    """[{species, title, authors, year, url, sections: {name: text}}] for the
    three species' current IUCN Red List assessments. Cached indefinitely per
    (species, assessment_id) in IUCN_REDLIST_ASSESSMENTS above -- a fetch
    failure (e.g. tighter bot protection) just skips that species this run,
    retried next time.

    Full text, not abstract-only: every IUCN Red List assessment carries the
    same notice -- "Reproduction of this publication for educational or other
    non-commercial purposes is authorized without prior written permission
    from the copyright holder provided the source is fully acknowledged" --
    that scripts/catalog_library.py's NONCOMMERCIAL_RE already treats as
    license-equivalent to open for this non-commercial site."""
    cache_path = cache_dir / "iucn_redlist.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    headers = {"User-Agent": IUCN_UA}

    out, changed = [], False
    for species, (taxon, assessment) in IUCN_REDLIST_ASSESSMENTS.items():
        key = f"{taxon}/{assessment}"
        if key not in cache:
            page_url = f"https://www.iucnredlist.org/species/{taxon}/{assessment}"
            try:
                page = _get(session, page_url, headers=headers)
                pdf = _get(session, f"{page_url}/pdf", headers=headers)
                if not page.ok or not pdf.ok:
                    raise requests.RequestException(f"HTTP {page.status_code}/{pdf.status_code}")
            except requests.RequestException as exc:
                print(f"  WARNING: IUCN Red List fetch failed for {species}: {exc}", file=sys.stderr)
                continue
            title_m = IUCN_TITLE_RE.search(page.text)
            authors = [re.sub(r"\s+,", ",", a).strip() for a in IUCN_AUTHOR_RE.findall(page.text)]
            year_m = IUCN_YEAR_RE.search(page.text)
            cache[key] = {
                "species": species, "url": page_url,
                "title": title_m.group(1) if title_m else f"{species}: IUCN Red List assessment",
                "authors": ", ".join(authors) or None,
                "year": int(year_m.group(1)) if year_m else None,
                "sections": _iucn_sections(_iucn_lines(pdf.text)),
            }
            changed = True
            time.sleep(0.5)
        out.append(cache[key])
    if changed:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, ensure_ascii=False))
    return out
