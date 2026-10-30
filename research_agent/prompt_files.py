"""
WHAT:  Every prompt a model is given -- the roles' standing instructions and
       the longer notes code sends them -- read from Markdown files, not
       written into the code.
WHY:   They are what most changes how the agent behaves, and you should be
       able to read and change them without touching Python: for every run,
       or for one.
CONCEPT: A prompt is named by its path without ".md" ("roles/bootstrap/
       designer", "notes/design_ready"). The first file found wins:

    <run>/prompts/<name>.md                one run (its folder)
    $RESEARCH_AGENT_PROMPTS/<name>.md      every run of yours, if set
    ~/.research_agent/prompts/<name>.md    every run of yours
    research_agent/fields/<field>/prompts/  the run's field (research_agent/fields)
    research_agent/prompts/<name>.md       the defaults

       A role's instructions are its file plus the field's voice
       (`instructions`): how the field talks, for every role.

       A note's `$name` placeholders are filled in by the code that sends it
       (string.Template: `$$` is a dollar sign); what a placeholder means is
       written at the top of each default file, in an HTML comment that is
       never sent.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from string import Template

DEFAULT_DIR = Path(__file__).with_name("prompts")
ENV = "RESEARCH_AGENT_PROMPTS"
#: Your own prompts when $RESEARCH_AGENT_PROMPTS is not set:
#: $RESEARCH_AGENT_HOME/prompts.
HOME_ENV = "RESEARCH_AGENT_HOME"
#: A run folder's own prompts.
RUN_DIR = "prompts"

_COMMENT = re.compile(r"<!--.*?-->\s*", re.S)


def path(name: str, run_dir: str | os.PathLike | None = None) -> Path:
    """The file `name` is read from, first one found."""
    for folder in dirs(run_dir):
        candidate = folder / f"{name}.md"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"No prompt {name!r} (looked in {', '.join(map(str, dirs(run_dir)))}).")


def dirs(run_dir: str | os.PathLike | None = None) -> list[Path]:
    from research_agent import fields

    found = []
    if run_dir:
        found.append(Path(run_dir) / RUN_DIR)
    found.append(user_dir())
    found.append(fields.load(fields.of_run(run_dir)).prompts)
    found.append(DEFAULT_DIR)
    return found


def user_dir() -> Path:
    """Your own prompts, for every run: $RESEARCH_AGENT_PROMPTS, else
    ~/.research_agent/prompts."""
    if os.environ.get(ENV):
        return Path(os.environ[ENV]).expanduser()
    return Path(os.environ.get(HOME_ENV) or "~/.research_agent").expanduser() / "prompts"


def text(name: str, run_dir: str | os.PathLike | None = None, **fields) -> str:
    """The prompt `name`, its placeholders filled from `fields`."""
    raw = path(name, run_dir).read_text(encoding="utf-8")
    body = _COMMENT.sub("", raw).strip()
    return Template(body).safe_substitute({k: str(v) for k, v in fields.items()})


def instructions(name: str, run_dir: str | os.PathLike | None = None) -> str:
    """A role's standing instructions: its prompt file, and the voice of the
    run's field when it has one."""
    body = text(name, run_dir)
    try:
        voice = text(VOICE, run_dir)
    except FileNotFoundError:
        return body
    return f"{body}\n\n{voice}" if voice else body


#: The field's voice: how its researchers talk, added to every role.
VOICE = "voice"


def names() -> list[str]:
    """Every default prompt's name."""
    return sorted(str(p.relative_to(DEFAULT_DIR).with_suffix(""))
                  for p in DEFAULT_DIR.rglob("*.md") if p.name != "README.md")
