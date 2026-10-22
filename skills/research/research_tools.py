#!/usr/bin/env python3
"""
WHAT:  Literature and resource lookups for the research agent, as one script
       it runs from the command line.
WHY:   The research agent runs on any provider (antigravity by default), in
       the session's workspace -- a copy of the USER's project, not of this
       tool. So the skills are a standalone script, standard library only,
       called by absolute path, and every provider that can run a command can
       use them. Measured from this machine: Hugging Face's paper search
       answers in under a second with arXiv ids; arXiv's own API answers in
       ~30 s and rate-limits (HTTP 429), so it is the fallback, with backoff;
       Semantic Scholar refuses without a key, so it is not here.

Usage:
    research_tools.py papers   "lora linear probe"     # papers, newest-relevant first
    research_tools.py arxiv    "lora AND sentiment"    # arXiv's own search (slow)
    research_tools.py paper    2106.09685              # one paper: abstract, links
    research_tools.py cites    2106.09685              # how cited, and the top citers
    research_tools.py datasets "imdb sentiment"        # Hugging Face datasets
    research_tools.py models   "distilbert"            # Hugging Face models
Add --limit N (default 8) and --json for machine-readable output.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

AGENT = "codex-langgraph-agent research tools (mailto:none)"
TIMEOUT = 60


def _get(url: str, *, attempts: int = 4) -> bytes:
    """GET with backoff on 429/5xx: arXiv asks for patience, and a research
    turn that dies on the first refusal answers nothing."""
    delay = 5.0
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(url, headers={"User-Agent": AGENT})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == attempts:
                raise SystemExit(f"{url}: HTTP {exc.code} {exc.reason}")
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == attempts:
                raise SystemExit(f"{url}: {exc}")
        time.sleep(delay)
        delay *= 2
    raise SystemExit(f"{url}: no answer")


def _json(url: str):
    return json.loads(_get(url))


def _short(text: str, n: int = 320) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


# ---- the commands ----------------------------------------------------------

def papers(query: str, limit: int) -> list[dict]:
    rows = _json("https://huggingface.co/api/papers/search?q=" + urllib.parse.quote(query))
    out = []
    for row in rows[:limit]:
        paper = row.get("paper", row)
        out.append({
            "arxiv": paper.get("id", ""),
            "title": _short(paper.get("title") or row.get("title"), 200),
            "published": str(paper.get("publishedAt", ""))[:10],
            "upvotes": paper.get("upvotes", 0),
            "summary": _short(paper.get("summary") or row.get("summary")),
        })
    return out


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


def arxiv(query: str, limit: int) -> list[dict]:
    q = urllib.parse.urlencode({"search_query": f"all:{query}", "start": 0,
                                "max_results": limit, "sortBy": "relevance"})
    return _atom(_get(f"https://export.arxiv.org/api/query?{q}"))


def paper(arxiv_id: str, limit: int) -> list[dict]:
    arxiv_id = arxiv_id.replace("arXiv:", "").strip()
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
    except SystemExit:
        found = _atom(_get("https://export.arxiv.org/api/query?id_list="
                           + urllib.parse.quote(arxiv_id)))
        for item in found:
            item["url"] = f"https://arxiv.org/abs/{item['arxiv']}"
        return found


def cites(arxiv_id: str, limit: int) -> list[dict]:
    """How cited, and the most-cited works citing it -- found on OpenAlex by
    TITLE, and only when the titles match exactly. Measured: OpenAlex's
    record for LoRA (2106.09685) carries another paper's title, so both its
    DOI lookup and its search returned a wrong citation count -- refused
    here as "unknown" rather than reported."""
    title = paper(arxiv_id, 1)[0].get("title", "")
    if not title:
        raise SystemExit(f"No title found for {arxiv_id}.")
    found = _json("https://api.openalex.org/works?" + urllib.parse.urlencode(
        {"search": title, "sort": "cited_by_count:desc", "per-page": 5}))
    norm = lambda t: "".join(ch for ch in str(t).lower() if ch.isalnum())  # noqa: E731
    work = next((w for w in found.get("results", []) if norm(w.get("title")) == norm(title)), None)
    if work is None:
        return [{"arxiv": arxiv_id, "title": title,
                 "note": "OpenAlex has no work with exactly this title; citation count unknown"}]
    key = work.get("id", "").rsplit("/", 1)[-1]
    out = [{"arxiv": arxiv_id, "title": _short(work.get("title"), 200),
            "cited_by": work.get("cited_by_count", 0), "openalex": key}]
    citing = _json(f"https://api.openalex.org/works?filter=cites:{key}"
                   f"&sort=cited_by_count:desc&per-page={limit}")
    for row in citing.get("results", []):
        out.append({"citing": _short(row.get("title"), 200),
                    "year": row.get("publication_year"),
                    "cited_by": row.get("cited_by_count", 0),
                    "doi": row.get("doi") or ""})
    return out


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


def datasets(query: str, limit: int) -> list[dict]:
    return _hub("datasets", query, limit, "downloads")


def models(query: str, limit: int) -> list[dict]:
    return _hub("models", query, limit, "downloads")


COMMANDS = {"papers": papers, "arxiv": arxiv, "paper": paper, "cites": cites,
            "datasets": datasets, "models": models}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("Usage:")[0].strip(),
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("query", help="search words, or an arXiv id for paper/cites")
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    rows = COMMANDS[args.command](args.query, max(1, min(args.limit, 25)))
    if args.json:
        print(json.dumps(rows, indent=1))
        return 0
    if not rows:
        print("(nothing found)")
    for row in rows:
        print("\n".join(f"{k}: {v}" for k, v in row.items() if v not in ("", None)))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
