"""
WHAT:  The provider-neutral contract every backend implements.
WHY:   So a node can say "ask the model this and give me a PlannerOutput"
       without knowing whether that means the Codex SDK, a subprocess, or an
       HTTP call.  This is the seam that makes requirement 3 possible.
CONCEPT: Not LangGraph -- a plain dependency-inversion boundary.  Nodes depend
       on this Protocol; concrete providers live in sibling modules.

--------------------------------------------------------------------------
THE CONTRACT, IN ONE SENTENCE
--------------------------------------------------------------------------
    give me a conversation handle (or None to start a new one), a prompt, and
    a Pydantic class -> I give you back a validated instance of that class,
    plus the handle to continue the conversation next time.

Everything else -- how the schema is enforced, how the conversation is stored,
what "read-only" means -- is the provider's problem, not the node's.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Generic, Protocol, TypeVar

from pydantic import BaseModel

OutputT = TypeVar("OutputT", bound=BaseModel)


class Access(Enum):
    """How much of the repository a model turn is allowed to touch.

    This replaces openai_codex.Sandbox in the node files.  It used to be
    imported straight from the Codex SDK, which meant every node had a hard
    dependency on Codex even though the README claimed the backend module was
    the only provider boundary.

    The members are ordered from least to most powerful, and `rank` below is
    what lets the factory check "can this backend do what this role needs?".
    """

    NONE = "none"
    """No repository access at all.  A plain chat API is like this: it can
    reason about text you paste into the prompt, but it cannot open a file."""

    READ_ONLY = "read_only"
    """May look at the repository. May not change it."""

    WRITE = "write"
    """May edit files inside the repository."""

    FULL = "full"
    """May also reach outside the workspace / use the network."""

    @property
    def rank(self) -> int:
        return _RANK[self]

    def __le__(self, other: "Access") -> bool:
        return self.rank <= other.rank


_RANK = {Access.NONE: 0, Access.READ_ONLY: 1, Access.WRITE: 2, Access.FULL: 3}


@dataclass(frozen=True)
class Usage:
    """Token accounting, normalised across providers.

    Every provider reports usage differently (and some not at all), so each
    backend converts into this shape and telemetry.py only has to understand
    one of them.  All fields default to 0/None so a backend that cannot report
    something just leaves it out.
    """

    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    context_window: int | None = None
    cost_usd: float | None = None


@dataclass(frozen=True)
class StructuredRun(Generic[OutputT]):
    """The result of one model turn."""

    data: OutputT
    """A validated instance of the Pydantic class the caller asked for."""

    thread_id: str
    """Opaque handle for continuing this conversation.  Pass it back next
    time.  Nodes store it in state (see ProviderState in agent/state.py) --
    that is all LangGraph ever knows about a provider conversation."""

    is_new_thread: bool
    usage: Usage | None


# ---------------------------------------------------------------------------
# Errors.  Distinct types so callers can react differently: a config problem
# should stop the program, a bad model response might be worth retrying.
# ---------------------------------------------------------------------------

class BackendError(RuntimeError):
    """Anything that went wrong inside a backend."""


class BackendUnavailable(BackendError):
    """This backend cannot run: not installed, not configured, not written yet.

    Raised at STARTUP where possible (see backends/__init__.py) so you find
    out before spending tokens, not halfway through a task.
    """


class BackendOutputError(BackendError):
    """The model replied, but not with valid JSON for the requested schema."""


class BackendOutOfCredits(BackendError):
    """The account has no credit left.

    Distinct from every other failure because waiting genuinely helps: a human
    can top up and the same call will then succeed. So the backend waits and
    retries rather than dying, and the run continues where it paused.
    """


class BackendBusy(BackendError):
    """The provider is overloaded. Transient; retrying shortly usually works."""


class BackendTimeout(BackendError):
    """The provider did not finish within the configured time.

    Worth its own type because the right response differs from other errors:
    a timeout usually means the request was too big or too vague, not that
    anything is broken.
    """


class AgentBackend(Protocol):
    """What every backend must provide.

    A Protocol rather than a base class: a backend just needs these
    attributes and this method, so a test double is 10 lines with no import
    of this file at all.
    """

    name: str
    """Short identifier, matching the `provider` value in config."""

    supports_repo_access: bool
    """False for pure chat APIs that cannot see the filesystem."""

    max_access: Access
    """The most this backend can do.  The factory refuses to assign a role
    that needs more than this -- e.g. the executor needs WRITE, so it cannot
    be served by a backend whose max_access is NONE."""

    def run_structured(
        self,
        *,
        thread_id: str | None,
        repo_path: str,
        access: Access,
        developer_instructions: str,
        prompt: str,
        output_model: type[OutputT],
    ) -> StructuredRun[OutputT]:
        """Run one turn and return a validated `output_model` instance.

        thread_id=None starts a new conversation; otherwise continue that one.
        `developer_instructions` is the role's persona -- pass it EVERY time,
        including on resume (see the note in codex.py about why).
        """
        ...

    def close(self) -> None:
        """Release anything held open.  Safe to call more than once."""
        ...
