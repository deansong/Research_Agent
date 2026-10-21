"""
WHAT:  The supervisor -- review a finished run, then finish, re-plan or
       redesign and run again (agent/supervisor.py, and the loop in
       webui/runner.py and agent/cli.py).
WHY:   It is the one piece that starts phases on its own. The tests drive
       whole sessions through the web API on the fake backend, so the loop is
       exercised end to end: design, run, review, redesign, run again.

Run with:   python -m pytest tests/test_supervisor.py -q
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import time

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from agent import activity, supervisor  # noqa: E402

TIMEOUT = 30.0


# ---- the decision ---------------------------------------------------------------

def _review(verdict: str) -> dict:
    return {"verdict": verdict, "summary": "s", "findings": ["f1"],
            "plan_feedback": "add seed 3", "design_feedback": "split s3"}


@pytest.mark.parametrize("verdict, iteration, expected", [
    ("done", 1, "finish"),
    ("stop", 1, "finish"),
    ("improve_experiment", 1, "replan"),
    ("improve_agent", 1, "redesign"),
    ("improve_agent", 3, "finish"),          # the iteration limit
])
def test_auto_mode_acts_on_the_verdict_up_to_the_limit(verdict, iteration, expected):
    with tempfile.TemporaryDirectory() as tmp:
        out = supervisor._decide({"mode": "auto", "iteration": iteration, "max_iterations": 3,
                                  "review": _review(verdict), "session_dir": tmp,
                                  "agent_dir": "", "report": "r"})
        assert out["decision"] == expected
        saved = json.loads((pathlib.Path(tmp) / "iterations" / str(iteration)
                            / "review.json").read_text())
        assert saved["decision"] == expected, "every decision is archived"


def test_feedback_carries_the_findings_and_the_right_half():
    replan = supervisor.feedback_for(_review("improve_experiment"), "replan", "use 5 seeds")
    assert "add seed 3" in replan and "f1" in replan and "use 5 seeds" in replan
    redesign = supervisor.feedback_for(_review("improve_agent"), "redesign")
    assert "split s3" in redesign and "add seed 3" not in redesign
    assert supervisor.feedback_for(_review("done"), "finish") == ""


def test_the_report_has_the_run_the_outputs_the_files_and_earlier_reviews():
    with tempfile.TemporaryDirectory() as tmp:
        session, artifacts = pathlib.Path(tmp) / "s", pathlib.Path(tmp) / "a"
        session.mkdir(), artifacts.mkdir()
        (artifacts / "results.json").write_text("{}")
        activity.record_visit(session, "supervisor", kind="supervisor")   # last run's end
        activity.record_visit(session, "s1_code", kind="agent", route="check_1",
                              provider="codex", model="m", seconds=3, output_tokens=10)
        activity.record_visit(session, "check_1", kind="agent", route="redo")
        activity.record_visit(session, "s1_code", kind="agent", route="check_1")
        supervisor.archive(session_dir=session, agent_dir="", iteration=1, report="r",
                           review={**_review("improve_agent"), "decision": "redesign"})

        report = supervisor.run_report(
            session_dir=session, artifacts_dir=artifacts, plan={"steps": [
                {"id": "1", "title": "Code", "check": "pytest passes"}]},
            brief="the brief", values={"outcome": "exit", "outputs": {
                "check_1": {"verdict": "redo", "evidence": "ran pytest: 2 failed"}}},
            iteration=2)

    assert "s1_code: 2 visit(s)" in report, "visits counted per node, this run only"
    assert "codex/m" in report, "which model ran it"
    assert "ran pytest: 2 failed" in report, "a checker's evidence"
    assert "results.json" in report, "the files the run produced"
    assert "iteration 1: improve_agent" in report, "earlier reviews"
    assert "pytest passes" in report, "the plan's checks"


# ---- whole sessions through the web API ----------------------------------------------

def _client(repo: pathlib.Path, mode: str, iterations: int = 3):
    from fastapi.testclient import TestClient

    from webui import server

    server._RUNNERS.clear()

    class Args:
        backend = "fake"
        model = None
        backend_role = None
        config = None
        session = None
        supervisor = mode
        max_iterations = iterations

    return TestClient(server.create_app(repo, Args()))


def _until(client, sid: str, *, purpose: str | None = None, done: bool = False) -> dict:
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        detail = client.get(f"/api/sessions/{sid}").json()
        if done and not detail["busy"]:
            return detail
        if not done and detail["waiting"]:
            if purpose is None or detail["pending"]["purpose"] == purpose:
                return detail
            raise AssertionError(f"asked {detail['pending']['purpose']!r}, "
                                 f"expected {purpose!r}: {detail['pending']['question'][:300]}")
        if not done and not detail["busy"]:
            raise AssertionError(f"finished early: {detail.get('error')}")
        time.sleep(0.05)
    raise AssertionError("timed out")


def _answer(client, sid, text):
    assert client.post(f"/api/sessions/{sid}/answer", json={"text": text}).status_code == 200


def _design_and_run(client, repo) -> tuple[str, pathlib.Path]:
    sid = client.post("/api/sessions", json={"repo": str(repo), "task": "survey it",
                                             "backend": "fake"}).json()["id"]
    _until(client, sid, purpose="discussion")
    _answer(client, sid, "/plan")
    _until(client, sid, purpose="plan_review")
    _answer(client, sid, "/approve")
    detail = _until(client, sid, purpose="design_review")
    _answer(client, sid, "/approve")
    # The designed agent's own final stop, then it ends.
    _until(client, sid, purpose="findings")
    _answer(client, sid, "/approve")
    return sid, pathlib.Path(detail["folder"])


def test_manual_mode_reviews_the_run_and_waits_for_a_decision(tmp_path):
    from webui.tests.conftest import temp_repo

    with temp_repo() as tmp:
        repo = pathlib.Path(tmp)
        client = _client(repo, "manual")
        sid, session = _design_and_run(client, repo)

        asked = _until(client, sid, purpose="supervisor_review")
        assert "/accept" in asked["pending"]["question"]
        _answer(client, sid, "/finish")
        _until(client, sid, done=True)

        review = json.loads((session / "iterations" / "1" / "review.json").read_text())
        assert review["decision"] == "finish"

        # What the Iterations and Timeline tabs read.
        listed = client.get(f"/api/sessions/{sid}/iterations").json()
        assert listed["mode"] == "manual" and listed["iterations"][0]["n"] == 1
        first = listed["iterations"][0]
        assert first["review"]["decision"] == "finish" and first["nodes"] > 0
        assert "ITERATION 1" in first["report"], "the report the supervisor read"
        visits = client.get(f"/api/sessions/{sid}/timeline").json()["visits"]
        assert visits[-1]["node"] == "supervisor"
        assert (session / "iterations" / "1" / "agent" / "graph.json").exists()
        assert any(v["node"] == "supervisor" for v in activity.timeline(session))


def test_a_manual_redesign_designs_again_with_the_findings_and_runs_again(tmp_path):
    from webui.tests.conftest import temp_repo

    with temp_repo() as tmp:
        repo = pathlib.Path(tmp)
        client = _client(repo, "manual")
        sid, session = _design_and_run(client, repo)

        _until(client, sid, purpose="supervisor_review")
        _answer(client, sid, "/redesign give the checker more evidence")
        # Back in the design phase, at the designer -- not the discussion.
        _until(client, sid, purpose="design_review")
        designer = activity.latest(session, "designer")
        assert "FINDINGS FROM THE REVIEW OF THE LAST RUN" in designer.sent["prompt"]
        assert "give the checker more evidence" in designer.sent["prompt"]
        _answer(client, sid, "/approve")

        _until(client, sid, purpose="findings")          # iteration 2 ran
        _answer(client, sid, "/approve")
        _until(client, sid, purpose="supervisor_review")
        _answer(client, sid, "/finish")
        _until(client, sid, done=True)

        decisions = [json.loads((session / "iterations" / str(n) / "review.json")
                                .read_text())["decision"] for n in (1, 2)]
        assert decisions == ["redesign", "finish"]


def test_auto_mode_redesigns_without_asking_and_stops_at_the_limit(monkeypatch):
    """Auto: the review says improve_agent, so it redesigns -- approving the
    design itself -- runs again, and finishes at max_iterations."""
    from agent.backends.fake import FakeBackend
    from agent.backends.base import StructuredRun, Usage
    from webui.tests.conftest import temp_repo

    original = FakeBackend.run_structured

    def reviewing(self, **kw):
        if kw["output_model"] is supervisor.SupervisorReview:
            data = supervisor.SupervisorReview.model_validate(_review("improve_agent"))
            return StructuredRun(data=data, thread_id="r", is_new_thread=True,
                                 usage=Usage(input_tokens=1), events=[])
        return original(self, **kw)

    monkeypatch.setattr(FakeBackend, "run_structured", reviewing)

    with temp_repo() as tmp:
        repo = pathlib.Path(tmp)
        client = _client(repo, "auto", iterations=2)
        sid, session = _design_and_run(client, repo)

        # No supervisor question and no design approval: straight into
        # iteration 2's own final stop.
        _until(client, sid, purpose="findings")
        _answer(client, sid, "/approve")
        _until(client, sid, done=True)

        decisions = [json.loads((session / "iterations" / str(n) / "review.json")
                                .read_text())["decision"] for n in (1, 2)]
        assert decisions == ["redesign", "finish"], "redesign once, then the limit"


def test_a_config_file_can_set_the_supervisor():
    """Read through the real config-file schema. Nothing else in the suite
    loads a config FILE, which is how a missing import in its model reached
    `web` startup: every session in a repo with a .agent/config.json failed."""
    import types

    from agent.config import load_config

    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp)
        (repo / ".agent").mkdir()
        (repo / ".agent" / "config.json").write_text(json.dumps(
            {"supervisor": "auto", "max_iterations": 5,
             "roles": {"runner": {"options": {"timeout": 60}}}}))
        none = types.SimpleNamespace(backend=None, model=None, backend_role=None,
                                     session=None, config=None)
        cfg = load_config(repo_path=repo, cli=none)
        assert (cfg.supervisor, cfg.max_iterations) == ("auto", 5)

        (repo / ".agent" / "config.json").write_text(json.dumps({"supervisor": "sometimes"}))
        with pytest.raises(SystemExit):
            load_config(repo_path=repo, cli=none)
