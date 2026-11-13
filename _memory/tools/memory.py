#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
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
        })
    return out


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

    overrides = json.loads(OVERRIDES.read_text()) if OVERRIDES.exists() else {}
    for key, ov in overrides.items():
        if key in refs:
            refs[key].update({k: v for k, v in ov.items()})
    items = sorted(refs.values(), key=lambda r: (-(r.get("year") or 0), r["title"].lower()))
    REFS.write_text(json.dumps(items, indent=1, ensure_ascii=False) + "\n")
    c = sum(1 for r in items if r["status"] == "confirmed")
    print(f"references.json: {len(items)} total, {c} confirmed, {len(items) - c} candidates")


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
    ap.add_argument("cmd", choices=["sync", "index", "search", "stats"])
    ap.add_argument("query", nargs="?")
    ap.add_argument("--offline", action="store_true")
    a = ap.parse_args()
    if a.cmd == "sync":
        if not a.offline:
            fetch()
        merge()
        build_index()
    elif a.cmd == "index":
        build_index()
    elif a.cmd == "search":
        search(a.query or sys.exit("query required"))
    else:
        stats()
