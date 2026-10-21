"""
WHAT:  Skills for the design phase: reference text a model is handed, and the
       tools it can run.
WHY:   The discussor's checklist used to live inside its instructions, which
       made the persona long and hard to change; it is a skill now, handed to
       it in the prompt. The research agent's tools are a standalone script
       (research_tools.py) so any provider that can run a command can use
       them.
CONCEPT: A skill here is a Markdown file plus, optionally, a script --
       the same shape agy and Claude Code use for theirs. Edit the .md files
       to change what the models are told.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE / "research_tools.py"


def experiment_design() -> str:
    """The discussor's checklist."""
    return (HERE / "experiment_design.md").read_text().strip()


def research_template() -> str:
    """The research agent's tools and method, with `{tools}` where the command
    goes -- the text a person edits."""
    return (HERE / "research.md").read_text().strip()


def fill(text: str) -> str:
    """Spell the tools command out in full: the research agent runs in the
    session's workspace, a copy of the USER's project, where this package is
    not importable."""
    return text.replace("{tools}", f"{shlex.quote(sys.executable)} {shlex.quote(str(TOOLS))}")


def research() -> str:
    return fill(research_template())


def acl_paper_template() -> str:
    """How the paper node writes the paper, with `{tools}` and `{paper_dir}`
    where the command and the folder go -- the text a person edits."""
    return (HERE / "acl_paper.md").read_text().strip()


def acl_paper(paper_dir: str = "{artifacts_dir}/paper") -> str:
    """The paper skill, ready to send. `paper_dir` stays a placeholder by
    default, so the run fills in its own artifacts directory."""
    return fill(acl_paper_template()).replace("{paper_dir}", paper_dir)
