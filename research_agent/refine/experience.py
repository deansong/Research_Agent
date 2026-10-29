"""
WHAT:  The lessons library: after a refinement, what each edit did and at
       most one lesson that transfers, kept per role and read by that role's
       later proposers.
WHY:   harness2's second level of recursion -- what one refinement learned
       guides the next, so the same edit is not rediscovered or retried.
CONCEPT: Markdown, one file per role, in your prompts folder:
       <prompts dir>/library/<role>.md, newest last. Only lessons resting on
       evidence are kept; a refinement whose record shows no clear contrast
       adds its edit rows and no lesson.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from research_agent import prompt_files

#: Characters of a role's library its proposers see (the newest).
SHOWN_CHARS = 4000


class EditRow(BaseModel):
    change: str
    observed: str
    verdict: Literal["helped", "hurt", "no observed effect", "confounded"]


class Lessons(BaseModel):
    edits: list[EditRow] = Field(default_factory=list, max_length=16)
    cue: str = ""
    lesson: str = ""
    """"" when the record shows no reusable contrast."""
    evidence: str = ""
    confidence: str = ""


def library(role: str) -> Path:
    return prompt_files.user_dir() / "library" / f"{role}.md"


def lessons_for(role: str) -> str:
    """The newest lessons for `role`'s proposers ("" when there are none)."""
    path = library(role)
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    return text[-SHOWN_CHARS:]


def write(run, *, role: str, record: str, folder: Path) -> Lessons:
    """Ask for the edit rows and the lesson, and keep the lesson."""
    out, _ = run.call("refiner", brief=record, schema=Lessons, skills=None,
                      instructions=prompt_files.text("refine/experience", run.folder, role=role),
                      label="lessons")
    if out.lesson.strip():
        path = library(role)
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n## {stamp} (refinement {folder.name})\n"
                         f"- cue: {out.cue.strip()}\n- lesson: {out.lesson.strip()}\n"
                         f"- evidence: {out.evidence.strip()}\n"
                         f"- confidence: {out.confidence.strip()}\n")
    return out
