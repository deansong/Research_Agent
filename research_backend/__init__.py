"""
research_backend -- talk to Codex, Claude Code and Antigravity the same way.

Each provider is a Provider object, set up once; each conversation is a
Session it opens. Use as many of both as you like, side by side:

    from research_backend import Provider

    codex  = Provider("codex", model="gpt-5.6-sol")
    claude = Provider("claude_code", model="opus", access="write")

    print(codex.models())               # the models codex accepts
    print(codex.usage())                # its limits, and when they reset
    print(claude.status())              # logged in? how to log in if not

    reader = codex.new_session("lit-review")
    writer = claude.new_session("draft", instructions="You write LaTeX.")
    print(reader.send("Which datasets exist for hotel review classification?"))
    print(writer.send("Draft a related-work paragraph."))

    # later, from any script:
    codex.session("lit-review").send("Which of those are free to use?")

A structured answer instead of text: pass a Pydantic class.

    class Pick(BaseModel):
        dataset: str
        why: str

    reply = reader.send("Pick one.", schema=Pick)
    reply.data.dataset

For every provider at once:

    import research_backend as rb
    rb.providers()        # ['antigravity', 'claude_code', 'codex', 'fake']
    print(rb.usage())     # every provider's limits
    print(rb.status())    # every provider's login
    print(rb.sessions())  # every session, whichever provider
    rb.session("lit-review")

Everything here asks the provider itself; nothing is a hard-coded guess except
where a provider cannot answer (Claude Code cannot list its models or report
its limits outside a turn -- see catalog.py and limits.py).

Sessions are kept in ~/.research_backend (or $RESEARCH_BACKEND_HOME, or the
`home` you give a Provider); see home.py for the layout.
"""

from __future__ import annotations

from research_backend.auth import AuthStatus
from research_backend.base import (
    Access,
    BackendBusy,
    BackendCancelled,
    BackendError,
    BackendOutOfCredits,
    BackendOutputError,
    BackendTimeout,
    BackendUnavailable,
    Usage,
)
from research_backend.catalog import ModelInfo as Model
from research_backend.catalog import ModelList
from research_backend.conversation import (
    Reply,
    Session,
    SessionBusy,
    SessionNotFound,
    list_sessions,
    open_session,
)
from research_backend.limits import Quota, Window
from research_backend.provider import Provider

__all__ = [
    # one provider, and its conversations
    "Provider", "Session", "Reply",
    # every provider at once
    "providers", "models", "usage", "status", "sessions", "session",
    # the types those return
    "Model", "ModelList", "Quota", "Window", "AuthStatus", "Usage", "Access",
    # what can go wrong
    "BackendError", "BackendUnavailable", "BackendOutputError", "BackendOutOfCredits",
    "BackendBusy", "BackendCancelled", "BackendTimeout", "SessionNotFound", "SessionBusy",
]


class Listing(list):
    """A list that prints one item after another, readably."""

    def __str__(self) -> str:
        return "\n\n".join(str(item) for item in self)


def providers() -> list[str]:
    """The providers you can send to. ("api" is registered but not written.)"""
    from research_backend.backends import PROVIDERS, STUB_PROVIDERS

    return sorted(set(PROVIDERS) - STUB_PROVIDERS)


def _real() -> list[str]:
    return [p for p in providers() if p != "fake"]


def models(provider: str | None = None, *, refresh: bool = False):
    """The models a provider will accept: a ModelList you can loop over (each
    a Model with .id, .label, .default) and print. Without a provider, every
    provider's, in a Listing.

    Lists are cached for ten minutes; refresh=True asks again."""
    from research_backend.catalog import list_all, list_models

    if provider is not None:
        return list_models(provider, refresh=refresh)
    return Listing(list_all(_real(), refresh=refresh))


def usage(provider: str | None = None):
    """How much of a provider's usage limits is used and when each resets: a
    Quota. Without a provider, every provider's, in a Listing."""
    from research_backend.limits import usage_all, usage_of

    if provider is not None:
        return usage_of(provider)
    return Listing(usage_all(_real()))


def status(provider: str | None = None):
    """Whether a provider is installed and logged in, and how to log in if not:
    an AuthStatus. Without a provider, every provider's, in a Listing."""
    from research_backend.auth import status_all, status_for

    if provider is not None:
        return status_for(provider)
    return Listing(status_all(_real()))


def session(session_id: str, *, home=None) -> Session:
    """A session made earlier, by its id, whichever provider it is on."""
    return open_session(session_id, home=home)


def sessions(*, home=None) -> Listing:
    """Every session, whichever provider, the most recently used first."""
    return Listing(list_sessions(home=home))
