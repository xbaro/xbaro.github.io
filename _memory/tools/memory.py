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
    except (urllib.error.URLError, ValueError, OSError) as e:   # network error, non-JSON/undecodable body, timeout
        sys.exit(f"request failed for {url.split('?')[0]}: {type(e).__name__}")


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
    fetch_portal()


PRC_RP = "rp20011"                       # owner's single researcher profile in the Portal de la Recerca de Catalunya
PRC_BASE = "https://portalrecerca.csuc.cat"
THESES = ROOT / "theses.json"


def fetch_portal():
    """Portal de la Recerca de Catalunya (CSUC DSpace-CRIS): publications via OpenSearch Atom + theses tab."""
    import html
    def get(url):
        req = urllib.request.Request(url, headers={"User-Agent": "personal-site-memory"})
        with urllib.request.urlopen(req, timeout=90) as r:
            return r.read().decode("utf-8", "replace")
    atom = get(f"{PRC_BASE}/open-search?query=author_authority:{PRC_RP}&format=atom&rpp=200")
    pubs = []
    for e in re.findall(r"<entry>.*?</entry>", atom, flags=re.S):
        g = lambda tag: html.unescape(re.sub(r"\s+", " ", (re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", e, re.S) or [0, ""])[1])).strip()
        y = re.search(r"<published>(\d{4})", e)
        pubs.append({"title": g("title"), "year": int(y.group(1)) if y else None, "handle": g("id").replace("http://", "https://"),
                     "authors": [html.unescape(a) for a in re.findall(r"<author><name>(.*?)</name>", e)]})
    import time
    for attempt in range(5):   # the portal sometimes returns this tab empty: retry until the table header shows up
        tab = get(f"{PRC_BASE}/orcid/{ORCID}/theses.html?onlytab=true")
        if "Director" in tab:
            break
        time.sleep(2 * (attempt + 1))
    text = [t.strip() for t in re.sub(r"<[^>]+>", "\n", re.sub(r"<(script|style).*?</\1>", "", tab, flags=re.S)).split("\n") if t.strip()]
    theses, i = [], 0
    while i < len(text) - 2:
        if re.fullmatch(r"\d{4}", text[i]) and not re.fullmatch(r"\d{4}", text[i + 1]):
            theses.append({"year": int(text[i]), "title": html.unescape(text[i + 1]), "directors": html.unescape(text[i + 2])})
            i += 3
        else:
            i += 1
    if not theses and (SRC / "portal_prc.json").exists():   # never replace known theses by an empty (failed) fetch
        theses = json.loads((SRC / "portal_prc.json").read_text()).get("theses", [])
        print("  [portal] theses tab came back empty; keeping previous theses")
    (SRC / "portal_prc.json").write_text(json.dumps({"publications": pubs, "theses": theses}, indent=1, ensure_ascii=False) + "\n")
    # theses are not publications: keep them as a separate collection (future supervision section)
    th_titles = {norm_title(t["title"]) for t in theses}
    handle = {norm_title(p["title"]): p["handle"] for p in pubs}
    out = []
    for t in theses:
        authors = [a for p in pubs if norm_title(p["title"]) == norm_title(t["title"]) for a in p["authors"]]
        own = "Bar" in t["directors"] and "Baró Solé" not in t["directors"]
        out.append({"title": t["title"], "year": t["year"], "directors": [d.strip() for d in t["directors"].split(";")],
                    "student": None if own else (authors[0] if authors else None),
                    "role": "author" if (own or not authors or authors[0].startswith("Baró")) else "co-director",
                    "repository_url": handle.get(norm_title(t["title"])), "source": "Portal de la Recerca de Catalunya"})
    THESES.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    print(f"portal: {len(pubs)} items ({len(theses)} theses) -> sources/portal_prc.json, theses.json")


def load_portal():
    p = SRC / "portal_prc.json"
    if not p.exists():
        return []
    d = json.loads(p.read_text())
    th = {norm_title(t["title"]) for t in d["theses"]}
    return [x for x in d["publications"] if norm_title(x["title"]) not in th]


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
    quality = parse_quality(lines)
    (SRC / "cv_quality.json").write_text(json.dumps(quality, indent=1, ensure_ascii=False) + "\n")
    indexing = parse_b_indexing(docx_paragraphs(path)) if str(path).endswith(".docx") else []
    if indexing:
        (SRC / "cv_indexing.json").write_text(json.dumps(indexing, indent=1, ensure_ascii=False) + "\n")
    print(f"cv: {len(pubs)} publications, {len(talks)} talks, {len(quality)} quality records (K), "
          f"{len(indexing)} per-item indexing records (B.1/B.3) -> sources/cv_*.json")


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


def docx_paragraphs(path):
    """Every paragraph in document order, including those nested in tables/form fields."""
    import docx
    from docx.oxml.ns import qn
    d = docx.Document(str(path))
    return [t for t in ("".join(x.text or "" for x in p.iter(qn("w:t"))).strip() for p in d.element.body.iter(qn("w:p"))) if t]


def parse_b_indexing(paras):
    """Per-item indexing fields of the AQU CV: B.1 (journal articles: WoS/Scopus/JIF/SJR/quartiles/ranking) and B.3 (quality text)."""
    out, sec, rec = [], None, []

    def flush():
        nonlocal rec
        if not rec or sec not in ("B.1", "B.3"):
            rec = []
            return
        g = lambda pat: next((re.sub(r"^[^:]*:\s*", "", p).strip() for p in rec if re.match(pat, p)), None)
        n = lambda s: (float(s.replace(",", ".")) if s and re.fullmatch(r"[\d.,]+", s.strip()) else None)
        title = g(r"^Títol(?! del)") or g(r"^Títol del capítol") or g(r"^Títol del llibre")
        if not title:
            rec = []
            return
        d = {"title": title, "section": sec}
        if sec == "B.1":
            si = [p.split(":")[-1].strip().upper() for p in rec if p.startswith("(SI/NO)")]
            d.update({"wos_indexed": si[0] == "SI" if si else None, "scopus_indexed": si[1] == "SI" if len(si) > 1 else None,
                      "cites_wos": n(g(r"^Nombre de citacions de l.article \(Wos")), "cites_scopus": n(g(r"^Nombre de citacions de l.article:")),
                      "jcr_if": n(g(r"^JIF del JCR")), "sjr": n(g(r"^Índex SJR")),
                      "jcr_quartile": g(r"^Quartil JCR"), "sjr_quartile": g(r"^Quartil SJR"),
                      "jcr_category": g(r"^Categoria JCR"), "sjr_category": g(r"^Categoria SJR"), "ranking": g(r"^Rànquing"),
                      "cites_scholar": n((re.search(r"Google Scholar:\s*(\d+)", " ".join(rec)) or [None, None])[1])})
        else:
            qt = g(r"^Indicis de qualitat")
            d["quality_text"] = qt
            for key, pat in (("cites_wos", r"WoS\s+(\d+)"), ("cites_scopus", r"Scopus\s+(\d+)"), ("cites_scholar", r"Google\s+(\d+)"),
                             ("cites_publisher", r"(?:Springer|Science ?Direct|Elsevier)[^.]*?(\d+)\s*cites")):
                mm = re.search(pat, qt or "")
                if mm:
                    d[key] = mm.group(1)
            for key in ("cites_wos", "cites_scopus", "cites_scholar", "cites_publisher"):
                if key in d:
                    d[key] = float(d[key])
        for k in ("cites_wos", "cites_scopus", "cites_scholar", "cites_publisher"):
            if d.get(k) is not None:
                d[k] = int(d[k])
        out.append({k: v for k, v in d.items() if v not in (None, "")})
        rec = []

    for p in paras:
        m = re.match(r"^(B\.[1-5])\s", p)
        if m or re.match(r"^[C-K]\.\s", p):
            flush()
            sec = m.group(1) if m else None
            continue
        if p.startswith("Autors/"):
            flush()
            rec = [p]
        elif rec:
            rec.append(p)
    flush()
    return out


def parse_quality(lines):
    """Section K of the AQU CV: 'ten most relevant contributions' with JCR/SJR/GGS/SPI indicators and citations."""
    start = next((i for i, ln in enumerate(lines) if ln.startswith("K. Feu constar")), None)
    out = []
    for ln in lines[start + 1:] if start is not None else []:
        if "Indicis de Qualitat" not in ln:
            continue
        head, _, tail = ln.partition("Indicis de Qualitat")
        num = lambda pat: (lambda m: float(m.group(1)) if m else None)(re.search(pat, tail))
        jq = re.search(r"JCR\s+\w+\s+(Q\d)\s*(.*?)\s*/\s*SJ", tail)
        sj = re.search(r"SJ\w*\s+I\w*:\s*([\d.]+)\s*(.*?)\s*/\s*Cites", tail)
        q = {"kind": head.split("]")[0].lstrip("[").strip(), "citation": re.sub(r"\s+", " ", head).strip(" /"),
             "jcr_if": num(r"JCR IF:\s*([\d.]+)"), "jcr_quartile": jq.group(1) if jq else None,
             "jcr_categories": [c.strip() for c in jq.group(2).split(" / ")] if jq else None,
             "sjr": float(sj.group(1)) if sj else None,
             "sjr_quartiles_raw": sj.group(2).strip() if sj else None,
             "cites_wos": num(r"Cites WoS:\s*(\d+)"), "cites_scopus": num(r"Cites Scopus:\s*(\d+)"),
             "cites_scholar": num(r"[Cc]it(?:es|acions) Google Scholar:\s*(\d+)"),
             "ggs_rating": (lambda m: m.group(1).strip() if m else None)(re.search(r"GGS Rating:\s*(.+?)\s*/", tail)),
             "spi": (lambda m: m.group(1).strip() if m else None)(re.search(r"(SPI:[^/]+)", tail)),
             "source": "CV (AQU) section K, 2024-12-31"}
        for k in ("jcr_if", "sjr", "cites_wos", "cites_scopus", "cites_scholar"):
            if q[k] is not None and q[k] == int(q[k]) and k.startswith("cites"):
                q[k] = int(q[k])
        out.append({k: v for k, v in q.items() if v is not None})
    return out


def load_cv_quality():
    p = SRC / "cv_quality.json"
    return json.loads(p.read_text()) if p.exists() else []


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
                near = difflib.SequenceMatcher(None, nt, t).ratio() >= 0.92 or (min(len(nt), len(t)) >= 30 and (nt in t or t in nt))
                if (not ry or not rec.get("year") or abs(ry - rec["year"]) <= 1) and near:
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
    for rec in load_portal():          # institutional repository record linked to the owner's PRC profile
        r = add({"title": rec["title"], "year": rec["year"], "authors": rec["authors"]}, "prc")
        r["status"] = "confirmed"
        r["repository_url"] = rec["handle"]
    for rec in load_scholar():         # the owner's Google Scholar profile (manual BibTeX export)
        add(rec, "scholar")["status"] = "confirmed"
    for rec in load_cv():              # the owner's own CV: authoritative, like ORCID
        r = add({k: v for k, v in rec.items() if k not in ("authors_cv", "cv_section", "scholar_cited_by")}, "cv")
        r["status"] = "confirmed"
        if rec.get("scholar_cited_by") is not None:
            r["scholar_cited_by"] = rec["scholar_cited_by"]
        if not r.get("authors") and rec.get("authors_cv"):
            r["authors"] = [a.strip() for a in re.split(r";|,\s(?=[A-ZÁÉÍÓÚ])", rec["authors_cv"]) if a.strip()]

    if DOI_CACHE.exists():             # DOI verification/lookup results -> attach, assign resolved DOIs, de-duplicate by DOI
        for k, res in json.loads(DOI_CACHE.read_text()).items():
            if k not in refs:
                continue
            refs[k]["doi_check"] = {a: b for a, b in res.items() if a != "candidate"}
            if res.get("candidate"):
                refs[k]["doi_candidate"] = res["candidate"]
            if res.get("resolved_doi") and not refs[k].get("doi"):
                refs[k]["doi"], refs[k]["doi_source"] = res["resolved_doi"], "crossref-search"
        seen = {}
        for k in list(refs):
            d = refs[k].get("doi")
            if not d:
                continue
            if d in seen:
                keep = refs[seen[d]]
                for a, b in refs[k].items():
                    if a == "sources":
                        keep["sources"] = sorted(set(keep["sources"]) | set(b))
                    elif a == "status":
                        keep["status"] = "confirmed" if "confirmed" in (keep["status"], b) else keep["status"]
                    elif a not in keep or keep[a] in (None, "", []):
                        keep[a] = b
                keep.setdefault("merged_ids", []).append(k)
                del refs[k]
                print(f"  [doi] merged duplicate by DOI {d}: {keep['title'][:60]}")
            else:
                seen[d] = k
    ip = SRC / "cv_indexing.json"      # per-item indexing from CV sections B.1 (articles) and B.3
    for rec in (json.loads(ip.read_text()) if ip.exists() else []):
        nt, k = norm_title(rec["title"]), by_title.get(norm_title(rec["title"]))
        if k is None:
            k = next((kk for t, kk in by_title.items() if difflib.SequenceMatcher(None, nt, t).ratio() >= 0.92
                      or (min(len(t), len(nt)) >= 25 and (t.startswith(nt) or nt.startswith(t)))), None)
        if k is None:
            print("  [indexing B] no match for:", rec["title"][:80])
            continue
        refs[k]["indexing"] = {a: b for a, b in rec.items() if a not in ("title", "section")} | {"source": f"CV (AQU) {rec['section']}, 2024-12-31"}
    for q in load_cv_quality():        # CV section K (10 most relevant): adds GGS/SPI and fills gaps
        text = norm_title(q["citation"])
        cands = [(len(norm_title(r["title"])), k) for k, r in refs.items()
                 if len(norm_title(r["title"])) >= 15 and norm_title(r["title"]) in text]
        if not cands:
            print("  [indexing K] no match for:", q["citation"][:90])
            continue
        k = max(cands)[1]
        ix = refs[k].setdefault("indexing", {})
        for a, b in q.items():
            if a not in ("citation", "kind", "source"):
                ix.setdefault(a, b)
        ix.setdefault("source", q["source"])
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


DOI_CACHE = SRC / "doi_check.json"


def _tsim(a, b):
    a, b = norm_title(a), norm_title(b)
    if not a or not b:
        return 0.0
    if min(len(a), len(b)) >= 20 and (a in b or b in a):
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _registry_view(msg):
    """Normalise a Crossref work (or DataCite/CSL record) into comparable fields."""
    title = msg.get("title")
    title = title[0] if isinstance(title, list) and title else (title or "")
    parts = (msg.get("issued") or {}).get("date-parts") or [[None]]
    fam = [norm_title(a.get("family") or a.get("name") or "") for a in msg.get("author", [])]
    ct = msg.get("container-title")
    return {"title": title, "year": parts[0][0], "authors_norm": fam, "type": msg.get("type"),
            "container": (ct[0] if isinstance(ct, list) and ct else ct) or None, "relation": msg.get("relation") or {}}


def _doi_lookup(doi):
    q = urllib.parse.quote(doi)
    try:
        return _registry_view(http_json(f"https://api.crossref.org/works/{q}")["message"]), "crossref"
    except SystemExit:
        pass
    try:  # DataCite / others (e.g. arXiv 10.48550): resolve through doi.org content negotiation
        return _registry_view(http_json(f"https://doi.org/{q}", {"Accept": "application/vnd.citationstyles.csl+json"})), "doi.org"
    except SystemExit:
        return None, None


def check_dois(refresh=False):
    """Verify each DOI against its registry and look up missing ones. Results -> sources/doi_check.json."""
    import datetime
    import time
    refs = [r for r in json.loads(REFS.read_text()) if r["status"] == "confirmed"]
    cache = json.loads(DOI_CACHE.read_text()) if DOI_CACHE.exists() else {}
    today = datetime.date.today().isoformat()
    for n, r in enumerate(refs, 1):
        old = cache.get(r["id"])
        if old and not refresh and (old.get("doi") == r.get("doi") or old.get("resolved_doi")):
            continue
        res = {"checked": today}
        if r.get("doi"):
            v, via = _doi_lookup(r["doi"])
            res["doi"] = r["doi"]
            if v is None:
                res["status"] = "unresolved"      # DOI not found in Crossref/DataCite: wrong or not registered
            else:
                sim = _tsim(r["title"], v["title"])
                author_ok = any(a.startswith("baro") for a in v["authors_norm"]) if v["authors_norm"] else None
                year_ok = abs((v["year"] or 0) - (r.get("year") or 0)) <= 1 if v["year"] and r.get("year") else None
                res.update({"registry": via, "title_sim": round(sim, 3), "author_ok": author_ok, "year_ok": year_ok,
                            "registry_title": v["title"], "registry_year": v["year"]})
                rel = {k: [x.get("id") for x in vv] for k, vv in v["relation"].items()
                       if k in ("is-preprint-of", "has-preprint", "is-version-of", "has-version") for vv in [vv]}
                if rel:
                    res["relation"] = rel
                res["status"] = ("verified" if sim >= 0.9 and author_ok is not False and year_ok is not False
                                 else "mismatch" if sim < 0.6 else "partial")
        else:                                      # no DOI: bibliographic search, accept only high-confidence matches
            q = urllib.parse.urlencode({"query.bibliographic": r["title"], "query.author": "Baró", "rows": 5,
                                        "select": "DOI,title,issued,author,container-title,type,score"})
            try:
                items = http_json(f"https://api.crossref.org/works?{q}")["message"]["items"]
            except SystemExit:
                items = []
            best = None
            for it in items:
                v = _registry_view(it)
                sim = _tsim(r["title"], v["title"])
                author_ok = any(a.startswith("baro") for a in v["authors_norm"])
                year_ok = abs((v["year"] or 0) - (r.get("year") or 0)) <= 1 if v["year"] and r.get("year") else False
                cand = {"doi": norm_doi(it["DOI"]), "title_sim": round(sim, 3), "author_ok": author_ok, "year_ok": year_ok,
                        "registry_title": v["title"], "registry_year": v["year"]}
                if best is None or cand["title_sim"] > best["title_sim"]:
                    best = cand
            if best and best["title_sim"] >= 0.95 and best["author_ok"] and best["year_ok"]:
                res.update({"status": "resolved", "resolved_doi": best["doi"], **{k: best[k] for k in ("title_sim", "registry_title", "registry_year")}})
            elif best and best["title_sim"] >= 0.8:
                res.update({"status": "candidate", "candidate": best})
            else:
                res["status"] = "none_found"
        cache[r["id"]] = res
        time.sleep(0.12)
        if n % 25 == 0:
            DOI_CACHE.write_text(json.dumps(cache, indent=1, ensure_ascii=False))
    DOI_CACHE.write_text(json.dumps(cache, indent=1, ensure_ascii=False) + "\n")
    from collections import Counter
    print("doi_check:", dict(Counter(v["status"] for v in cache.values())))


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
    ap.add_argument("cmd", choices=["sync", "index", "search", "stats", "import-cv", "bib", "metrics", "doi", "portal"])
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
    elif a.cmd == "portal":
        fetch_portal()
        merge()
        build_index()
        write_bib()
    elif a.cmd == "doi":
        check_dois(refresh=a.refresh)
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
