"""
WHAT:  Structural checks on every backend, real ones included.
WHY:   A refactor once dedented CodexBackend._get_thread out of its class and
       every other test still passed -- because nothing in the suite ever
       touched a real backend. The failure only appeared against a live API
       call, which is the slowest and most expensive place to find it.
CONCEPT: None. These are cheap structural assertions: no network, no API key,
       no model. They just check each backend is SHAPED like a backend.

Run with:   python tests/test_backends.py
"""

from __future__ import annotations

import inspect
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from agent.backends import PROVIDERS, _load
from agent.backends.base import Access

REQUIRED_ATTRS = ("name", "supports_repo_access", "max_access")
REQUIRED_METHODS = ("run_structured", "close")


def test_every_registered_provider_loads():
    for provider in PROVIDERS:
        cls = _load(provider)
        assert inspect.isclass(cls), f"{provider} did not resolve to a class"
    print(f"PASS  all {len(PROVIDERS)} registered providers import and resolve")


def test_every_backend_has_the_protocol_shape():
    """Catches a method accidentally dedented out of its class."""
    for provider in PROVIDERS:
        cls = _load(provider)
        for attr in REQUIRED_ATTRS:
            assert hasattr(cls, attr), f"{provider}.{attr} is missing"
        for method in REQUIRED_METHODS:
            assert callable(getattr(cls, method, None)), f"{provider}.{method} is missing"
        assert isinstance(cls.max_access, Access), f"{provider}.max_access is not an Access"
    print(f"PASS  every backend exposes {REQUIRED_ATTRS + REQUIRED_METHODS}")


def test_codex_backend_keeps_its_private_helpers():
    """The specific regression: _get_thread and _run_turn must be METHODS.

    Both were briefly module-level (or nested in another function) after an
    edit, which turned every real Codex call into an AttributeError while the
    whole offline suite stayed green.
    """
    from agent.backends.codex import CodexBackend

    for method in ("_get_thread", "_run_turn", "run_structured", "close"):
        assert method in vars(CodexBackend), \
            f"CodexBackend.{method} is not defined on the class"

    signature = inspect.signature(CodexBackend._get_thread)
    for param in ("thread_id", "repo_path", "access", "developer_instructions"):
        assert param in signature.parameters, f"_get_thread lost the {param} parameter"

    print("PASS  CodexBackend._get_thread and ._run_turn are methods on the class")


def test_run_structured_signatures_agree():
    """Every backend must accept the same keyword arguments, since nodes call
    them interchangeably."""
    expected = {"thread_id", "repo_path", "access", "developer_instructions",
                "prompt", "output_model"}
    for provider in PROVIDERS:
        cls = _load(provider)
        params = set(inspect.signature(cls.run_structured).parameters) - {"self"}
        assert expected <= params, f"{provider}.run_structured is missing {expected - params}"
    print("PASS  every run_structured accepts the same keyword arguments")


def test_stub_backends_fail_loudly_at_construction():
    """A stub must refuse at startup, not halfway through a task."""
    from agent.backends.base import BackendUnavailable

    for provider in ("api", "claude_code", "antigravity"):
        cls = _load(provider)
        try:
            cls(model=None)
            raise AssertionError(f"{provider} constructed silently; it should refuse")
        except BackendUnavailable as exc:
            assert len(str(exc)) > 80, f"{provider}'s message should explain what to do"
    print("PASS  api / claude_code / antigravity refuse at construction with real guidance")


def test_progress_unwraps_the_threaditem_wrapper():
    """The regression that printed nothing at all during a live run.

    Concrete items arrive wrapped in a ThreadItem RootModel, so
    type(item).__name__ is always the literal "ThreadItem". The first version
    of _progress matched on that name, matched nothing, and stayed silent
    while the executor was visibly running commands and writing files.

    Built from the REAL SDK types rather than hand-made stubs, so the test
    fails if the SDK changes the wrapper -- which is the thing worth knowing.
    """
    from openai_codex.generated.v2_all import (
        CommandExecutionThreadItem,
        ItemCompletedNotification,
        ItemStartedNotification,
        ThreadItem,
    )

    from agent.backends._progress import describe

    command = CommandExecutionThreadItem.model_construct(
        id="c1", command="python -m pytest -q", exit_code=0,
    )
    wrapped = ThreadItem.model_construct(root=command)

    class Event:
        def __init__(self, payload):
            self.payload = payload

    started = Event(ItemStartedNotification.model_construct(
        item=wrapped, thread_id="t", turn_id="u", started_at_ms=0))
    line = describe(started)
    assert line and "pytest" in line, f"a started command produced {line!r}"
    assert line.strip().startswith("$"), line

    # A command that succeeded was already announced when it started, so
    # completion should stay quiet rather than printing it twice.
    finished = Event(ItemCompletedNotification.model_construct(
        item=wrapped, thread_id="t", turn_id="u", completed_at_ms=1))
    assert describe(finished) is None, "a clean command should not print twice"

    # A failure must speak up.
    failed = ThreadItem.model_construct(
        root=CommandExecutionThreadItem.model_construct(
            id="c2", command="pytest", exit_code=1))
    line = describe(Event(ItemCompletedNotification.model_construct(
        item=failed, thread_id="t", turn_id="u", completed_at_ms=1)))
    assert line and "exited 1" in line, f"a failed command produced {line!r}"

    print("PASS  progress unwraps ThreadItem and reports commands (and failures)")


def test_progress_never_raises():
    """Reporting must not be able to break the turn it is describing."""
    from agent.backends._progress import describe

    class Weird:
        payload = object()

    for event in (None, object(), Weird(), "not an event"):
        describe(event)   # must not raise
    print("PASS  describe() tolerates anything the stream throws at it")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\nAll backend tests passed.")
