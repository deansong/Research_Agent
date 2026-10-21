"""
WHAT:  The I/O and timeout edges of every backend, driven for real.
WHY:   Each test here is a way a turn used to hang, crash or leak -- found by
       reading the supervising loops against what a subprocess or an RPC can
       actually do, not by waiting for a run to hit them.
CONCEPT: Same rule as test_cli_backend.py: a fake CLI spawned with
       sys.executable, real pipes, real process groups, no mocked Popen.

Run with:   python -m pytest tests/test_backend_io.py -q
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import threading
import time

import pytest
from pydantic import BaseModel

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from agent.backends._cli import MAX_ARG_BYTES, CliBackend  # noqa: E402
from agent.backends.base import (Access, BackendBusy, BackendCancelled,  # noqa: E402
                                 BackendError, BackendOutputError,
                                 BackendTimeout)


class Answer(BaseModel):
    answer: str


class Fake(CliBackend):
    """A CLI whose behaviour is whatever script the test passes in."""

    name = "fake_io"
    label = "FakeIO"
    executable_default = sys.executable

    def __init__(self, body: str, *, via_stdin: bool = False, transient: str = "",
                 extra_arg: str = "", **options):
        self._body = body
        self._transient = transient
        self._extra = extra_arg
        self.prompt_via_stdin = via_stdin
        super().__init__(executable=sys.executable, **options)

    def argv(self, *, prompt, instructions, access, schema_path, session):
        argv = [sys.executable, "-u", "-c", self._body]
        return argv + ([self._extra] if self._extra else [])

    def session_args(self, thread_id):
        return ([], thread_id or "minted")

    def is_envelope(self, event):
        return event.get("event") == "result"

    def record_for(self, event):
        if event.get("event") == "result":
            return None
        return {"kind": "command", "phase": "completed",
                "command": event.get("command", "x"), "output": event.get("out", "")}

    def final_text(self, envelope):
        return envelope.get("out", "")

    def transient_error(self, envelope):
        if self._transient and envelope.get("error") == self._transient:
            return self._transient
        return ""


def _run(backend, prompt="go"):
    return backend.run_structured(
        thread_id=None, repo_path="/tmp", access=Access.READ_ONLY,
        developer_instructions="", prompt=prompt, output_model=Answer)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers kill(0); it is dead for our purposes.
    try:
        with open(f"/proc/{pid}/stat") as handle:
            return handle.read().split()[2] != "Z"
    except OSError:
        return False


# ---------------------------------------------------------------------------
# the CLI transport
# ---------------------------------------------------------------------------

def test_the_prompt_can_travel_on_stdin():
    """claude's prompt goes on stdin, so no argv entry carries it. Bigger than
    a pipe buffer on purpose: a naive write-then-read deadlocks there."""
    body = ('import json,sys\n'
            'text=sys.stdin.read()\n'
            'print(json.dumps({"event":"result","out":json.dumps('
            '{"answer":str(len(text))})}))\n')
    prompt = "x" * 300_000
    run = _run(Fake(body, via_stdin=True, timeout=30, max_seconds=60), prompt)
    assert run.data.answer == str(len(prompt))


def test_a_child_that_never_reads_stdin_cannot_hang_the_turn():
    """The writer is a thread: a CLI that exits without reading its prompt
    must not leave us blocked on a full pipe outside every clock."""
    body = ('import json\n'
            'print(json.dumps({"event":"result","out":json.dumps({"answer":"early"})}))\n')
    started = time.monotonic()
    run = _run(Fake(body, via_stdin=True, timeout=30, max_seconds=60), "y" * 500_000)
    assert run.data.answer == "early"
    assert time.monotonic() - started < 15


def test_a_background_grandchild_holding_stdout_does_not_stall_a_finished_turn():
    """`nohup python train.py &` inherits stdout. EOF then waits on the
    training run, and a finished turn used to sit until the idle timeout and
    be reported as silent."""
    body = ('import json,subprocess,sys\n'
            'subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"])\n'
            'print(json.dumps({"event":"result","out":json.dumps({"answer":"done"})}))\n')
    started = time.monotonic()
    run = _run(Fake(body, timeout=40, max_seconds=120))
    assert run.data.answer == "done"
    assert time.monotonic() - started < 20, "returned on the envelope, not on EOF"


def test_abandoning_a_turn_kills_the_commands_it_started(tmp_path):
    """terminate() on the CLI alone left its tool commands running -- on a
    research box, a training run holding the GPU with nobody to stop it."""
    pidfile = tmp_path / "grandchild.pid"
    body = ('import subprocess,sys,time\n'
            'p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(120)"],'
            'stdout=subprocess.DEVNULL)\n'
            f'open({str(pidfile)!r},"w").write(str(p.pid))\n'
            'time.sleep(120)\n')
    with pytest.raises(BackendTimeout):
        _run(Fake(body, timeout=1.0, max_seconds=60))
    pid = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(pid), "the grandchild outlived the abandoned turn"


def test_stop_also_reaches_the_grandchildren(tmp_path):
    pidfile = tmp_path / "grandchild.pid"
    body = ('import json,subprocess,sys,time\n'
            'p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(120)"],'
            'stdout=subprocess.DEVNULL)\n'
            f'open({str(pidfile)!r},"w").write(str(p.pid))\n'
            'while True:\n'
            '    print(json.dumps({"event":"step"})); time.sleep(0.1)\n')
    backend = Fake(body, timeout=30, max_seconds=60)
    backend.cancel = threading.Event()
    threading.Timer(1.0, backend.cancel.set).start()
    with pytest.raises(BackendCancelled):
        _run(backend)
    pid = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(pid)


def test_bytes_that_are_not_utf8_do_not_kill_the_reader():
    """Under LANG=C the pipe decoded as ASCII, and one byte of a model name or
    a file path in command output ended the turn with UnicodeDecodeError."""
    body = ('import json,sys\n'
            'sys.stdout.buffer.write(b"\\xff\\xfe not utf-8 \\xe9\\n")\n'
            'sys.stdout.buffer.write(json.dumps({"event":"step","out":"caf\\u00e9"}).encode()+b"\\n")\n'
            'sys.stderr.buffer.write(b"\\xff" * 1000)\n'
            'sys.stdout.buffer.write(json.dumps({"event":"result",'
            '"out":json.dumps({"answer":"ok"})}).encode()+b"\\n")\n'
            'sys.stdout.flush()\n')
    run = _run(Fake(body, timeout=30, max_seconds=60))
    assert run.data.answer == "ok"
    assert any(e.get("kind") == "cli_stdout" for e in run.events)


def test_a_json_line_that_is_not_an_object_is_kept_not_fatal():
    body = ('import json\n'
            'print("[1,2,3]"); print("42")\n'
            'print(json.dumps({"event":"result","out":json.dumps({"answer":"ok"})}))\n')
    assert _run(Fake(body, timeout=30, max_seconds=60)).data.answer == "ok"


def test_a_busy_provider_is_retried_then_succeeds(tmp_path):
    counter = tmp_path / "n"
    body = ('import json,pathlib\n'
            f'p=pathlib.Path({str(counter)!r})\n'
            'n=int(p.read_text()) if p.exists() else 0\n'
            'p.write_text(str(n+1))\n'
            'if n<2: print(json.dumps({"event":"result","error":"503 no capacity","out":""}))\n'
            'else: print(json.dumps({"event":"result","out":json.dumps({"answer":"third"})}))\n')
    backend = Fake(body, transient="503 no capacity", timeout=30, max_seconds=60)
    import agent.backends._cli as cli
    real = cli.wait_or_cancel
    cli.wait_or_cancel = lambda seconds, cancel=None, pause=None: None
    try:
        run = _run(backend)
    finally:
        cli.wait_or_cancel = real
    assert run.data.answer == "third"
    assert counter.read_text() == "3"


def test_a_busy_provider_gives_up_with_its_own_words():
    body = ('import json\n'
            'print(json.dumps({"event":"result","error":"overloaded","out":""}))\n')
    backend = Fake(body, transient="overloaded", busy_attempts=1,
                   timeout=30, max_seconds=60)
    import agent.backends._cli as cli
    real = cli.wait_or_cancel
    cli.wait_or_cancel = lambda seconds, cancel=None, pause=None: None
    try:
        with pytest.raises(BackendBusy) as caught:
            _run(backend)
    finally:
        cli.wait_or_cancel = real
    assert "overloaded" in str(caught.value)


def test_stop_during_a_busy_backoff_is_immediate():
    body = ('import json\n'
            'print(json.dumps({"event":"result","error":"overloaded","out":""}))\n')
    backend = Fake(body, transient="overloaded", busy_attempts=5,
                   timeout=30, max_seconds=60)
    backend.cancel = threading.Event()
    threading.Timer(1.0, backend.cancel.set).start()
    started = time.monotonic()
    with pytest.raises(BackendCancelled):
        _run(backend)
    assert time.monotonic() - started < 8, "the 5s+10s backoff ignored Stop"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="MAX_ARG_STRLEN is Linux")
def test_an_argument_the_kernel_would_refuse_is_named_before_spawning():
    backend = Fake("print(1)", extra_arg="z" * (MAX_ARG_BYTES + 10),
                   timeout=30, max_seconds=60)
    with pytest.raises(BackendError) as caught:
        _run(backend)
    assert "Linux refuses" in str(caught.value)
    assert "Shorten" in str(caught.value)


# ---------------------------------------------------------------------------
# the provider vocabularies
# ---------------------------------------------------------------------------

def test_claude_sends_its_prompt_on_stdin_and_not_in_argv():
    from agent.backends.claude_code import ClaudeCodeBackend

    backend = ClaudeCodeBackend.__new__(ClaudeCodeBackend)
    backend.executable, backend.model = "claude", None
    backend.max_turns = backend.max_budget_usd = None
    assert ClaudeCodeBackend.prompt_via_stdin is True
    argv = backend.argv(prompt="THE-PROMPT", instructions="",
                        access=Access.READ_ONLY, schema_path="/dev/null", session=[])
    assert "THE-PROMPT" not in argv
    assert argv[:2] == ["claude", "-p"]


def test_claude_and_agy_tell_busy_from_broken():
    from agent.backends.antigravity import AntigravityBackend
    from agent.backends.claude_code import ClaudeCodeBackend

    claude = ClaudeCodeBackend.__new__(ClaudeCodeBackend)
    assert claude.transient_error({"is_error": True, "result": "API Error: 529 Overloaded"})
    assert not claude.transient_error({"is_error": True, "result": "Authentication error"})
    assert not claude.transient_error({"is_error": False, "result": "overloaded"})

    agy = AntigravityBackend.__new__(AntigravityBackend)
    measured = {"result": {"status": "ERROR", "error": (
        "API error (attempt 1): UNAVAILABLE (code 503): No capacity available "
        "for model gemini-3.8-flash-medium on the server")}}
    assert agy.transient_error(measured)
    assert not agy.transient_error({"result": {"status": "SUCCESS", "error": ""}})
    assert not agy.transient_error({"result": {"status": "ERROR", "error": "bad schema"}})


# ---------------------------------------------------------------------------
# codex
# ---------------------------------------------------------------------------

def test_classify_leaves_our_own_errors_alone():
    """A timeout message quotes the last thing the turn printed. A log line
    with "429" or "billing" in it turned a timeout into busy retries, or into
    twenty minutes of waiting for credits."""
    from agent.backends.codex import classify

    timeout = BackendTimeout("went silent. last: HTTP 429 from the dataset mirror, billing")
    assert classify(timeout) is timeout
    interrupt = KeyboardInterrupt()
    assert classify(interrupt) is interrupt


def test_a_codex_reply_off_schema_is_an_output_error(monkeypatch):
    """planner.py and designer.py repair on BackendOutputError only; a raw
    ValidationError crashed the session instead."""
    from unittest.mock import MagicMock

    from agent.backends.codex import CodexBackend

    backend = CodexBackend(MagicMock())
    thread = MagicMock()
    thread.id = "t-1"
    monkeypatch.setattr(backend, "_get_thread", lambda **kw: (thread, True))

    result = MagicMock()
    result.final_response = '{"wrong": 1}'
    monkeypatch.setattr(backend, "_run_with_recovery", lambda *a: (result, []))
    with pytest.raises(BackendOutputError) as caught:
        backend.run_structured(thread_id=None, repo_path="/tmp", access=Access.READ_ONLY,
                               developer_instructions="", prompt="p", output_model=Answer)
    assert '{"wrong": 1}' in str(caught.value)

    result.final_response = ""
    with pytest.raises(BackendOutputError):
        backend.run_structured(thread_id=None, repo_path="/tmp", access=Access.READ_ONLY,
                               developer_instructions="", prompt="p", output_model=Answer)


def test_stop_during_a_credit_wait_is_immediate():
    from unittest.mock import MagicMock

    from agent.backends.codex import CodexBackend

    backend = CodexBackend(MagicMock(), credit_wait_seconds=60, credit_wait_attempts=5)
    backend.cancel = threading.Event()
    backend._run_turn = lambda *a: (_ for _ in ()).throw(RuntimeError("out of credits"))
    threading.Timer(0.5, backend.cancel.set).start()
    started = time.monotonic()
    with pytest.raises(BackendCancelled):
        backend._run_with_recovery(None, "p", {})
    assert time.monotonic() - started < 5


def test_a_turn_rpc_that_never_returns_hits_the_idle_timeout():
    """turn() ran before the watchdog started, so a wedged app-server hung the
    node with no timeout and a Stop button that did nothing."""
    from unittest.mock import MagicMock

    from agent.backends.codex import CodexBackend

    class Wedged:
        def turn(self, prompt, output_schema=None):
            time.sleep(60)

    backend = CodexBackend(MagicMock(), timeout=0.5)
    started = time.monotonic()
    with pytest.raises(BackendTimeout):
        backend._run_turn(Wedged(), "go", {})
    assert time.monotonic() - started < 10


def test_a_thread_start_that_never_returns_is_bounded_and_stoppable():
    from unittest.mock import MagicMock

    from agent.backends.codex import CodexBackend

    client = MagicMock()
    client.thread_start.side_effect = lambda **kw: time.sleep(60)
    backend = CodexBackend(client, timeout=0.5)
    with pytest.raises(BackendTimeout) as caught:
        backend._get_thread(thread_id=None, repo_path="/tmp", access=Access.READ_ONLY,
                            developer_instructions="")
    assert "starting a Codex thread" in str(caught.value)

    backend = CodexBackend(client, timeout=60)
    backend.cancel = threading.Event()
    threading.Timer(0.3, backend.cancel.set).start()
    with pytest.raises(BackendCancelled):
        backend._get_thread(thread_id=None, repo_path="/tmp", access=Access.READ_ONLY,
                            developer_instructions="")


def test_an_interrupt_that_fails_does_not_hide_the_timeout():
    from unittest.mock import MagicMock

    from agent.backends.codex import CodexBackend

    class Stream:
        def __iter__(self):
            time.sleep(60)
            yield from ()

        def close(self):
            pass

    class Handle:
        id = "turn-x"

        def stream(self):
            return Stream()

        def interrupt(self):
            raise ConnectionError("app-server went away")

    class Thread_:
        def turn(self, prompt, output_schema=None):
            return Handle()

    backend = CodexBackend(MagicMock(), timeout=0.5)
    import agent.backends.codex as codex

    real = codex.CodexBackend._abandon

    def quick(self, handle, worker):
        # Keep the 30s worker join out of the test; the interrupt is the point.
        worker_join = worker.join
        worker.join = lambda timeout=None: worker_join(0.1)
        return real(self, handle, worker)

    codex.CodexBackend._abandon = quick
    try:
        with pytest.raises(BackendTimeout):
            backend._run_turn(Thread_(), "go", {})
    finally:
        codex.CodexBackend._abandon = real


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
