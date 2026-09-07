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


def test_configured_options_reach_the_backend():
    """Options in config must actually arrive, for every provider.

    The codex branch of _instantiate built `cls(client, model=...)` and dropped
    spec.options on the floor. So `{"options": {"timeout": 3600}}` did nothing
    -- and BackendTimeout's own message told you to use exactly that. A dead
    end is worse than no advice.
    """
    from unittest.mock import MagicMock

    from agent.backends import _instantiate
    from agent.config import BackendConfig

    spec = BackendConfig(provider="codex", model="m", options={"timeout": 1234})
    backend = _instantiate(spec, role="anything", codex_client=MagicMock())
    assert backend.timeout == 1234, (
        f"configured timeout did not reach the backend (got {backend.timeout})"
    )
    assert backend.model == "m"

    # And the stubs, which take **options too -- they must not choke on it.
    from agent.backends.base import BackendUnavailable

    for provider in ("api", "claude_code", "antigravity"):
        try:
            _instantiate(BackendConfig(provider=provider, options={"anything": 1}),
                         role="r", codex_client=None)
        except BackendUnavailable:
            pass   # refusing is correct; crashing on an unexpected kwarg is not

    print("PASS  configured options reach the backend (timeout included)")


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


def test_provider_errors_are_classified():
    """Out-of-credits and overload arrive as plain RuntimeErrors carrying the
    provider's message. The SDK's is_retryable_error only knows ServerBusyError,
    so we read the message -- and the distinction matters, because one of these
    is fixed by waiting and the other is not."""
    from agent.backends.base import BackendBusy, BackendOutOfCredits
    from agent.backends.codex import classify

    credits = classify(RuntimeError("Your workspace is out of credits. Add credits to continue."))
    assert isinstance(credits, BackendOutOfCredits), type(credits)

    for text in ("server busy", "Rate limit exceeded", "overloaded, try again later"):
        assert isinstance(classify(RuntimeError(text)), BackendBusy), text

    # A genuine bug must NOT be mistaken for something waiting will fix.
    bug = classify(RuntimeError("invalid_json_schema: Missing 'question'."))
    assert not isinstance(bug, (BackendBusy, BackendOutOfCredits)), bug
    print("PASS  out-of-credits / busy / real errors are told apart")


def test_out_of_credits_waits_then_succeeds():
    """The behaviour the whole change exists for: top up, and it carries on."""
    from unittest.mock import MagicMock

    from agent.backends.codex import CodexBackend

    backend = CodexBackend(MagicMock(), credit_wait_seconds=0, credit_wait_attempts=5)
    calls = {"n": 0}

    def flaky(thread, prompt, schema):
        calls["n"] += 1
        if calls["n"] < 3:          # broke twice, then someone added credits
            raise RuntimeError("Your workspace is out of credits.")
        return "the result"

    backend._run_turn = flaky
    assert backend._run_with_recovery(None, "p", {}) == "the result"
    assert calls["n"] == 3, calls
    print("PASS  out of credits: waits, retries, and succeeds once topped up")


def test_out_of_credits_eventually_gives_up_with_resume_advice():
    from unittest.mock import MagicMock

    from agent.backends.base import BackendOutOfCredits
    from agent.backends.codex import CodexBackend

    backend = CodexBackend(MagicMock(), credit_wait_seconds=0, credit_wait_attempts=2)
    backend._run_turn = lambda *a: (_ for _ in ()).throw(
        RuntimeError("Your workspace is out of credits."))

    try:
        backend._run_with_recovery(None, "p", {})
        raise AssertionError("should have given up")
    except BackendOutOfCredits as exc:
        assert "--session" in str(exc), "the message must say how to resume"
    print("PASS  gives up after the configured waits, and says how to resume")


def test_a_real_error_is_not_retried():
    """Retrying a malformed request just spends the same money twice."""
    from unittest.mock import MagicMock

    from agent.backends.codex import CodexBackend

    backend = CodexBackend(MagicMock(), credit_wait_seconds=0)
    calls = {"n": 0}

    def broken(thread, prompt, schema):
        calls["n"] += 1
        raise RuntimeError("invalid_json_schema: Missing 'question'.")

    backend._run_turn = broken
    try:
        backend._run_with_recovery(None, "p", {})
    except RuntimeError:
        pass
    assert calls["n"] == 1, f"a schema error was retried {calls['n']} times"
    print("PASS  a genuine error is raised immediately, not retried")


def test_busy_backs_off_then_gives_up():
    from unittest.mock import MagicMock

    from agent.backends.base import BackendBusy
    from agent.backends.codex import CodexBackend

    backend = CodexBackend(MagicMock(), busy_attempts=2)
    backend._wait = lambda *a, **k: None      # no real sleeping in tests
    calls = {"n": 0}

    def busy(thread, prompt, schema):
        calls["n"] += 1
        raise RuntimeError("server busy")

    backend._run_turn = busy
    try:
        backend._run_with_recovery(None, "p", {})
        raise AssertionError("should have given up")
    except BackendBusy:
        pass
    assert calls["n"] == 3, calls   # first try + 2 retries
    print("PASS  provider-busy backs off a bounded number of times")



# ---------------------------------------------------------------------------
# the timeout measures SILENCE, not elapsed time
# ---------------------------------------------------------------------------


class _Stream:
    """A notification stream that emits `events` with `gap` seconds between."""

    def __init__(self, events, gap):
        self._events = list(events)
        self._gap = gap
        self.closed = False

    def __iter__(self):
        import time

        for event in self._events:
            time.sleep(self._gap)
            yield event

    def close(self):
        self.closed = True


class _Handle:
    def __init__(self, stream):
        self.id = "turn-1"
        self._stream = stream
        self.interrupted = False

    def stream(self):
        return self._stream

    def interrupt(self):
        self.interrupted = True


class _Thread:
    def __init__(self, handle):
        self._handle = handle

    def turn(self, prompt, output_schema=None):
        return self._handle


def _drain(items, turn_id):
    """Stand-in for _collect that really consumes the stream.

    Worth its own function with this comment: the first version of these tests
    patched _collect with a plain return value, so it never iterated the
    stream, the worker finished in microseconds, and all three tests "passed"
    without exercising the loop at all.
    """
    for _ in items:
        pass
    return "collected"


def _quiet_event():
    """An event describe() deliberately does not print.

    This is the whole crux. A turn that is reasoning steadily emits plenty of
    these, and the old code only reset its clock on PRINTABLE events -- so
    thinking hard looked exactly like hanging.
    """
    from agent.backends._progress import describe

    class Payload:
        pass

    class Event:
        payload = Payload()

    assert describe(Event()) is None, "this fixture must be a non-printing event"
    return Event()


def test_a_turn_that_keeps_streaming_is_never_killed_for_being_slow():
    """The regression that cost a real run.

    A node ran past the old 600s wall-clock deadline and was interrupted while
    Codex was still sending events -- ten minutes of real work discarded,
    because a node is atomic. The limit is on silence now, so a turn that keeps
    talking runs as long as it needs.
    """
    from unittest.mock import MagicMock, patch

    from agent.backends.codex import CodexBackend

    # Eight events, 0.05s apart: 0.4s of steady chatter. The idle limit is
    # 0.25s -- shorter than the run, longer than any single gap -- so a
    # wall-clock rule would kill this and an idle rule must not.
    stream = _Stream([_quiet_event() for _ in range(8)], gap=0.05)
    handle = _Handle(stream)
    backend = CodexBackend(MagicMock(), timeout=0.25, max_seconds=30)

    with patch("agent.backends.codex._collect", _drain):
        result, recorded = backend._run_turn(_Thread(handle), "go", {})

    assert result == "collected", result
    # _run_turn returns the full event record alongside the result. These
    # fixture events are the non-printing kind, so they contribute nothing --
    # which is the point: a turn's record must not depend on what was worth
    # printing. Real content is checked in test_progress_records_everything.
    assert isinstance(recorded, list)
    assert not handle.interrupted, "a productive turn must not be interrupted"
    assert stream.closed
    print("PASS  a turn streaming events is not killed for running long")


def test_a_turn_that_goes_quiet_is_abandoned():
    """The other half: silence really is a hang, and must not wait for ever."""
    from unittest.mock import MagicMock, patch

    from agent.backends.base import BackendTimeout
    from agent.backends.codex import CodexBackend

    # Two events with a five-second gap: the second never arrives in time.
    stream = _Stream([_quiet_event(), _quiet_event()], gap=5.0)
    handle = _Handle(stream)
    backend = CodexBackend(MagicMock(), timeout=0.3, max_seconds=30)

    with patch("agent.backends.codex._collect", _drain):
        try:
            backend._run_turn(_Thread(handle), "go", {})
            raise AssertionError("expected BackendTimeout")
        except BackendTimeout as exc:
            assert "went silent" in str(exc), str(exc)
            # The advice must be about silence, not about the task being big.
            assert "SILENCE, not on total time" in str(exc), str(exc)

    assert handle.interrupted, "a wedged turn must be interrupted"
    print("PASS  a turn that stops sending events is abandoned, and says why")


def test_the_absolute_cap_catches_a_runaway():
    """Idle detection alone would let a keepalive every few seconds run for
    ever, so there is a backstop -- and its advice is the right advice for the
    case it actually catches: split the node."""
    from unittest.mock import MagicMock, patch

    from agent.backends.base import BackendTimeout
    from agent.backends.codex import CodexBackend

    stream = _Stream([_quiet_event() for _ in range(200)], gap=0.01)
    handle = _Handle(stream)
    # Never idle, but capped almost immediately.
    backend = CodexBackend(MagicMock(), timeout=30, max_seconds=0.1)

    with patch("agent.backends.codex._collect", _drain):
        try:
            backend._run_turn(_Thread(handle), "go", {})
            raise AssertionError("expected BackendTimeout")
        except BackendTimeout as exc:
            assert "absolute cap" in str(exc), str(exc)
            assert "Split it across more nodes" in str(exc), str(exc)

    assert handle.interrupted
    print("PASS  a runaway hits the cap, and is told to split the node")


def test_stop_interrupts_a_turn_that_is_still_streaming():
    """The whole point of reaching the provider rather than waiting for it.

    A run can only be stopped between nodes unless something can interrupt the
    turn in flight -- and with a Codex turn measured in minutes, "stop after
    this node" during a long executor step looks exactly like a broken button.

    Note this turn is NOT idle and NOT over its cap: a chatty, healthy,
    long-running turn is precisely the case that has to be interruptible.
    """
    import threading

    from unittest.mock import MagicMock, patch

    from agent.backends.base import BackendCancelled
    from agent.backends.codex import CodexBackend

    stream = _Stream([_quiet_event() for _ in range(200)], gap=0.02)
    handle = _Handle(stream)
    backend = CodexBackend(MagicMock(), timeout=30, max_seconds=30)

    cancel = threading.Event()
    backend.cancel = cancel
    threading.Timer(0.15, cancel.set).start()

    with patch("agent.backends.codex._collect", _drain):
        try:
            backend._run_turn(_Thread(handle), "go", {})
            raise AssertionError("expected BackendCancelled")
        except BackendCancelled as exc:
            assert "Stopped by request" in str(exc), str(exc)
            # It must say what survives, or the user cannot tell what it cost.
            assert "checkpointed" in str(exc), str(exc)

    assert handle.interrupted, "the provider's turn must actually be interrupted"
    print("PASS  stop interrupts a healthy, streaming turn rather than waiting it out")


def test_cancellation_is_checked_before_the_timeout():
    """If you have asked to stop, no other verdict about the turn matters.

    Reporting a timeout to somebody who pressed Stop is confusing -- it reads
    as a failure when it was a decision.
    """
    import threading

    from unittest.mock import MagicMock, patch

    from agent.backends.base import BackendCancelled
    from agent.backends.codex import CodexBackend

    # Silent AND cancelled: both conditions true at once.
    stream = _Stream([_quiet_event(), _quiet_event()], gap=5.0)
    handle = _Handle(stream)
    backend = CodexBackend(MagicMock(), timeout=0.2, max_seconds=30)
    backend.cancel = threading.Event()
    backend.cancel.set()

    with patch("agent.backends.codex._collect", _drain):
        try:
            backend._run_turn(_Thread(handle), "go", {})
            raise AssertionError("expected BackendCancelled")
        except BackendCancelled:
            pass
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(f"got {type(exc).__name__}, wanted cancellation") from None
    print("PASS  a cancelled turn reports cancellation, not a timeout")


def test_a_backend_with_no_cancel_set_is_unaffected():
    """`cancel` defaults to None, and the check must not fire on that."""
    from unittest.mock import MagicMock, patch

    from agent.backends.codex import CodexBackend

    stream = _Stream([_quiet_event() for _ in range(4)], gap=0.02)
    handle = _Handle(stream)
    backend = CodexBackend(MagicMock(), timeout=5, max_seconds=30)
    assert backend.cancel is None

    with patch("agent.backends.codex._collect", _drain):
        result, _ = backend._run_turn(_Thread(handle), "go", {})
    assert result == "collected"
    assert not handle.interrupted
    print("PASS  an un-armed backend runs normally")

if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\nAll backend tests passed.")
