"""
WHAT:  Drives a whole session over HTTP, with no browser and no model.
WHY:   The interesting parts of the web layer -- the worker thread, the answer
       queue, the SSE replay -- cannot be checked by reading the code. They are
       timing, and timing is only ever proved by running it.
CONCEPT: FastAPI's TestClient plus `--backend fake`.

Run with:   python -m pytest webui/tests -q
        or: python webui/tests/test_api.py
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import threading
import time

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from webui import server  # noqa: E402

#: How long to wait for the worker thread to reach its next question. Generous
#: because the fake backend is instant but thread scheduling is not.
TIMEOUT = 20.0


def _client(tmp: pathlib.Path) -> TestClient:
    """A fresh app over an empty repo, with every role on the fake backend."""
    server._RUNNERS.clear()

    class Args:
        backend = "fake"
        model = None
        backend_role = None
        config = None
        session = None

    return TestClient(server.create_app(tmp, Args()))


def _wait_for_question(client, sid: str, *, after: str = "") -> dict:
    """Block until the session is parked on a question, and return it.

    Polling the detail endpoint rather than reading the SSE stream keeps this
    test honest about the thing it is checking: `waiting` is what the browser's
    Send button is enabled by, so it is what has to be right.
    """
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        detail = client.get(f"/api/sessions/{sid}").json()
        if detail["waiting"] and detail["pending"]["question"] != after:
            return detail
        if not detail["busy"]:
            raise AssertionError(
                f"session finished without asking again "
                f"(phase={detail['phase']}, error={detail['error']!r})"
            )
        time.sleep(0.05)
    raise AssertionError(f"no question within {TIMEOUT}s")


def _wait_until_done(client, sid: str) -> dict:
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        detail = client.get(f"/api/sessions/{sid}").json()
        if not detail["busy"]:
            return detail
        time.sleep(0.05)
    raise AssertionError(f"still running after {TIMEOUT}s")


def test_a_whole_session_over_http():
    """Discuss, plan, approve, design, run, exit -- the entire pipeline.

    This is the acceptance test for the web layer. If it passes, the worker
    thread, the answer queue, the interrupt/resume cycle and the two-phase
    handoff all work through HTTP exactly as they do through the terminal.
    """
    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp)
        client = _client(repo)

        created = client.post("/api/sessions", json={
            "repo": str(repo), "task": "survey the repository", "backend": "fake",
        })
        assert created.status_code == 200, created.text
        sid = created.json()["id"]
        assert created.json()["phase"] in ("idle", "designing")

        # The design phase interviews you first.
        first = _wait_for_question(client, sid)
        assert first["pending"]["purpose"] == "discussion", first["pending"]
        # The browser is told which slash commands are legal HERE -- computed
        # server-side from the registry, so it never has to guess.
        assert first["phase"] == "designing"

        # /plan is the first gate: the human's decision, never the model's.
        assert client.post(f"/api/sessions/{sid}/answer",
                           json={"text": "/plan"}).status_code == 200
        review = _wait_for_question(client, sid)
        assert review["pending"]["purpose"] == "plan_review", review["pending"]

        # The plan was written to disk BEFORE approval -- that is the hook the
        # web plan editor uses.
        plan_file = pathlib.Path(review["folder"]) / "plan.json"
        assert plan_file.exists(), "planner should have written plan.json"
        assert json.loads(plan_file.read_text())["steps"], "plan has no steps"

        assert client.post(f"/api/sessions/{sid}/answer",
                           json={"text": "/approve"}).status_code == 200

        # THE SECOND GATE. The agent is designed and on disk, but it must not
        # be running yet: this is the point at which you look at the graph, and
        # can rewire it, before anything with write access starts.
        design = _wait_for_question(client, sid)
        assert design["pending"]["purpose"] == "design_review", design["pending"]
        assert design["phase"] == "designing", design["phase"]
        assert design["has_agent"], "the folder must exist so it can be edited"
        assert design["agent_name"] == "fake-generated", design["agent_name"]
        assert not (pathlib.Path(design["folder"]) / "approved").exists()
        # The graph endpoint must work HERE -- reviewing it is the whole point.
        assert client.get(f"/api/sessions/{sid}/agent").status_code == 200

        assert client.post(f"/api/sessions/{sid}/answer",
                           json={"text": "/approve"}).status_code == 200

        # Only now is the designed agent running, and asking its own question.
        work = _wait_for_question(client, sid)
        assert work["phase"] == "running", work
        assert work["agent_name"] == "fake-generated", work["agent_name"]
        assert (pathlib.Path(work["folder"]) / "approved").exists(), \
            "approval must be recorded on disk"

        assert client.post(f"/api/sessions/{sid}/answer",
                           json={"text": "/exit"}).status_code == 200
        done = _wait_until_done(client, sid)
        assert done["phase"] == "finished", done
        assert not done["error"], done["error"]

        print("PASS  discuss -> /plan -> plan_review -> design_review -> run, over HTTP")


def test_answering_when_nothing_asked_is_refused():
    """The two-tabs bug, made impossible.

    Without this, a second answer sits in the queue and gets consumed at the
    NEXT question -- answering something nobody watched being asked. 409 rather
    than 400: it is a state conflict, and a browser retries those differently.
    """
    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp)
        client = _client(repo)
        sid = client.post("/api/sessions", json={
            "repo": str(repo), "task": "survey the repository", "backend": "fake",
        }).json()["id"]

        _wait_for_question(client, sid)

        first = client.post(f"/api/sessions/{sid}/answer", json={"text": "hello"})
        assert first.status_code == 200, first.text

        # The graph is thinking now, not waiting. A second answer must bounce.
        second = client.post(f"/api/sessions/{sid}/answer", json={"text": "hello again"})
        assert second.status_code == 409, second.text
        assert second.json()["detail"]["code"] == "not_waiting", second.json()
        print("PASS  a second answer is refused while none is outstanding")


def test_events_replay_from_a_sequence_number():
    """A dropped connection must lose nothing and repeat nothing.

    The browser's EventSource reconnects by itself and sends back the last id it
    saw. This checks the server half of that: ask for everything after seq N and
    get exactly that, with no gap and no duplicate.
    """
    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp)
        client = _client(repo)
        sid = client.post("/api/sessions", json={
            "repo": str(repo), "task": "survey the repository", "backend": "fake",
        }).json()["id"]
        _wait_for_question(client, sid)

        runner = server._RUNNERS[sid]
        everything = runner.bus.replay(0)
        assert everything, "the design phase should have emitted something"

        seqs = [e.seq for e in everything]
        assert seqs == list(range(1, len(seqs) + 1)), f"seq must be gapless: {seqs}"

        # The whole point: resuming from the middle returns the tail, exactly.
        midpoint = seqs[len(seqs) // 2]
        tail = runner.bus.replay(midpoint)
        assert [e.seq for e in tail] == [s for s in seqs if s > midpoint]

        kinds = {e.type for e in everything}
        assert "question" in kinds, kinds
        assert "log" in kinds, f"node prints should have been captured: {kinds}"
        print("PASS  events are gapless and replayable from any sequence number")


def test_node_prints_reach_the_stream():
    """The stdout tee, end to end.

    The most useful output in the system -- what the executor ran, what it
    touched -- is written with print() inside a node and never passes through
    the GraphSession seam. If webui/capture.py stops working, the browser goes
    quiet during exactly the minutes you most want to watch.
    """
    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp)
        client = _client(repo)
        sid = client.post("/api/sessions", json={
            "repo": str(repo), "task": "survey the repository", "backend": "fake",
        }).json()["id"]
        _wait_for_question(client, sid)

        lines = [e.data["text"] for e in server._RUNNERS[sid].bus.replay(0)
                 if e.type == "log"]
        joined = "\n".join(lines)
        # From bootstrap/nodes/discussor.py and agent/telemetry.py respectively:
        # one node narration and one accounting line, neither of which the
        # session adapter knows anything about.
        assert any("[discussor]" in line for line in lines), joined
        assert any("[tokens:" in line for line in lines), joined
        print("PASS  print() inside a node arrives as a log event")


def test_two_threads_do_not_cross_streams():
    """Routing is per thread, which is what makes a global sys.stdout swap safe.

    This is the property the whole capture design rests on. sys.stdout is
    process-global, so if routing were global too, two sessions running at once
    would pour into whichever bus was installed last -- and you would see the
    other session's executor output in your own browser tab.
    """
    from webui import capture

    mine: list[str] = []
    theirs: list[str] = []
    unrouted: list[str] = []
    started = threading.Event()
    finish = threading.Event()

    def other_session():
        with capture.routed_to(theirs.append):
            started.set()
            print("belongs to the other session")
            finish.wait(timeout=5)

    def no_session():
        # Nothing registered for this thread, so it must fall through to
        # whatever stdout was there before -- never into somebody's sink.
        print("belongs to nobody")

    thread = threading.Thread(target=other_session)
    thread.start()
    started.wait(timeout=5)

    with capture.routed_to(mine.append):
        print("belongs to me")
        plain = threading.Thread(target=no_session)
        plain.start()
        plain.join()

    finish.set()
    thread.join()

    assert mine == ["belongs to me"], mine
    assert theirs == ["belongs to the other session"], theirs
    assert unrouted == [], unrouted
    print("PASS  two concurrent sessions never see each other's output")



def test_pause_stops_the_run_and_start_resumes_it():
    """Pause has to be a pause, not a quiet abort.

    It stops between nodes, which is exactly where the graph checkpoints, so
    the guarantee is that Start carries on from the same question rather than
    beginning again. That is the whole difference between this and /exit.
    """
    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp)
        client = _client(repo)
        sid = client.post("/api/sessions", json={
            "repo": str(repo), "task": "pause me", "backend": "fake",
        }).json()["id"]

        first = _wait_for_question(client, sid)
        question = first["pending"]["question"]

        paused = client.post(f"/api/sessions/{sid}/pause")
        assert paused.status_code == 200, paused.text
        assert "stop" in paused.json()["detail"], paused.json()

        done = _wait_until_done(client, sid)
        assert not done["busy"], done
        # NOT an error: stopping on purpose is not a failure.
        assert not done["error"], done["error"]

        restarted = client.post(f"/api/sessions/{sid}/start", json={})
        assert restarted.status_code == 200, restarted.text
        again = _wait_for_question(client, sid)
        assert again["pending"]["question"] == question, \
            "resuming must come back to the same question, not start over"
        print("PASS  pause stops between nodes and start resumes the same question")


def test_pause_and_stop_are_refused_when_nothing_is_running():
    """A control that reports success while doing nothing is worse than one
    that is disabled -- so the server says so plainly."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp)
        client = _client(repo)
        sid = client.post("/api/sessions", json={
            "repo": str(repo), "task": "idle", "backend": "fake", "start": False,
        }).json()["id"]

        for verb in ("pause", "stop"):
            response = client.post(f"/api/sessions/{sid}/{verb}")
            assert response.status_code == 200, response.text
            assert response.json()["detail"] == "not running", response.json()
        print("PASS  pause and stop say 'not running' rather than pretending")

def test_the_running_turn_is_served_while_it_runs():
    """What the "still working" line expands into.

    The endpoint has to answer without being told which node is busy: the
    heartbeat line does not say, and making the browser track it would be
    asking it to hold state the server already has.
    """
    from agent import activity

    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp)
        client = _client(repo)
        sid = client.post("/api/sessions", json={
            "repo": str(repo), "task": "idle", "backend": "fake", "start": False,
        }).json()["id"]

        # Nothing running yet: an honest "no", not a 404 and not an empty turn
        # dressed up as a real one.
        quiet = client.get(f"/api/sessions/{sid}/activity")
        assert quiet.status_code == 200, quiet.text
        assert quiet.json() == {"running": False, "turn": None}, quiet.json()

        session_dir = server._RUNNERS[sid].paths.session
        activity.write_in_flight(session_dir, "evidence_design", [
            {"kind": "command", "phase": "completed", "at": 9,
             "command": "pytest -q", "exit_code": 1},
        ], elapsed=310.0)

        live = client.get(f"/api/sessions/{sid}/activity").json()
        assert live["running"] is True, live
        assert live["turn"]["node"] == "evidence_design"
        assert live["turn"]["partial"] is True
        assert live["turn"]["counts"] == {"command": 1}, live["turn"]["counts"]
        # The point of the whole exercise: the command itself comes back, not
        # a count of commands.
        assert live["turn"]["events"][0]["command"] == "pytest -q"
        print("PASS  the in-flight turn is served with its actual events")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\nAll web API tests passed.")
