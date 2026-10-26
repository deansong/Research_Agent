"""
WHAT:  Papers of the major venues, from their own records: the ACL Anthology
       (ACL, EMNLP, NAACL, EACL, COLING, Findings, TACL, CL, workshops), ICML
       (PMLR), NeurIPS and ICLR (OpenReview), AAAI (its proceedings, by
       track) -- searched by topic, venue, year and track -- and downloading a
       paper's PDF (and its text).
WHY:   "Which methods were published at ACL in track X that apply to task Y"
       needs the venue's own list, not a web search.
CONCEPT: Standard library only; big listings are downloaded once and cached
       under $RESEARCH_TOOLS_CACHE (default ~/.cache/research_tools).

What each source has, measured:
    ACL Anthology  every paper with its abstract (one 47 MB file, updated
                   daily; cached a week). Venue and volume (long, short,
                   findings, demo, industry ...), but NO area/track: the
                   Anthology does not record them -- search by topic instead.
    PMLR (ICML)    one bibliography with abstracts per year (2019-2025).
    OpenReview     NeurIPS, ICLR (and ICML) through its search API; its
                   listing API and PDF links refuse scripts. Each paper's
                   `primary_area` (NeurIPS 2024+, ICLR 2024+) is its track.
    AAAI           its proceedings' feed, one set per track per year
                   ("AAAI Technical Track on Speech and Natural Language
                   Processing I"); fetched by track, cached.
"""

from __future__ import annotations

import gzip
import json
import math
import os
import re
import shutil
import subprocess
import time
import urllib.parse
from pathlib import Path

#: ICML's PMLR volumes, checked against each volume's bibliography.
ICML_VOLUMES = {2019: 97, 2020: 119, 2021: 139, 2022: 162, 2023: 202, 2024: 235, 2025: 267}
ACL_BIB = "https://aclanthology.org/anthology+abstracts.bib.gz"
#: Old-style Anthology ids: their first letter is the venue.
OLD_ACL = {"P": "acl", "D": "emnlp", "N": "naacl", "E": "eacl", "C": "coling", "Q": "tacl",
           "J": "cl", "K": "conll", "S": "semeval", "L": "lrec", "I": "ijcnlp", "W": "workshop",
           "Y": "paclic", "O": "rocling", "U": "alta", "R": "ranlp", "F": "jeptalnrecital"}
#: The Anthology's main venues (anything else is a workshop or a smaller event).
ACL_MAIN = {"acl", "emnlp", "naacl", "eacl", "coling", "aacl", "ijcnlp", "tacl", "cl", "conll",
            "lrec", "findings"}
OPENREVIEW = {"neurips": "NeurIPS.cc", "iclr": "ICLR.cc", "icml": "ICML.cc"}
STOP = set("a an and the of for in on to with by from is are be as at or via using use based "
           "into over under than that this these those can how what which do does method "
           "methods approach approaches paper papers task tasks".split())


# ---- fetching and caching ----------------------------------------------------------------


def cache_dir() -> Path:
    path = Path(os.environ.get("RESEARCH_TOOLS_CACHE") or Path.home() / ".cache" / "research_tools")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _fresh(path: Path, days: float) -> bool:
    return path.exists() and time.time() - path.stat().st_mtime < days * 86400


def _download(url: str, path: Path, get) -> Path:
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(get(url))
    tmp.replace(path)
    return path


# ---- a small BibTeX reader ------------------------------------------------------------------

_ENTRY = re.compile(r"@(\w+)\s*\{\s*([^,\s]+)\s*,", re.S)


def parse_bibtex(text: str):
    """Yield {"type", "key", field: value} for each entry. Values in "..." or
    {...}, braces nested; enough for the Anthology's and PMLR's files."""
    for m in _ENTRY.finditer(text):
        entry = {"type": m.group(1).lower(), "key": m.group(2)}
        i, n = m.end(), len(text)
        while i < n:
            f = re.compile(r"\s*([A-Za-z_]+)\s*=\s*").match(text, i)
            if not f:
                break
            name, i = f.group(1).lower(), f.end()
            if i < n and text[i] in "{\"":
                close = "}" if text[i] == "{" else '"'
                depth, j = 0, i + 1
                while j < n:
                    c = text[j]
                    if c == "\\":
                        j += 2
                        continue
                    if c == "{":
                        depth += 1
                    elif c == "}" and (depth > 0 or close == '"'):
                        depth -= 1
                    elif c == close and depth == 0:
                        break
                    j += 1
                value, i = text[i + 1:j], j + 1
            else:
                v = re.compile(r"[^,}\n]*").match(text, i)
                value, i = v.group(0).strip(), v.end()
            entry[name] = _clean(value)
            c = re.compile(r"\s*,?").match(text, i)
            i = c.end()
            if i < n and text[i] == "}":
                break
        yield entry


def _clean(value: str) -> str:
    value = re.sub(r"\\[a-zA-Z]+\s*\{([^{}]*)\}", r"\1", value)      # \emph{x} -> x
    value = re.sub(r"\\['`^\"~=.uvHc]\s*\{?([a-zA-Z])\}?", r"\1", value)  # accents
    value = value.replace("{", "").replace("}", "").replace("\\", "")
    value = re.sub(r"<[^>]+>", "", value)                               # PMLR's <em>
    return " ".join(value.split())


# ---- matching a topic -----------------------------------------------------------------------


def terms(query: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9][a-z0-9\-]*", query.lower())
            if len(w) > 1 and w not in STOP]


def _stem(w: str) -> str:
    for suffix in ("ations", "ation", "ings", "ing", "ies", "es", "s"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 4:
            return w[: -len(suffix)]
    return w


def score(query_terms: list[str], title: str, abstract: str, phrase: str = "") -> float:
    """How well a paper matches: most query words must appear (stems, title
    counting triple); 0 when too few do."""
    if not query_terms:
        return 1.0
    t, a = title.lower(), abstract.lower()
    hits = 0.0
    found = 0
    for w in query_terms:
        s = _stem(w)
        in_t, in_a = s in t, s in a
        if in_t or in_a:
            found += 1
            hits += 3 * in_t + min(a.count(s), 3)
    if found < max(1, math.ceil(0.6 * len(query_terms))):
        return 0.0
    if phrase and len(phrase.split()) > 1 and phrase.lower() in t + " " + a:
        hits += 5
    return hits * found / len(query_terms)


def _rank(rows: list[dict], limit: int) -> list[dict]:
    """Best first (the score stays, for merging sources; search() drops it)."""
    rows.sort(key=lambda r: (-r["_score"], -(int(str(r.get("year") or 0)[:4] or 0))))
    return rows[:limit]


def _year_ok(year, since, exact) -> bool:
    try:
        y = int(str(year)[:4])
    except ValueError:
        return not (since or exact)
    return (not since or y >= since) and (not exact or y == exact)


# ---- the ACL Anthology ----------------------------------------------------------------------


def acl_id_venue(acl_id: str) -> tuple[str, str]:
    """(venue, volume kind) from an Anthology id: 2024.acl-long.12 -> (acl,
    long); 2023.findings-emnlp.45 -> (emnlp, findings); P19-1001 -> (acl, "")."""
    m = re.match(r"^(\d{4})\.([a-z0-9]+)(?:-([a-z0-9]+))?\.\d+$", acl_id)
    if m:
        event, vol = m.group(2), m.group(3) or ""
        if event == "findings":
            return vol, "findings"
        return event, vol
    m = re.match(r"^([A-Z])\d{2}-\d+$", acl_id)
    return (OLD_ACL.get(m.group(1), "other"), "") if m else ("other", "")


def acl_index(get) -> Path:
    """The Anthology as one JSON line per paper (id, title, abstract, venue,
    kind, year, booktitle), rebuilt from the bibliography at most weekly."""
    cache = cache_dir()
    index, bib = cache / "acl_index.jsonl", cache / "anthology+abstracts.bib.gz"
    if _fresh(index, 7):
        return index
    if not _fresh(bib, 7):
        _download(ACL_BIB, bib, get)
    text = gzip.decompress(bib.read_bytes()).decode("utf-8", "replace")
    tmp = index.with_suffix(".part")
    with open(tmp, "w", encoding="utf-8") as out:
        for e in parse_bibtex(text):
            url = e.get("url", "")
            m = re.search(r"aclanthology\.org/([^/]+)/?$", url)
            if not m or e["type"] == "proceedings":
                continue
            acl_id = m.group(1)
            venue, kind = acl_id_venue(acl_id)
            out.write(json.dumps({"id": acl_id, "title": e.get("title", ""),
                                  "abstract": e.get("abstract", ""), "venue": venue,
                                  "kind": kind, "year": e.get("year", ""),
                                  "booktitle": e.get("booktitle") or e.get("journal", "")},
                                 ensure_ascii=False) + "\n")
    tmp.replace(index)
    return index


def search_acl(query: str, venues: set[str], kinds: set[str], since, year, limit, get) -> list[dict]:
    """`venues`: acl, emnlp, findings, tacl, ... or "anthology" for all;
    `kinds`: long, short, findings, demo, industry, main, srw ..."""
    words, rows = terms(query), []
    everything = not venues or "anthology" in venues
    with open(acl_index(get), encoding="utf-8") as f:
        for line in f:
            p = json.loads(line)
            if not _year_ok(p["year"], since, year):
                continue
            if not everything and not (p["venue"] in venues or
                                       ("findings" in venues and p["kind"] == "findings") or
                                       ("workshop" in venues and p["venue"] not in ACL_MAIN)):
                continue
            if kinds and p["kind"] not in kinds and not ("demo" in kinds and p["kind"] == "demos"):
                continue
            s = score(words, p["title"], p["abstract"], query)
            if s:
                rows.append({"_score": s, "title": p["title"],
                             "venue": f"{p['venue'].upper()} {p['year']} {p['kind']}".strip(),
                             "year": p["year"], "id": p["id"],
                             "url": f"https://aclanthology.org/{p['id']}/",
                             "pdf": f"https://aclanthology.org/{p['id']}.pdf",
                             "summary": _short(p["abstract"]), "found_in": "acl anthology"})
    return _rank(rows, limit)


def acl_paper(acl_id: str, get) -> dict | None:
    with open(acl_index(get), encoding="utf-8") as f:
        for line in f:
            if line.startswith(f'{{"id": "{acl_id}"'):
                p = json.loads(line)
                return {"title": p["title"], "venue": p["booktitle"], "year": p["year"],
                        "id": p["id"], "abstract": p["abstract"],
                        "url": f"https://aclanthology.org/{p['id']}/",
                        "pdf": f"https://aclanthology.org/{p['id']}.pdf"}
    return None


# ---- ICML on PMLR -----------------------------------------------------------------------------


def icml_index(year: int, get) -> Path | None:
    vol = ICML_VOLUMES.get(year)
    if vol is None:
        return None
    path = cache_dir() / f"icml_{year}.jsonl"
    if path.exists():
        return path                       # a published volume does not change
    text = get(f"https://proceedings.mlr.press/v{vol}/assets/bib/bibliography.bib").decode(
        "utf-8", "replace")
    with open(path, "w", encoding="utf-8") as out:
        for e in parse_bibtex(text):
            if e["type"] != "inproceedings":
                continue
            out.write(json.dumps({"title": e.get("title", ""), "abstract": e.get("abstract", ""),
                                  "url": e.get("url", ""), "pdf": e.get("pdf", ""),
                                  "year": year}, ensure_ascii=False) + "\n")
    return path


def search_icml(query, since, year, limit, get) -> list[dict]:
    words, rows = terms(query), []
    for y in sorted(ICML_VOLUMES, reverse=True):
        if not _year_ok(y, since, year):
            continue
        with open(icml_index(y, get), encoding="utf-8") as f:
            for line in f:
                p = json.loads(line)
                s = score(words, p["title"], p["abstract"], query)
                if s:
                    rows.append({"_score": s, "title": p["title"], "venue": f"ICML {y}",
                                 "year": y, "url": p["url"], "pdf": p["pdf"],
                                 "summary": _short(p["abstract"]), "found_in": "pmlr"})
    return _rank(rows, limit)


# ---- NeurIPS, ICLR (and ICML) on OpenReview ------------------------------------------------------


def search_openreview(query, venue, since, year, track, limit, get) -> list[dict]:
    """Accepted papers only: a submission's `venue` says "Submitted to" or
    "Rejected"/"Withdrawn" until it is accepted ("ICLR 2024 poster")."""
    group = OPENREVIEW[venue]
    rows, words, seen = [], terms(query), set()
    for offset in (0, 100):
        url = ("https://api2.openreview.net/notes/search?" + urllib.parse.urlencode(
            {"term": query, "group": group, "limit": 100, "offset": offset, "source": "forum"}))
        notes = json.loads(get(url)).get("notes") or []
        for n in notes:
            c = {k: (v.get("value") if isinstance(v, dict) else v)
                 for k, v in (n.get("content") or {}).items()}
            label, venueid = str(c.get("venue") or ""), str(c.get("venueid") or "")
            if (not label or re.search(r"submitted|submission|rejected|withdrawn|desk",
                                       label + " " + venueid, re.I)):
                continue
            # The main conference only: the group also holds its workshops
            # ("TRL @ NeurIPS 2024 Poster").
            if not re.match(rf"{venue}\s+20\d\d", label, re.I) or "@" in label:
                continue
            y = re.search(r"(20\d\d)", label)
            if not _year_ok(y.group(1) if y else "", since, year):
                continue
            area = str(c.get("primary_area") or "").replace("_", " ")
            if track and not all(_stem(w) in (area + " " + label).lower() for w in terms(track)):
                continue
            if n["id"] in seen:
                continue
            seen.add(n["id"])
            s = score(words, str(c.get("title", "")), str(c.get("abstract", "")), query)
            if s:
                rows.append({"_score": s, "title": c.get("title", ""), "venue": label,
                             "year": y.group(1) if y else "", "track": area,
                             "id": f"openreview:{n['id']}",
                             "url": f"https://openreview.net/forum?id={n['id']}",
                             "keywords": ", ".join(c.get("keywords") or [])[:200],
                             "summary": _short(str(c.get("abstract", ""))),
                             "found_in": "openreview"})
        if len(notes) < 100:
            break
    return _rank(rows, limit)


# ---- AAAI, by track ---------------------------------------------------------------------------

AAAI_OAI = "https://ojs.aaai.org/index.php/AAAI/oai"


def aaai_sets(get) -> list[tuple[str, str]]:
    """Every track (set) of the AAAI proceedings: (spec, name)."""
    path = cache_dir() / "aaai_sets.json"
    if _fresh(path, 30):
        return [tuple(x) for x in json.loads(path.read_text())]
    out, token = [], ""
    while True:
        url = AAAI_OAI + ("?verb=ListSets" + (f"&resumptionToken={urllib.parse.quote(token)}"
                                              if token else ""))
        x = get(url).decode("utf-8", "replace")
        out += [(s, _clean(n)) for s, n in
                re.findall(r"<setSpec>([^<]*)</setSpec>\s*<setName>([^<]*)</setName>", x)]
        t = re.search(r"<resumptionToken[^>]*>([^<]+)</resumptionToken>", x)
        if not t:
            break
        token = t.group(1)
    out = [(s, n.replace("&amp;", "&")) for s, n in out]
    path.write_text(json.dumps(out))
    return out


def aaai_records(spec: str, get) -> list[dict]:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", spec)
    path = cache_dir() / f"aaai_{safe}.json"
    if _fresh(path, 60):
        return json.loads(path.read_text())
    out, token = [], ""
    while True:
        params = ({"verb": "ListRecords", "resumptionToken": token} if token else
                  {"verb": "ListRecords", "metadataPrefix": "oai_dc", "set": spec})
        x = get(AAAI_OAI + "?" + urllib.parse.urlencode(params)).decode("utf-8", "replace")
        for r in x.split("<record>")[1:]:
            def field(tag):
                return [_clean(v) for v in re.findall(rf"<dc:{tag}[^>]*>(.*?)</dc:{tag}>", r, re.S)]
            source = " ".join(field("source"))
            y = re.search(r"AAAI-(\d\d)", source)
            ids = field("identifier")
            out.append({"title": (field("title") or [""])[0],
                        "abstract": (field("description") or [""])[0],
                        "year": f"20{y.group(1)}" if y else (field("date") or [""])[0][:4],
                        "url": next((i for i in ids if i.startswith("http")), ""),
                        "doi": next((i for i in ids if i.startswith("10.")), ""),
                        "subjects": ", ".join(field("subject"))})
        t = re.search(r"<resumptionToken[^>]*>([^<]+)</resumptionToken>", x)
        if not t:
            break
        token = t.group(1)
    path.write_text(json.dumps(out))
    return out


def search_aaai(query, track, since, year, limit, get) -> list[dict]:
    if not track:
        raise SystemExit("AAAI is searched by track: add --track (e.g. --track \"natural "
                         "language\"); `tracks --venue aaai` lists them.")
    want = [_stem(w) for w in terms(track)]
    sets = [(s, n) for s, n in aaai_sets(get) if all(w in n.lower() for w in want)]
    if not sets:
        raise SystemExit(f"No AAAI track matches {track!r}; `tracks --venue aaai` lists them.")
    words, rows = terms(query), []
    for spec, name in sets:
        for p in aaai_records(spec, get):
            if not _year_ok(p["year"], since, year):
                continue
            s = score(words, p["title"], p["abstract"] + " " + p["subjects"], query)
            if s:
                rows.append({"_score": s, "title": p["title"], "venue": f"AAAI {p['year']}",
                             "year": p["year"], "track": name, "doi": p["doi"],
                             "url": p["url"], "summary": _short(p["abstract"]),
                             "found_in": "aaai proceedings"})
    return _rank(rows, limit)


# ---- one entry point ----------------------------------------------------------------------------

VENUES = sorted(ACL_MAIN | {"anthology", "workshop", "neurips", "iclr", "icml", "aaai"})


def search(query: str, venues: list[str], *, since=None, year=None, track="", kinds=(),
           limit: int = 10, get=None) -> list[dict]:
    venues = [v.lower() for v in venues] or ["anthology"]
    unknown = [v for v in venues if v not in VENUES]
    if unknown:
        raise SystemExit(f"Unknown venue {', '.join(unknown)}; they are {', '.join(VENUES)}.")
    acl = {v for v in venues if v in ACL_MAIN | {"anthology", "workshop"}}
    rows, notes = [], []
    if acl:
        if track:
            notes.append("the ACL Anthology records no tracks or areas, so --track was not "
                         "applied to it: put the area's words in the query, or use --kind")
        rows += search_acl(query, acl, set(kinds), since, year, limit, get)
    if "icml" in venues:
        rows += search_icml(query, since, year, limit, get)
        newest = max(ICML_VOLUMES)
        if not year or year > newest:
            # Years PMLR has not published yet: OpenReview.
            try:
                rows += search_openreview(query, "icml", max(since or 0, newest + 1), year,
                                          track, limit, get)
            except SystemExit as exc:
                notes.append(f"icml on openreview: {exc}")
    for v in ("neurips", "iclr"):
        if v in venues:
            try:
                rows += search_openreview(query, v, since, year, track, limit, get)
            except SystemExit as exc:
                notes.append(f"{v}: {exc}")
    if "aaai" in venues:
        rows += search_aaai(query, track, since, year, limit, get)
    seen, merged = set(), []
    for r in _rank(rows, len(rows)):
        key = re.sub(r"[^a-z0-9]", "", str(r.get("title", "")).lower())
        if key not in seen:
            seen.add(key)
            r.pop("_score", None)
            merged.append(r)
    rows = merged[:limit]
    if notes:
        rows.append({"note": "; ".join(notes)})
    return rows


def tracks(venue: str, query: str, get) -> list[dict]:
    venue = venue.lower()
    if venue == "aaai":
        words = [_stem(w) for w in terms(query)]
        return [{"track": n, "set": s} for s, n in aaai_sets(get)
                if all(w in n.lower() for w in words)]
    if venue in OPENREVIEW:
        return [{"note": f"{venue}: each paper's track is its primary_area (2024 on), shown "
                         f"with the results of `venue`; filter with --track \"<words>\" (e.g. "
                         f"--track \"interpretability\")."}]
    return [{"note": "The ACL Anthology records venues and volumes (long, short, findings, "
                     "demo, industry, srw, workshops) but no areas or tracks; filter with --kind, "
                     "and put the area's words in the query."}]


# ---- downloading a paper --------------------------------------------------------------------------


def pdf_url(ident: str, get) -> str:
    ident = ident.strip()
    m = re.search(r"aclanthology\.org/([^/]+?)(?:\.pdf)?/?$", ident) or \
        re.match(r"^(\d{4}\.[a-z0-9-]+\.\d+|[A-Z]\d{2}-\d{4})$", ident)
    if m:
        return f"https://aclanthology.org/{m.group(1)}.pdf"
    m = re.search(r"(?:arxiv\.org/(?:abs|pdf)/|^arxiv:)?(\d{4}\.\d{4,5})(v\d+)?$", ident, re.I)
    if m:
        return f"https://arxiv.org/pdf/{m.group(1)}"
    m = re.search(r"proceedings\.mlr\.press/v(\d+)/([^/.]+)\.html$", ident)
    if m:
        # Where PMLR's own bibliography points its PDFs.
        return (f"https://raw.githubusercontent.com/mlresearch/v{m.group(1)}/main/assets/"
                f"{m.group(2)}/{m.group(2)}.pdf")
    m = re.search(r"(?:openreview\.net/(?:forum|pdf)\?id=|^openreview:)([\w-]+)", ident)
    if m:
        return f"https://openreview.net/pdf?id={m.group(1)}"
    m = re.search(r"(10\.\d{4,9}/\S+)", ident)
    if m:
        w = json.loads(get("https://api.openalex.org/works/doi:" + urllib.parse.quote(m.group(1))))
        url = ((w.get("best_oa_location") or {}).get("pdf_url")
               or (w.get("open_access") or {}).get("oa_url"))
        if not url:
            raise SystemExit(f"No open-access PDF known for {m.group(1)}.")
        return url
    if ident.startswith("http"):
        return ident
    raise SystemExit(f"Cannot tell where {ident!r} is: give an ACL id, arXiv id, DOI, "
                     f"OpenReview id (openreview:ID), PMLR page or a PDF URL.")


def download(ident: str, folder: str, text: bool, get) -> list[dict]:
    url = pdf_url(ident, get)
    try:
        data = get(url)
    except SystemExit as exc:
        hint = (" OpenReview refuses downloads by script: find the paper's arXiv version "
                "(search its title) and download that." if "openreview" in url else "")
        raise SystemExit(f"{exc}{hint}") from None
    if not data.startswith(b"%PDF"):
        raise SystemExit(f"{url} did not return a PDF (it may need a browser or a login).")
    out = Path(folder).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    name = ident.rstrip("/").split("/")[-1].split("=")[-1]
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", re.sub(r"\.(pdf|html?)$", "", name))
    pdf = out / f"{name or 'paper'}.pdf"
    pdf.write_bytes(data)
    row = {"pdf": str(pdf), "from": url, "bytes": len(data)}
    if text:
        if not shutil.which("pdftotext"):
            row["text"] = "(pdftotext is not installed: read the PDF another way)"
        else:
            txt = pdf.with_suffix(".txt")
            subprocess.run(["pdftotext", "-layout", str(pdf), str(txt)], check=False,
                           timeout=120, capture_output=True)
            if txt.exists():
                body = txt.read_text(encoding="utf-8", errors="replace")
                row.update(text=str(txt), characters=len(body),
                           starts=" ".join(body.split())[:300])
    return [row]


def _short(text: str, n: int = 320) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"
