#!/usr/bin/env python3
"""
WHAT:  Literature and resource lookups for the research agent, as one script
       it runs from the command line.
WHY:   The research agent runs on any provider (antigravity by default), in
       the session's workspace -- a copy of the USER's project, not of this
       tool. So the skills are a standalone script, standard library only,
       called by absolute path, and every provider that can run a command can
       use them.

Where each search goes, measured from this machine:
    OpenAlex          ~250M works, every field and publisher; ~0.7 s. The
                      broad default. OPENALEX_API_KEY (optional) gives a
                      budget of its own; without it the IP shares a small one.
    Semantic Scholar  good for CS and citation graphs; refuses (HTTP 429)
                      without a key most of the time. SEMANTIC_SCHOLAR_API_KEY.
    PubMed            biomedicine (NCBI E-utilities); ~0.4 s. NCBI_API_KEY
                      (optional) raises the rate from 3 to 10 a second.
    Europe PMC        biomedicine and life-science preprints, open full text.
    Hugging Face      ML papers with arXiv ids, in under a second.
    arXiv             its own API: ~30 s and rate-limited, so a fallback.
    Google Scholar    has no API, and scraping it is against its terms; only
                      through SerpAPI, with SERPAPI_API_KEY.

Usage:
    research_tools.py search   "low-rank adaptation"   # OpenAlex, + Semantic Scholar with its key
    research_tools.py search   "sepsis prediction" --sources openalex,pubmed --since 2022
    research_tools.py openalex "..."  |  s2 "..."  |  pubmed "..."  |  europepmc "..."
    research_tools.py scholar  "..."                    # Google Scholar via SerpAPI (key)
    research_tools.py papers   "lora linear probe"     # Hugging Face: ML papers, arXiv ids
    research_tools.py arxiv    "lora AND sentiment"    # arXiv's own search (slow)
    research_tools.py paper    2106.09685              # one paper: arXiv id, DOI, PMID, ...
    research_tools.py cites    10.1038/s41586-021-03819-2
    research_tools.py datasets "imdb sentiment"        # Hugging Face datasets
    research_tools.py models   "distilbert"            # Hugging Face models
Add --limit N (default 8), --since YEAR, and --json for machine-readable output.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

AGENT = "codex-langgraph-agent research tools (mailto:none)"
TIMEOUT = 60


class NotFound(Exception):
    """The source answered: there is no such thing (HTTP 404)."""


def _get(url: str, *, attempts: int = 4, headers: dict | None = None, delay: float = 5.0) -> bytes:
    """GET with backoff on 429/5xx: arXiv and Semantic Scholar ask for
    patience, and a research turn that dies on the first refusal answers
    nothing. A 404 is an answer: NotFound."""
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(url, headers={"User-Agent": AGENT, **(headers or {})})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise NotFound(url) from None
            if exc.code not in (429, 500, 502, 503, 504) or attempt == attempts:
                raise SystemExit(f"{_safe(url)}: HTTP {exc.code} {exc.reason}"
                                 + (_hint(url) if exc.code in (401, 403, 429) else ""))
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == attempts:
                raise SystemExit(f"{_safe(url)}: {exc}")
        time.sleep(delay)
        delay *= 2
    raise SystemExit(f"{_safe(url)}: no answer")


def _safe(url: str) -> str:
    """A URL fit to print: keys taken out."""
    return re.sub(r"(api_key|key)=[^&]+", r"\1=***", url)


def _hint(url: str) -> str:
    if "semanticscholar" in url:
        return " -- Semantic Scholar limits keyless use; set SEMANTIC_SCHOLAR_API_KEY, or use openalex"
    if "openalex" in url:
        return " -- OpenAlex's shared budget is spent; set OPENALEX_API_KEY"
    if "serpapi" in url:
        return " -- check SERPAPI_API_KEY"
    return ""


def _json(url: str, **kw):
    return json.loads(_get(url, **kw))


def _short(text: str, n: int = 320) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _norm(title) -> str:
    return "".join(ch for ch in str(title or "").lower() if ch.isalnum())


def _doi(value) -> str:
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", str(value or ""), flags=re.I).strip()


# ---- what an id is -----------------------------------------------------------------

ARXIV_ID = re.compile(r"^(?:arxiv:)?(\d{4}\.\d{4,5})(v\d+)?$", re.I)
DOI_ID = re.compile(r"^(?:doi:|https?://(?:dx\.)?doi\.org/)?(10\.\d{4,9}/\S+)$", re.I)
PMID_ID = re.compile(r"^(?:pmid:?\s*|https?://pubmed\.ncbi\.nlm\.nih\.gov/)?(\d{5,9})/?$", re.I)
OPENALEX_ID = re.compile(r"^(?:https?://openalex\.org/)?(W\d+)$", re.I)
S2_ID = re.compile(r"^(?:s2:)?([0-9a-f]{40})$", re.I)


def kind_of(ident: str) -> tuple[str, str]:
    """("arxiv" | "doi" | "pmid" | "openalex" | "s2", the bare id)."""
    ident = ident.strip()
    for kind, pattern in (("arxiv", ARXIV_ID), ("doi", DOI_ID), ("openalex", OPENALEX_ID),
                          ("s2", S2_ID), ("pmid", PMID_ID)):
        found = pattern.match(ident)
        if found:
            return kind, found.group(1)
    raise SystemExit(f"{ident!r} is not an arXiv id, DOI, PMID, OpenAlex id (W...) or "
                     f"Semantic Scholar id.")


# ---- OpenAlex: everything --------------------------------------------------------------

def _openalex_url(path: str, params: dict) -> str:
    key = os.environ.get("OPENALEX_API_KEY")
    mail = os.environ.get("OPENALEX_EMAIL")
    params = {**params, **({"api_key": key} if key else {}), **({"mailto": mail} if mail else {})}
    return f"https://api.openalex.org/{path}?" + urllib.parse.urlencode(params)


def _abstract(inverted: dict | None) -> str:
    """OpenAlex keeps abstracts as word -> positions."""
    if not inverted:
        return ""
    at = {}
    for word, places in inverted.items():
        for p in places:
            at[p] = word
    return " ".join(at[i] for i in sorted(at))


def _from_openalex(w: dict, *, full: bool = False) -> dict:
    ids = w.get("ids") or {}
    loc = w.get("primary_location") or {}
    source = (loc.get("source") or {}).get("display_name") or ""
    arxiv = ""
    for l in w.get("locations") or []:
        m = re.search(r"arxiv\.org/abs/(\d{4}\.\d{4,5})", str(l.get("landing_page_url") or ""))
        if m:
            arxiv = m.group(1)
            break
    abstract = _abstract(w.get("abstract_inverted_index"))
    return {
        "title": _short(w.get("title") or w.get("display_name"), 220),
        "year": w.get("publication_year"),
        "venue": source,
        "authors": ", ".join(a.get("author", {}).get("display_name", "")
                             for a in (w.get("authorships") or [])[:4]),
        "doi": _doi(w.get("doi")),
        "arxiv": arxiv,
        "pmid": str(ids.get("pmid") or "").rsplit("/", 1)[-1],
        "openalex": str(w.get("id") or "").rsplit("/", 1)[-1],
        "cited_by": w.get("cited_by_count"),
        "type": w.get("type") or "",
        "open_access": (w.get("open_access") or {}).get("oa_url") or "",
        ("abstract" if full else "summary"): abstract if full else _short(abstract),
    }


def openalex(query: str, limit: int, since: int | None = None) -> list[dict]:
    params = {"search": query, "per-page": limit}
    if since:
        params["filter"] = f"from_publication_date:{since}-01-01"
    found = _json(_openalex_url("works", params))
    return [_from_openalex(w) for w in found.get("results", [])]


# ---- Semantic Scholar -------------------------------------------------------------------

S2_FIELDS = "title,year,venue,authors,externalIds,citationCount,abstract,openAccessPdf,url"


def _s2_key() -> str:
    return os.environ.get("SEMANTIC_SCHOLAR_API_KEY") or os.environ.get("S2_API_KEY") or ""


def _s2_headers() -> dict:
    """Semantic Scholar refuses keyless use almost every time (measured: HTTP
    429), so without a key it is not asked at all."""
    if not _s2_key():
        raise SystemExit("Semantic Scholar is not used without a key: set "
                         "SEMANTIC_SCHOLAR_API_KEY. Use openalex (or search) instead.")
    return {"x-api-key": _s2_key()}


def _from_s2(p: dict, *, full: bool = False) -> dict:
    ext = p.get("externalIds") or {}
    return {
        "title": _short(p.get("title"), 220),
        "year": p.get("year"),
        "venue": p.get("venue") or "",
        "authors": ", ".join(a.get("name", "") for a in (p.get("authors") or [])[:4]),
        "doi": ext.get("DOI") or "",
        "arxiv": ext.get("ArXiv") or "",
        "pmid": str(ext.get("PubMed") or ""),
        "s2": p.get("paperId") or "",
        "cited_by": p.get("citationCount"),
        "open_access": (p.get("openAccessPdf") or {}).get("url") or "",
        ("abstract" if full else "summary"): (p.get("abstract") or "") if full
        else _short(p.get("abstract")),
    }


def s2(query: str, limit: int, since: int | None = None, *, patient: bool = True) -> list[dict]:
    headers = _s2_headers()
    params = {"query": query, "limit": limit, "fields": S2_FIELDS}
    if since:
        params["year"] = f"{since}-"
    found = _json("https://api.semanticscholar.org/graph/v1/paper/search?"
                  + urllib.parse.urlencode(params), headers=headers,
                  attempts=4 if patient else 2, delay=3.0)
    return [_from_s2(p) for p in found.get("data") or []]


# ---- PubMed and Europe PMC -----------------------------------------------------------------

def _eutils(tool: str, params: dict) -> bytes:
    key = os.environ.get("NCBI_API_KEY")
    params = {**params, "tool": "research_tools", **({"api_key": key} if key else {})}
    return _get(f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/{tool}.fcgi?"
                + urllib.parse.urlencode(params), delay=1.0)


def _summaries(pmids: list[str]) -> list[dict]:
    if not pmids:
        return []
    found = json.loads(_eutils("esummary", {"db": "pubmed", "id": ",".join(pmids),
                                            "retmode": "json"})).get("result", {})
    out = []
    for pmid in pmids:
        r = found.get(pmid) or {}
        if not r or r.get("error"):
            continue
        ids = {a.get("idtype"): a.get("value") for a in r.get("articleids") or []}
        out.append({"title": _short(r.get("title"), 220),
                    "year": str(r.get("pubdate", ""))[:4],
                    "venue": r.get("fulljournalname") or r.get("source") or "",
                    "authors": ", ".join(a.get("name", "") for a in (r.get("authors") or [])[:4]),
                    "doi": ids.get("doi") or "", "pmid": pmid, "pmc": ids.get("pmc") or "",
                    "type": ", ".join(r.get("pubtype") or []),
                    "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"})
    return out


def pubmed(query: str, limit: int, since: int | None = None) -> list[dict]:
    params = {"db": "pubmed", "term": query, "retmax": limit, "retmode": "json",
              "sort": "relevance"}
    if since:
        params.update(datetype="pdat", mindate=str(since), maxdate="3000")
    ids = json.loads(_eutils("esearch", params)).get("esearchresult", {}).get("idlist", [])
    return _summaries(ids)


def _pubmed_abstract(pmid: str) -> str:
    root = ET.fromstring(_eutils("efetch", {"db": "pubmed", "id": pmid, "retmode": "xml"}))
    parts = []
    for node in root.iter("AbstractText"):
        label = node.get("Label")
        text = "".join(node.itertext()).strip()
        parts.append(f"{label}: {text}" if label else text)
    return " ".join(parts)


def europepmc(query: str, limit: int, since: int | None = None) -> list[dict]:
    q = f"({query}) AND PUB_YEAR:[{since} TO 3000]" if since else query
    found = _json("https://www.ebi.ac.uk/europepmc/webservices/rest/search?" + urllib.parse.urlencode(
        {"query": q, "format": "json", "pageSize": limit, "resultType": "core"}))
    out = []
    for r in (found.get("resultList") or {}).get("result", []):
        out.append({"title": _short(r.get("title"), 220), "year": r.get("pubYear"),
                    "venue": (r.get("journalInfo") or {}).get("journal", {}).get("title")
                    or r.get("bookOrReportDetails", {}).get("publisher") or r.get("source", ""),
                    "authors": _short(r.get("authorString"), 120),
                    "doi": r.get("doi") or "", "pmid": r.get("pmid") or "",
                    "pmc": r.get("pmcid") or "", "cited_by": r.get("citedByCount"),
                    "type": "preprint" if r.get("source") == "PPR" else "",
                    "summary": _short(re.sub(r"<[^>]+>", " ", r.get("abstractText") or ""))})
    return out


# ---- Google Scholar, only through SerpAPI ----------------------------------------------------

def scholar(query: str, limit: int, since: int | None = None) -> list[dict]:
    key = os.environ.get("SERPAPI_API_KEY")
    if not key:
        raise SystemExit("Google Scholar has no API, and scraping it is against its terms. "
                         "Set SERPAPI_API_KEY to search it through SerpAPI; meanwhile "
                         "`search` (OpenAlex + Semantic Scholar) covers most of what it indexes.")
    params = {"engine": "google_scholar", "q": query, "num": min(limit, 20), "api_key": key}
    if since:
        params["as_ylo"] = since
    found = _json("https://serpapi.com/search.json?" + urllib.parse.urlencode(params))
    out = []
    for r in found.get("organic_results") or []:
        info = r.get("publication_info") or {}
        out.append({"title": _short(r.get("title"), 220), "venue": _short(info.get("summary"), 160),
                    "cited_by": ((r.get("inline_links") or {}).get("cited_by") or {}).get("total"),
                    "url": r.get("link") or "", "summary": _short(r.get("snippet"))})
    return out


# ---- several at once, merged -----------------------------------------------------------------

SOURCES = {"openalex": openalex, "s2": s2, "pubmed": pubmed, "europepmc": europepmc,
           "scholar": scholar}
#: Sources that are asked only with a key, and the variable that holds it.
NEEDS_KEY = {"s2": "SEMANTIC_SCHOLAR_API_KEY", "scholar": "SERPAPI_API_KEY"}


def _keyed(name: str) -> bool:
    """The source can be asked: it needs no key, or its key is set."""
    if name == "s2":
        return bool(_s2_key())
    var = NEEDS_KEY.get(name)
    return not var or bool(os.environ.get(var))


def default_sources() -> tuple[str, ...]:
    """OpenAlex always; Semantic Scholar when its key is set. Google Scholar
    is never a default: each SerpAPI search spends a paid credit, so it is
    asked only by name (`scholar`, or --sources ...,scholar)."""
    return ("openalex",) + (("s2",) if _keyed("s2") else ())


def search(query: str, limit: int, since: int | None = None,
           sources: tuple[str, ...] | None = None) -> list[dict]:
    """Every source asked at once; the same paper found twice is one row,
    with its ids put together and `found_in` saying where. A source that
    cannot answer is said in a last row, not fatal; one that needs a key
    nobody set is not asked, and said so."""
    sources = default_sources() if sources is None else sources
    skipped = [f"{s} (no {NEEDS_KEY[s]})" for s in sources if not _keyed(s)]
    sources = tuple(s for s in sources if _keyed(s)) or ("openalex",)
    def ask(name):
        try:
            if name == "s2":
                return name, s2(query, limit, since, patient=False), ""
            return name, SOURCES[name](query, limit, since), ""
        except (SystemExit, NotFound, Exception) as exc:  # noqa: BLE001 - one source failing is an answer
            why = re.sub(r"https?://\S+: ", "", str(exc)) or type(exc).__name__
            return name, [], why
    with ThreadPoolExecutor(max_workers=len(sources)) as pool:
        answers = list(pool.map(ask, sources))
    merged: list[dict] = []
    seen: dict[str, dict] = {}
    # Interleave: each source's best first, so one source does not crowd out the rest.
    for rank in range(limit):
        for name, rows, _ in answers:
            if rank >= len(rows):
                continue
            row = rows[rank]
            keys = [k for k in (row.get("doi", "").lower(), row.get("arxiv"), row.get("pmid"),
                                _norm(row.get("title"))) if k]
            hit = next((seen[k] for k in keys if k in seen), None)
            if hit is None:
                hit = {**row, "found_in": name}
                merged.append(hit)
            else:
                hit["found_in"] += f", {name}"
                for k, v in row.items():
                    if v not in ("", None) and hit.get(k) in ("", None):
                        hit[k] = v
            for k in keys:
                seen[k] = hit
    failed = [f"{name}: {why}" for name, _, why in answers if why]
    if failed:
        merged.append({"note": "not answered -- " + " | ".join(failed)})
    if skipped:
        merged.append({"note": "not asked -- " + ", ".join(skipped)})
    return merged


# ---- Hugging Face and arXiv ------------------------------------------------------------------

def papers(query: str, limit: int, since: int | None = None) -> list[dict]:
    rows = _json("https://huggingface.co/api/papers/search?q=" + urllib.parse.quote(query))
    out = []
    for row in rows:
        paper = row.get("paper", row)
        published = str(paper.get("publishedAt", ""))[:10]
        if since and published[:4].isdigit() and int(published[:4]) < since:
            continue
        out.append({
            "arxiv": paper.get("id", ""),
            "title": _short(paper.get("title") or row.get("title"), 200),
            "published": published,
            "upvotes": paper.get("upvotes", 0),
            "summary": _short(paper.get("summary") or row.get("summary")),
        })
    return out[:limit]


_ATOM = {"a": "http://www.w3.org/2005/Atom"}


def _atom(feed: bytes) -> list[dict]:
    root = ET.fromstring(feed)
    out = []
    for entry in root.findall("a:entry", _ATOM):
        link = entry.findtext("a:id", "", _ATOM)
        out.append({
            "arxiv": link.rsplit("/abs/", 1)[-1],
            "title": _short(entry.findtext("a:title", "", _ATOM), 200),
            "published": entry.findtext("a:published", "", _ATOM)[:10],
            "authors": ", ".join(a.findtext("a:name", "", _ATOM)
                                 for a in entry.findall("a:author", _ATOM)[:4]),
            "summary": _short(entry.findtext("a:summary", "", _ATOM)),
        })
    return out


def arxiv(query: str, limit: int, since: int | None = None) -> list[dict]:
    q = urllib.parse.urlencode({"search_query": f"all:{query}", "start": 0,
                                "max_results": limit, "sortBy": "relevance"})
    rows = _atom(_get(f"https://export.arxiv.org/api/query?{q}"))
    return [r for r in rows if not since or r["published"][:4] >= str(since)]


# ---- one paper, and who cites it --------------------------------------------------------------

def _arxiv_paper(arxiv_id: str) -> list[dict]:
    try:
        row = _json(f"https://huggingface.co/api/papers/{urllib.parse.quote(arxiv_id)}")
        return [{
            "arxiv": row.get("id", arxiv_id),
            "title": _short(row.get("title"), 300),
            "published": str(row.get("publishedAt", ""))[:10],
            "authors": ", ".join(a.get("name", "") for a in row.get("authors", [])[:8]),
            "abstract": " ".join(str(row.get("summary", "")).split()),
            "code": row.get("githubRepo", "") or "",
            "url": f"https://arxiv.org/abs/{arxiv_id}",
            "full_text": f"https://arxiv.org/html/{arxiv_id}",
        }]
    except (SystemExit, NotFound):
        found = _atom(_get("https://export.arxiv.org/api/query?id_list="
                           + urllib.parse.quote(arxiv_id)))
        for item in found:
            item["url"] = f"https://arxiv.org/abs/{item['arxiv']}"
        return found


def paper(ident: str, limit: int = 1, since: int | None = None) -> list[dict]:
    """One paper by arXiv id, DOI, PMID, OpenAlex or Semantic Scholar id:
    its full abstract and links. [] when there is no such paper."""
    kind, key = kind_of(ident)
    try:
        if kind == "arxiv":
            return _arxiv_paper(key)
        if kind == "pmid":
            found = _summaries([key])
            if found:
                found[0]["abstract"] = _pubmed_abstract(key)
            return found
        if kind == "s2":
            return [_from_s2(_json(f"https://api.semanticscholar.org/graph/v1/paper/{key}?fields="
                                   + S2_FIELDS, headers=_s2_headers()), full=True)]
        path = f"works/doi:{key}" if kind == "doi" else f"works/{key}"
        try:
            row = _from_openalex(_json(_openalex_url(path, {})), full=True)
        except NotFound:
            if kind != "doi":
                raise
            # Not in OpenAlex: Crossref is the DOI registry itself.
            m = _json("https://api.crossref.org/works/" + urllib.parse.quote(key)).get("message", {})
            row = {"title": _short((m.get("title") or [""])[0], 300),
                   "year": ((m.get("issued") or {}).get("date-parts") or [[None]])[0][0],
                   "venue": (m.get("container-title") or [""])[0], "doi": key,
                   "abstract": re.sub(r"<[^>]+>", "", m.get("abstract") or "")}
        row["url"] = f"https://doi.org/{row['doi']}" if row.get("doi") else row.get("open_access", "")
        return [row]
    except NotFound:
        return []


def cites(ident: str, limit: int, since: int | None = None) -> list[dict]:
    """How often cited, and the most-cited works citing it -- on OpenAlex.
    A DOI, PMID or OpenAlex id is looked up directly. An arXiv id is found by
    TITLE, and only when the titles match exactly. Measured: OpenAlex's
    record for LoRA (2106.09685) carries another paper's title, so a loose
    match returned a wrong citation count -- refused here as "unknown"."""
    kind, key = kind_of(ident)
    work = None
    try:
        if kind in ("doi", "pmid", "openalex"):
            path = {"doi": f"works/doi:{key}", "pmid": f"works/pmid:{key}", "openalex": f"works/{key}"}[kind]
            work = _json(_openalex_url(path, {}))
        else:
            found = paper(ident, 1)
            title = found[0].get("title", "") if found else ""
            if not title:
                return []
            if kind == "arxiv":
                # arXiv's own DOI first: OpenAlex indexes it.
                try:
                    work = _json(_openalex_url(f"works/doi:10.48550/arXiv.{key}", {}))
                except NotFound:
                    work = None
                if work is not None and _norm(work.get("title")) != _norm(title):
                    work = None
            if work is None:
                hits = _json(_openalex_url("works", {"search": title, "sort": "cited_by_count:desc",
                                                     "per-page": 5}))
                work = next((w for w in hits.get("results", [])
                             if _norm(w.get("title")) == _norm(title)), None)
            if work is None:
                return _s2_cites(ident, kind, key, title, limit)
    except NotFound:
        return []
    oa = str(work.get("id", "")).rsplit("/", 1)[-1]
    out = [{"id": ident, "title": _short(work.get("title"), 200),
            "cited_by": work.get("cited_by_count", 0), "openalex": oa}]
    params = {"filter": f"cites:{oa}" + (f",from_publication_date:{since}-01-01" if since else ""),
              "sort": "cited_by_count:desc", "per-page": limit + 1}
    for row in _json(_openalex_url("works", params)).get("results", [])[:limit + 1]:
        if str(row.get("id", "")).endswith(oa) or len(out) > limit:
            continue                                   # OpenAlex lists some works as citing themselves
        out.append({"citing": _short(row.get("title"), 200), "year": row.get("publication_year"),
                    "cited_by": row.get("cited_by_count", 0), "doi": _doi(row.get("doi"))})
    return out


def _s2_cites(ident: str, kind: str, key: str, title: str, limit: int) -> list[dict]:
    """Semantic Scholar's count, when OpenAlex has no record to trust."""
    s2key = {"arxiv": f"arXiv:{key}", "doi": f"DOI:{key}", "pmid": f"PMID:{key}"}.get(kind, key)
    if not _s2_key():
        return [{"id": ident, "title": title,
                 "note": "OpenAlex has no work with exactly this title; citation count unknown "
                         "(Semantic Scholar, which might know, needs SEMANTIC_SCHOLAR_API_KEY)"}]
    try:
        p = _json(f"https://api.semanticscholar.org/graph/v1/paper/{urllib.parse.quote(s2key)}"
                  f"?fields=title,citationCount,influentialCitationCount", headers=_s2_headers(),
                  attempts=3, delay=3.0)
    except (SystemExit, NotFound) as exc:
        why = re.sub(r"https?://\S+: ", "", str(exc)) or "not found"
        return [{"id": ident, "title": title,
                 "note": f"OpenAlex has no work with exactly this title, and Semantic Scholar "
                         f"did not answer ({why}); citation count unknown"}]
    out = [{"id": ident, "title": _short(p.get("title"), 200), "cited_by": p.get("citationCount"),
            "influential": p.get("influentialCitationCount"), "source": "Semantic Scholar"}]
    try:
        citing = _json(f"https://api.semanticscholar.org/graph/v1/paper/{urllib.parse.quote(s2key)}"
                       f"/citations?fields=title,year,citationCount,externalIds&limit=100",
                       headers=_s2_headers(), attempts=2, delay=3.0).get("data") or []
    except (SystemExit, NotFound):
        citing = []
    top = sorted((c.get("citingPaper") or {} for c in citing),
                 key=lambda c: -(c.get("citationCount") or 0))[:limit]
    out += [{"citing": _short(c.get("title"), 200), "year": c.get("year"),
             "cited_by": c.get("citationCount"), "doi": (c.get("externalIds") or {}).get("DOI") or ""}
            for c in top]
    return out


# ---- Hugging Face Hub -------------------------------------------------------------------------

def _hub(kind: str, query: str, limit: int, sort: str) -> list[dict]:
    q = urllib.parse.urlencode({"search": query, "limit": limit, "sort": sort,
                                "direction": -1})
    out = []
    for row in _json(f"https://huggingface.co/api/{kind}?{q}"):
        tags = [t for t in row.get("tags", []) if t.split(":")[0] in
                ("license", "size_categories", "task_categories", "language")]
        out.append({"id": row.get("id", ""), "downloads": row.get("downloads", 0),
                    "likes": row.get("likes", 0),
                    "pipeline": row.get("pipeline_tag", ""),
                    "tags": ", ".join(tags[:8]),
                    "url": f"https://huggingface.co/{'datasets/' if kind == 'datasets' else ''}"
                           f"{row.get('id', '')}"})
    return out


def datasets(query: str, limit: int, since: int | None = None) -> list[dict]:
    return _hub("datasets", query, limit, "downloads")


def models(query: str, limit: int, since: int | None = None) -> list[dict]:
    return _hub("models", query, limit, "downloads")


COMMANDS = {"search": search, "openalex": openalex, "s2": s2, "pubmed": pubmed,
            "europepmc": europepmc, "scholar": scholar, "papers": papers, "arxiv": arxiv,
            "paper": paper, "cites": cites, "datasets": datasets, "models": models}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("Usage:")[0].strip(),
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("query", help="search words; for paper/cites an arXiv id, DOI, PMID, "
                                      "OpenAlex id (W...) or Semantic Scholar id")
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--since", type=int, help="only work published in or after this year")
    parser.add_argument("--sources", help="for search: a comma list of "
                                          f"{', '.join(SOURCES)} (default: openalex, and s2 when "
                                          f"its key is set; scholar only when named)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    limit = max(1, min(args.limit, 25))
    if args.command == "search":
        sources = tuple(s.strip() for s in (args.sources or ",".join(default_sources())).split(",")
                        if s.strip())
        unknown = [s for s in sources if s not in SOURCES]
        if unknown:
            parser.error(f"unknown source(s): {', '.join(unknown)}; they are {', '.join(SOURCES)}")
        rows = search(args.query, limit, args.since, sources)
    else:
        try:
            rows = COMMANDS[args.command](args.query, limit, args.since)
        except NotFound:
            rows = []
    if args.json:
        print(json.dumps(rows, indent=1, ensure_ascii=False))
        return 0
    if not rows:
        print("(nothing found)")
    for row in rows:
        print("\n".join(f"{k}: {v}" for k, v in row.items() if v not in ("", None)))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
