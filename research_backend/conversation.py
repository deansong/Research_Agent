"""
WHAT:  A conversation with one model that you can come back to: by its id,
       from another script, days later.
WHY:   Each provider keeps its own conversations (a Codex thread, a `claude`
       session, an `agy` conversation) under an id of its own. A Session
       remembers that id, with everything needed to continue -- provider,
       model, folder, access, instructions -- and every turn so far.
CONCEPT: One folder per session under ~/.research_backend/sessions/<id>/;
       session.json in it is plain, indented JSON you can read.

    s = new_session("codex", model="gpt-5.6-sol", name="lit-review")
    s.send("Which datasets exist for hotel review classification?")
    ...
    open_session("lit-review").send("Which of those are free to use?")
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from research_backend import home as _home
from research_backend import json_schema
from research_backend.base import Access, BackendError, BackendUnavailable, Usage

#: What a session id may look like: it is a folder name.
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


class SessionNotFound(BackendError):
    """No session has that id."""


class SessionBusy(BackendError):
    """The session is answering another message. A conversation takes one
    turn at a time, whichever process sends it."""


class Text(BaseModel):
    """What a plain message is asked for: one string of text."""

    text: str


@dataclass(frozen=True)
class Reply:
    """What came back from one message."""

    session: str
    turn: int
    text: str
    """The answer. For a `schema` reply, its JSON."""
    data: BaseModel | dict | None = None
    """For a `schema` reply, the answer: an instance of the Pydantic class, or
    a plain dict for a JSON schema. Else None."""
    tokens: Usage | None = None
    seconds: float = 0.0
    events: list[dict] = field(default_factory=list, repr=False)
    """What the model did on the way: commands, file changes, reasoning."""

    def __str__(self) -> str:
        return self.text


class Session:
    """One conversation with one model. Make one with new_session(), get one
    back with open_session(); send() talks to it."""

    def __init__(self, record: dict, path: Path):
        self._record = record
        self._path = path

    # ---- what it is ----------------------------------------------------------

    @property
    def id(self) -> str:
        return self._record["id"]

    @property
    def provider(self) -> str:
        return self._record["provider"]

    @property
    def model(self) -> str | None:
        return self._record.get("model")

    @property
    def workdir(self) -> str:
        return self._record["workdir"]

    @property
    def effort(self) -> str | None:
        """How hard the model thinks; None = the model's default."""
        return self._record.get("effort")

    @property
    def access(self) -> Access:
        return Access(self._record["access"])

    @property
    def instructions(self) -> str:
        return self._record.get("instructions", "")

    @property
    def options(self) -> dict:
        return dict(self._record.get("options") or {})

    @property
    def thread_id(self) -> str | None:
        """The provider's own id for this conversation; None until the first
        reply."""
        return self._record.get("thread_id")

    @property
    def turns(self) -> list[dict]:
        """Every message and answer so far, oldest first."""
        return [dict(t) for t in self._record.get("turns", [])]

    @property
    def folder(self) -> Path:
        return self._path.parent

    def __repr__(self) -> str:
        return (f"Session({self.id!r}, provider={self.provider!r}, "
                f"model={self.model!r}, turns={len(self._record.get('turns', []))})")

    def __str__(self) -> str:
        turns = self._record.get("turns", [])
        effort = f" ({self.effort} effort)" if self.effort else ""
        lines = [f"{self.id}: {self.provider}/{self.model or 'default model'}{effort}, "
                 f"{self.access.value} access in {self.workdir}",
                 f"  {len(turns)} turn(s); started {_short(self._record['created_at'])}, "
                 f"last {_short(self._record['updated_at'])}"]
        if turns:
            last = turns[-1]
            said = last.get("error") or last.get("reply") or ""
            lines.append(f"  last: {_one_line(last['prompt'])} -> {_one_line(said)}")
        return "\n".join(lines)

    def history(self, last: int | None = None) -> str:
        """The conversation so far, as text."""
        turns = self._record.get("turns", [])
        if last is not None:
            turns = turns[-last:]
        parts = []
        for t in turns:
            parts.append(f"--- turn {t['turn']} ({_short(t['at'])}) ---\n"
                         f"you: {t['prompt']}\n"
                         f"{self.provider}: {t.get('error') or t.get('reply', '')}")
        return "\n\n".join(parts) or "(no turns yet)"

    # ---- talking to it -------------------------------------------------------

    def send(self, prompt: str, *, schema=None, progress: bool = False) -> Reply:
        """Send one message and wait for the answer.

        `schema` asks for a structured answer: a Pydantic class (the reply's
        `data` is an instance of it), or a JSON schema -- a dict, JSON text, or
        the path of a .json file (`data` is a dict). Without one the answer is
        plain text.
        `progress=True` prints what the model is doing while it works -- the
        commands it runs, the files it changes; otherwise the turn is quiet
        and the same detail is in the reply's `events`.

        Raises SessionBusy if this session is already answering a message,
        and the provider's error (BackendError and its kinds) if the turn
        fails -- which is recorded in the session either way.
        """
        with _lock(self):
            self._reload()
            options = {**self.options, **({"effort": self.effort} if self.effort else {})}
            backend = _backend(self.provider, self.model, options, quiet=not progress)
            output_model = _output_model(schema)
            turn = len(self._record.get("turns", [])) + 1
            started = time.monotonic()
            entry: dict[str, Any] = {"turn": turn, "at": _now(), "prompt": prompt}
            try:
                run = backend.run_structured(
                    thread_id=self.thread_id, repo_path=self.workdir, access=self.access,
                    developer_instructions=self.instructions, prompt=prompt,
                    output_model=output_model)
            except BaseException as exc:
                entry.update(error=f"{type(exc).__name__}: {exc}",
                             seconds=round(time.monotonic() - started, 1))
                self._append(entry)
                raise

            seconds = round(time.monotonic() - started, 1)
            if schema is None:
                text, data = run.data.text, None
            elif json_schema.is_raw(output_model):
                data = run.data.root
                text = json.dumps(data, indent=2, ensure_ascii=False)
                entry["schema"] = output_model.raw_schema
            else:
                text, data = run.data.model_dump_json(indent=2), run.data
                entry["schema"] = schema.__name__
            entry.update(reply=text, seconds=seconds, tokens=_tokens(run.usage),
                         events=len(run.events))
            if run.thread_id:
                self._record["thread_id"] = run.thread_id
            self._append(entry)
            return Reply(session=self.id, turn=turn, text=text, data=data,
                         tokens=run.usage, seconds=seconds, events=list(run.events))

    def update(self, *, model: str | None | object = ..., effort: str | None | object = ...,
               access: str | Access | None = None,
               instructions: str | None = None) -> "Session":
        """Change the model, effort, access or instructions from the next turn
        on. The conversation continues: the providers accept another model or
        other instructions on a conversation they already have."""
        with _lock(self):
            self._reload()
            if model is not ...:
                self._record["model"] = model
            if effort is not ...:
                from research_backend.backends import load

                self._record["effort"] = _effort(self.provider, load(self.provider), effort,
                                                 self.model)
            if access is not None:
                from research_backend.backends import load

                cls = load(self.provider)
                level = _access(access, cls)
                _check_access(self.provider, cls, level)
                self._record["access"] = level.value
            if instructions is not None:
                self._record["instructions"] = instructions
            self._save()
        return self

    def rename(self, new_id: str) -> "Session":
        """Give the session another id. Its folder moves with it."""
        _check_id(new_id)
        target = self.folder.parent / new_id
        if target.exists():
            raise ValueError(f"There is already a session called {new_id!r}.")
        with _lock(self):
            self.folder.rename(target)
        self._path = target / "session.json"
        if Path(self._record["workdir"]).is_relative_to(target.parent / self.id):
            self._record["workdir"] = str(target / Path(self._record["workdir"]).relative_to(
                target.parent / self.id))
        self._record["id"] = new_id
        self._save()
        return self

    def delete(self) -> None:
        """Forget the session: its record and, if it made one, its workspace.
        A workdir you chose is left alone."""
        import shutil

        with _lock(self):
            pass
        shutil.rmtree(self.folder, ignore_errors=True)

    # ---- keeping it ------------------------------------------------------------

    def _append(self, entry: dict) -> None:
        self._record.setdefault("turns", []).append(entry)
        self._record["updated_at"] = entry["at"]
        self._save()

    def _save(self) -> None:
        _home.write_json(self._path, self._record)

    def _reload(self) -> None:
        """Another process may have sent a turn since this object was made."""
        if self._path.exists():
            self._record = _home.read_json(self._path)


# ---- making and finding sessions --------------------------------------------------


def new_session(provider: str, *, model: str | None = None, effort: str | None = None,
                name: str | None = None,
                workdir: str | os.PathLike | None = None,
                access: str | Access | None = None, instructions: str = "",
                home: str | os.PathLike | None = None, **options) -> Session:
    """Start a conversation. Nothing is sent until send().

    provider      "codex", "claude_code", "antigravity" or "fake"
    model         a model id from models(provider); None = the provider's default
    effort        how hard the model thinks: a level from its backend's `efforts`;
                  None = the model's default
    name          the session's id; made up from the provider and time if None
    workdir       the folder the model works in; a new empty one if None
    access        "none", "read_only", "write" or "full"; default read_only
                  (none on antigravity, which cannot do read_only)
    instructions  standing instructions, sent with every message
    home          where sessions are kept; ~/.research_backend if None
    options       passed to the provider: timeout, max_seconds, gpu, ...
    """
    from research_backend.backends import load

    cls = load(provider)
    access = _access(access, cls)
    _check_access(provider, cls, access)
    effort = _effort(provider, cls, effort, model)

    session_id = name or f"{provider}-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
    _check_id(session_id)
    folder = _folder(session_id, home)
    if (folder / "session.json").exists():
        raise ValueError(
            f"There is already a session called {session_id!r}. Continue it with "
            f"RB(session={session_id!r}) or .open_session({session_id!r}), or use "
            f".new_session({session_id!r}, exist_ok=True).")
    if workdir is None:
        work = folder / "workspace"
        work.mkdir(parents=True, exist_ok=True)
    else:
        work = Path(workdir).expanduser().resolve()
        if not work.is_dir():
            raise FileNotFoundError(f"workdir {work} is not a folder.")

    now = _now()
    record = {"id": session_id, "provider": provider, "model": model, "effort": effort,
              "workdir": str(work), "access": access.value, "instructions": instructions,
              "options": options, "thread_id": None, "created_at": now, "updated_at": now,
              "turns": []}
    session = Session(record, folder / "session.json")
    session._save()
    return session


def open_session(session_id: str, *, home: str | os.PathLike | None = None) -> Session:
    """A session made earlier, by its id -- in this process or any other."""
    _check_id(session_id)
    path = _folder(session_id, home) / "session.json"
    if not path.exists():
        known = ", ".join(s.id for s in list_sessions(home=home)[:10]) or "none yet"
        raise SessionNotFound(f"No session called {session_id!r}. Sessions: {known}")
    return Session(_home.read_json(path), path)


def exists(session_id: str, *, home: str | os.PathLike | None = None) -> bool:
    _check_id(session_id)
    return (_folder(session_id, home) / "session.json").exists()


def list_sessions(*, home: str | os.PathLike | None = None) -> list[Session]:
    """Every session, the most recently used first."""
    root = _root(home)
    found = []
    for path in root.glob("*/session.json") if root.exists() else ():
        try:
            found.append(Session(_home.read_json(path), path))
        except (OSError, ValueError):
            continue
    return sorted(found, key=lambda s: s._record.get("updated_at", ""), reverse=True)


# ---- the backends a process has made ------------------------------------------------

_backends: dict[tuple, Any] = {}
_backends_lock = threading.Lock()


def _backend(provider: str, model: str | None, options: dict, *, quiet: bool = True):
    """One backend per (provider, model, options, quiet), made on first use.
    Quiet is part of the key so a session watched with progress=True does not
    make another one, running at the same time, start printing."""
    from research_backend.backends import make

    key = (provider, model, tuple(sorted((k, repr(v)) for k, v in options.items())), quiet)
    with _backends_lock:
        if key not in _backends:
            backend = make(provider, model, **options)
            backend.quiet = quiet
            _backends[key] = backend
        return _backends[key]


# ---- small helpers ------------------------------------------------------------------


def _root(where) -> Path:
    """The sessions folder under `where`, or under the default home."""
    return (Path(where).expanduser() if where else _home.home()) / "sessions"


def _folder(session_id: str, where=None) -> Path:
    return _root(where) / session_id


def _output_model(schema):
    """What the backend is asked for: plain text, a Pydantic class, or a class
    standing for a JSON schema."""
    if schema is None:
        return Text
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        return schema
    return json_schema.model_for(schema)


def _check_id(session_id: str) -> None:
    if not _ID.match(session_id or ""):
        raise ValueError(f"{session_id!r} cannot be a session id: use letters, digits, "
                         f"'.', '_' and '-', starting with a letter or digit.")


def _access(access, cls) -> Access:
    if access is None:
        return Access.NONE if Access.READ_ONLY in getattr(
            cls, "unsupported_access", frozenset()) else Access.READ_ONLY
    return access if isinstance(access, Access) else Access(str(access).lower())


def _effort(provider: str, cls, effort: str | None, model: str | None = None) -> str | None:
    """Checked against the model's own levels where the provider lists them
    (codex), else against everything the provider's CLI takes."""
    from research_backend.base import checked_effort
    from research_backend.catalog import model_efforts

    if effort is None:
        return None
    allowed = model_efforts(provider, model) or getattr(cls, "efforts", ())
    who = f"{provider} {model}" if model else provider
    return checked_effort(who, effort, allowed)


def _check_access(provider: str, cls, access: Access) -> None:
    if access in getattr(cls, "unsupported_access", frozenset()):
        raise BackendUnavailable(f"{provider} cannot do {access.value} access; "
                                 f"choose another level or provider.")
    if not access <= cls.max_access:
        raise BackendUnavailable(f"{provider} can do at most "
                                 f"{cls.max_access.value} access, not {access.value}.")


class _lock:
    """One turn at a time per session, across processes: an exclusive lock on
    <session>/.lock, refused rather than waited for."""

    def __init__(self, session: Session):
        self.path = session.folder / ".lock"

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "w")
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.handle.close()
            raise SessionBusy(f"Session {self.path.parent.name!r} is answering "
                              f"another message; send this one when it is done.") from None
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.handle, fcntl.LOCK_UN)
        self.handle.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _short(stamp: str) -> str:
    try:
        return datetime.fromisoformat(stamp).astimezone().strftime("%d %b %H:%M")
    except (TypeError, ValueError):
        return str(stamp)


def _one_line(text: str, width: int = 60) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[: width - 3] + "..."


def _tokens(usage: Usage | None) -> dict | None:
    if usage is None:
        return None
    return {k: v for k, v in vars(usage).items() if v not in (None, 0)}
