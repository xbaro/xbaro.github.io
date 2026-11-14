#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["python-docx", "truststore"]
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

try:   # use the OS trust store: some institutional servers (e.g. openaccess.uoc.edu) omit the intermediate certificate
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

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
    d = re.sub(r"^https?://(dx\.)?doi\.org/", "", d).rstrip(".")   # CV exports sometimes end the DOI with a full stop
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
THESES_OV = ROOT / "theses_overrides.json"


def fetch_portal():
    """Portal de la Recerca de Catalunya (CSUC DSpace-CRIS): publications via OpenSearch Atom + theses tab."""
    import html
    def get(url):
        import time
        req = urllib.request.Request(url, headers={"User-Agent": "personal-site-memory"})
        for attempt in range(5):   # the portal intermittently answers 500
            try:
                with urllib.request.urlopen(req, timeout=90) as r:
                    return r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError:
                if attempt == 4:
                    raise
                time.sleep(3 * (attempt + 1))
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
    projects = []   # public research projects tab: start date, title, institutions, researchers
    for attempt in range(5):
        tab = get(f"{PRC_BASE}/orcid/{ORCID}/publicresearcherprojects.html?onlytab=true")
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", tab, flags=re.S)
        cells = [[html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c))).strip() for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, flags=re.S)] for r in rows]
        projects = [{"start": c[0], "title": c[1], "institutions": c[2], "researchers": c[3]} for c in cells
                    if len(c) >= 4 and re.fullmatch(r"\d\d-\d\d-\d{4}", c[0])]
        if projects:
            break
        time.sleep(2 * (attempt + 1))
    if not projects and (SRC / "portal_prc.json").exists():
        projects = json.loads((SRC / "portal_prc.json").read_text()).get("projects", [])
        print("  [portal] projects tab came back empty; keeping previous projects")
    (SRC / "portal_prc.json").write_text(json.dumps({"publications": pubs, "theses": theses, "projects": projects}, indent=1, ensure_ascii=False) + "\n")
    print(f"portal: {len(pubs)} items ({len(theses)} theses, {len(projects)} projects) -> sources/portal_prc.json")


def flip_name(n):
    """'Last, First' -> 'First Last' (the CV mixes both styles)."""
    n = re.sub(r"^(Dr|Dra)\.\s*", "", (n or "").strip())
    return " ".join(x.strip() for x in n.split(",", 1)[::-1]) if "," in n else (n or None)


def split_people(s, level):
    s = re.sub(r"\b(Dr|Dra)\.\s*", "", s or "")
    if ";" in s:
        return [flip_name(x) for x in s.split(";") if x.strip()]
    if level == "master" and s.count(",") == 1 and " i " not in s:
        return [flip_name(s)]
    return [x.strip() for x in re.split(r",\s*|\s+i\s+|\s+and\s+", s) if x.strip()]


def parse_theses(paras):
    """CV section G.1: doctoral theses (finished/ongoing) and master's theses directed by the owner."""
    out, level, rec = [], None, {}

    def iso(s):
        m = re.search(r"(\d{2})/(\d{2})/(\d{4})", s or "")
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None

    def flush():
        nonlocal rec
        if rec.get("title"):
            ds = split_people(rec.get("directors", ""), level[0])
            when = rec.get("date") or ""
            out.append({"title": rec["title"].strip().rstrip("."), "level": level[0], "status": level[1],
                        "student": flip_name(rec.get("student")), "directors": ds, "date": iso(when) or None,
                        "year": int((re.search(r"(19|20)\d{2}", when) or [None])[0]) if re.search(r"(19|20)\d{2}", when) else None,
                        "institution": rec.get("institution"), "source": "CV (AQU) G.1, 2024-12-31"})
        rec = {}

    own, in7 = {}, False
    for p in paras:
        if p.startswith("7. Tesi doctoral"):
            in7 = True
            continue
        if in7 and re.match(r"^8\.\s", p):
            in7 = False
            if own.get("title"):
                out.append({"title": own["title"].rstrip("."), "level": "phd", "status": "defended", "student": "Xavier Baró Solé",
                            "directors": split_people(own.get("directors", ""), "phd"), "date": iso(own.get("date")),
                            "year": int(own["date"][-4:]) if own.get("date") else None, "institution": own.get("univ"),
                            "role": "author", "source": "CV (AQU) section 7, 2024-12-31"})
        if in7:
            k, _, v = p.partition(":")
            for key, label in (("title", "Títol"), ("directors", "Directors"), ("univ", "Universitat"), ("date", "Data de defensa")):
                if k.startswith(label) and key not in own:
                    own[key] = v.strip()
            continue
        if p.startswith("G.1.1"):
            flush(); level = ("phd", "defended"); continue
        if p.startswith("G.1.2"):
            flush(); level = ("phd", "ongoing"); continue
        if p.startswith("G.1.3"):
            flush(); level = ("master", "defended"); continue
        if p.startswith("G.2") or re.match(r"^H\.\s", p):
            flush(); level = None; continue
        if level is None:
            continue
        k, _, v = p.partition(":")
        v = v.strip()
        if k.startswith("Directors"):
            flush(); rec["directors"] = v
        elif k.startswith("Títol del treball"):
            rec["title"] = v
        elif k.startswith("Data de"):
            rec["date"] = v
        elif k.startswith("Nom del doctorand") or k.startswith("Nom de l"):
            rec["student"] = v
        elif k.startswith("Institució"):
            rec["institution"] = v
    flush()
    return out


REPOS_SCAN = SRC / "repos_scan.json"
_ME = re.compile(r"^Bar[oó](,|\s+i\s+Sol[eé],|\s+Sol[eé],)\s*X(avier|\.)?(\s|$)", re.I)


def is_me(name):
    """Strict match for the owner's name variants; excludes homonyms (Baró Queralt, Romero Baró, ...)."""
    return bool(_ME.match((name or "").strip())) and "Queralt" not in name


def initials(name):
    n = flip_name(name or "")
    return "".join(w[0] + "." for w in re.split(r"[\s-]+", n) if w and w[0].isalpha()) or None


def scan_repos():
    """Find final-degree/master/doctoral works in UOC (O2), UB (Dipòsit Digital) and UAB (DDD) where the owner is tutor/advisor/director."""
    import html
    import time

    def get(u):
        for _ in range(3):
            try:
                return urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "personal-site-memory", "Accept": "application/json"}),
                                              timeout=90).read().decode("utf8", "replace")
            except Exception:
                time.sleep(2)
        return ""

    out = []
    for repo, base in (("UOC", "https://openaccess.uoc.edu"), ("UB", "https://diposit.ub.edu")):
        items = {}
        for q in ("Baró Solé, Xavier", "Baró, Xavier", "Baró Solé"):
            page = 0
            while True:
                d = json.loads(get(f"{base}/server/api/discover/search/objects?" + urllib.parse.urlencode(
                    {"query": q, "size": 100, "page": page, "dsoType": "ITEM"})) or "{}")
                if not d:
                    break
                sr = d["_embedded"]["searchResult"]
                for o in sr["_embedded"]["objects"]:
                    it = o["_embedded"]["indexableObject"]
                    items[it["uuid"]] = it
                page += 1
                if page >= sr["page"]["totalPages"]:
                    break
                time.sleep(0.3)
        for it in items.values():
            md = it["metadata"]
            v = lambda k: [x["value"] for x in md.get(k, [])]
            typ = next((t.split("/")[-1] for t in v("dc.type") if "Thesis" in t), None)
            if not typ:
                continue
            roles = {k.replace("dc.contributor.", ""): ns for k in md if k.startswith("dc.contributor")
                     for ns in [v(k)] if any(is_me(n) for n in ns)}
            students = [n for n in v("dc.contributor.author") if not is_me(n)]
            if not roles or not (students or "author" not in roles):
                continue
            if "author" in roles and not students:      # his own works
                continue
            role = ("tutor" if "tutor" in roles or "advisor" in roles or ("director" in roles and typ == "doctoralThesis") else
                    "advisor-listed-as-author" if "author" in roles else "coordinator")
            out.append({"repo": repo, "level": {"bachelorThesis": "bachelor", "masterThesis": "master", "doctoralThesis": "phd"}[typ],
                        "title": (v("dc.title") or [it["name"]])[0].strip(), "date": (v("dc.date.issued") or [""])[0],
                        "student_initials": initials(students[0]) if students else None, "role": role,
                        "metadata_roles": sorted(roles), "uri": (v("dc.identifier.uri") or [""])[0]})
    seen = {}
    for q in ('"Baró Solé, Xavier"', '"Baró, Xavier"'):
        s = get("https://ddd.uab.cat/search?" + urllib.parse.urlencode({"p": q, "of": "xm", "rg": 100}))
        for r in re.findall(r"<record>.*?</record>", s, flags=re.S):
            rid = re.search(r'<controlfield tag="001">(\d+)', r).group(1)
            sf = lambda tag: [{c: html.unescape(x).strip() for c, x in re.findall(r'<subfield code="(\w)">(.*?)</subfield>', b, flags=re.S)}
                              for b in re.findall(rf'<datafield tag="{tag}"[^>]*>(.*?)</datafield>', r, flags=re.S)]
            coll = [x.get("a") for x in sf("980")]
            if rid in seen or "TRERECPRO" not in coll:
                continue
            if not any(is_me(x.get("a")) and "dir" in (x.get("e") or "") for x in sf("700")):
                continue
            seen[rid] = {"repo": "UAB", "level": "research", "title": (sf("245") or [{}])[0].get("a", ""),
                         "date": ((sf("260") or [{}])[0].get("c", "")), "student_initials": initials((sf("100") or [{}])[0].get("a")),
                         "role": "tutor", "metadata_roles": ["700 dir."], "uri": f"https://ddd.uab.cat/record/{rid}"}
    out += list(seen.values())
    prev = json.loads(REPOS_SCAN.read_text()) if REPOS_SCAN.exists() else []
    for repo in ("UOC", "UB", "UAB"):          # a repository that suddenly returns nothing is an API hiccup, not a fact
        if not any(r["repo"] == repo for r in out) and any(r["repo"] == repo for r in prev):
            print(f"  [repos] {repo} returned nothing; keeping previous results")
            out += [r for r in prev if r["repo"] == repo]
    out.sort(key=lambda x: (x["repo"], x["date"]))
    REPOS_SCAN.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    from collections import Counter
    print("repos:", dict(Counter((r["repo"], r["level"], r["role"]) for r in out)))


ACTIVITIES = ROOT / "activities.json"
WP_SITE = "xbaro.wordpress.com"
MERIT_CATS = (("Premis", "awards"), ("Organització d", "event_organization"), ("Revisor en revistes", "reviewing"),
              ("Editor revistes", "editorial_boards"), ("Acreditacions docents", "teaching_accreditation"),
              ("Innovació docent", "teaching_innovation"), ("Participació en jurats", "juries"))


def parse_merits(paras):
    """CV sections J (other merits) and F (research stays > 1 month)."""
    out = {k: [] for _, k in MERIT_CATS} | {"research_stays": []}
    cat, stay = None, {}

    def idx(prefix):
        return next((i for i, p in enumerate(paras) if p.startswith(prefix)), None)

    j0, j1, f0, f1 = idx("J. Altres"), idx("K. Feu"), idx("F. Estades"), idx("G. Direcció")
    for p in paras[(f0 or 0) + 1:(f1 or 0)]:
        k, _, v = p.partition(":")
        key = {"Centre": "institution", "Localitat": "city", "País": "country", "Any": "year", "Durada (mesos)": "months",
               "Tema": "topic", "Clau": "role"}.get(k.strip())
        if key and v.strip():
            stay[key] = v.strip()
    if stay.get("institution"):
        out["research_stays"].append(stay)
    for p in paras[(j0 or 0) + 1:(j1 or 0)]:
        c = next((key for pre, key in MERIT_CATS if p.startswith(pre) and len(p) < 70), None)
        if c:
            cat = c
        elif cat and not p.startswith("("):
            out[cat].append(p)
    return out


def fetch_wordpress():
    """Archive of the old personal blog (xbaro.wordpress.com, 2009-2011): public WordPress.com API."""
    import html
    d = json.loads(urllib.request.urlopen(urllib.request.Request(
        f"https://public-api.wordpress.com/rest/v1.1/sites/{WP_SITE}/posts?number=100&type=any&fields=ID,type,title,date,URL,categories,content",
        headers={"User-Agent": "personal-site-memory"}), timeout=60).read())
    posts = []
    for x in sorted(d.get("posts", []), key=lambda x: x["date"]):
        c = re.sub(r"<a [^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", r"\2 <\1>", html.unescape(x.get("content", "")), flags=re.S)
        posts.append({"date": x["date"][:10], "title": html.unescape(x["title"]), "category": next(iter(x.get("categories") or {}), None),
                      "text": re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip(), "url": x["URL"]})
    (SRC / "wordpress_xbaro.json").write_text(json.dumps(posts, indent=1, ensure_ascii=False) + "\n")
    print(f"wordpress: {len(posts)} posts -> sources/wordpress_xbaro.json")


def build_activities():
    """activities.json = CV sections J/F (merits) + archive of the old blog, with notes on discrepancies."""
    mp, wp = SRC / "cv_merits.json", SRC / "wordpress_xbaro.json"
    out = json.loads(mp.read_text()) if mp.exists() else {}
    out["old_blog_news"] = json.loads(wp.read_text()) if wp.exists() else []
    out["_notes"] = ["The old blog announced ICCV 2011 for 25 Sep-2 Oct 2011 (written in 2010); the CV gives 6-13 Nov 2011 (the real dates). Trust the CV.",
                     "Blog posts about attending ICME 2009 and WORLDCOMP'09 (IPCV09) and the CDCovers benchmark are not in the CV; roles are unspecified."]
    ACTIVITIES.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")


PROJECTS = ROOT / "projects.json"


def parse_projects(paras):
    """CV section A: A.1 competitive projects, A.2 non-competitive projects, A.3 contracts / technology transfer."""
    a = next((i for i, p in enumerate(paras) if p.startswith("A. Projectes")), None)
    b = next((i for i, p in enumerate(paras) if p.startswith("B. Publicacions")), None)
    if a is None or b is None:
        return []
    kinds = {"A.1": "competitive", "A.2": "non-competitive", "A.3": "contract"}
    out, sec, rec = [], None, {}
    keymap = {"Títol del projecte": "title", "Títol del contracte": "title", "Entitat finançadora": "funder",
              "Empresa/administració finançadora": "funder", "Referència de la concessió": "reference", "Import concedit": "amount_raw",
              "Durada": "duration", "Investigador/a principal": "pi", "Investigador responsable": "pi", "País": "country"}

    def flush():
        nonlocal rec
        if rec.get("title"):
            ym = re.findall(r"\b((?:19|20)\d{2})\b", rec.get("duration", ""))
            amt = re.search(r"[\d.]+(?:,\d+)?", rec.get("amount_raw", "") or "")
            pi = [re.sub(r"^(Dr|Dra)\.\s*", "", x.strip()) for x in re.split(r"\s+i\s+|,", rec.get("pi", "")) if x.strip()]
            me = any("Baró" in x for x in pi)
            ref = rec.get("reference", "")
            out.append({"title": rec["title"].strip(), "kind": "research-group recognition (SGR)" if "SGR" in ref else kinds[sec],
                        "funder": rec.get("funder", "").strip(" ."), "reference": ref or None, "country": rec.get("country"),
                        "start": int(ym[0]) if ym else None, "end": int(ym[-1]) if ym else None,
                        "amount_total_eur": float(amt.group().replace(".", "").replace(",", ".")) if amt else None,
                        "amount_share_eur": (lambda s: float(s.group(1).replace(".", "").replace(",", ".")) if s else None)(
                            re.search(r"\(\D*([\d.]+(?:,\d+)?)", rec.get("amount_raw", "") or "")),
                        "amount_raw": rec.get("amount_raw"), "principal_investigators": pi,
                        "role": ("co-PI" if len(pi) > 1 else "PI") if me else "participant", "source": "CV (AQU) section A, 2024-12-31"})
        rec = {}

    for p in paras[a + 1:b]:
        m = re.match(r"^(A\.[1-3])\s", p)
        if m:
            flush(); sec = m.group(1); continue
        k, _, v = p.partition(":")
        key = keymap.get(k.strip())
        if key == "title":
            flush()
        if key and sec:
            rec[key] = v.strip()
    flush()
    return out


TEACHING = ROOT / "teaching.json"
UB_ACTIVITIES = ("Classes teoricopràctiques", "Classes teòriques", "Pràctiques de problemes", "Pràctiques de laboratori",
                 "Pràctiques amb tecnologia", "Seminari", "Treball pràctic", "Treball tutelat", "Tutories")
SUPERVISION = ("treball de fi de grau", "treball de fi de carrera", "tesi", "treballs acadèmicament dirigits")


def _sentence_case(s):
    s = s.strip()
    if not s.isupper():
        return s
    s = s[0] + s[1:].lower()
    s = re.sub(r"\((\w+)\)", lambda m: "(" + m.group(1).upper() + ")", s)
    return re.sub(r"\b(ii|iii|iv)\b|\bi$", lambda m: m.group().upper(), s)   # roman numerals ('Programació II'), not the conjunction 'i'


def parse_ub_teaching(text):
    """One 'Encàrrecs i dedicació docent' PDF (UB) as text from `pdftotext -layout`. Only course metadata is kept (no timetables, rooms or exam dates)."""
    year = (re.search(r"Curs (\d{4}-\d{4})", text) or [None, None])[1]
    out, degree, sem, cur = [], None, None, None
    for ln in text.splitlines():
        m = re.match(r"^\s*(\w+) - (.+?) - Inici docència: (\d)\w* semestre", ln)
        if m:
            degree, sem, cur = (m.group(1), _sentence_case(m.group(2))), int(m.group(3)), None
            continue
        m = re.match(r"^\s*(\d{5,6}) - (.+?) \(([\d.]+) crèdits?\)", ln)
        if m and degree:
            cur = {"academic_year": year, "institution": "Universitat de Barcelona", "degree_code": degree[0], "degree": degree[1], "semester": sem,
                   "subject_code": m.group(1), "subject": _sentence_case(m.group(2)), "credits": float(m.group(3)),
                   "activities": [], "languages": []}
            cur["kind"] = "supervision" if cur["subject"].lower() in SUPERVISION else "course"
            out.append(cur)
            continue
        if cur:
            for a in UB_ACTIVITIES:
                if re.match(rf"^\s*{a}", ln) and a not in cur["activities"]:
                    cur["activities"].append(a)
            for lang in ("Català", "Castellà", "Anglès", "Francès"):
                if re.search(rf"\b{lang}\b", ln) and lang not in cur["languages"]:
                    cur["languages"].append(lang)
    return out


def import_teaching(pdfs):
    """UB teaching assignments from PDFs (needs the `pdftotext` binary: brew install poppler)."""
    import subprocess
    rows = []
    for f in pdfs:
        txt = subprocess.run(["pdftotext", "-layout", str(f), "-"], capture_output=True, text=True, check=True).stdout
        rows += parse_ub_teaching(txt)
    rows.sort(key=lambda r: (r["academic_year"], r["semester"], r["subject_code"]))
    for r in rows:
        if r["academic_year"] == "2026-2027":
            r["status"] = "planned"      # assignment for a future course, subject to validation by the university
    (SRC / "ub_teaching.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False) + "\n")
    print(f"ub teaching: {len(rows)} subject-semesters over {len({r['academic_year'] for r in rows})} courses -> sources/ub_teaching.json")


def build_teaching():
    """teaching.json = UB undergraduate teaching (PDFs) + postgraduate teaching (CV section G.2)."""
    ub, pg = SRC / "ub_teaching.json", SRC / "cv_postgrad_teaching.json"
    out = {"ub": json.loads(ub.read_text()) if ub.exists() else [], "postgraduate": json.loads(pg.read_text()) if pg.exists() else [],
           "_notes": ["UB data = 'Encàrrecs i dedicació docent' PDFs; the university marks them as informational and subject to validation. 2026-2027 is a plan.",
                      "UB gap 2016-2023 (owner was at UOC) and UOC/UAB undergraduate teaching are not in these documents."]}
    out["summary"] = teaching_summary(out)
    TEACHING.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")


def _year_periods(years):
    """['2012-2013','2013-2014','2024-2025'] -> [[2012, 2014], [2024, 2025]] (start year, end year; consecutive courses merged)."""
    ys = sorted({int(re.match(r"(\d{4})", y).group(1)) for y in years})
    periods = []
    for y in ys:
        if periods and y == periods[-1][1]:
            periods[-1][1] = y + 1
        else:
            periods.append([y, y + 1])
    return periods


def teaching_summary(t):
    """One row per (institution, subject): the periods it was taught, instead of one row per course.
    Names are keyed by language so the site can add translations (ca is the source language)."""
    groups = {}
    ov = ROOT / "teaching_overrides.json"
    excl = json.loads(ov.read_text())["exclude"] if ov.exists() else []
    for r in t["ub"]:
        if any(e["subject"] == r["subject"] and r["academic_year"] in e["academic_years"] for e in excl):
            continue                                     # owner override (e.g. exam proctoring only)
        if r.get("kind") != "course":
            continue                                     # TFG/TFC/Tesi are supervision (see theses.json)
        g = groups.setdefault(("Universitat de Barcelona", r["subject"].lower()), {
            "institution": "Universitat de Barcelona", "level": "master" if "Intel" in r["degree"] else "undergraduate",
            "name": {"ca": r["subject"]}, "programmes": set(), "languages": set(), "years": [], "planned_years": []})
        g["programmes"].add(r["degree"])
        g["languages"].update(r.get("languages") or [])
        (g["planned_years"] if r.get("status") == "planned" else g["years"]).append(r["academic_year"])
    insts = (("Oberta", "Universitat Oberta de Catalunya"), ("Barcelona", "Universitat de Barcelona"), ("Paris-Saclay", "Université Paris-Saclay"))
    alias = {"object recognition": "Reconeixement de persones i objectes", "seminaris en intel·ligència artificial ub": "Seminaris en intel·ligència artificial (UB)"}   # same MSc course, English title in the CV
    for r in t["postgraduate"]:
        m = re.match(r"(.+?)\s*\((.+)\)\s*$", r["course"])
        name, prog = (m.group(1), m.group(2)) if m else (r["course"], None)
        if name.lower().startswith("projecte final"):
            continue                                     # master thesis supervision, not a course
        inst = next((v for k, v in insts if k in r["institution"]), r["institution"])
        key = alias.get(name.lower(), name)
        g = groups.setdefault((inst, key.lower()), {"institution": inst, "level": "master", "name": {"ca": key},
                                            "programmes": set(), "languages": set(), "years": [], "planned_years": []})
        if prog:
            g["programmes"].add(prog)
        g["years"] += r["academic_years"]
    rows = []
    for g in groups.values():
        rows.append({"institution": g["institution"], "level": g["level"], "name": g["name"],
                     "programmes": sorted(g["programmes"]), "languages": sorted(g["languages"]),
                     "periods": _year_periods(g["years"]) if g["years"] else [],
                     "planned": _year_periods(g["planned_years"]) if g["planned_years"] else []})
    rows.sort(key=lambda x: (x["institution"], x["level"], (x["periods"] or x["planned"])[0][0], x["name"]["ca"]))
    return rows


def build_site_data():
    """Derive the PUBLIC data files read by Jekyll (_data/*.json) from the memory. Names are resolved per language."""
    t = json.loads(TEACHING.read_text())
    tr = json.loads((ROOT / "i18n_teaching.json").read_text())
    langs = ["ca", "en", "es"]

    def loc(table, ca):
        e = table.get(ca, {})
        return {l: (ca if l == "ca" and "ca" not in e else e.get(l, ca)) for l in langs}

    def course(y):                       # 2012 -> "2012/13"
        return f"{y}/{(y + 1) % 100:02d}"

    def label(periods):                  # [[2012, 2016]] -> "2012/13–2015/16"; one course -> "2024/25"
        return [course(a) if b - a == 1 else f"{course(a)}–{course(b - 1)}" for a, b in periods]

    def span(periods):                   # one range per subject: first course taught -> last course taught
        a, b = min(p[0] for p in periods), max(p[1] for p in periods)
        return course(a) if b - a == 1 else f"{course(a)}–{course(b - 1)}"

    inst_order = ["Universitat de Barcelona", "Universitat Oberta de Catalunya", "Université Paris-Saclay"]
    inst_logo = {"Universitat de Barcelona": "ub.png", "Universitat Oberta de Catalunya": "uoc-negre.png", "Université Paris-Saclay": "paris-saclay.svg"}   # black-and-white logos in images/logos/ (shown if present)
    grouped = {}
    for r in t["summary"]:
        if not r["periods"]:   # only what has been taught: courses that are merely planned are not published
            continue
        prog = loc(tr["programmes"], r["programmes"][0]) if r["programmes"] else {l: "—" for l in langs}
        g = grouped.setdefault(r["institution"], {}).setdefault(prog["ca"], {"name": prog, "level": r["level"], "subjects": []})
        g["subjects"].append({"name": loc(tr["names"], r["name"]["ca"]), "period_label": span(r["periods"]), "start": min(p[0] for p in r["periods"]),
                              "languages": r["languages"]})
    out_rows = []
    for inst in sorted(grouped, key=lambda i: inst_order.index(i) if i in inst_order else 99):
        progs = sorted(grouped[inst].values(), key=lambda g: (g["level"] != "undergraduate", min(x["start"] for x in g["subjects"]), g["name"]["ca"]))
        for g in progs:
            g["subjects"].sort(key=lambda x: (x["start"], x["name"]["ca"]))
            for x in g["subjects"]:
                del x["start"]
        logo = next((f"/images/logos/{inst_logo[inst]}" for _ in [0] if inst in inst_logo and (ROOT.parent / "images" / "logos" / inst_logo[inst]).exists()), None)
        out_rows.append({"institution": inst, "logo": logo, "programmes": progs})
    out = ROOT.parent / "_data" / "teaching.json"
    out.write_text(json.dumps(out_rows, indent=1, ensure_ascii=False) + "\n")
    print(f"{out.relative_to(ROOT.parent)}: {sum(len(g['subjects']) for i in out_rows for g in i['programmes'])} subjects in {sum(len(i['programmes']) for i in out_rows)} programmes")

    ov_p = ROOT / "projects_overrides.json"
    pov = json.loads(ov_p.read_text()) if ov_p.exists() else {}
    projs = []
    for r in json.loads((ROOT / "projects.json").read_text()):
        o = {**pov.get("by_title", {}).get(r["title"], {}), **pov.get("by_ref", {}).get(r.get("reference"), {})}
        if o.get("public") is False:
            continue
        projs.append({"title": o.get("title", r["title"]), "kind": {"non-competitive": "contract", "research-group recognition (SGR)": "sgr"}.get(r["kind"], r["kind"]),
                      "funder": o.get("funder") or pov.get("by_funder", {}).get(r["funder"], r["funder"]), "reference": r.get("reference"),
                      "start": r.get("start"), "end": o.get("end", r.get("end")),
                      "pis": [AUTHOR_ALIASES.get(a, a) for a in o.get("pis", r.get("principal_investigators", []))]})
    projs.sort(key=lambda x: (-(x["start"] or 0), x["title"].lower()))
    out = ROOT.parent / "_data" / "projects.json"
    out.write_text(json.dumps(projs, indent=1, ensure_ascii=False) + "\n")   # amounts are deliberately not exported
    print(f"{out.relative_to(ROOT.parent)}: {len(projs)} projects")

    pubs = []
    bibkeys = {i: k for i, k, _ in bib_entries()}
    oa_all = json.loads(OA_PATH.read_text()) if OA_PATH.exists() else {}
    for r in json.loads(REFS.read_text()):
        if r["status"] != "confirmed" or r.get("public") is False:   # only verified references reach the site
            continue
        pubs.append({"bibkey": bibkeys.get(r["id"]), "title": r["title"], "authors": r.get("authors", []), "venue": r.get("venue") or "", "year": r.get("year"),
                     "type": {"preprint": "report"}.get(r.get("type"), r.get("type") or "other"), "doi": r.get("doi"), "url": None if r.get("doi") else r.get("url"),
                     "isbn": r.get("isbn"), "label": "preprint" if r.get("type") == "preprint" else None,
                     "oa": ({k: oa_all[r["id"]].get(k) for k in ("pdf", "html", "version")} if (oa_all.get(r["id"]) or {}).get("pdf") or (oa_all.get(r["id"]) or {}).get("html") else None)})
    pubs.sort(key=lambda x: (-(x["year"] or 0), x["title"].lower()))
    out = ROOT.parent / "_data" / "publications.json"
    out.write_text(json.dumps(pubs, indent=1, ensure_ascii=False) + "\n")
    print(f"{out.relative_to(ROOT.parent)}: {len(pubs)} publications")
    (ROOT.parent / "files").mkdir(exist_ok=True)
    (ROOT.parent / "files" / "publications.bib").write_text(BIB.read_text())   # downloadable BibTeX of the verified publications


def _title_sim(a, b):
    return difflib.SequenceMatcher(None, norm_title(a), norm_title(b)).ratio()


def _is_direct_pdf(url):
    return bool(re.search(r"\.pdf($|\?)|/pdf/|/bitstream/|pdfdirect|/document$", url or "", flags=re.I))


def enrich_open_access(cache, refs):
    """Second pass for references OpenAlex says nothing about: Semantic Scholar (openAccessPdf) by DOI or title, then arXiv by title
    (only accepted when the title is near-identical and the owner is among the authors)."""
    import time
    import xml.etree.ElementTree as ET
    added = 0
    for r in refs:
        c = cache.get(r["id"]) or {}
        if c.get("pdf"):
            continue
        found = None
        try:
            if r.get("doi"):
                q = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{urllib.parse.quote(r['doi'])}?fields=title,openAccessPdf"
                d = http_json(q)
                found = (d.get("openAccessPdf") or {}).get("url")
                src = "semantic-scholar"
            if not found and r.get("title"):
                q = "https://api.semanticscholar.org/graph/v1/paper/search?" + urllib.parse.urlencode(
                    {"query": r["title"][:200], "limit": 3, "fields": "title,openAccessPdf,authors"})
                for d in (http_json(q).get("data") or []):
                    if _title_sim(d.get("title") or "", r["title"]) >= 0.93 and any("Bar" in (a.get("name") or "") and "Xavier" in (a.get("name") or "") or (a.get("name") or "").endswith("Baró") or (a.get("name") or "").endswith("Baro") for a in d.get("authors", [])):
                        found = (d.get("openAccessPdf") or {}).get("url")
                        src = "semantic-scholar-title"
                        if found:
                            break
        except SystemExit:
            pass
        time.sleep(1.1)
        if not found and r.get("title"):
            try:
                req = urllib.request.Request("http://export.arxiv.org/api/query?" + urllib.parse.urlencode(
                    {"search_query": f'ti:"{r["title"][:150]}"', "max_results": 3}), headers={"User-Agent": "personal-site-memory"})
                with urllib.request.urlopen(req, timeout=40) as resp:
                    root = ET.fromstring(resp.read())
                ns = {"a": "http://www.w3.org/2005/Atom"}
                for e in root.findall("a:entry", ns):
                    t = (e.findtext("a:title", "", ns) or "").strip()
                    au = [(x.findtext("a:name", "", ns) or "") for x in e.findall("a:author", ns)]
                    if _title_sim(t, r["title"]) >= 0.93 and any(n.endswith(("Baró", "Baro")) for n in au):
                        found, src = (e.findtext("a:id", "", ns) or "").replace("http://", "https://").replace("/abs/", "/pdf/"), "arxiv"
                        break
            except Exception:
                pass
            time.sleep(3.2)
        if found:
            direct = _is_direct_pdf(found)   # a landing page is a full-text link (HTML), not a PDF
            cache[r["id"]] = {**c, "status": c.get("status") if c.get("is_oa") else "green", "is_oa": True, "pdf": found if direct else None,
                              "html": c.get("html") if direct else found,
                              "version": c.get("version") or "submittedVersion", "license": c.get("license"), "host": c.get("host") or "repository", "via": src}
            added += 1
    return added


OA_PATH = SRC / "open_access.json"


def fetch_open_access(refresh=False):
    """Open-access full-text links per verified reference from OpenAlex (best OA location + other OA locations).
    Stores sources/open_access.json: id -> {status, pdf, html, version, license, host}."""
    import time
    refs = [r for r in json.loads(REFS.read_text()) if r["status"] == "confirmed" and r.get("public") is not False]
    cache = json.loads(OA_PATH.read_text()) if OA_PATH.exists() else {}
    sel = "open_access,best_oa_location,locations"
    rank = {"publishedVersion": 0, "acceptedVersion": 1, "submittedVersion": 2}
    for r in refs:
        if r["id"] in cache and not refresh:
            continue
        if r.get("doi"):
            url = f"https://api.openalex.org/works/doi:{urllib.parse.quote(r['doi'])}?select={sel}"
        elif r.get("openalex_id"):
            url = f"https://api.openalex.org/works/{r['openalex_id'].rsplit('/', 1)[-1]}?select={sel}"
        else:
            cache[r["id"]] = {"status": "unknown"}
            continue
        try:
            w = http_json(url)
        except SystemExit:
            cache[r["id"]] = {"status": "unknown"}
            time.sleep(0.2)
            continue
        locs = [l for l in ([w.get("best_oa_location")] + (w.get("locations") or [])) if l and l.get("is_oa")]
        locs.sort(key=lambda l: (rank.get(l.get("version"), 3), 0 if l.get("pdf_url") else 1))
        pdf = next((l for l in locs if l.get("pdf_url")), None)
        html = next((l for l in locs if l.get("landing_page_url") and not l.get("pdf_url")), None) or (locs[0] if locs else None)
        oa = w.get("open_access") or {}
        cache[r["id"]] = {"status": oa.get("oa_status") or "closed", "is_oa": bool(oa.get("is_oa")),
                          "pdf": pdf["pdf_url"] if pdf else None,
                          "html": html["landing_page_url"] if html and not pdf else (html or {}).get("landing_page_url"),
                          "version": (pdf or html or {}).get("version"), "license": (pdf or html or {}).get("license"),
                          "host": ((pdf or html or {}).get("source") or {}).get("type")}
        time.sleep(0.15)
    extra = enrich_open_access(cache, refs)
    OA_PATH.write_text(json.dumps(cache, indent=1, ensure_ascii=False) + "\n")
    print(f"open_access: second pass (Semantic Scholar / arXiv) added {extra} PDF links")
    n = sum(1 for v in cache.values() if v.get("pdf") or v.get("html"))
    print(f"open_access: {len(refs)} refs checked, {n} with an open full-text link -> sources/open_access.json")


def parse_postgrad_teaching(paras):
    """CV section G.2: postgraduate courses taught."""
    a = next((i for i, p in enumerate(paras) if p.startswith("G.2 Activitats")), None)
    b = next((i for i, p in enumerate(paras) if re.match(r"^H\.\s", p)), None)
    out, rec = [], {}
    for p in paras[(a or 0) + 1:(b or 0)]:
        k, _, v = p.partition(":")
        key = {"Tipus d": "type", "Nom de l": "course", "Curs acadèmic": "academic_years", "Nombre d": "hours", "Institució": "institution"}
        kk = next((val for pre, val in key.items() if k.startswith(pre)), None)
        if kk == "type" and rec:
            out.append(rec); rec = {}
        if kk:
            rec[kk] = v.strip()
    if rec:
        out.append(rec)
    for r in out:
        r["hours"] = float(r["hours"]) if re.fullmatch(r"[\d.]+", r.get("hours", "")) else None
        r["academic_years"] = [y.strip() for y in re.split(r",|\si\s", r.get("academic_years", "")) if y.strip()]
        r["source"] = "CV (AQU) G.2, 2024-12-31"
    return out


def build_theses():
    """theses.json = CV section G.1 (authoritative) enriched with Portal de la Recerca records (repository link) + the owner's own thesis."""
    cvp, prp = SRC / "cv_theses.json", SRC / "portal_prc.json"
    cv = json.loads(cvp.read_text()) if cvp.exists() else []
    portal = json.loads(prp.read_text()) if prp.exists() else {"publications": [], "theses": []}
    handles = {norm_title(p["title"]): p for p in portal["publications"]}

    def find(title):
        n = norm_title(title)
        return next((p for t, p in handles.items() if difflib.SequenceMatcher(None, n, t).ratio() >= 0.9), None)

    out = []
    for t in cv:
        pr = find(t["title"])
        t = dict(t, role=t.get("role") or ("director" if len(t["directors"]) == 1 else "co-director"))
        if pr:
            t["repository_url"] = pr["handle"]
            t["source"] += " + Portal de la Recerca de Catalunya"
        out.append(t)
    for t in portal["theses"]:             # portal theses not in the CV list (e.g. the owner's own 2009 thesis)
        if not any(difflib.SequenceMatcher(None, norm_title(t["title"]), norm_title(c["title"])).ratio() >= 0.9 for c in cv):
            pr = find(t["title"])
            out.append({"title": t["title"].rstrip("."), "level": "phd", "status": "defended", "year": t["year"],
                        "student": "Xavier Baró Solé", "directors": [d.strip() for d in t["directors"].split(";")],
                        "role": "author", "repository_url": pr["handle"] if pr else None, "source": "Portal de la Recerca de Catalunya"})
    scan = json.loads(REPOS_SCAN.read_text()) if REPOS_SCAN.exists() else []
    for r in scan:                          # repository records: link existing theses, add the new ones where the owner was tutor/director
        m = next((o for o in out if difflib.SequenceMatcher(None, norm_title(o["title"]), norm_title(r["title"])).ratio() >= 0.9), None)
        if m:
            m.setdefault("repository_url", r["uri"])
            continue
        if r["role"] == "coordinator":
            continue
        yr = (re.search(r"(19|20)\d{2}", r["date"]) or [None])[0]
        out.append({"title": r["title"].rstrip("."), "level": r["level"], "status": "defended", "student": r["student_initials"],
                    "directors": ["Xavier Baró"], "year": int(yr) if yr else None, "date": r["date"] or None,
                    "institution": {"UOC": "Universitat Oberta de Catalunya", "UB": "Universitat de Barcelona", "UAB": "Universitat Autònoma de Barcelona"}[r["repo"]],
                    "role": "director", "repository_url": r["uri"], "source": f"{r['repo']} institutional repository (role in metadata: {', '.join(r['metadata_roles'])})"})
    tov = json.loads(THESES_OV.read_text()) if THESES_OV.exists() else {}
    done = {norm_title(c) for c in tov.get("confirmed", [])}
    for x in out:                           # owner's review state; anything new from a scan stays pending until reviewed
        x["review"] = "confirmed" if norm_title(x["title"]) in done else "pending"
    order = {"phd": 0, "master": 1, "research": 2, "bachelor": 3}
    out.sort(key=lambda x: (order.get(x["level"], 9), x["status"] != "ongoing", -(x.get("year") or 0)))
    THESES.write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    return out


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
    if str(path).endswith(".docx"):
        pg = parse_postgrad_teaching(docx_paragraphs(path))
        (SRC / "cv_postgrad_teaching.json").write_text(json.dumps(pg, indent=1, ensure_ascii=False) + "\n")
        print(f"cv postgraduate teaching: {len(pg)} courses")
        pr = parse_projects(docx_paragraphs(path))
        PROJECTS.write_text(json.dumps(pr, indent=1, ensure_ascii=False) + "\n")
        print(f"cv projects: {len(pr)} -> projects.json")
        mer = parse_merits(docx_paragraphs(path))
        (SRC / "cv_merits.json").write_text(json.dumps(mer, indent=1, ensure_ascii=False) + "\n")
        print("cv merits:", {k: len(v) for k, v in mer.items()})
        th = parse_theses(docx_paragraphs(path))
        (SRC / "cv_theses.json").write_text(json.dumps(th, indent=1, ensure_ascii=False) + "\n")
        print(f"cv: {len(th)} directed theses (G.1) -> sources/cv_theses.json")
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
    recs = json.loads(p.read_text()) if p.exists() else []
    for r in recs:
        r["doi"] = norm_doi(r.get("doi"))
    return recs


def ref_id(rec):
    return f"doi:{rec['doi']}" if rec.get("doi") else f"t:{norm_title(rec['title'])[:60].replace(' ', '-')}-{rec.get('year') or 'nd'}"


AUTHOR_ALIASES = {"Sérgio Escalera": "Sergio Escalera", "David Baneres": "David Bañeres", "Umut Guclu": "Umut Güçlü",
                  "Carlos Andujar": "Carlos Andújar", "Miguel Angel Bautista": "Miguel Ángel Bautista", "Jordi Vitria": "Jordi Vitrià",
                  "Jordi M. Vitria": "Jordi Vitrià", "Petia I. Radeva": "Petia Radeva", "Miguel A Bautista": "Miguel Ángel Bautista",
                  "Marcel AJ van Gerven": "Marcel A. J. van Gerven", "Marcel van Gerven": "Marcel A. J. van Gerven", "U. Güçlü": "Umut Güçlü",
                  "J. Wan": "Jun Wan", "M. Asadi": "Maryam Asadi-Aghbolaghi", "X Baró": "Xavier Baró", "David Masip Rodo": "David Masip",
                  "Àgata Lapedriza Garcia": "Àgata Lapedriza", "Jordi Vitrià Marca": "Jordi Vitrià", "Ana Elena Guerrero": "Ana‐Elena Guerrero‐Roldán", "Julio C. S. Jacques": "Julio C. S. Jacques Junior"}


def merge():
    refs = {}
    by_title = {}

    def add(rec, source):
        def conflict(k):   # two different DOIs are two different works, whatever their titles look like
            a, b = rec.get("doi"), refs[k].get("doi")
            return bool(a and b and a != b and not a.startswith("10.48550/") and not b.startswith("10.48550/"))   # arXiv DOI = preprint of the same work

        key = None
        if rec.get("doi") and f"doi:{rec['doi']}" in refs:
            key = f"doi:{rec['doi']}"
        elif norm_title(rec["title"]) in by_title and not conflict(by_title[norm_title(rec["title"])]):
            key = by_title[norm_title(rec["title"])]
        else:  # fuzzy: near-identical title, year within 1
            nt = norm_title(rec["title"])
            for t, k in by_title.items():
                ry = refs[k].get("year")
                near = difflib.SequenceMatcher(None, nt, t).ratio() >= 0.92 or (min(len(nt), len(t)) >= 30 and (nt in t or t in nt))
                if (not ry or not rec.get("year") or abs(ry - rec["year"]) <= 1) and near and not conflict(k):
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
    for r in refs.values():            # one spelling per author (same person written with/without accents)
        if r.get("authors"):
            r["authors"] = [AUTHOR_ALIASES.get(a, a) for a in r["authors"]]
    build_theses()
    build_activities()
    build_teaching()
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


def bib_entries():
    """BibTeX entries (id, key, text) of the verified references (confirmed and not hidden with public=false)."""
    items = [r for r in json.loads(REFS.read_text()) if r["status"] == "confirmed" and r.get("public") is not False]
    used, out = set(), []   # out: list of (ref id, key, entry text)
    for r in items:
        first = (r.get("authors") or ["anon"])[0].split()[-1]
        base = re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", first).encode("ascii", "ignore").decode().lower())
        word = next((w for w in norm_title(r["title"]).split() if len(w) > 3), "x")
        key = f"{base}{r.get('year') or 'nd'}{word}"
        while key in used:
            key += "b"
        used.add(key)
        kind = BIB_TYPES.get(r.get("type"), "misc")
        venue = r.get("venue") or ""
        extra = []
        if kind == "article":
            m = re.match(r"^(.*?)\s+(\d+)\((\d+)\):\s*([\d]+)\s*[-\u2013]\s*([\d]+)$", venue)   # "Journal 24(198):1-6"
            if m:
                venue = m.group(1)
                extra = [("volume", m.group(2)), ("number", m.group(3)), ("pages", f"{m.group(4)}--{m.group(5)}")]
            extra.insert(0, ("journal", venue))
        elif kind in ("inproceedings", "incollection"):
            extra = [("booktitle", venue)]
        elif kind == "book":   # the venue is often the series; Springer DOIs (10.1007) identify the publisher
            extra = [("publisher", "Springer"), ("series", venue)] if (r.get("doi") or "").startswith("10.1007/") else [("publisher", venue)]
        elif kind == "techreport":
            extra = [("institution", venue or "Unpublished report")]
        else:
            extra = [("howpublished", venue), ("note", "Preprint" if r.get("type") == "preprint" else None)]
        names = _bib_esc(" and ".join(r.get("authors", [])))
        who = ("editor", names) if (kind == "book" and r.get("edited")) else ("author", names)
        f = [("title", "{" + _bib_esc(r["title"]) + "}"), who, ("year", str(r["year"]) if r.get("year") else None)]
        f += [(k, _bib_esc(v)) for k, v in extra] + [("doi", r.get("doi")), ("isbn", r.get("isbn")), ("url", None if r.get("doi") else r.get("url"))]
        body = ",\n".join(f"  {k} = {{{v}}}" for k, v in f if v)
        out.append((r["id"], key, f"@{kind}{{{key},\n{body}\n}}"))
    return out


def write_bib():
    """references.bib: all verified references, for easy export/reuse."""
    out = bib_entries()
    BIB.write_text("\n\n".join(t for _, _, t in out) + "\n")
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
    ap.add_argument("cmd", choices=["sync", "index", "search", "stats", "import-cv", "bib", "metrics", "doi", "portal", "repos", "blog", "import-teaching", "site", "oa"])
    ap.add_argument("query", nargs="?")
    ap.add_argument("paths", nargs="*", help="import-teaching: the UB 'Encàrrecs i dedicació docent' PDFs")
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
    elif a.cmd == "import-teaching":
        import_teaching(([a.query] if a.query else []) + a.paths)
        merge()
    elif a.cmd == "site":
        build_site_data()
    elif a.cmd == "blog":
        fetch_wordpress()
        merge()
    elif a.cmd == "repos":
        scan_repos()
        merge()
    elif a.cmd == "portal":
        fetch_portal()
        merge()
        build_index()
        write_bib()
    elif a.cmd == "oa":
        fetch_open_access()
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
