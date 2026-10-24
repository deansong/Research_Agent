"""
WHAT:  One run's model calls, for every stage: the run folder, which provider
       each role uses, and `call()` -- one research_backend call with a brief,
       a schema and a check, retried with the problems fed back.
WHY:   The bootstrap and the execution stage call models the same way, show
       the same lines in the terminal, and keep every call as a session in
       <run>/rb -- so that lives here once.
CONCEPT: A call is a new session named <role>-NNN unless it names one; naming
       an existing session continues it (the discussor's conversation, a step
       owner woken after its jobs finished).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import research_backend as RB

#: Answer attempts per call: the first, and two with the problems fed back.
ATTEMPTS = 3

#: Skills for a call: look them up in Run.skills by role (or skills_key).
BY_ROLE = object()


class NodeFailed(RB.BackendError):
    """A node could not give a usable answer within its attempts."""


@dataclass
class Run:
    folder: Path
    roles: dict
    """Role -> a RoleConfig (provider, model, effort, options)."""
    skills_dir: Path | None = None
    skills: dict | None = None
    """Per role or node: None, "auto" or a list of skill names."""
    progress: bool = False
    """Print what each model does while it works (its commands, files)."""
    trace: bool = False
    """Print every model call's full input and output."""
    quiet: bool = False
    """Print nothing about calls -- not even that one has started."""
    instructions: dict = field(default_factory=dict)
    """Role -> its standing instructions."""

    def __post_init__(self):
        self.folder = Path(self.folder).expanduser().resolve()
        self._traced: set[str] = set()

    @property
    def rb_home(self) -> Path:
        return self.folder / "rb"

    def call(self, role: str, *, brief: str, schema, skills_key: str | None = None,
             skills=BY_ROLE, instructions: str | None = None, access: str | None = None,
             workdir: Path | None = None, session: str | None = None, label: str = "",
             check: Callable[[object], list[str]] | None = None):
        """One node call: (answer, session id). Retries with the problems.

        `session` names the session: an existing one is continued (and given
        these instructions from now on), a new one is started."""
        cfg = self.roles[role]
        chosen = (self.skills or {}).get(skills_key or role) if skills is BY_ROLE else skills
        skills_dir = self.skills_dir if self.skills_dir and Path(self.skills_dir).is_dir() else None
        standing = self.instructions.get(role, "") if instructions is None else instructions
        rb = RB(cfg.provider, cfg.model, effort=cfg.effort, instructions=standing,
                access=access, workdir=str(workdir or self.folder), home=self.rb_home,
                skills=chosen if skills_dir else None, skills_dir=skills_dir, **cfg.options)
        rb.new_session(session or self.next_name(role), exist_ok=bool(session))
        if rb.session.instructions != standing:
            rb.set_instructions(standing)
        label = label or (skills_key or role).replace("_", " ")
        prompt = brief
        for attempt in range(1, ATTEMPTS + 1):
            self._announce(label, cfg, rb, attempt, prompt, chosen if skills_dir else None)
            started = time.monotonic()
            try:
                # Tracing shows the work as it happens too, not only its ends.
                reply = rb.send(prompt, schema=schema, progress=self.progress or self.trace)
                problems = check(reply.data) if check else []
            except RB.BackendOutputError as exc:
                problems = [str(exc)[:2000]]
                reply = None
            self._report(label, reply, problems, time.monotonic() - started)
            if not problems:
                return reply.data, rb.session_id
            if attempt == ATTEMPTS:
                raise NodeFailed(f"{role} gave no usable answer in {ATTEMPTS} attempts. "
                                 f"Last problems:\n" + "\n".join(f"- {p}" for p in problems))
            prompt = ("Your answer could not be used:\n" + "\n".join(f"- {p}" for p in problems)
                      + "\n\nAnswer the same task again, fixing these.")

    def next_name(self, role: str) -> str:
        folder = self.rb_home / "sessions"
        taken = len(list(folder.glob(f"{role}-*"))) if folder.is_dir() else 0
        return f"{role}-{taken + 1:03d}"

    def catalogue(self) -> list[tuple[str, str]]:
        """Every skill in the folder: (name, description)."""
        if not (self.skills_dir and Path(self.skills_dir).is_dir()):
            return []
        return [(s.name, s.description) for s in RB.list_skills(self.skills_dir)]

    # ---- what the terminal shows -----------------------------------------------------

    def _announce(self, label, cfg, rb, attempt, prompt, skills) -> None:
        if self.quiet:
            return
        again = f", attempt {attempt}" if attempt > 1 else ""
        print(f"  > {label}: asking {cfg} (session {rb.session_id}{again})...", flush=True)
        if not self.trace:
            return
        role = label.split()[0]
        if role not in self._traced:
            self._traced.add(role)
            print(box(f"{role}: standing instructions", rb.session.instructions))
        sent = prompt
        if skills is not None and attempt == 1 and not rb.session.turns:
            sent = f"[+ skills, sent in front of the first message: {_skills_word(skills)}]\n\n" + sent
        print(box(f"{label}: INPUT", sent))

    def _report(self, label, reply, problems, seconds) -> None:
        if self.quiet:
            return
        if self.trace and reply is not None:
            tokens = reply.tokens
            used = (f", {tokens.input_tokens:,} in / {tokens.output_tokens:,} out tokens"
                    if tokens else "")
            print(box(f"{label}: OUTPUT ({seconds:.0f}s{used})", reply.text))
        if problems:
            print(f"  ! {label}: answer not usable -- " + "; ".join(p[:200] for p in problems[:4]),
                  flush=True)
        elif not self.trace:
            print(f"  < {label}: answered in {seconds:.0f}s", flush=True)


def box(title: str, text: str) -> str:
    """A titled block of text for the terminal."""
    rule = "-" * max(10, 78 - len(title))
    return f"\n---- {title} {rule}\n{(text or '').rstrip()}\n{'-' * 84}\n"


def _skills_word(skills) -> str:
    return skills if isinstance(skills, str) else ", ".join(skills)
