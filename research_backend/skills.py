"""
WHAT:  Skills: a folder of <name>/SKILL.md -- the format Claude Code and
       Antigravity use -- and which of them a session is given.
WHY:   Reusable know-how (how to run a power analysis, how to write an ACL
       paper) should reach a model on any provider the same way, chosen per
       session rather than wired into code.
CONCEPT: A skill is plain files a model reads. A session's choice is one of:

         None        no skills
         "auto"      a list -- name, description, where its SKILL.md is --
                     and the model opens the ones its task needs
         [names]     those skills' text, in full

       Self-contained on purpose: the folder is always passed in, so this
       works for any project's skills, not only this repository's.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

SKILL_FILE = "SKILL.md"
ENV = "RESEARCH_BACKEND_SKILLS_DIR"


@dataclass(frozen=True)
class Skill:
    name: str
    """The folder's name -- what a choice refers to."""
    description: str
    """When to use it, from the front matter -- what "auto" shows the model."""
    folder: Path
    body: str
    """The instructions, without the front matter."""

    def __str__(self) -> str:
        return f"{self.name}: {self.description}"


class SkillList(list):
    """Skills, printed as a table; `chosen` names are marked with *."""

    def __init__(self, items=(), folder: Path | None = None, chosen=None):
        super().__init__(items)
        self.folder = folder
        self.chosen = chosen

    @property
    def names(self) -> list[str]:
        return [s.name for s in self]

    def __str__(self) -> str:
        if self.folder is None:
            return f"(no skills folder: pass skills_dir=, or set ${ENV})"
        if not self:
            return f"(no skills in {self.folder})"
        picked = set(self.chosen) if isinstance(self.chosen, list) else set()
        width = max(len(s.name) for s in self)
        lines = [f"{len(self)} skills in {self.folder}"
                 + (" (all offered: auto)" if self.chosen == "auto" else "")]
        for s in self:
            mark = "*" if s.name in picked else " "
            lines.append(f" {mark} {s.name:<{width}}  {_one_line(s.description, 90)}")
        return "\n".join(lines)


def folder_of(skills_dir) -> Path | None:
    """The skills folder: the one given, else $RESEARCH_BACKEND_SKILLS_DIR,
    else None."""
    chosen = skills_dir or os.environ.get(ENV)
    return Path(chosen).expanduser().resolve() if chosen else None


def discover(skills_dir) -> list[Skill]:
    """Every skill in the folder, by name. Read fresh each time: adding a skill
    is adding a folder, and should show at once."""
    root = folder_of(skills_dir)
    if root is None or not root.is_dir():
        return []
    return [s for s in (_parse(p) for p in sorted(root.glob(f"*/{SKILL_FILE}"))) if s]


def get(skills_dir, name: str) -> Skill | None:
    return next((s for s in discover(skills_dir) if s.name == name), None)


def checked(choice, skills_dir) -> tuple[None | str | list[str], str | None]:
    """(choice, folder) normalised and checked; raises ValueError saying what
    is wrong and what there is."""
    if choice in (None, "none", [], ()):
        return None, (str(folder_of(skills_dir)) if folder_of(skills_dir) else None)
    root = folder_of(skills_dir)
    if root is None:
        raise ValueError(f"Choosing skills needs a skills folder: pass skills_dir=..., "
                         f"or set ${ENV}.")
    if not root.is_dir():
        raise FileNotFoundError(f"No skills folder at {root}.")
    if choice == "auto":
        return "auto", str(root)
    if isinstance(choice, str):
        choice = [choice]
    names = [str(n) for n in choice]
    known = {s.name for s in discover(root)}
    unknown = [n for n in names if n not in known]
    if unknown:
        raise ValueError(f"No skill called {', '.join(map(repr, unknown))} in {root}. "
                         f"It has: {', '.join(sorted(known)) or 'none'}.")
    return list(dict.fromkeys(names)), str(root)


def block(choice, skills_dir, *, workspace: str = "") -> str:
    """What the model is sent for a choice -- "" for none."""
    if choice is None:
        return ""
    root = folder_of(skills_dir)
    if choice == "auto":
        available = discover(root)
        if not available:
            return ""
        lines = [f"  - {s.name}: {s.description}\n    {s.folder / SKILL_FILE}"
                 for s in available]
        return ("SKILLS YOU CAN USE -- shared instructions and tools. When one "
                "fits your task, read its SKILL.md first and follow it. Never "
                "change these files.\n" + "\n".join(lines))
    parts = []
    for name in choice:
        skill = get(root, name)
        if skill is not None:
            parts.append(f"=== SKILL: {skill.name} (folder: {skill.folder}) ===\n"
                         + expand(skill.body, skill, root, workspace=workspace))
    if not parts:
        return ""
    return ("SKILLS FOR THIS TASK -- follow them. Their folders are read-only "
            "to you.\n\n" + "\n\n".join(parts))


def expand(text: str, skill: Skill | None, skills_dir, *, workspace: str = "") -> str:
    """Fill a skill's few placeholders. Plain replacement, not str.format:
    skill text is full of braces (LaTeX, JSON) that are not placeholders, and
    any other {name} -- another program's -- is left as written."""
    root = str(folder_of(skills_dir) or "")
    pairs = {"{skills_dir}": root, "{python}": sys.executable,
             "{skill_dir}": str(skill.folder) if skill else root}
    if workspace:
        pairs["{workspace}"] = workspace
    for token, value in pairs.items():
        text = text.replace(token, value)
    return text


# ---- reading SKILL.md --------------------------------------------------------------

_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)


def _parse(path: Path) -> Skill | None:
    """A SKILL.md: `---` front matter (name, description), then the text.
    The folder name is the skill's name, whatever the front matter says."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    body, meta = text, {}
    found = _FRONT.match(text)
    if found:
        body, meta = found.group(2), _front_matter(found.group(1))
    return Skill(name=path.parent.name, description=meta.get("description", ""),
                 folder=path.parent, body=body.strip())


def _front_matter(text: str) -> dict[str, str]:
    """The top-level `key: value` pairs of the YAML front matter -- enough of
    YAML for what skills use: plain or quoted values, and block values
    (`description: >` or `|`, then indented lines)."""
    meta: dict[str, str] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line.strip() or line[0].isspace() or ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip()
        if value[:1] in (">", "|"):
            held = []
            while i < len(lines) and (not lines[i].strip() or lines[i][0].isspace()):
                held.append(lines[i].strip())
                i += 1
            value = ("\n" if value.startswith("|") else " ").join(h for h in held if h).strip()
        meta[key.strip().lower()] = value.strip('"').strip("'")
    return meta


def _one_line(text: str, width: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[: width - 3] + "..."
