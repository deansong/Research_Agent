"""
research_backend -- talk to Codex, Claude Code and Antigravity the same way.

    import research_backend as rb

    rb.providers()                      # ['antigravity', 'claude_code', 'codex', 'fake']
    print(rb.models("codex"))           # the models codex will accept
    print(rb.usage())                   # how much of every provider's limits is used
    print(rb.status())                  # which providers are logged in

    s = rb.new_session("codex", model="gpt-5.6-sol", name="lit-review")
    print(s.send("Which datasets exist for hotel review classification?"))

    # later, from anywhere:
    rb.send("lit-review", "Which of those are free to use?")
    print(rb.session("lit-review").history())
    print(rb.sessions())

A structured answer instead of text: pass a Pydantic class.

    class Pick(BaseModel):
        dataset: str
        why: str

    reply = s.send("Pick one.", schema=Pick)
    reply.data.dataset

Every function here asks the provider itself; nothing is a hard-coded guess
except where a provider cannot answer (Claude Code cannot list its models or
report its limits outside a turn -- see catalog.py and limits.py).

Sessions are kept in ~/.research_backend (or $RESEARCH_BACKEND_HOME); see
home.py for the layout.
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
    new_session,
    open_session,
)
from research_backend.limits import Quota, Window

__all__ = [
    # what is there
    "providers", "models", "usage", "status",
    # conversations
    "new_session", "session", "sessions", "send", "Session", "Reply",
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


def session(session_id: str) -> Session:
    """A session made earlier, by its id."""
    return open_session(session_id)


def sessions() -> Listing:
    """Every session, the most recently used first."""
    return Listing(list_sessions())


def send(session_id: str, prompt: str, *, schema=None, progress: bool = False) -> Reply:
    """Send a message to a session by its id, continuing its conversation."""
    return open_session(session_id).send(prompt, schema=schema, progress=progress)
