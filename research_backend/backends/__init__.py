"""
WHAT:  The backends: one module per provider -- per way of running a model --
       and `make()`, which builds one by name.
WHY:   Everything above this folder talks to a provider through the same
       method -- run_structured() -- so nothing above it needs to know whether
       a turn is the Codex SDK, a `claude` subprocess or an `agy` subprocess.

    codex         the openai-codex SDK (Codex app-server)
    claude_code   the `claude` CLI, one subprocess per turn
    antigravity   the `agy` CLI, one subprocess per turn
    api           a plain chat API -- registered, not written yet
    fake          canned answers, no model, no network: for tests
"""

from __future__ import annotations

import atexit
import importlib
import threading

from research_backend.base import BackendUnavailable

#: provider name -> "module:Class". Adding a provider is a module and a line here.
PROVIDERS: dict[str, str] = {
    "codex": "research_backend.backends.codex:CodexBackend",
    "claude_code": "research_backend.backends.claude_code:ClaudeCodeBackend",
    "antigravity": "research_backend.backends.antigravity:AntigravityBackend",
    "api": "research_backend.backends.api:ApiBackend",
    "fake": "research_backend.backends.fake:FakeBackend",
}

#: Registered, but not implemented.
STUB_PROVIDERS: frozenset[str] = frozenset({"api"})


def load(provider: str, registry: dict[str, str] | None = None):
    """The class for `provider`, imported only now -- so using Claude Code does
    not need the Codex SDK installed."""
    registry = PROVIDERS if registry is None else registry
    if provider not in registry:
        known = ", ".join(sorted(registry))
        raise BackendUnavailable(f"Unknown provider '{provider}'. Known providers: {known}")
    module_path, class_name = registry[provider].split(":")
    return getattr(importlib.import_module(module_path), class_name)


# ---- the one Codex client a process needs ----------------------------------

_codex = None
_codex_lock = threading.Lock()


def codex_client():
    """A Codex client, opened on first use and shared by every Codex backend
    this process makes. Closed when the process exits."""
    global _codex
    with _codex_lock:
        if _codex is None:
            from research_backend.backends.codex import open_client

            context = open_client()
            _codex = (context, context.__enter__())
            atexit.register(close_codex_client)
        return _codex[1]


def close_codex_client() -> None:
    global _codex
    with _codex_lock:
        if _codex is not None:
            context, _ = _codex
            _codex = None
            context.__exit__(None, None, None)


def make(provider: str, model: str | None = None, **options):
    """A ready backend for `provider`, e.g. make("codex", "gpt-5.6-sol").

    `options` go to the provider's constructor: timeout, max_seconds, gpu,
    executable, ... -- see each provider's __init__.
    """
    cls = load(provider)
    if provider == "codex":
        return cls(codex_client(), model=model, **options)
    return cls(model=model, **options)
