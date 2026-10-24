"""
WHAT:  Checking a researcher's sources: does the arXiv paper it cites exist?
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
TIMEOUT = 60


def arxiv_id(source: str) -> str | None:
    found = ARXIV.search(source or "")
    return found.group(1) if found else None


def tools_path(skills_dir: Path | None) -> Path | None:
    path = (skills_dir / "research" / "research_tools.py") if skills_dir else None
    return path if path and path.is_file() else None


@lru_cache(maxsize=512)
def _paper(tools: str, arxiv: str) -> bool | None:
    try:
        done = subprocess.run([sys.executable, tools, "paper", arxiv, "--json"],
                              capture_output=True, text=True, timeout=TIMEOUT)
        found = json.loads(done.stdout or "null")
    except (subprocess.SubprocessError, OSError, ValueError):
        return None
    if not isinstance(found, list):
        return None
    return bool(found)


def source(text: str, skills_dir: Path | None) -> bool | None:
    """True: the arXiv paper exists. False: it does not. None: not an arXiv
    source, or the check could not run (offline, no tool)."""
    arxiv = arxiv_id(text)
    tools = tools_path(skills_dir)
    if not arxiv or tools is None:
        return None
    return _paper(str(tools), arxiv)
