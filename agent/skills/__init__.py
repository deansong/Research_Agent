"""
WHAT:  Skills: reusable know-how in <repo>/skills/<name>/SKILL.md -- the
       same format Claude Code and antigravity use -- and, per node, which
       of them it gets: none, all of them to discover, or a chosen set.
WHY:   The discussor's checklist, the research agent's tools and the paper
       format were each wired into one node in code. Now any node can be
       given any skill, on any provider, and adding one is adding a folder.
CONCEPT: Not LangGraph. A skill is plain files a model reads. What differs
       per node is how it meets them:

         none       not at all
         auto       a list -- name, description, folder -- and it opens the
                    ones its task needs (cheap: one line per skill)
         selected   the chosen skills' text in full, in its prompt

       The defaults are skills/suggestions.json: per design-agent node, and
       per role in a designed agent (the designer starts from these).
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

#: <repo>/skills -- beside the agent package, so every provider can read it.
#: AGENT_SKILLS_DIR moves it.
DEFAULT_DIR = Path(__file__).resolve().parents[2] / "skills"
SKILL_FILE = "SKILL.md"
SUGGESTIONS = "suggestions.json"
MODES = ("none", "auto", "selected")


def skills_dir() -> Path:
    return Path(os.environ.get("AGENT_SKILLS_DIR") or DEFAULT_DIR).expanduser().resolve()


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    folder: Path
    body: str

    def as_dict(self) -> dict:
        return {"name": self.name, "description": self.description,
                "path": str(self.folder)}


_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)


def _parse(path: Path) -> Skill | None:
    """A SKILL.md: `---` front matter (name, description), then the text.
    The folder name is the skill's name, whatever the front matter says --
    it is what a choice refers to, and what a person sees on disk."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    body = text
    meta: dict[str, str] = {}
    found = _FRONT.match(text)
    if found:
        body = found.group(2)
        meta = _front_matter(found.group(1))
    return Skill(name=path.parent.name, description=meta.get("description", ""),
                 folder=path.parent, body=body.strip())


def _front_matter(text: str) -> dict[str, str]:
    """The top-level `key: value` pairs of a SKILL.md's YAML front matter --
    enough of YAML for what skills use: plain or quoted values, and block
    values (`description: >` or `|`, then indented lines), which several
    published skills use for a long description."""
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
            block = []
            while i < len(lines) and (not lines[i].strip() or lines[i][0].isspace()):
                block.append(lines[i].strip())
                i += 1
            joiner = "\n" if value.startswith("|") else " "
            value = joiner.join(b for b in block if b).strip()
        meta[key.strip().lower()] = value.strip('"').strip("'")
    return meta


def discover() -> list[Skill]:
    """Every skill in the folder, by name. Read fresh each time: adding a
    skill is adding a folder, and should show at once."""
    root = skills_dir()
    if not root.is_dir():
        return []
    return [s for s in (_parse(p) for p in sorted(root.glob(f"*/{SKILL_FILE}"))) if s]


def get(name: str) -> Skill | None:
    return next((s for s in discover() if s.name == name), None)


def names() -> list[str]:
    return [s.name for s in discover()]


# ---------------------------------------------------------------------------
# a node's choice
# ---------------------------------------------------------------------------

def choice(value) -> dict:
    """Normalise a choice -- a dict, a model, or None -- to {mode, skills}."""
    if value is None:
        return {"mode": "none", "skills": []}
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    mode = str(value.get("mode") or "none")
    picked = [str(n) for n in value.get("skills") or []]
    return {"mode": mode if mode in MODES else "none",
            "skills": picked if mode == "selected" else []}


def suggestions() -> dict:
    """skills/suggestions.json, or {} when there is none (or it is broken --
    defaults are a convenience, never a reason not to start)."""
    try:
        value = json.loads((skills_dir() / SUGGESTIONS).read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def suggested(section: str, key: str) -> dict:
    """The default choice for a design-agent node (section "bootstrap") or a
    designed agent's role (section "roles"); none when it names nothing."""
    return choice((suggestions().get(section) or {}).get(key))


def missing(value) -> list[str]:
    """The names in a choice that are not a skill (any more)."""
    known = set(names())
    return [n for n in choice(value)["skills"] if n not in known]


def expand(text: str, skill: Skill | None = None, *, artifacts_dir: str = "",
           workspace: str = "") -> str:
    """Fill a skill's few tokens. Plain replacement, not the prompt renderer:
    skill text is full of braces (LaTeX, JSON) that are not placeholders."""
    pairs = {"{skills_dir}": str(skills_dir()), "{python}": sys.executable,
             "{skill_dir}": str(skill.folder) if skill else str(skills_dir())}
    if artifacts_dir:
        pairs["{artifacts_dir}"] = artifacts_dir
    if workspace:
        pairs["{workspace}"] = workspace
    for token, value in pairs.items():
        text = text.replace(token, value)
    return text


def block(value, *, artifacts_dir: str = "", workspace: str = "") -> str:
    """What a node is sent for its choice -- "" for none."""
    picked = choice(value)
    if picked["mode"] == "auto":
        available = discover()
        if not available:
            return ""
        lines = [f"  - {s.name}: {s.description}\n    {s.folder / SKILL_FILE}"
                 for s in available]
        return ("SKILLS YOU CAN USE -- shared instructions and tools. When one "
                "fits your task, read its SKILL.md first and follow it. Never "
                "change these files.\n" + "\n".join(lines))
    if picked["mode"] == "selected":
        parts = []
        for name in picked["skills"]:
            skill = get(name)
            if skill is None:
                continue
            parts.append(f"=== SKILL: {skill.name} (folder: {skill.folder}) ===\n"
                         + expand(skill.body, skill, artifacts_dir=artifacts_dir,
                                  workspace=workspace))
        if not parts:
            return ""
        return ("SKILLS FOR THIS TASK -- follow them. Their folders are "
                "read-only to you.\n\n" + "\n\n".join(parts))
    return ""


def text_of(name: str, **paths) -> str:
    """One skill's text, tokens filled -- "" when there is no such skill."""
    skill = get(name)
    return expand(skill.body, skill, **paths) if skill else ""


# ---- the skills that used to be wired in by name, for the callers and tests
# that still ask for them that way ------------------------------------------

def experiment_design() -> str:
    return text_of("experiment-design")


def research() -> str:
    return text_of("research")


def acl_paper(artifacts_dir: str = "{artifacts_dir}") -> str:
    return text_of("acl-paper", artifacts_dir=artifacts_dir)
