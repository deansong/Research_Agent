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


# ---------------------------------------------------------------------------
# The providers' vocabulary. argv() is pure, so these need no subprocess.
# ---------------------------------------------------------------------------

def _agy():
    from agent.backends.antigravity import AntigravityBackend

    backend = AntigravityBackend.__new__(AntigravityBackend)
    backend.executable = "agy"
    backend.model = "gemini-3.8-flash-low"
    backend.effort = None
    backend.max_seconds = 7200.0
    backend._warned_dangerous = True          # silence the notice in tests
    return backend


def test_antigravity_access_maps_to_flags(capsys):
    """A mis-mapped access is a node declared read_only that edits your
    repository. This is the cheapest possible test for it."""
    backend = _agy()
    dangerous = "--dangerously-skip-permissions"

    for access in (Access.NONE, Access.READ_ONLY):
        flags = backend.access_flags(access)
        assert flags == ["--sandbox"], (access, flags)
        assert dangerous not in flags

    for access in (Access.WRITE, Access.FULL):
        assert backend.access_flags(access) == [dangerous], access
    print("PASS  only write and full reach --dangerously-skip-permissions")


def test_antigravity_says_when_it_skips_permissions(capsys):
    """A silent --dangerously-skip-permissions is the one thing in this
    backend that can damage a machine."""
    from agent.backends.antigravity import AntigravityBackend

    backend = _agy()
    backend._warned_dangerous = False
    backend.access_flags(Access.WRITE)
    first = capsys.readouterr().out
    assert "--dangerously-skip-permissions" in first, first

    backend.access_flags(Access.WRITE)
    assert capsys.readouterr().out == "", "once per process, not once per turn"
    print("PASS  skipping permissions is announced, once")


def test_antigravity_sends_instructions_in_the_prompt():
    """The one asymmetry between the two providers: agy has no system-prompt
    flag, so a persona that is not prepended simply vanishes."""
    argv = _agy().argv(prompt="do the thing", instructions="You are the RUNNER.",
                       access=Access.READ_ONLY, schema_path="/tmp/s.json",
                       session=[])
    assert "--append-system-prompt" not in argv
    body = argv[argv.index("-p") + 1]
    assert "You are the RUNNER." in body and "do the thing" in body
    assert body.index("You are the RUNNER.") < body.index("do the thing")
    print("PASS  agy gets its persona through the prompt, in front")


def test_antigravity_never_mints_a_conversation_id():
    """Measured: --conversation <fresh-uuid> warns 'not found' and then makes
    a DIFFERENT conversation. Trusting a minted id would mean every turn
    starting fresh while we believed it was resuming."""
    fragment, minted = _agy().session_args(None)
    assert fragment == [] and minted is None, (fragment, minted)

    fragment, minted = _agy().session_args("conv-7")
    assert fragment == ["--conversation", "conv-7"] and minted is None
    print("PASS  ids come back from agy; they are never invented here")


def test_antigravity_reads_structured_output_not_response():
    """They differ: `response` carried two keys the model invented that the
    schema never asked for, and extra="forbid" would reject them."""
    envelope = {"event": "result", "result": {
        "status": "SUCCESS",
        "response": '{"answer":"ok","toolAction":"Finishing","toolSummary":"x"}',
        "structured_output": {"answer": "ok"},
        "conversation_id": "c-1",
        "usage": {"input_tokens": 11, "output_tokens": 2,
                  "thinking_tokens": 3, "cache_read_tokens": 4},
    }}
    backend = _agy()
    assert json.loads(backend.final_text(envelope)) == {"answer": "ok"}
    assert backend.session_id_from(envelope) == "c-1"

    usage = backend.usage_from(envelope)
    assert (usage.input_tokens, usage.output_tokens) == (11, 2)
    assert usage.reasoning_tokens == 3 and usage.cached_input_tokens == 4
    print("PASS  the parsed structured_output wins over the raw response")


def test_antigravity_explains_a_denied_tool_call():
    """SUCCESS with no answer is what a permission denial looks like, and the
    remedy is about access, not about retrying."""
    backend = _agy()
    envelope = {"result": {"status": "SUCCESS", "response": ""}}
    assert backend.final_text(envelope) == ""
    why = backend._why_empty(envelope)
    assert "DENIED" in why or "denied" in why.lower(), why
    assert "read_only" in why, "name the likely cause"
    print("PASS  an empty successful turn is explained as a denial")


def test_antigravity_maps_steps_into_the_shared_vocabulary():
    """Reuse `command`/`reasoning` rather than inventing names, so headline(),
    Turn.counts() and the web UI activity panel work with no changes."""
    backend = _agy()

    started = backend.record_for({"event": "step_update", "step_update": {
        "state": "ACTIVE", "step_type": "tool", "tool_name": "run_command",
        "tool_info": {"parameters": {"CommandLine": "pytest -q"}}}})
    assert started == {"kind": "command", "phase": "started",
                       "command": "pytest -q"}, started

    failed = backend.record_for({"event": "step_update", "step_update": {
        "state": "ERROR", "step_type": "tool", "tool_name": "run_command",
        "tool_info": {"parameters": {"CommandLine": "x"},
                      "error": {"message": "permission check failed"}}}})
    assert failed["phase"] == "error" and failed["exit_code"] == 1
    assert "permission check failed" in failed["output"]

    assert backend.record_for({"event": "init", "init": {}}) is None
    assert backend.record_for({"event": "result", "result": {}}) is None
    print("PASS  agy steps become the records the rest of the project reads")


def _claude():
    from agent.backends.claude_code import ClaudeCodeBackend

    backend = ClaudeCodeBackend.__new__(ClaudeCodeBackend)
    backend.executable = "claude"
    backend.model = "opus"
    backend.max_turns = None
    backend.max_budget_usd = None
    return backend


def test_claude_access_maps_to_flags():
    backend = _claude()

    read_only = backend.access_flags(Access.READ_ONLY)
    assert "bypassPermissions" not in read_only
    assert "--disallowed-tools" in read_only, "the deny-list is the guarantee"
    assert "Write" in read_only[read_only.index("--disallowed-tools") + 1]
    assert "--allowed-tools" in read_only, "the allow-list is the intent"

    assert backend.access_flags(Access.WRITE) == [
        "--permission-mode", "acceptEdits"]
    assert backend.access_flags(Access.FULL) == [
        "--permission-mode", "bypassPermissions"]
    print("PASS  only full reaches bypassPermissions")


def test_claude_keeps_the_workspace_out_of_the_turn():
    """With cwd=repo_path the CLI would load that repo's CLAUDE.md, settings,
    hooks and plugins -- instruction injection from the directory under study,
    and hooks running commands nobody asked for."""
    argv = _claude().argv(prompt="go", instructions="be terse",
                          access=Access.READ_ONLY, schema_path="/dev/null",
                          session=[])
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--setting-sources") + 1] == "", argv
    print("PASS  workspace settings, hooks and MCP are kept out")


def test_claude_mints_its_own_session_id():
    """The stub's good idea, kept: --session-id takes a uuid we choose, so the
    handle semantics match Codex's thread id exactly."""
    import uuid as _uuid

    fragment, minted = _claude().session_args(None)
    assert fragment[0] == "--session-id" and minted == fragment[1]
    _uuid.UUID(minted)                       # must be a real uuid or the CLI refuses

    fragment, minted = _claude().session_args("abc-123")
    assert fragment == ["--resume", "abc-123"] and minted == "abc-123"
    print("PASS  turn 1 names the conversation; later turns resume it")


def test_claude_reports_cost_which_nothing_else_does():
    """telemetry.py has printed Usage.cost_usd since it was written and it has
    been None every time, because Codex does not report cost."""
    envelope = {"type": "result", "is_error": False, "result": '{"answer":"a"}',
                "session_id": "s-1", "total_cost_usd": 0.0123,
                "usage": {"input_tokens": 100, "output_tokens": 20,
                          "cache_read_input_tokens": 80,
                          "output_tokens_details": {"thinking_tokens": 7}}}
    backend = _claude()
    usage = backend.usage_from(envelope)
    assert usage.cost_usd == 0.0123, usage
    assert usage.cached_input_tokens == 80 and usage.reasoning_tokens == 7
    assert backend.final_text(envelope) == '{"answer":"a"}'
    assert backend.session_id_from(envelope) == "s-1"
    print("PASS  cost_usd is populated for the first time")


def test_claude_treats_is_error_not_the_exit_code_as_the_signal():
    """Measured: an authentication failure came back is_error true with exit
    status 0. Trusting the exit code would have fed that text to Pydantic."""
    envelope = {"type": "result", "is_error": True,
                "result": "Authentication error", "permission_denials": []}
    backend = _claude()
    assert backend.final_text(envelope) == "", "an errored turn has no answer"
    why = backend._why_empty(envelope)
    assert "Authentication error" in why and "refresh the login" in why
    print("PASS  is_error is the signal, and the message says what to do")


def test_claude_maps_tool_use_into_the_shared_vocabulary():
    backend = _claude()

    bash = backend.record_for({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}}]}})
    assert bash == {"kind": "command", "phase": "started",
                    "command": "pytest -q"}, bash

    write = backend.record_for({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Write",
         "input": {"file_path": "results/a.json"}}]}})
    assert write["kind"] == "file_change"
    assert write["changes"][0]["path"] == "results/a.json"

    failed = backend.record_for({"type": "user", "message": {"content": [
        {"type": "tool_result", "content": "boom", "is_error": True}]}})
    assert failed["exit_code"] == 1 and failed["output"] == "boom"

    delta = backend.record_for({"type": "stream_event", "event": {
        "delta": {"text": "tok"}}})
    assert delta["transient"] is True and delta["stream"] == "message"
    print("PASS  claude tool use becomes the records the project already reads")
