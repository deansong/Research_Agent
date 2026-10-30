"""
WHAT:  Checking a researcher's sources: does the paper it cites -- by arXiv
       id, ACL Anthology id, DOI or PMID -- exist?
WHY:   Models invent references. Evidence carries `verified` -- True, False,
       or None when the source is not something that can be checked here --
       so the plan and the user can tell a citation from a guess.
CONCEPT: Uses the research skill's own tool (skills/research/research_tools.py
       paper <id> --json): a list with the paper, or [] when there is none.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

ARXIV = re.compile(r"(?<![\d.])(\d{4}\.\d{4,5})(?:v\d+)?(?![\d])")
DOI = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)", re.I)
PMID = re.compile(r"(?:\bPMID:?\s*|pubmed\.ncbi\.nlm\.nih\.gov/)(\d{5,9})\b", re.I)
TIMEOUT = 60


def arxiv_id(source: str) -> str | None:
    found = ARXIV.search(source or "")
    return found.group(1) if found else None


ACL = re.compile(r"aclanthology\.org/(\d{4}\.[a-z0-9-]+\.\d+|[A-Z]\d{2}-\d{4})")


def paper_id(source: str) -> str | None:
    """The id to look a cited paper up by: an ACL Anthology id (from its
    URL), a DOI (arXiv's own DOIs as their arXiv id), else a PMID, else an
    arXiv id; None for anything else (a dataset, a model, a plain URL)."""
    acl = ACL.search(source or "")
    if acl:
        return acl.group(1)
    doi = DOI.search(source or "")
    if doi:
        bare = doi.group(1).rstrip(".,;)]}\"'")      # BibTeX's braces and quotes too
        arxiv = re.match(r"10\.48550/arxiv\.(\d{4}\.\d{4,5})", bare, re.I)
        return arxiv.group(1) if arxiv else bare
    pmid = PMID.search(source or "")
    if pmid:
        return f"PMID:{pmid.group(1)}"
    return arxiv_id(source)


def tools_path(skills_dir: Path | None) -> Path | None:
    path = (skills_dir / "research" / "research_tools.py") if skills_dir else None
    return path if path and path.is_file() else None


@lru_cache(maxsize=512)
def _paper(tools: str, ident: str) -> bool | None:
    try:
        done = subprocess.run([sys.executable, tools, "paper", ident, "--json"],
                              capture_output=True, text=True, timeout=TIMEOUT)
        found = json.loads(done.stdout or "null")
    except (subprocess.SubprocessError, OSError, ValueError):
        return None
    if not isinstance(found, list):
        return None
    return bool(found)


def source(text: str, skills_dir: Path | None) -> bool | None:
    """True: the paper exists. False: it does not. None: not a paper id
    (arXiv, DOI, PMID), or the check could not run (offline, no tool)."""
    ident = paper_id(text)
    tools = tools_path(skills_dir)
    if not ident or tools is None:
        return None
    return _paper(str(tools), ident)
