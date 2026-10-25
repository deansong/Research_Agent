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

import json
import threading
import time
from datetime import datetime, timezone
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
    listener: Callable[[str, dict], None] | None = None
    """Told of every call as it starts and ends, and of notes -- the web UI's
    feed. ("call", {...}) and ("note", {"text": ...})."""

    def __post_init__(self):
        self.folder = Path(self.folder).expanduser().resolve()
        self._traced: set[str] = set()
        # One discussor turn at a time: the graph's, and the user's asides
        # while the graph works, share one conversation.
        self.discussor_lock = threading.Lock()

    def say(self, text: str) -> None:
        """A line about the work (not a model call): printed, and passed on."""
        if not self.quiet:
            print(text, flush=True)
        self._tell("note", {"text": text.strip()})

    def _tell(self, kind: str, data: dict) -> None:
        if self.listener:
            try:
                self.listener(kind, data)
            except Exception:           # the feed must never stop the work
                pass

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
        if session:
            self._retire_if_moved(session, cfg.provider)
        rb.new_session(session or self.next_name(role), exist_ok=bool(session))
        if rb.session.instructions != standing:
            rb.set_instructions(standing)
        label = label or (skills_key or role).replace("_", " ")
        prompt = brief
        for attempt in range(1, ATTEMPTS + 1):
            self._announce(label, cfg, rb, attempt, prompt, chosen if skills_dir else None)
            started = time.monotonic()
            live = None
            if self.listener:
                info = {"label": label, "session": rb.session_id, "attempt": attempt,
                        "model": str(cfg)}
                live = lambda snapshot, info=info: self._tell("live", {**info, "snapshot": snapshot})  # noqa: E731
            try:
                # Tracing shows the work as it happens too, not only its ends.
                reply = rb.send(prompt, schema=schema, progress=self.progress or self.trace,
                                on_progress=live)
                problems = check(reply.data) if check else []
            except RB.BackendOutputError as exc:
                problems = [str(exc)[:2000]]
                reply = None
            self._keep(label, rb, attempt, reply, problems, time.monotonic() - started)
            self._report(label, reply, problems, time.monotonic() - started,
                         session=rb.session_id, attempt=attempt)
            if not problems:
                return reply.data, rb.session_id
            if attempt == ATTEMPTS:
                raise NodeFailed(f"{role} gave no usable answer in {ATTEMPTS} attempts. "
                                 f"Last problems:\n" + "\n".join(f"- {p}" for p in problems))
            prompt = ("Your answer could not be used:\n" + "\n".join(f"- {p}" for p in problems)
                      + "\n\nAnswer the same task again, fixing these.")

    def _keep(self, label, rb, attempt, reply, problems, seconds) -> None:
        """What the model did in this call, every event of it, in
        <run>/activity/<session>.jsonl -- research_backend's session keeps
        only the input and the answer. For looking back at a node's work."""
        record = {"session": rb.session_id, "turn": len(rb.session.turns), "label": label,
                  "attempt": attempt, "seconds": round(seconds, 1), "problems": problems,
                  "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "events": list(reply.events) if reply is not None else [],
                  "reply": (reply.text if reply is not None else "")[:20000]}
        try:
            folder = self.folder / "activity"
            folder.mkdir(parents=True, exist_ok=True)
            with open(folder / f"{rb.session_id}.jsonl", "a", encoding="utf-8") as out:
                out.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass                        # a record that cannot be kept must not stop the work

    def takes_over(self, session: str, role: str) -> bool:
        """The named conversation is on another provider than the role's now
        (its model was changed): the next call starts it afresh."""
        return self._moved(session, self.roles[role].provider) is not None

    def _moved(self, session: str, provider: str):
        from research_backend import conversation

        if not conversation.exists(session, home=self.rb_home):
            return None
        found = conversation.open_session(session, home=self.rb_home)
        return found if found.provider != provider else None

    def _retire_if_moved(self, session: str, provider: str) -> None:
        """A provider cannot continue another's thread: keep the old session
        as <session>-<provider>-NNN, so the name starts afresh."""
        old = self._moved(session, provider)
        if old is None:
            return
        folder = self.rb_home / "sessions"
        n = len(list(folder.glob(f"{session}-{old.provider}-*"))) + 1
        old.rename(f"{session}-{old.provider}-{n:03d}")

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
        self._tell("call", {"state": "start", "label": label, "model": str(cfg),
                            "session": rb.session_id, "attempt": attempt, "prompt": prompt})
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

    def _report(self, label, reply, problems, seconds, *, session="", attempt=1) -> None:
        tokens = reply.tokens if reply is not None else None
        self._tell("call", {"state": "problems" if problems else "end", "label": label,
                            "session": session, "attempt": attempt,
                            "seconds": round(seconds, 1), "problems": [p[:300] for p in problems[:4]],
                            "tokens": {"input": tokens.input_tokens, "output": tokens.output_tokens}
                            if tokens else None})
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
