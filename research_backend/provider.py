"""
WHAT:  Provider -- one provider, set up once (model, access, instructions,
       where its sessions are kept), from which you open as many sessions as
       you like.
WHY:   Different work wants different providers at once: a Codex session
       reading papers while a Claude Code session drafts, each continuing
       its own conversation. One object per provider keeps each one's
       settings in one place.

    codex  = Provider("codex", model="gpt-5.6-sol")
    claude = Provider("claude_code", model="opus", access="write")

    reader = codex.new_session("lit-review")
    writer = claude.new_session("draft", instructions="You write LaTeX.")
    reader.send("Which datasets exist for ...?")
    writer.send("Draft the related-work section.")

    codex.session("lit-review").send("Which of those are free?")   # later
"""

from __future__ import annotations

import os

from research_backend import conversation
from research_backend.auth import AuthStatus
from research_backend.catalog import ModelList
from research_backend.conversation import Session
from research_backend.limits import Quota


class Provider:
    """One provider and its settings. Every session it opens starts from them;
    any of them can be changed for one session in new_session()."""

    def __init__(self, name: str, model: str | None = None, *,
                 access: str | None = None, instructions: str = "",
                 home: str | os.PathLike | None = None, **options):
        """
        name          "codex", "claude_code", "antigravity" or "fake"
        model         a model id from .models(); None = the provider's default
        access        "none", "read_only", "write" or "full"; default read_only
                      (none on antigravity, which cannot do read_only)
        instructions  standing instructions for its sessions
        home          where its sessions are kept; ~/.research_backend if None
        options       passed to the provider: timeout, max_seconds, gpu, ...
        """
        from research_backend.backends import load

        cls = load(name)  # an unknown name fails here, not at the first send
        self.name = name
        self.model = model
        self.access = conversation._access(access, cls)
        conversation._check_access(name, cls, self.access)
        self.instructions = instructions
        self.home = home
        self.options = options

    def __repr__(self) -> str:
        return f"Provider({self.name!r}, model={self.model!r}, access={self.access.value!r})"

    # ---- about the provider --------------------------------------------------

    def models(self, *, refresh: bool = False) -> ModelList:
        """The models it accepts. Loop over it, or print it."""
        from research_backend.catalog import list_models

        return list_models(self.name, refresh=refresh)

    def usage(self) -> Quota:
        """How much of its usage limits is used, and when each resets."""
        from research_backend.limits import usage_of

        return usage_of(self.name)

    def status(self) -> AuthStatus:
        """Whether it is installed and logged in, and how to log in if not."""
        from research_backend.auth import status_for

        return status_for(self.name)

    # ---- its sessions ------------------------------------------------------------

    def new_session(self, name: str | None = None, *, model: str | None = None,
                    workdir: str | os.PathLike | None = None, access: str | None = None,
                    instructions: str | None = None, **options) -> Session:
        """Start a conversation. Anything not given comes from this Provider.

        name     the session's id, to continue it by later; made up if None
        workdir  the folder the model works in; a new empty one if None
        """
        return conversation.new_session(
            self.name, model=model or self.model, name=name, workdir=workdir,
            access=access or self.access,
            instructions=self.instructions if instructions is None else instructions,
            home=self.home, **{**self.options, **options})

    def session(self, session_id: str) -> Session:
        """One of its sessions, made earlier -- to continue it."""
        found = conversation.open_session(session_id, home=self.home)
        if found.provider != self.name:
            raise ValueError(f"Session {session_id!r} is a {found.provider} session, "
                             f"not {self.name}.")
        return found

    def sessions(self) -> list[Session]:
        """Its sessions, the most recently used first."""
        from research_backend import Listing

        return Listing(s for s in conversation.list_sessions(home=self.home)
                       if s.provider == self.name)
