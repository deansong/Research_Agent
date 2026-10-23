"""
WHAT:  ResearchBackend -- what `RB(...)` returns: a provider, a model, and the
       session you are talking to, with everything you do with them as methods.
WHY:   One object to hold "who am I talking to, and in which conversation",
       so a script reads as what it does. Make one per conversation you want
       open at once; each continues its own session.

    import research_backend as RB

    reader = RB(provider="codex", model="gpt-5.6-sol")
    reader.new_session("lit-review")
    reader.send("Which datasets exist for ...?")
    print(reader.context())

    writer = RB(provider="claude_code", model="opus")
    writer.send("Draft the introduction.")       # starts a session if none

    later = RB(session="lit-review")             # continue it, from anywhere
    later.send("Which of those are free to use?")
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from pydantic import BaseModel

from research_backend import conversation
from research_backend.base import Access, Usage
from research_backend.conversation import Reply, Session


class ResearchBackend:
    """A provider, a model and the current session. See the module docstring."""

    def __init__(self, provider: str | None = None, model: str | None = None, *,
                 session: str | None = None, access: str | None = None,
                 instructions: str = "", workdir: str | os.PathLike | None = None,
                 home: str | os.PathLike | None = None, **options):
        """
        provider      "codex", "claude_code", "antigravity" or "fake"; or set it
                      later with set_provider()
        model         a model id from list_models(); None = the provider's default
        session       continue this session (its provider and model come with it)
        access        "none", "read_only", "write" or "full"; default read_only
                      (none on antigravity, which cannot do read_only)
        instructions  standing instructions, sent with every message
        workdir       the folder the model works in; a new empty one per session
                      if None
        home          where sessions are kept; ~/.research_backend if None
        options       passed to the provider: timeout, max_seconds, gpu, ...
        """
        self._provider: str | None = None
        self._model = model
        self._access = access
        self._instructions = instructions
        self._workdir = workdir
        self._home = home
        self._options = options
        self._session: Session | None = None
        if provider is not None:
            self.set_provider(provider, model)
        if session is not None:
            if provider is not None:
                found = conversation.open_session(session, home=home)
                if found.provider != provider:
                    raise ValueError(f"Session {session!r} is on {found.provider}, "
                                     f"not {provider}.")
            self.open_session(session)

    # ---- what is set ----------------------------------------------------------

    @property
    def provider(self) -> str | None:
        return self._provider

    @property
    def model(self) -> str | None:
        return self._session.model if self._session else self._model

    @property
    def session_id(self) -> str | None:
        """The current session's id -- what you continue it by. None until a
        session is started or opened."""
        return self._session.id if self._session else None

    @property
    def session(self) -> Session | None:
        """The current session itself: its turns, folder, provider thread id."""
        return self._session

    def __repr__(self) -> str:
        return (f"RB(provider={self.provider!r}, model={self.model!r}, "
                f"session={self.session_id!r})")

    # ---- choosing -------------------------------------------------------------

    def set_provider(self, provider: str, model: str | None = None) -> "ResearchBackend":
        """Talk to another provider. A session belongs to one provider, so the
        current session is put aside; the next send() starts a new one, or
        open_session() continues an old one."""
        from research_backend.backends import load

        cls = load(provider)  # an unknown name fails here, not at the first send
        if self._access is not None:
            conversation._check_access(provider, cls, conversation._access(self._access, cls))
        if self._session and self._session.provider != provider:
            self._session = None
        self._provider = provider
        self._model = model
        return self

    def set_model(self, model: str | None) -> "ResearchBackend":
        """Use another model from the next message on -- in the current session
        too, which carries on with the new model."""
        self._model = model
        if self._session:
            self._session.update(model=model)
        return self

    def set_access(self, access: str) -> "ResearchBackend":
        """"none", "read_only", "write" or "full", from the next message on."""
        if self._provider:
            from research_backend.backends import load

            cls = load(self._provider)
            conversation._check_access(self._provider, cls, conversation._access(access, cls))
        self._access = access
        if self._session:
            self._session.update(access=access)
        return self

    def set_instructions(self, instructions: str) -> "ResearchBackend":
        """Standing instructions, from the next message on."""
        self._instructions = instructions
        if self._session:
            self._session.update(instructions=instructions)
        return self

    # ---- about providers ----------------------------------------------------------

    def list_providers(self) -> list[str]:
        from research_backend import list_providers

        return list_providers()

    def list_models(self, provider: str | None = None):
        """The models a provider accepts -- the current one unless you name one."""
        from research_backend.catalog import list_models

        return list_models(self._need(provider))

    def usage(self, provider: str | None = None):
        """How much of a provider's usage limits is used, and when each resets."""
        from research_backend.limits import usage_of

        return usage_of(self._need(provider))

    def status(self, provider: str | None = None):
        """Whether a provider is installed and logged in, and how to log in if not."""
        from research_backend.auth import status_for

        return status_for(self._need(provider))

    # ---- sessions -----------------------------------------------------------------

    def new_session(self, name: str | None = None, *,
                    workdir: str | os.PathLike | None = None) -> str:
        """Start a new conversation and make it the current one. Returns its id;
        without a name, one is made up from the provider and the time."""
        self._session = conversation.new_session(
            self._need(None), model=self._model, name=name,
            workdir=workdir if workdir is not None else self._workdir,
            access=self._access, instructions=self._instructions, home=self._home,
            **self._options)
        return self._session.id

    def open_session(self, session_id: str) -> str:
        """Continue a session made earlier, from any script. Its provider and
        model become the current ones."""
        self._session = conversation.open_session(session_id, home=self._home)
        self._provider = self._session.provider
        self._model = self._session.model
        return self._session.id

    def list_sessions(self, provider: str | None = None):
        """Every session, the most recently used first; only one provider's if
        you name it."""
        from research_backend import list_sessions

        return list_sessions(provider, home=self._home)

    def delete_session(self, session_id: str | None = None) -> None:
        """Forget a session -- the current one unless you name one."""
        target = (conversation.open_session(session_id, home=self._home)
                  if session_id else self._session)
        if target is None:
            raise ValueError("No session to delete.")
        if self._session and target.id == self._session.id:
            self._session = None
        target.delete()

    # ---- talking ----------------------------------------------------------------------

    def send(self, prompt: str, *, schema: type[BaseModel] | None = None,
             progress: bool = False) -> Reply:
        """Send a message in the current session and wait for the answer; with
        no session yet, one is started. `schema` (a Pydantic class) asks for a
        structured answer, in the reply's `data`; `progress=True` prints what
        the model does while it works."""
        if self._session is None:
            self.new_session()
        return self._session.send(prompt, schema=schema, progress=progress)

    def history(self, last: int | None = None) -> str:
        """The conversation so far, as text."""
        if self._session is None:
            return "(no session yet)"
        self._session._reload()
        return self._session.history(last)

    def context(self) -> "Context":
        """Where the current session stands: how much of the model's context
        window the conversation fills, and its last turns."""
        if self._session is None:
            return Context(None, self._provider, self._model, 0, None,
                           _window(self._provider, self._model, None), "")
        session = self._session
        session._reload()
        turns = session.turns
        tokens = window = None
        for turn in reversed(turns):
            if turn.get("tokens"):
                tokens = context_tokens(session.provider, turn["tokens"])
                window = turn["tokens"].get("context_window")
                break
        return Context(session.id, session.provider, session.model, len(turns), tokens,
                       _window(session.provider, session.model, window),
                       session.history(last=3))

    # ---- small helpers --------------------------------------------------------------

    def _need(self, provider: str | None) -> str:
        chosen = provider or self._provider
        if not chosen:
            raise ValueError("No provider chosen: RB(provider=...) or set_provider(...). "
                             "list_providers() says which there are.")
        return chosen


@dataclass(frozen=True)
class Context:
    """Where a session stands. Printing it says so in words."""

    session_id: str | None
    provider: str | None
    model: str | None
    turns: int
    tokens: int | None
    """About how many tokens the conversation fills: the last turn's input."""
    window: int
    """How many the model can read."""
    recent: str
    """The last few turns, as text."""

    @property
    def percent(self) -> float | None:
        return None if self.tokens is None else round(100 * self.tokens / self.window, 1)

    def __str__(self) -> str:
        who = f"{self.provider}/{self.model or 'default model'}"
        if self.session_id is None:
            return f"{who}: no session yet. Window: {self.window:,} tokens."
        lines = [f"{self.session_id}: {who}, {self.turns} turn(s)"]
        if self.tokens is None:
            lines.append(f"context: not reported yet, of a {self.window:,}-token window")
        else:
            lines.append(f"context: about {self.tokens:,} of {self.window:,} tokens "
                         f"({self.percent:g}%)")
        if self.recent:
            lines += ["", self.recent]
        return "\n".join(lines)


def context_tokens(provider: str, tokens: dict | Usage) -> int:
    """How much a turn's prompt filled the context, from its token counts.

    Measured 2026-10-23: codex and antigravity count cached tokens inside
    input_tokens (15,857 input of which 14,080 cached); claude_code counts
    them apart (6 input, 38,284 cache reads)."""
    if isinstance(tokens, Usage):
        tokens = vars(tokens)
    read = int(tokens.get("input_tokens") or 0)
    cached = int(tokens.get("cached_input_tokens") or 0)
    return read + cached if provider == "claude_code" else max(read, cached)


def _window(provider: str | None, model: str | None, reported: int | None) -> int:
    from research_backend.catalog import context_window

    return context_window(provider or "", model, reported)
