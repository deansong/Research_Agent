"""
research_backend -- talk to Codex, Claude Code and Antigravity the same way.

    import research_backend as RB

    RB.list_providers()                 # ['antigravity', 'claude_code', 'codex', 'fake']
    RB.list_models("codex")             # the models codex accepts
    RB.list_efforts("codex")            # its reasoning-effort levels
    RB.usage("codex")                   # its limits, and when they reset
    RB.usage()                          # every provider's
    RB.list_sessions()                  # every session so far

    reader = RB(provider="codex", model="gpt-5.6-sol", effort="high")   # or RB(), then:
    reader.set_provider("codex")
    reader.list_models()
    reader.set_effort("medium")
    reader.new_session("lit-review")
    reader.session_id                   # 'lit-review'
    reader.send("Which datasets exist for hotel review classification?")
    reader.context()                    # how full its context window is, last turns
    reader.usage()

    writer = RB(provider="claude_code", model="opus")     # another, side by side
    writer.send("Draft the introduction.")                # starts a session if none

    later = RB(session="lit-review")    # continue a session, from any script
    later.send("Which of those are free to use?")

A structured answer instead of text: pass a Pydantic class, and the reply's
`data` is an instance of it.

    reply = reader.send("Pick one.", schema=Pick)
    reply.data.dataset

Everything asks the provider itself; nothing is a hard-coded guess except where
a provider cannot answer (Claude Code cannot list its models or report its
limits outside a turn -- see catalog.py and limits.py).

Sessions are kept in ~/.research_backend (or $RESEARCH_BACKEND_HOME, or the
`home` you give RB); see home.py for the layout.
"""

from __future__ import annotations

import sys
import types

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
from research_backend.client import Context, ResearchBackend
from research_backend.conversation import Reply, Session, SessionBusy, SessionNotFound
from research_backend.limits import Quota, Window

__all__ = [
    # RB(...) makes one of these
    "ResearchBackend",
    # every provider at once
    "list_providers", "list_models", "list_efforts", "usage", "status", "list_sessions",
    # what those and its methods return
    "Reply", "Context", "Session", "Model", "ModelList", "Quota", "Window",
    "AuthStatus", "Usage", "Access",
    # what can go wrong
    "BackendError", "BackendUnavailable", "BackendOutputError", "BackendOutOfCredits",
    "BackendBusy", "BackendCancelled", "BackendTimeout", "SessionNotFound", "SessionBusy",
]


class Listing(list):
    """A list that prints one item after another, readably."""

    def __str__(self) -> str:
        return "\n\n".join(str(item) for item in self)


def list_providers() -> list[str]:
    """The providers you can send to. ("api" is registered but not written.)"""
    from research_backend.backends import PROVIDERS, STUB_PROVIDERS

    return sorted(set(PROVIDERS) - STUB_PROVIDERS)


def _real() -> list[str]:
    return [p for p in list_providers() if p != "fake"]


def list_models(provider: str | None = None, *, refresh: bool = False):
    """The models a provider accepts: a ModelList you can loop over (each a
    Model with .id, .label, .default) and print. Without a provider, every
    provider's. Lists are cached for ten minutes; refresh=True asks again."""
    from research_backend.catalog import list_all
    from research_backend.catalog import list_models as one

    if provider is not None:
        return one(provider, refresh=refresh)
    return Listing(list_all(_real(), refresh=refresh))


def list_efforts(provider: str, model: str | None = None) -> list[str]:
    """The reasoning-effort levels a model has, least first (the provider's
    default model when None). Codex says per model; for the others it is what
    their CLI's --effort takes."""
    from research_backend.backends import load
    from research_backend.catalog import model_efforts

    return list(model_efforts(provider, model) or getattr(load(provider), "efforts", ()))


def usage(provider: str | None = None):
    """How much of a provider's usage limits is used, and when each resets: a
    Quota. Without a provider, every provider's."""
    from research_backend.limits import usage_all, usage_of

    if provider is not None:
        return usage_of(provider)
    return Listing(usage_all(_real()))


def status(provider: str | None = None):
    """Whether a provider is installed and logged in, and how to log in if not.
    Without a provider, every provider's."""
    from research_backend.auth import status_all, status_for

    if provider is not None:
        return status_for(provider)
    return Listing(status_all(_real()))


def list_sessions(provider: str | None = None, *, home=None) -> Listing:
    """Every session, the most recently used first; only one provider's if you
    name it. Continue one with RB(session=its_id)."""
    from research_backend.conversation import list_sessions as every

    return Listing(s for s in every(home=home) if provider in (None, s.provider))


class _CallableModule(types.ModuleType):
    """So that `import research_backend as RB; RB(provider="codex")` works:
    calling the module makes a ResearchBackend."""

    def __call__(self, *args, **kwargs) -> ResearchBackend:
        return ResearchBackend(*args, **kwargs)


sys.modules[__name__].__class__ = _CallableModule
