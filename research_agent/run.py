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

#: How long a model out of credits (or unreachable) is benched when the
#: provider does not say when its limit resets.
BENCH_SECONDS = 3600.0

#: How long a model at capacity (or a busy provider) is benched.
BUSY_BENCH_SECONDS = 600.0

#: Turns of a conversation a backup taking it over is given.
HANDOVER_TURNS = 8

#: A conversation whose next prompt -- its last turn plus the new brief --
#: would use more than this share of the model's context window starts afresh,
#: handed over, before it overflows. Below it, the provider's own compaction
#: (Codex's) does the work. Measured: a discussor conversation of 36 turns on a
#: 128k-token model overflowed at 137k, and stopped the run.
ROLL_SHARE = 0.8

#: The share of the model's window one brief may take: a single prompt bigger
#: than the window cannot be compacted or handed over. Measured: discussor
#: briefs of 160k-380k characters on a 128k-token model.
BRIEF_SHARE = 0.25


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
    interrupt: threading.Event | None = None
    """Set, it ends a call that is waiting (for credits, for a full model):
    the web UI's Pause. The call raises BackendCancelled; the run carries on
    from that node."""
    listener: Callable[[str, dict], None] | None = None
    """Told of every call as it starts and ends, and of notes -- the web UI's
    feed. ("call", {...}) and ("note", {"text": ...})."""

    def __post_init__(self):
        self.folder = Path(self.folder).expanduser().resolve()
        self._traced: set[str] = set()
        # One discussor turn at a time: the graph's, and the user's asides
        # while the graph works, share one conversation.
        self.discussor_lock = threading.Lock()
        #: Models out of credits or unreachable -> until when (epoch seconds):
        #: their roles' calls go to the backups meanwhile.
        self.benched: dict[str, float] = {}

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
             check: Callable[[object], list[str]] | None = None,
             takeover: Callable[[], str] | None = None):
        """One node call: (answer, session id). Retries with the problems.

        `session` names the session: an existing one is continued (and given
        these instructions from now on), a new one is started.

        When the role's model is out of credits or cannot be reached, the
        call moves to the role's backups in order (RoleConfig.backup), and
        the model stays benched until its limit resets. A named conversation
        moved that way starts afresh on the backup with `takeover()` -- or,
        without one, the old session's recent turns -- before the brief."""
        label = label or (skills_key or role).replace("_", " ")
        chain = self._chain(self.roles[role])
        for n, cfg in enumerate(chain):
            final = n == len(chain) - 1
            sent = brief
            if n and session and self._moved(session, cfg.provider) is not None:
                sent = takeover() if takeover else self._handover(session) + "\n\n" + brief
            elif session and self._full(session, cfg, brief):
                sent = self._roll(session, label, cfg, brief, takeover, "is nearly full")
            try:
                try:
                    return self._call_on(cfg, role, final=final, brief=sent, schema=schema,
                                         skills_key=skills_key, skills=skills,
                                         instructions=instructions, access=access,
                                         workdir=workdir, session=session, label=label,
                                         check=check)
                except Exception as exc:  # noqa: BLE001 -- only an overflow is handled here
                    if not (session and _overflowed(exc)):
                        raise
                    # The conversation outgrew the model's window: a fresh one
                    # takes over, once.
                    sent = self._roll(session, label, cfg, brief, takeover, "overflowed")
                    return self._call_on(cfg, role, final=final, brief=sent, schema=schema,
                                         skills_key=skills_key, skills=skills,
                                         instructions=instructions, access=access,
                                         workdir=workdir, session=session, label=label,
                                         check=check)
            except (RB.BackendOutOfCredits, RB.BackendUnavailable, RB.BackendBusy) as exc:
                if final:
                    raise
                until = self._bench(cfg, exc)
                self.say(f"  ! {label}: {cfg} {_why(exc)}; {chain[n + 1]} takes over "
                         f"(until {until})")

    def current(self, role: str):
        """The model a role's next call runs on: the first of it and its
        backups that is not benched."""
        return self._chain(self.roles[role])[0]

    def _chain(self, cfg) -> list:
        """The role's model, then its backups, without the benched ones (the
        last is always kept: with nothing else left, it waits for credits)."""
        backups = cfg.backups() if hasattr(cfg, "backups") else []
        if not backups:
            return [cfg]
        everything = [cfg, *backups]
        now = time.time()
        free = [c for c in everything if self.benched.get(str(c), 0) <= now]
        return free or everything[-1:]

    def _bench(self, cfg, exc) -> str:
        """Bench a model until its limit resets (as the provider says), else
        for BENCH_SECONDS. Returns when, as a clock time."""
        from research_backend import quota

        delay = quota.reset_delay(str(exc)) if isinstance(exc, RB.BackendOutOfCredits) else None
        if isinstance(exc, RB.BackendBusy):
            delay = BUSY_BENCH_SECONDS            # full for now; worth trying again soon
        else:
            delay = delay + 60 if delay is not None else BENCH_SECONDS
        self.benched[str(cfg)] = time.time() + delay
        return quota.until(delay)

    def _handover(self, session: str) -> str:
        """For a conversation moved to another provider: its recent turns."""
        from research_backend import conversation

        try:
            old = conversation.open_session(session, home=self.rb_home)
        except Exception:  # noqa: BLE001 - nothing to hand over
            return ""
        turns = old.turns[-HANDOVER_TURNS:]
        if not turns:
            return ""
        lines = [f"[sent] {(t.get('prompt') or '')[-2000:]}\n[you answered] "
                 f"{(t.get('reply') or '')[:2000]}" for t in turns]
        return ("=== YOU TAKE OVER THIS CONVERSATION ===\n"
                "Until now another model held it and could not go on. Its last "
                f"{len(turns)} turns:\n\n" + "\n\n".join(lines)
                + "\n\n=== NOW ===")

    def _call_on(self, cfg, role: str, *, final: bool, brief: str, schema, skills_key,
                 skills, instructions, access, workdir, session, label, check):
        """The call on one model: up to ATTEMPTS answers."""
        chosen = (self.skills or {}).get(skills_key or role) if skills is BY_ROLE else skills
        skills_dir = self.skills_dir if self.skills_dir and Path(self.skills_dir).is_dir() else None
        standing = self.instructions.get(role, "") if instructions is None else instructions
        options = dict(cfg.options)
        if not final:
            # A backup waits: this model does not wait for its credits, and
            # retries a full or busy provider only once.
            options["credit_wait_hours"] = 0
            options["busy_attempts"] = 1
        rb = RB(cfg.provider, cfg.model, effort=cfg.effort, instructions=standing,
                access=access, workdir=str(workdir or self.folder), home=self.rb_home,
                skills=chosen if skills_dir else None, skills_dir=skills_dir, **options)
        if session:
            self._retire_if_moved(session, cfg.provider)
        rb.new_session(session or self.next_name(role), exist_ok=bool(session))
        if rb.session.instructions != standing:
            rb.set_instructions(standing)
        if session and (rb.session.model, rb.session.effort) != (cfg.model, cfg.effort):
            # A continued conversation keeps the model it started with unless
            # the role's model or effort was changed since: then it moves on
            # (same provider; another provider starts afresh, see above).
            rb.session.update(model=cfg.model, effort=cfg.effort)
        if skills_dir and session and rb.session.skills != (chosen or None):
            # A continued conversation (the discussor's) keeps its skills
            # unless they were changed since: then it gets the new ones.
            rb.set_skills(chosen or None)
        prompt = brief
        for attempt in range(1, ATTEMPTS + 1):
            self._announce(label, cfg, rb, attempt, prompt, chosen if skills_dir else None)
            started = time.monotonic()
            failed_events: list = []
            live = None
            if self.listener:
                info = {"label": label, "session": rb.session_id, "attempt": attempt,
                        "model": str(cfg)}
                live = lambda snapshot, info=info: self._tell("live", {**info, "snapshot": snapshot})  # noqa: E731
            try:
                # Tracing shows the work as it happens too, not only its ends.
                reply = rb.send(prompt, schema=schema, progress=self.progress or self.trace,
                                on_progress=live, interrupt=self.interrupt)
                problems = check(reply.data) if check else []
            except RB.BackendOutputError as exc:
                problems = [str(exc)[:2000]]
                reply = None
                failed_events = list(getattr(exc, "events", None) or [])
            except (RB.BackendOutOfCredits, RB.BackendUnavailable, RB.BackendBusy) as exc:
                self._report(label, None, [f"{cfg}: {_why(exc)}"], time.monotonic() - started,
                             session=rb.session_id, attempt=attempt)
                raise
            except BaseException as exc:
                # Paused, stopped, or failed: the call has ended either way,
                # and whoever shows calls under way must hear it.
                self._report(label, None, [f"{type(exc).__name__}: {str(exc)[:200]}"],
                             time.monotonic() - started, session=rb.session_id, attempt=attempt)
                raise
            self._keep(label, rb, attempt, reply, problems, time.monotonic() - started,
                       events=failed_events if reply is None else None)
            self._report(label, reply, problems, time.monotonic() - started,
                         session=rb.session_id, attempt=attempt)
            if not problems:
                return reply.data, rb.session_id
            if attempt == ATTEMPTS:
                raise NodeFailed(f"{role} gave no usable answer in {ATTEMPTS} attempts. "
                                 f"Last problems:\n" + "\n".join(f"- {p}" for p in problems))
            prompt = ("Your answer could not be used:\n" + "\n".join(f"- {p}" for p in problems)
                      + "\n\nAnswer the same task again, fixing these." + _shape_hint(schema, problems))

    def _keep(self, label, rb, attempt, reply, problems, seconds, events=None) -> None:
        """What the model did in this call, every event of it, in
        <run>/activity/<session>.jsonl -- research_backend's session keeps
        only the input and the answer. For looking back at a node's work."""
        record = {"session": rb.session_id, "turn": len(rb.session.turns), "label": label,
                  "attempt": attempt, "seconds": round(seconds, 1), "problems": problems,
                  "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "events": list(reply.events) if reply is not None else list(events or []),
                  "reply": (reply.text if reply is not None else "")[:20000]}
        try:
            folder = self.folder / "activity"
            folder.mkdir(parents=True, exist_ok=True)
            with open(folder / f"{rb.session_id}.jsonl", "a", encoding="utf-8") as out:
                out.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass                        # a record that cannot be kept must not stop the work

    def takes_over(self, session: str, role: str) -> bool:
        """The named conversation is on another provider than the one the
        role's next call runs on (its model was changed, or it moved to a
        backup or back): the next call starts it afresh."""
        return self._moved(session, self.current(role).provider) is not None

    def _moved(self, session: str, provider: str):
        from research_backend import conversation

        if not conversation.exists(session, home=self.rb_home):
            return None
        found = conversation.open_session(session, home=self.rb_home)
        return found if found.provider != provider else None

    def brief_budget(self, role: str) -> int:
        """How many characters one brief for `role` may use, on the model its
        next call runs on."""
        from research_backend import catalog

        cfg = self.current(role)
        return catalog.prompt_budget_chars(cfg.provider, cfg.model, share=BRIEF_SHARE)

    def _full(self, session: str, cfg, brief: str = "") -> bool:
        """The conversation's last turn plus this brief would use more than
        ROLL_SHARE of the model's window: the next turn might not fit."""
        from research_backend import catalog, conversation

        if not conversation.exists(session, home=self.rb_home):
            return False
        old = conversation.open_session(session, home=self.rb_home)
        if old.provider != cfg.provider or not old.turns:
            return False
        tokens = next((t.get("tokens") for t in reversed(old.turns) if t.get("tokens")), None) or {}
        used = (tokens.get("input_tokens") or 0) + (tokens.get("output_tokens") or 0)
        window = catalog.context_window(cfg.provider, cfg.model, tokens.get("context_window"))
        return used + len(brief) / catalog.CHARS_PER_TOKEN > ROLL_SHARE * window

    def _roll(self, session: str, label: str, cfg, brief: str, takeover, why: str) -> str:
        """Keep the conversation as <session>-<provider>-NNN and start a fresh
        one, handed over: `takeover()` (the discussor's: the recent chat and
        the whole state), else the old conversation's last turns. Returns the
        brief to send."""
        from research_backend import conversation

        old = conversation.open_session(session, home=self.rb_home)
        folder = self.rb_home / "sessions"
        n = len(list(folder.glob(f"{session}-{old.provider}-*"))) + 1
        kept = f"{session}-{old.provider}-{n:03d}"
        old.rename(kept)
        self.say(f"  ~ {label}: the conversation {why} for {cfg}'s context window; a fresh one "
                 f"takes over, handed the recent conversation (kept as {kept}).")
        return takeover() if takeover else self._handover(kept) + "\n\n" + brief

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


def _overflowed(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(m in text for m in ("context_length_exceeded", "exceeds the context window",
                                   "maximum context length", "prompt is too long"))


def _shape_hint(schema, problems: list[str]) -> str:
    """When the answer missed the schema: what the answer must be. Measured: a
    small model ending a long agentic turn with a search query as text
    ({"q": "..."}), which ends the turn as if it were the answer."""
    if not any("does not match the schema" in p or "no final response" in p for p in problems):
        return ""
    fields = list(getattr(schema, "model_fields", {}) or {})
    shape = f" with the fields {', '.join(fields)}" if fields else ""
    return ("\n\nYour final message must be ONLY one JSON object" + shape + ". Any other "
            "message -- a search query, a tool's arguments, a note to yourself -- ends your turn "
            "and is not an answer: run tools as tools (commands), and finish with the object.")


def _why(exc: BaseException) -> str:
    if isinstance(exc, RB.BackendOutOfCredits):
        return "is out of credits or over its usage limit"
    if isinstance(exc, RB.BackendBusy):
        return "is at capacity or busy"
    return f"cannot be used ({str(exc).splitlines()[0][:160] if str(exc) else type(exc).__name__})"


def _skills_word(skills) -> str:
    return skills if isinstance(skills, str) else ", ".join(skills)
