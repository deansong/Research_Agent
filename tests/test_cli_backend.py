"""
WHAT:  The shared CLI adapter, driven by a real subprocess.
WHY:   `claude` and `agy` are the same machine, and this is the machine. It is
       tested before either provider is named, so the provider modules only
       have to get their vocabulary right.
CONCEPT: A fake CLI, spawned with sys.executable, not a mocked Popen.

WHY A FAKE EXECUTABLE RATHER THAN A MOCK
----------------------------------------
Real pipes, real buffering, real termination, for the same cost. This suite
learned the lesson once already: the first version of the codex loop tests
patched _collect with a plain return value, so the stream was never iterated
and three tests "passed" without exercising the loop at all (see `_drain` in
test_backends.py). A mocked Popen cannot deadlock on a full stderr pipe, which
is one of the things these tests exist to prove does not happen.

Run with:   python -m pytest tests/test_cli_backend.py -q
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import threading

import pytest
from pydantic import BaseModel

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from agent.backends._cli import CliBackend  # noqa: E402
from agent.backends.base import (Access, BackendCancelled,  # noqa: E402
                                 BackendOutputError, BackendTimeout, Usage)


class Answer(BaseModel):
    answer: str
    note: str = ""


def _script(body: str) -> list[str]:
    """A fake CLI: prints NDJSON, sleeps, whatever the test needs."""
    return [sys.executable, "-u", "-c", body]


class Fake(CliBackend):
    """A provider whose whole vocabulary is 'whatever the test passed in'."""

    name = "fake_cli"
    label = "FakeCLI"
    executable_default = sys.executable

    def __init__(self, body: str = "", **options):
        self._body = body
        super().__init__(executable=sys.executable, **options)

    def argv(self, *, prompt, instructions, access, schema_path, session):
        return _script(self._body)

    def session_args(self, thread_id):
        return ([], thread_id or "minted-1")

    def is_envelope(self, event):
        return event.get("event") == "result"

    def record_for(self, event):
        if event.get("event") == "delta":
            return {"kind": "message_delta", "transient": True,
                    "stream": "message", "text": event.get("text", "")}
        if event.get("event") == "result":
            return None
        return {"kind": event.get("kind", "command"), "phase": "completed",
                "command": event.get("command", "x")}

    def final_text(self, envelope):
        return envelope.get("out", "")

    def usage_from(self, envelope):
        u = envelope.get("usage") or {}
        return Usage(input_tokens=u.get("in", 0), output_tokens=u.get("out", 0))

    def session_id_from(self, envelope):
        return envelope.get("id")


def _run(backend, **kw):
    return backend.run_structured(
        thread_id=kw.pop("thread_id", None), repo_path=kw.pop("repo_path", "/tmp"),
        access=kw.pop("access", Access.READ_ONLY), developer_instructions="be brief",
        prompt="go", output_model=Answer)


_OK = (
    'import json,sys\n'
    'print(json.dumps({"event":"step","kind":"command","command":"ls"}))\n'
    'print(json.dumps({"event":"result","id":"conv-9",'
    '"out":json.dumps({"answer":"ok","note":"n"}),"usage":{"in":5,"out":2}}))\n'
)


def test_a_whole_turn_through_a_real_subprocess():
    """The happy path, end to end: spawn, stream, parse, validate."""
    run = _run(Fake(_OK))
    assert run.data.answer == "ok" and run.data.note == "n"
    assert run.thread_id == "minted-1", "a minted id wins over the envelope's"
    assert run.is_new_thread is True
    assert run.usage.input_tokens == 5 and run.usage.output_tokens == 2
    assert [e["kind"] for e in run.events] == ["command"], run.events
    print("PASS  a turn runs through a real child process and validates")


def test_streamed_text_is_tallied_not_recorded():
    """The distinction _progress.py argues for, now for a second transport."""
    body = (
        'import json\n'
        'for i in range(40): print(json.dumps({"event":"delta","text":"x"}))\n'
        'print(json.dumps({"event":"result","id":"c","out":json.dumps({"answer":"a"})}))\n'
    )
    run = _run(Fake(body))
    assert run.data.answer == "a"
    assert not any(e["kind"] == "message_delta" for e in run.events), run.events
    print("PASS  deltas are counted, not kept, on the CLI transport too")


def test_stderr_is_drained_so_a_chatty_child_cannot_deadlock():
    """The hazard codex.py does not have, so nobody porting would think of it.

    A child that writes more than a pipe buffer (~64 KB) to stderr blocks on
    the write, stops producing stdout, and presents as a hung model. One
    megabyte here; without the second reader thread this test hangs until the
    idle timeout rather than failing.
    """
    body = (
        'import json,sys\n'
        'sys.stderr.write("noise\\n" * 200000)\n'
        'print(json.dumps({"event":"result","id":"c","out":json.dumps({"answer":"survived"})}))\n'
    )
    backend = Fake(body, timeout=25, max_seconds=60)
    assert _run(backend).data.answer == "survived"
    print("PASS  1 MB of stderr does not wedge the turn")


def test_junk_on_stdout_is_recorded_rather_than_fatal():
    """Banners and update notices are normal on a CLI's stdout."""
    body = (
        'import json\n'
        'print("Update available! Run `x upgrade`.")\n'
        'print("{ truncated json")\n'
        'print(json.dumps({"event":"result","id":"c","out":json.dumps({"answer":"fine"})}))\n'
    )
    run = _run(Fake(body))
    assert run.data.answer == "fine"
    junk = [e for e in run.events if e["kind"] == "cli_stdout"]
    assert len(junk) == 2, junk
    assert "Update available" in junk[0]["text"]
    print("PASS  unparseable stdout is kept as a record, not raised")


def test_a_quiet_child_is_abandoned_and_really_dies():
    """The assertion codex cannot make: no leaked child process."""
    body = 'import time\ntime.sleep(120)\n'
    backend = Fake(body, timeout=0.4, max_seconds=60)
    with pytest.raises(BackendTimeout) as caught:
        _run(backend)
    assert "went silent" in str(caught.value)
    assert "Nothing arrived at all" in str(caught.value), str(caught.value)
    print("PASS  a silent child is killed, and says which kind of silence")


def test_stop_terminates_the_subprocess():
    body = (
        'import json,time\n'
        'for i in range(100):\n'
        '    print(json.dumps({"event":"step","command":"tick"})); time.sleep(0.1)\n'
    )
    backend = Fake(body, timeout=30, max_seconds=60)
    backend.cancel = threading.Event()
    threading.Timer(0.5, backend.cancel.set).start()

    with pytest.raises(BackendCancelled):
        _run(backend)
    print("PASS  Stop terminates the child rather than waiting it out")


def test_no_answer_raises_output_error_not_a_crash():
    """agy's measured shape: SUCCESS, empty response, no structured output."""
    body = 'import json\nprint(json.dumps({"event":"result","id":"c","out":""}))\n'
    with pytest.raises(BackendOutputError) as caught:
        _run(Fake(body))
    assert "no answer at all" in str(caught.value)
    print("PASS  a successful-looking turn with no answer is an output error")


def test_bad_json_raises_output_error_carrying_the_reply():
    body = ('import json\n'
            'print(json.dumps({"event":"result","id":"c","out":"not json at all"}))\n')
    with pytest.raises(BackendOutputError) as caught:
        _run(Fake(body))
    assert "not json at all" in str(caught.value), "show what it actually sent"
    print("PASS  an unparseable answer reports what it was")


def test_the_environment_is_scrubbed():
    """Measured: `claude` run inside a Claude Code session reused the PARENT's
    session id, because it inherits ~76 CLAUDE_* variables."""
    body = ('import json,os\n'
            'leaked=[k for k in os.environ if k.startswith(("CLAUDE_","ANTHROPIC_"))]\n'
            'print(json.dumps({"event":"result","id":"c",'
            '"out":json.dumps({"answer":str(len(leaked))})}))\n')
    os.environ["CLAUDE_CODE_SESSION_ID"] = "parent-session"
    try:
        assert _run(Fake(body)).data.answer == "0"
    finally:
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
    print("PASS  CLAUDE_*/ANTHROPIC_* never reach the child")


def test_a_missing_executable_fails_at_construction():
    from agent.backends.base import BackendUnavailable

    class Missing(CliBackend):
        name = "missing"
        executable_default = "definitely-not-a-real-binary-xyz"

    with pytest.raises(BackendUnavailable) as caught:
        Missing()
    assert "not on PATH" in str(caught.value)
    assert len(str(caught.value)) > 80, "the message should say what to do"
    print("PASS  a CLI that is not installed fails at startup, not mid-task")


def test_progress_reports_carry_the_seven_keys():
    """The contract activity.arm_progress reads. A mismatch is a TypeError
    inside a try/except that exists so reporting cannot break the turn, so it
    shows up as the detail silently not appearing."""
    seen = {}
    body = ('import json,time\n'
            'for i in range(12):\n'
            '    print(json.dumps({"event":"delta","text":"tok"})); time.sleep(0.5)\n'
            'print(json.dumps({"event":"result","id":"c","out":json.dumps({"answer":"a"})}))\n')
    backend = Fake(body, timeout=30, max_seconds=60)
    backend.on_progress = seen.update
    _run(backend)

    assert set(seen) == {"events", "elapsed", "idle", "counts", "last",
                         "streamed", "live"}, sorted(seen)
    assert seen["streamed"] > 0 and seen["live"].get("message")
    print("PASS  progress reports match the sink that receives them")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
