"""
WHAT:  What a long, unattended run needs: it waits out an empty account or a
       usage limit for hours rather than minutes, Pause takes effect between
       nodes (and at once during a retry wait), and a run the server was
       stopped under carries on when the server starts again.
WHY:   A real run died after 20 minutes of credit checks; Pause did nothing
       until the run reached its next question; and a restarted server forgot
       every run it had.

Run with:   python -m pytest tests/test_long_runs.py -q
"""

from __future__ import annotations

import os
import pathlib
import sys
import threading
import time
from types import SimpleNamespace
from typing import TypedDict

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from agent import storage  # noqa: E402
from agent.backends import quota  # noqa: E402
from agent.backends.base import BackendCancelled, BackendOutOfCredits  # noqa: E402

NOW = 1_800_000_000.0


# ---- how long to wait ----------------------------------------------------------

@pytest.mark.parametrize("message, seconds", [
    ("Claude AI usage limit reached|1800003600", 3600),
    ("You've hit your usage limit. Try again in 2 hours 13 minutes.", 7980),
    ("Your quota will reset after 2h13m5s.", 7985),
    ("retry after 120 seconds", 120),
    ("out of credits", None),
    ("resets 5", None),
])
def test_the_reset_time_is_read_from_the_message(message, seconds):
    assert quota.reset_delay(message, now=NOW) == seconds


def test_a_named_clock_time_is_the_next_one():
    delay = quota.reset_delay("You've hit your limit · resets 6pm", now=NOW)
    assert delay is not None and 0 < delay <= 24 * 3600


def test_the_wait_backs_off_to_half_an_hour_and_lasts_a_day():
    wait = quota.CreditWait()
    gaps = [wait.next("out of credits")[0] for _ in range(8)]
    assert gaps[:6] == [60, 120, 240, 480, 960, 1800] and gaps[6:] == [1800, 1800]
    while True:
        try:
            wait.next("out of credits")
        except StopIteration:
            break
    assert wait.waited == pytest.approx(24 * 3600), "gives up after the budget, not 20 checks"


def test_a_named_reset_is_waited_for_in_one_go():
    seconds, why = quota.CreditWait().next("limit reached|%d" % (time.time() + 7200))
    assert 7200 <= seconds <= 7240 and "resets" in why


def test_a_cli_backend_waits_out_a_usage_limit_instead_of_retrying_it_as_busy(tmp_path):
    """claude_code and antigravity used to treat a quota as 'busy': three
    retries 5-20s apart, then the node failed."""
    from tests.test_backend_io import Fake, _run
    import agent.backends._cli as cli

    class Limited(Fake):
        def refusal_text(self, envelope):
            return envelope.get("error", "")

    counter = tmp_path / "n"
    body = ('import json,pathlib\n'
            f'p=pathlib.Path({str(counter)!r})\n'
            'n=int(p.read_text()) if p.exists() else 0\n'
            'p.write_text(str(n+1))\n'
            'if n<5: print(json.dumps({"event":"result","error":"usage limit reached","out":""}))\n'
            'else: print(json.dumps({"event":"result","out":json.dumps({"answer":"later"})}))\n')
    waits = []
    real = cli.wait_or_cancel
    cli.wait_or_cancel = lambda seconds, cancel=None, pause=None: waits.append(seconds)
    try:
        run = _run(Limited(body, timeout=30, max_seconds=60))
        assert run.data.answer == "later"
        assert waits == [60, 120, 240, 480, 960], "five long waits, not busy retries"

        counter.write_text("0")
        with pytest.raises(BackendOutOfCredits):
            _run(Limited(body, timeout=30, max_seconds=60, credit_wait_hours=0.01))
    finally:
        cli.wait_or_cancel = real


def test_pause_ends_a_retry_wait_at_once():
    from agent.backends._progress import wait_or_cancel

    pause = threading.Event()
    threading.Timer(0.3, pause.set).start()
    started = time.monotonic()
    with pytest.raises(BackendCancelled, match="Paused"):
        wait_or_cancel(3600, threading.Event(), pause)
    assert time.monotonic() - started < 3


# ---- Pause between nodes ------------------------------------------------------

class _S(TypedDict, total=False):
    seen: list


def test_pause_stops_after_the_node_in_flight_and_start_carries_on():
    """invoke() ran the whole graph to its next question, so Pause during a
    run never took effect. The runner now steps node by node."""
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph

    from webui.runner import _STOP, SessionRunner

    runs = []
    holder = SimpleNamespace(_pause=threading.Event())

    def node(name):
        def run(state):
            runs.append(name)
            if name == "one":
                holder._pause.set()        # Pause pressed while "one" runs
            return {"seen": (state.get("seen") or []) + [name]}
        return run

    builder = StateGraph(_S)
    for name in ("one", "two", "three"):
        builder.add_node(name, node(name))
    builder.add_edge(START, "one")
    builder.add_edge("one", "two")
    builder.add_edge("two", "three")
    builder.add_edge("three", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "t"}}

    assert SessionRunner._advance(holder, graph, {"seen": []}, config) is _STOP
    assert runs == ["one"], "stopped after the node in flight, before the next"
    assert graph.get_state(config).next == ("two",)

    holder._pause.clear()                   # Start
    assert SessionRunner._advance(holder, graph, None, config) == {}
    assert runs == ["one", "two", "three"], "carried on; nothing ran twice"


# ---- a restarted server carries on ---------------------------------------------

def test_a_run_the_server_stopped_under_resumes_when_it_starts_again(tmp_path):
    from tests.test_supervisor import _client, _until
    from webui import server
    from webui.tests.conftest import temp_repo

    with temp_repo() as tmp:
        repo = pathlib.Path(tmp)
        client = _client(repo, "off")
        sid = client.post("/api/sessions", json={
            "repo": str(repo), "task": "survey it", "backend": "fake",
            "supervisor": "off"}).json()["id"]
        _until(client, sid, purpose="discussion")
        session = repo / ".agent" / "sessions" / sid
        assert [p.name for p in storage.marked_running(repo)] == [sid]
        assert storage.load_options(storage.session_paths(repo, sid))["supervisor"] == "off"

        # The server stops (what its lifespan does on Ctrl-C or a restart).
        for runner in list(server._RUNNERS.values()):
            runner.shutdown(server_exit=True)
        server._RUNNERS.clear()
        assert storage.marked_running(repo) == [session], "kept for the next start"

        # ...and starts again.
        assert server.resume_interrupted(repo) == [sid]
        detail = _until(client, sid, purpose="discussion")
        assert detail["busy"], "running again, back at the question it was on"

        # Pause (or finishing, or closing) ends it for good: no resume.
        assert client.post(f"/api/sessions/{sid}/pause").status_code == 200
        _until(client, sid, done=True)
        assert storage.marked_running(repo) == []


def test_no_resume_forgets_the_marks_so_they_do_not_come_back_later(tmp_path):
    from webui import server

    paths = storage.session_paths(tmp_path, "s1")
    storage.mark_running(paths)
    assert server.forget_interrupted(tmp_path) == ["s1"]
    assert storage.marked_running(tmp_path) == []
    assert paths.session.is_dir(), "the session itself is untouched"


def test_a_finished_run_awaiting_the_supervisor_is_not_run_again_on_restart():
    """Measured: the agent had finished, the supervisor's decision was
    pending, the server restarted -- and resuming started the whole agent
    again from step 1, about to rewrite its results. A finished phase now
    hands back its result, so it comes back to the supervisor's question."""
    from tests.test_supervisor import _answer, _client, _until
    from webui import server
    from webui.tests.conftest import temp_repo

    with temp_repo() as tmp:
        repo = pathlib.Path(tmp)
        client = _client(repo, "manual")
        sid = client.post("/api/sessions", json={
            "repo": str(repo), "task": "survey it", "backend": "fake",
            "research": False}).json()["id"]
        session = repo / ".agent" / "sessions" / sid
        _until(client, sid, purpose="discussion")
        _answer(client, sid, "/plan")
        _until(client, sid, purpose="plan_review")
        _answer(client, sid, "/approve")
        _until(client, sid, purpose="design_review")
        _answer(client, sid, "/approve")
        _until(client, sid, purpose="findings")
        _answer(client, sid, "/approve")
        _until(client, sid, purpose="supervisor_review")
        turns = sorted(p.name for p in (session / "activity").rglob("0*.json"))

        for runner in list(server._RUNNERS.values()):      # the server restarts
            runner.shutdown(server_exit=True)
        server._RUNNERS.clear()
        assert server.resume_interrupted(repo) == [sid]

        asked = _until(client, sid, purpose="supervisor_review")
        assert asked["pending"]["purpose"] == "supervisor_review"
        assert sorted(p.name for p in (session / "activity").rglob("0*.json")) == turns, \
            "no node ran again"
        _answer(client, sid, "/finish")
        _until(client, sid, done=True)


def test_marks_live_outside_the_repository_and_old_ones_are_adopted(tmp_path):
    """Nothing a run does adds files to the repository -- the resume marks
    included. A mark left there by the old layout still resumes its session."""
    import json

    paths = storage.session_paths(tmp_path, "s1")
    storage.mark_running(paths)
    assert not (tmp_path / ".agent" / "running").exists()
    assert storage.marked_running(tmp_path) == [paths.session]

    legacy = tmp_path / ".agent" / "running"
    legacy.mkdir(parents=True)
    (legacy / "old.json").write_text(json.dumps({"session_dir": str(tmp_path / "old")}))
    assert tmp_path / "old" in storage.marked_running(tmp_path)
    assert not legacy.exists(), "moved out, and the folder removed"
