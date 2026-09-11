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

from agent.backends import PROVIDERS, STUB_PROVIDERS, _load
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


def _resolved(**cli_kwargs):
    """Models per role, with the given CLI flags and no environment."""
    import pathlib

    from agent.config import backend_for, load_config

    class Args:
        pass

    args = Args()
    for key, value in cli_kwargs.items():
        setattr(args, key, value)

    cfg = load_config(repo_path=pathlib.Path("/nonexistent-repo"),
                      cli=args, env={})
    return cfg, {role: backend_for(cfg, role).model
                 for role in ("designer", "coder", "runner", "checker")}


def test_the_default_model_is_pinned_and_split_by_role():
    """It used to be None -- "let the Codex CLI decide".

    Which meant the model that writes a 40,000-character design document was
    whatever ~/.codex/config.toml on that machine happened to say, invisible
    to --explain and different per checkout. Now it is a decision in the
    repository: the flagship tier by default, the mini tier for the two roles
    that run experiments and check them.
    """
    from agent.config import DEFAULT_MODEL, DEFAULT_ROLE_BACKENDS

    _, models = _resolved()
    assert models["designer"] == DEFAULT_MODEL == "gpt-5.6-sol", models
    assert models["coder"] == "gpt-5.6-sol", "code and reports get the default"
    assert models["runner"] == models["checker"] == "gpt-5.6-terra", models
    assert set(DEFAULT_ROLE_BACKENDS) == {"runner", "checker"}, DEFAULT_ROLE_BACKENDS
    print(f"PASS  default {DEFAULT_MODEL}, runs on "
          f"{DEFAULT_ROLE_BACKENDS['runner'][1]}")


def test_a_global_model_override_reaches_every_role():
    """The trap in seeding per-role defaults.

    --model and AGENT_MODEL only ever set `default.model`, so a built-in role
    model would silently outrank them: `--model gpt-5.6-pro` leaving two
    roles on the mini tier is the kind of half-applied setting you discover
    from a bill.
    """
    _, models = _resolved(model="gpt-5.6-pro")
    assert set(models.values()) == {"gpt-5.6-pro"}, models

    # But a model named FOR that role survives, because somebody asked.
    _, models = _resolved(model="gpt-5.6-pro",
                          backend_role=["runner=codex:gpt-5.6-luna"])
    assert models["runner"] == "gpt-5.6-luna", models
    assert models["checker"] == "gpt-5.6-pro", "only the named role is spared"
    print("PASS  --model reaches every role; a named role override wins")


def test_backend_fake_still_reaches_the_seeded_roles():
    """Why the seeded entries carry provider="".

    The whole test suite and every `--backend fake` run depend on one flag
    redirecting every role. A seeded provider="codex" on runner and checker
    would have quietly kept two roles on the real API -- which, in a suite
    that runs offline, would fail as a login error rather than as anything
    resembling this cause.
    """
    cfg, models = _resolved(backend="fake")
    from agent.config import backend_for

    for role in ("designer", "runner", "checker"):
        assert backend_for(cfg, role).provider == "fake", (role, cfg)
    print("PASS  --backend fake redirects the seeded roles too")


def test_the_default_models_are_real_model_ids():
    """A typo here is only discovered by a failed live turn.

    The names come from the tier table inside the codex binary, so they can
    be checked against it -- the same source that says sol is the
    flagship-equivalent tier and terra the mini-like one. Skipped when the
    binary is not installed rather than guessed at.
    """
    import pathlib

    from agent.config import DEFAULT_MODEL, DEFAULT_ROLE_BACKENDS

    try:
        import codex_cli_bin
    except ImportError:  # pragma: no cover - optional
        pytest.skip("codex_cli_bin not installed; cannot verify model ids")

    root = pathlib.Path(codex_cli_bin.__file__).parent / "bin"
    binaries = [p for p in root.glob("codex*") if p.is_file()]
    if not binaries:  # pragma: no cover
        pytest.skip(f"no codex binary under {root}")

    blob = max(binaries, key=lambda p: p.stat().st_size).read_bytes()
    for model in {DEFAULT_MODEL, *(m for _, m in DEFAULT_ROLE_BACKENDS.values())}:
        assert model.encode() in blob, (
            f"{model!r} does not appear in the installed codex binary. "
            f"A model id it does not know fails every turn."
        )
    print(f"PASS  {DEFAULT_MODEL} and "
          f"{sorted({m for _, m in DEFAULT_ROLE_BACKENDS.values()})} "
          f"are ids the installed codex knows")


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

    for provider in sorted(STUB_PROVIDERS):
        try:
            _instantiate(BackendConfig(provider=provider, options={"anything": 1}),
                         role="r", codex_client=None)
        except BackendUnavailable:
            pass   # refusing is correct; crashing on an unexpected kwarg is not

    print("PASS  configured options reach the backend (timeout included)")


def test_an_unconfigured_role_says_it_is_using_the_default(capsys):
    """The silence that would make the cheap-model option a lie.

    A designed agent invents its own role names, and backend_for() falls back
    to the default for any it does not recognise. The research skeleton puts
    experiment runs on a role called "runner" precisely so they can be pointed
    at a smaller model -- so if nobody says the role is unconfigured, the
    human believes they are saving money while every turn goes to the default.
    """
    from agent.backends import build_backends
    from agent.backends.base import Access
    from agent.config import AgentConfig, BackendConfig

    cfg = AgentConfig(default=BackendConfig(provider="fake", model="m-1"),
                      roles={"checker": BackendConfig(provider="fake", model="m-2")})
    build_backends(cfg, {"runner": Access.WRITE, "checker": Access.READ_ONLY,
                         "executor": Access.WRITE})

    printed = capsys.readouterr().out
    assert "runner" in printed and "m-1" in printed, printed
    # Configured roles are not mentioned, and neither are the built-ins --
    # a line that fires five times a run is one everybody learns to skip.
    assert "checker" not in printed, printed
    assert "executor" not in printed, printed
    print("PASS  an unconfigured role says which model it fell back to")


def test_stub_backends_fail_loudly_at_construction():
    """A stub must refuse at startup, not halfway through a task."""
    from agent.backends.base import BackendUnavailable

    for provider in sorted(STUB_PROVIDERS):
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
    """An event that counts as activity but deliberately prints nothing.

    This is the crux of the idle-timeout tests: a healthy turn emits plenty of
    events that are not worth a line, and the old code only reset its clock on
    PRINTABLE ones -- so thinking hard looked exactly like hanging.

    `token_count` is one of the few kinds _progress deliberately keeps quiet,
    because it arrives constantly and says nothing. It used to be enough to
    hand describe() an unrecognised object, but unrecognised events are now
    reported by name rather than swallowed -- which was the point of that
    change, and means this fixture has to name a genuinely quiet kind.
    """
    from agent.backends._progress import describe

    class Payload:
        pass

    class Event:
        payload = type("TokenCountNotification", (Payload,), {})()

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
            # The no-output branch: nothing arrived, so silence is the whole
            # story. The other branch is test_a_stall_after_a_long_answer...
            assert "Nothing arrived at all" in str(exc), str(exc)
            assert "SILENCE" in str(exc), str(exc)

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


def _delta_event(text: str = "tok"):
    """One token of the answer being typed, carrying its text.

    `delta: str` is a real field on AgentMessageDeltaNotification -- checked
    against the installed SDK -- so the fixture has to have it or the tests
    would be describing an event shape that does not exist.
    """
    class Payload:
        pass

    cls = type("AgentMessageDeltaNotification", (Payload,), {"delta": text})

    class Event:
        payload = cls()

    return Event()


def test_streamed_tokens_are_counted_but_not_kept():
    """The measured bug: 31,001 "events", 30,900 of them typing.

    Every one of those was recorded as an event, which made the number read as
    furious activity, made "last one 0s ago" true for thirty-five minutes so
    the idle clock could never fire, and made the in-flight record a
    multi-megabyte file rewritten every five seconds. They carry no text --
    only their own name -- so keeping them bought nothing at all.
    """
    from unittest.mock import MagicMock, patch

    from agent.backends._progress import record
    from agent.backends.codex import CodexBackend

    kept = record(_delta_event())
    assert kept["kind"] == "agent_message_delta" and kept["transient"] is True
    # The TEXT rides out even though the event does not: a delta carries
    # `delta: str`, and dropping it is how a turn that wrote 16,608 tokens
    # reported "last: * user message".
    assert kept["stream"] == "message" and kept["text"] == "tok", kept

    stream = _Stream([_delta_event() for _ in range(40)]
                     + [_quiet_event() for _ in range(2)], gap=0.001)
    handle = _Handle(stream)
    backend = CodexBackend(MagicMock(), timeout=30, max_seconds=30)

    with patch("agent.backends.codex._collect", _drain):
        _, recorded = backend._run_turn(_Thread(handle), "go", {})

    # Forty tokens typed and two things done: two records.
    assert len(recorded) == 2, [e.get("kind") for e in recorded]
    assert not any(e.get("kind") == "agent_message_delta" for e in recorded)
    print("PASS  streamed tokens are tallied separately, not recorded as work")


def test_the_progress_callback_and_its_only_caller_agree():
    """A six-argument callback and a five-argument sink is a TypeError inside a
    `try: ... except Exception: pass` -- so the detail behind a long turn would
    simply stop appearing, with nothing said anywhere. Checked by calling it.
    """
    import tempfile

    from agent import activity

    class Backend:
        on_progress = None

    with tempfile.TemporaryDirectory() as tmp:
        backend = Backend()
        activity.arm_progress(backend, tmp, "worker")
        # ONE dict, not positional arguments: they had grown twice and were
        # about to a third time, and each growth broke this contract silently.
        backend.on_progress({
            "events": [{"kind": "command", "phase": "completed"}],
            "elapsed": 310.0, "idle": 0.0, "counts": {"command": 1},
            "last": "message: {\"task_brief\": \"Compare hotel",
            "streamed": 14885,
            "live": {"message": '{"task_brief": "Compare hotel'},
        })

        turn = activity.in_flight(tmp)
        assert turn is not None and turn.node == "worker", turn
        import json
        raw = json.loads((activity.directory(tmp, "worker")
                          / activity.IN_FLIGHT).read_text())
        assert raw["streamed"] == 14885, raw
        assert raw["live"]["message"].startswith('{"task_brief"'), raw["live"]
        # And it survives the read back, which is what the browser gets.
        assert turn.progress["live"]["message"], turn.progress
    print("PASS  the progress callback matches the sink that receives it")


def test_the_live_tail_shows_what_is_being_written():
    """The answer to "why can I not see what it is saying".

    The text was arriving the whole time -- AgentMessageDeltaNotification
    carries `delta: str`, and so do the reasoning, plan and command-output
    streams -- and record() was reducing each one to its own name. So a turn
    could write a complete design and report "last: * user message".
    """
    from agent.backends._progress import LIVE_TAIL, _LiveText

    live = _LiveText()
    for chunk in ('{"task_brief": "Compare hotel-employee ', 'skill associations',
                  ' across model families"'):
        live.add("message", chunk)
    live.add("reasoning", "mapping the plan onto stages")

    tail = live.tail()
    assert tail.startswith("message: "), tail
    assert "Compare hotel-employee skill associations" in tail, tail
    # message beats reasoning: by the time it is answering, what it was
    # thinking is the less useful of the two.
    assert "mapping the plan" not in tail, tail

    # A tail, not a transcript. A design turn writes tens of thousands of
    # tokens and the question is what it is doing NOW.
    live.add("message", "x" * 50_000)
    assert len(live.streams["message"]) == LIVE_TAIL
    assert live.tail().endswith("x"), live.tail()[:80]
    print("PASS  the live tail carries the text, bounded, message first")


def test_a_stall_after_a_long_answer_is_not_reported_as_never_starting():
    """Two failures that look identical through a counter, and are not.

    The measured run: 863 seconds, 5 events, and the message concluded "a
    request that stopped responding rather than one that was slow". It had
    streamed 16,608 tokens of a finished design. Both halves of that message
    were true and together they pointed at the wrong remedy -- raising the
    timeout, when there was nothing left to wait for.
    """
    from unittest.mock import MagicMock

    from agent.backends._progress import _LiveText
    from agent.backends.codex import CodexBackend

    backend = CodexBackend(MagicMock())

    live = _LiveText()
    live.add("message", '{"task_brief": "Compare hotel-employee skill assoc')
    stalled = backend._silent_message(300, 863, 5, 16608, live)
    assert "16,608 tokens" in stalled, stalled
    assert "stall after the work" in stalled, stalled
    assert "Raising the timeout will not help" in stalled, stalled
    assert "task_brief" in stalled, "show what it had already written"

    never = backend._silent_message(300, 340, 2, 0, _LiveText())
    assert "Nothing arrived at all" in never, never
    assert "tokens" not in never.split("config.json")[0], \
        "do not claim tokens were written when none were"
    print("PASS  a stall after writing is told apart from one before")


def test_the_heartbeat_reports_typing_as_typing():
    """31,000 tokens of output is the honest answer to "why so slow", and it
    used to be reported as 31,000 events -- indistinguishable from work."""
    from agent.backends.codex import _heartbeat

    line = _heartbeat(2097, 0, 16, {"command": 12, "reasoning": 4},
                      "auditing the repository", 31001)
    assert "12 command" in line and "writing 31.0k" in line, line
    assert "31001 events" not in line, line
    print("PASS  the heartbeat separates what it did from what it is typing")


def test_a_turn_that_only_types_is_stopped():
    """The failure neither clock above can see.

    A model that emits a whole document, decides it was a draft, and emits
    another is never idle and is nowhere near the two-hour cap -- so it can
    burn an hour producing nothing. The cap is on ANSWER, and the advice is
    the advice that fixes it.
    """
    from unittest.mock import MagicMock, patch

    from agent.backends.base import BackendTimeout
    from agent.backends.codex import CodexBackend

    # A short `timeout` only to shorten the poll interval -- _poll_seconds
    # derives it from the time limits, and with the defaults the whole fixture
    # stream drains before the loop looks even once. It cannot fire: the gap
    # between events is a thousandth of the idle limit.
    stream = _Stream([_delta_event() for _ in range(500)], gap=0.002)
    handle = _Handle(stream)
    backend = CodexBackend(MagicMock(), timeout=0.4, max_seconds=30,
                           max_output_tokens=50)

    with patch("agent.backends.codex._collect", _drain):
        try:
            backend._run_turn(_Thread(handle), "go", {})
            raise AssertionError("expected BackendTimeout")
        except BackendTimeout as exc:
            assert "tokens of answer" in str(exc), str(exc)
            assert "not slowness" in str(exc).lower(), str(exc)
            assert "Split the biggest steps" in str(exc), str(exc)

    assert handle.interrupted
    print("PASS  a turn that only types hits the output cap and says why")


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
