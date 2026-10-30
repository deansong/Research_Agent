"""
WHAT:  Research fields: a run's field picks the skills its team may use, each
       built-in role's default skills, and the prompts that speak the field's
       language (a voice note added to every role, and any prompt it
       overrides).
WHY:   One set for every field does not work: an NLP study and a biology one
       need different tools and are written up in different words. A new
       field is a folder, not code.
CONCEPT: research_agent/fields/<field>/
           field.toml    title; `skills`: the catalogue a run may use;
                         [roles]: each built-in role's skills by default
           prompts/      overrides of research_agent/prompts/<name>.md, and
                         voice.md, added to every role's instructions
       A run's field is in its config.json ("field"); a run without one is
       "nlp".
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

FIELDS_DIR = Path(__file__).with_name("fields")
DEFAULT = "nlp"


@dataclass(frozen=True)
class Field:
    name: str
    title: str
    skills: tuple[str, ...] | None
    """The skills a run in this field may use; None: every skill in the folder."""
    roles: dict = field(default_factory=dict)
    """Built-in role -> its skills by default ([] for none)."""

    @property
    def prompts(self) -> Path:
        return FIELDS_DIR / self.name / "prompts"


def names() -> list[str]:
    return sorted(p.parent.name for p in FIELDS_DIR.glob("*/field.toml"))


@lru_cache(maxsize=None)
def load(name: str = DEFAULT) -> Field:
    path = FIELDS_DIR / name / "field.toml"
    if not path.is_file():
        raise ValueError(f"No field {name!r}; the fields are {', '.join(names())}.")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    skills = data.get("skills")
    return Field(name=name, title=data.get("title", name),
                 skills=tuple(skills) if skills is not None else None,
                 roles={k: list(v) for k, v in (data.get("roles") or {}).items()})


def of_run(run_dir: str | os.PathLike | None) -> str:
    """The field a run is in: its config.json says, else the default."""
    if run_dir:
        path = Path(run_dir) / "config.json"
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("field") or DEFAULT
        except (OSError, ValueError):
            pass
    return DEFAULT


def allows(run_dir, skill: str) -> bool:
    """Whether a run's field lets its team use `skill`."""
    allowed = load(of_run(run_dir)).skills
    return allowed is None or skill in allowed
