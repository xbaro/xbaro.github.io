#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["python-docx"]
# ///
"""Structured, indexed memory of the site's reference content.

Lives under _memory/ (underscore dirs are NOT published by Jekyll).

  uv run _memory/tools/memory.py sync      # fetch ORCID + OpenAlex, merge -> references.json
  uv run _memory/tools/memory.py sync --offline   # re-merge from cached sources only
  uv run _memory/tools/memory.py index     # rebuild SQLite FTS index
  uv run _memory/tools/memory.py search "domain adaptation"
  uv run _memory/tools/memory.py stats
"""
import argparse
import difflib
import json
import re
import sqlite3
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ORCID = "0000-0001-5338-3007"
ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "sources"
REFS = ROOT / "references.json"
OVERRIDES = ROOT / "overrides.json"
INDEX = ROOT / "index.sqlite"

TYPE_MAP = {
    "journal-article": "journal-article", "article": "journal-article",
    "conference-paper": "conference-paper", "proceedings-article": "conference-paper",
    "preprint": "preprint", "book-chapter": "book-chapter", "book": "book",
    "report": "report", "dissertation": "thesis", "dataset": "dataset",
}


def http_json(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": "personal-site-memory", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code} for {url.split('?')[0]}: {e.read()[:300].decode(errors='replace')}")


def norm_doi(d):
    if not d:
        return None
    d = d.strip().lower()
    d = re.sub(r"^https?://(dx\.)?doi\.org/", "", d)
    return d or None


def norm_title(t):
    t = unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def fetch():
    SRC.mkdir(parents=True, exist_ok=True)
    orcid = http_json(f"https://pub.orcid.org/v3.0/{ORCID}/works", {"Accept": "application/json"})
    (SRC / "orcid.json").write_text(json.dumps(orcid, indent=1, ensure_ascii=False))
    fields = ("id,doi,title,publication_year,publication_date,type,cited_by_count,"
              "authorships,primary_location,open_access,abstract_inverted_index")
    works, n = [], 1
    while True:  # basic page= paging works without an API key (cursor= does not)
        q = urllib.parse.urlencode({
            "filter": f"author.orcid:https://orcid.org/{ORCID}",
            "per-page": 100, "page": n, "select": fields})
        page = http_json(f"https://api.openalex.org/works?{q}")
        works += page["results"]
        if len(works) >= page["meta"]["count"] or not page["results"]:
            break
        n += 1
    (SRC / "openalex.json").write_text(json.dumps(works, indent=1, ensure_ascii=False))
    print(f"fetched: orcid={len(orcid.get('group', []))} openalex={len(works)}")


def abstract_text(inv):
    if not inv:
        return None
    pos = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos))


def load_orcid():
    out = []
    for g in json.loads((SRC / "orcid.json").read_text()).get("group", []):
        w = g["work-summary"][0]
        ids = {e["external-id-type"]: e["external-id-value"]
               for e in (g.get("external-ids") or {}).get("external-id", [])}
        year = ((w.get("publication-date") or {}).get("year") or {}).get("value")
        out.append({
            "title": (w.get("title") or {}).get("title", {}).get("value"),
            "venue": (w.get("journal-title") or {}).get("value"),
            "type": (w.get("type") or "").lower().replace("_", "-"),
            "year": int(year) if year else None,
            "doi": norm_doi(ids.get("doi")),
            "url": (w.get("url") or {}).get("value"),
            "orcid_put_code": w.get("put-code"),
        })
    return out


def load_openalex():
    out = []
    for w in json.loads((SRC / "openalex.json").read_text()):
        loc = w.get("primary_location") or {}
        out.append({
            "title": w.get("title"),
            "venue": (loc.get("source") or {}).get("display_name"),
            "type": w.get("type") or "",
            "year": w.get("publication_year"),
            "date": w.get("publication_date"),
            "doi": norm_doi(w.get("doi")),
            "url": (w.get("open_access") or {}).get("oa_url") or loc.get("landing_page_url"),
            "authors": [a["author"]["display_name"] for a in w.get("authorships", [])],
            "cited_by": w.get("cited_by_count"),
            "abstract": abstract_text(w.get("abstract_inverted_index")),
            "openalex_id": w.get("id"),
            "issn_l": (loc.get("source") or {}).get("issn_l"),
        })
    return out


CV_SECTIONS = {  # AQU normalised CV: section heading -> (reference type, label)
    "B.1": ("journal-article", "indexed journal"), "B.2": ("journal-article", "non-indexed journal"),
    "B.3": (None, "book / chapter"), "B.4": ("report", "other publication"),
    "B.5": ("conference-paper", "conference proceedings"),
}
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s,;]+")


def docx_text(path):
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    d, out = docx.Document(str(path)), []
    for el in d.element.body.iterchildren():
        if el.tag.endswith("}p"):
            out.append(Paragraph(el, d).text)
        elif el.tag.endswith("}tbl"):
            for row in Table(el, d).rows:
                cells = []
                for c in row.cells:
                    t = c.text.strip().replace("\n", " / ")
                    if t and (not cells or cells[-1] != t):
                        cells.append(t)
                out.append(" | ".join(cells))
    return out


def import_cv(path):
    """Extract ONLY publications (B) and conference talks (E) from an AQU CV.
    Personal data in the CV (ID number, address, birth date...) is never read into the repo."""
    lines = docx_text(path) if str(path).endswith(".docx") else Path(path).read_text().splitlines()
    pubs, talks, sec, rec = [], [], None, []

    def flush():
        nonlocal rec
        if rec:
            text = " / ".join(x.strip() for x in rec if x.strip())
            fields = {}
            for part in re.split(r"\s/\s", text):
                if ":" in part:
                    k, v = part.split(":", 1)
                    fields.setdefault(k.strip(), v.strip())
            get = lambda *keys: next((v for k, v in fields.items() if any(k.lower().startswith(x.lower()) for x in keys) and v), None)
            title = get("Títol del capítol") or get("Títol")
            if title:
                yr = get("Any")
                m = re.search(r"\b(19|20)\d{2}\b", yr or "")
                r = {"title": title, "authors_cv": get("Autors"), "year": int(m.group()) if m else None,
                     "venue": get("Revista", "Llibre d", "Capítol publicat", "Congrés", "Títol del llibre"),
                     "doi": norm_doi((DOI_RE.search(text) or [None])[0]) if DOI_RE.search(text) else None,
                     "cv_section": sec}
                if sec == "E":
                    r.update({"contribution": get("Tipus de contribució"), "place": get("Lloc")})
                    talks.append(r)
                else:
                    kind = CV_SECTIONS[sec][0] or ("book" if (get("Clau (L") or "").strip().upper().startswith("L") else "book-chapter")
                    r["type"] = kind
                    pubs.append(r)
        rec = []

    for ln in lines:
        m = re.match(r"^(B\.[1-5]|E)\.?\s", ln)
        if m and (ln.startswith("B.") or ln.startswith("E. Congressos")):
            flush()
            sec = m.group(1)
            continue
        if sec is None or re.match(r"^[A-D]\.\d?\s", ln) or ln.startswith(("C. ", "D. ")):
            if ln.startswith(("C. ", "D. ")):
                flush(); sec = None
            continue
        if ln.startswith("Autors/"):
            flush(); rec = [ln]
        elif ln.startswith("Altres indicis") and pubs and rec:
            c = re.search(r"Google Scholar:\s*(\d+)", ln)
            flush()
            if c and pubs:
                pubs[-1]["scholar_cited_by"] = int(c.group(1))
        elif rec:
            rec.append(ln)
    flush()
    SRC.mkdir(parents=True, exist_ok=True)
    (SRC / "cv_publications.json").write_text(json.dumps(pubs, indent=1, ensure_ascii=False) + "\n")
    (SRC / "cv_talks.json").write_text(json.dumps(talks, indent=1, ensure_ascii=False) + "\n")
    print(f"cv: {len(pubs)} publications, {len(talks)} talks -> sources/cv_*.json")


_ACC = {"'": "\u0301", "`": "\u0300", '"': "\u0308", "^": "\u0302", "~": "\u0303", "=": "\u0304",
        ".": "\u0307", "c": "\u0327", "v": "\u030c", "u": "\u0306", "H": "\u030b"}


def tex_decode(s):
    s = re.sub(r"\\([\'`\"^~=.cvuH])\s*\{?\s*(\\i|\\j|[A-Za-z])\s*\}?",
               lambda m: ("i" if m.group(2) == "\\i" else "j" if m.group(2) == "\\j" else m.group(2)) + _ACC[m.group(1)], s)
    s = s.replace("\\ss", "ß").replace("\\&", "&").replace("\\o", "ø").replace("\\O", "Ø").replace("\\l", "ł")
    s = re.sub(r"[{}]", "", s)
    return unicodedata.normalize("NFC", re.sub(r"\s+", " ", s)).strip()


def parse_bibtex(text):
    out, i = [], 0
    for m in re.finditer(r"@(\w+)\s*\{\s*([^,\s]+)\s*,", text):
        if m.start() < i:
            continue
        depth, j = 1, m.end()
        while j < len(text) and depth:
            depth += {"{": 1, "}": -1}.get(text[j], 0)
            j += 1
        body, i = text[m.end():j - 1], j
        fields = {}
        for fm in re.finditer(r"(\w+)\s*=\s*", body):
            k, p = fm.group(1).lower(), fm.end()
            if p < len(body) and body[p] == "{":
                d, q = 1, p + 1
                while q < len(body) and d:
                    d += {"{": 1, "}": -1}.get(body[q], 0)
                    q += 1
                fields[k] = body[p + 1:q - 1]
            else:
                fields[k] = re.match(r"[^,}\s]*", body[p:]).group()
        out.append((m.group(1).lower(), m.group(2), fields))
    return out


def load_scholar():
    p = SRC / "scholar.bib"
    if not p.exists():
        return []
    kinds = {"article": "journal-article", "inproceedings": "conference-paper", "incollection": "book-chapter",
             "book": "book", "phdthesis": "thesis", "mastersthesis": "thesis", "misc": "other"}
    out = []
    for kind, key, f in parse_bibtex(p.read_text()):
        title = tex_decode(f.get("title", ""))
        if not title:
            continue
        authors = []
        for a in re.split(r"\s+and\s+", tex_decode(f.get("author", ""))):
            a = a.strip()
            if "," in a:
                last, first = a.split(",", 1)
                a = f"{first.strip()} {last.strip()}"
            if a and a != "others":
                authors.append(a)
        out.append({"title": title, "type": kinds.get(kind, "other"), "authors": authors,
                    "year": int(f["year"]) if f.get("year", "").isdigit() else None,
                    "venue": tex_decode(f.get("journal") or f.get("booktitle") or f.get("publisher") or "") or None,
                    "scholar_key": key})
    return out


def load_cv():
    p = SRC / "cv_publications.json"
    return json.loads(p.read_text()) if p.exists() else []


def ref_id(rec):
    return f"doi:{rec['doi']}" if rec.get("doi") else f"t:{norm_title(rec['title'])[:60].replace(' ', '-')}-{rec.get('year') or 'nd'}"


def merge():
    refs = {}
    by_title = {}

    def add(rec, source):
        key = None
        if rec.get("doi") and f"doi:{rec['doi']}" in refs:
            key = f"doi:{rec['doi']}"
        elif norm_title(rec["title"]) in by_title:
            key = by_title[norm_title(rec["title"])]
        else:  # fuzzy: near-identical title, year within 1
            nt = norm_title(rec["title"])
            for t, k in by_title.items():
                ry = refs[k].get("year")
                if (not ry or not rec.get("year") or abs(ry - rec["year"]) <= 1) and difflib.SequenceMatcher(None, nt, t).ratio() >= 0.92:
                    key = k
                    break
        if key is None:
            key = ref_id(rec)
            refs[key] = {"id": key, "status": "candidate", "sources": []}
            by_title[norm_title(rec["title"])] = key
        r = refs[key]
        if source not in r["sources"]:
            r["sources"].append(source)
        for k, v in rec.items():
            if v not in (None, "", []) and not r.get(k):
                r[k] = v
        if r.get("type"):
            r["type"] = TYPE_MAP.get(r["type"], "other")
        return r

    for rec in load_orcid():           # ORCID first: authorship confirmed by the owner
        if rec["title"]:
            add(rec, "orcid")["status"] = "confirmed"
    for rec in load_openalex():
        if rec["title"]:
            add(rec, "openalex")
    for rec in load_scholar():         # the owner's Google Scholar profile (manual BibTeX export)
        add(rec, "scholar")["status"] = "confirmed"
    for rec in load_cv():              # the owner's own CV: authoritative, like ORCID
        r = add({k: v for k, v in rec.items() if k not in ("authors_cv", "cv_section", "scholar_cited_by")}, "cv")
        r["status"] = "confirmed"
        if rec.get("scholar_cited_by") is not None:
            r["scholar_cited_by"] = rec["scholar_cited_by"]
        if not r.get("authors") and rec.get("authors_cv"):
            r["authors"] = [a.strip() for a in re.split(r";|,\s(?=[A-ZÁÉÍÓÚ])", rec["authors_cv"]) if a.strip()]

    if METRICS.exists():               # attach last computed metrics (see `metrics` command)
        for k, m in json.loads(METRICS.read_text()).items():
            if k in refs:
                refs[k]["metrics"] = m
    overrides = json.loads(OVERRIDES.read_text()) if OVERRIDES.exists() else {}
    for key, ov in overrides.items():
        if key in refs:
            refs[key].update({k: v for k, v in ov.items()})
    items = sorted(refs.values(), key=lambda r: (-(r.get("year") or 0), r["title"].lower()))
    REFS.write_text(json.dumps(items, indent=1, ensure_ascii=False) + "\n")
    c = sum(1 for r in items if r["status"] == "confirmed")
    print(f"references.json: {len(items)} total, {c} confirmed, {len(items) - c} candidates")


BIB = ROOT / "references.bib"
METRICS = ROOT / "metrics.json"
BIB_TYPES = {"journal-article": "article", "conference-paper": "inproceedings", "book-chapter": "incollection",
             "book": "book", "thesis": "phdthesis", "report": "techreport", "preprint": "misc", "other": "misc"}


def _bib_esc(s):
    return re.sub(r"([&%#_$])", r"\\\1", s or "")


def write_bib():
    """Canonical BibTeX (UTF-8) of all confirmed references, for easy export/reuse."""
    items = [r for r in json.loads(REFS.read_text()) if r["status"] == "confirmed"]
    used, out = set(), []
    for r in items:
        first = (r.get("authors") or ["anon"])[0].split()[-1]
        base = re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", first).encode("ascii", "ignore").decode().lower())
        word = next((w for w in norm_title(r["title"]).split() if len(w) > 3), "x")
        key = f"{base}{r.get('year') or 'nd'}{word}"
        while key in used:
            key += "b"
        used.add(key)
        kind = BIB_TYPES.get(r.get("type"), "misc")
        venue_field = {"article": "journal", "inproceedings": "booktitle", "incollection": "booktitle"}.get(kind, "howpublished")
        f = [("title", "{" + _bib_esc(r["title"]) + "}"), ("author", _bib_esc(" and ".join(r.get("authors", [])))),
             ("year", str(r["year"]) if r.get("year") else None), (venue_field, _bib_esc(r.get("venue"))),
             ("doi", r.get("doi")), ("url", None if r.get("doi") else r.get("url"))]
        body = ",\n".join(f"  {k} = {{{v}}}" for k, v in f if v)
        out.append(f"@{kind}{{{key},\n{body}\n}}")
    BIB.write_text("\n\n".join(out) + "\n")
    print(f"references.bib: {len(out)} confirmed entries")


def _norm_issn(s):
    return re.sub(r"[^0-9X]", "", (s or "").upper())


def update_metrics(refresh=False):
    """Citations (Crossref, OpenAlex) + journal indicators (OpenAlex 2yr mean citedness, optional SCImago quartile)."""
    import csv
    import datetime
    import time
    refs = [r for r in json.loads(REFS.read_text()) if r["status"] == "confirmed"]
    cache_p = SRC / "crossref.json"
    cache = json.loads(cache_p.read_text()) if cache_p.exists() else {}
    for r in refs:
        d = r.get("doi")
        if d and (refresh or d not in cache):
            try:
                msg = http_json(f"https://api.crossref.org/works/{urllib.parse.quote(d)}")["message"]
                cache[d] = {"cited_by": msg.get("is-referenced-by-count"), "issn": msg.get("ISSN", [])}
            except SystemExit:
                cache[d] = {"cited_by": None, "issn": []}
            time.sleep(0.15)
    cache_p.write_text(json.dumps(cache, indent=1))
    # ISSNs per journal ref: OpenAlex issn_l / Crossref ISSN list
    issns = {}
    for r in refs:
        if r.get("type") == "journal-article":
            s = {_norm_issn(x) for x in ([r.get("issn_l")] + cache.get(r.get("doi"), {}).get("issn", [])) if x}
            if s:
                issns[r["id"]] = s
    allissn = sorted({i for s in issns.values() for i in s})
    jstats = {}
    for n in range(0, len(allissn), 40):
        batch = allissn[n:n + 40]
        q = urllib.parse.urlencode({"filter": "issn:" + "|".join(f"{i[:4]}-{i[4:]}" for i in batch),
                                    "per-page": 100, "select": "display_name,issn,issn_l,summary_stats"})
        for s in http_json(f"https://api.openalex.org/sources?{q}")["results"]:
            st = s.get("summary_stats") or {}
            for i in {_norm_issn(x) for x in (s.get("issn") or [])}:
                jstats[i] = {"journal": s["display_name"], "oa_2yr_mean_citedness": st.get("2yr_mean_citedness"),
                             "oa_h_index": st.get("h_index")}
    sjr = {}   # optional manual file(s): sources/scimago_<year>.csv (SCImago 'Download data', ';'-separated)
    for p in sorted(SRC.glob("scimago_*.csv")):
        year = re.search(r"(\d{4})", p.name).group(1)
        with open(p, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh, delimiter=";"):
                for i in re.split(r",\s*", row.get("Issn", "")):
                    sjr[_norm_issn(i)] = {"year": year, "sjr": row.get("SJR"), "quartile": row.get("SJR Best Quartile")}
    out = {}
    for r in refs:
        m = {"as_of": datetime.date.today().isoformat()}
        if r.get("cited_by") is not None:
            m["cited_by_openalex"] = r["cited_by"]
        if cache.get(r.get("doi"), {}).get("cited_by") is not None:
            m["cited_by_crossref"] = cache[r["doi"]]["cited_by"]
        if r.get("scholar_cited_by") is not None:
            m["cited_by_scholar_cv_2024"] = r["scholar_cited_by"]
        for i in issns.get(r["id"], ()):
            if i in jstats and "journal" not in m:
                m.update(jstats[i])
            if i in sjr and "sjr_quartile" not in m:
                m.update({"sjr": sjr[i]["sjr"], "sjr_quartile": sjr[i]["quartile"], "sjr_year": sjr[i]["year"]})
        out[r["id"]] = m
    METRICS.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    got = lambda k: sum(1 for m in out.values() if k in m)
    print(f"metrics.json: {len(out)} refs | crossref cites {got('cited_by_crossref')} | openalex cites {got('cited_by_openalex')} | "
          f"journal indicators {got('journal')} | SJR quartile {got('sjr_quartile')} ({len(sjr)} ISSNs from scimago files)")


def build_index():
    items = json.loads(REFS.read_text())
    if INDEX.exists():
        INDEX.unlink()
    db = sqlite3.connect(INDEX)
    db.execute("CREATE TABLE refs(id TEXT PRIMARY KEY, status TEXT, type TEXT, year INT, venue TEXT, doi TEXT, cited_by INT, json TEXT)")
    db.execute("CREATE VIRTUAL TABLE fts USING fts5(id UNINDEXED, title, authors, venue, abstract, tags, tokenize='unicode61 remove_diacritics 2')")
    for r in items:
        db.execute("INSERT INTO refs VALUES(?,?,?,?,?,?,?,?)", (
            r["id"], r["status"], r.get("type"), r.get("year"), r.get("venue"), r.get("doi"),
            r.get("cited_by"), json.dumps(r, ensure_ascii=False)))
        db.execute("INSERT INTO fts VALUES(?,?,?,?,?,?)", (
            r["id"], r["title"], ", ".join(r.get("authors", [])), r.get("venue") or "",
            r.get("abstract") or "", " ".join(r.get("tags", []))))
    db.commit()
    print(f"index.sqlite: {len(items)} references indexed")


def search(q, limit=15):
    db = sqlite3.connect(INDEX)
    rows = db.execute(
        "SELECT r.year, r.status, r.type, substr(r.json,1,0)||f.title FROM fts f JOIN refs r ON r.id=f.id "
        "WHERE fts MATCH ? ORDER BY rank LIMIT ?", (q, limit)).fetchall()
    for y, s, t, title in rows:
        print(f"{y}  [{s[:4]}|{t}]  {title}")
    print(f"{len(rows)} result(s)")


def stats():
    items = json.loads(REFS.read_text())
    from collections import Counter
    print("status:", dict(Counter(r["status"] for r in items)))
    print("type:  ", dict(Counter(r.get("type") for r in items)))
    print("years: ", dict(sorted(Counter(r.get("year") for r in items if r.get("year")).items(), reverse=True)))
    print("no DOI:", sum(1 for r in items if not r.get("doi")), "| no authors:", sum(1 for r in items if not r.get("authors")))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["sync", "index", "search", "stats", "import-cv", "bib", "metrics"])
    ap.add_argument("query", nargs="?")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--refresh", action="store_true", help="metrics: refetch even cached Crossref values")
    a = ap.parse_args()
    if a.cmd == "sync":
        if not a.offline:
            fetch()
        merge()
        build_index()
        write_bib()
    elif a.cmd == "index":
        build_index()
    elif a.cmd == "bib":
        write_bib()
    elif a.cmd == "metrics":
        update_metrics(refresh=a.refresh)
    elif a.cmd == "import-cv":
        import_cv(a.query or sys.exit("path to CV (.docx) required"))
    elif a.cmd == "search":
        search(a.query or sys.exit("query required"))
    else:
        stats()
