"""
WHAT:  Whole sessions with a hub agent, over HTTP on the fake backend: the
       discussor's conversation carried into the run as the orchestrator,
       the orchestrator's own end-of-run review instead of a supervisor, and
       another iteration when it (or you) says so.
WHY:   The continuity is the point of the redesign and it crosses three
       graphs -- design, run, design again -- so only a whole session can
       show it holds.

Run with:   python -m pytest tests/test_orchestrator_session.py -q
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from agent import activity  # noqa: E402
from agent.work import memory  # noqa: E402


def _start(client, repo):
    from tests.test_supervisor import _answer, _until

    sid = client.post("/api/sessions", json={"repo": str(repo), "task": "survey it",
                                             "backend": "fake"}).json()["id"]
    _until(client, sid, purpose="discussion")
    _answer(client, sid, "/plan")
    _until(client, sid, purpose="plan_review")
    _answer(client, sid, "/approve")
    _until(client, sid, purpose="design_review")
    _answer(client, sid, "/approve")
    return sid, repo / ".agent" / "sessions" / sid


def test_the_discussor_runs_the_experiment_and_reviews_it(tmp_path):
    from tests.test_supervisor import _answer, _client, _until
    from webui.tests.conftest import temp_repo

    with temp_repo() as tmp:
        repo = pathlib.Path(tmp)
        client = _client(repo, "manual", topology="hub")
        sid, session = _start(client, repo)

        asked = _until(client, sid, purpose="finish_review")
        assert "/redesign" in asked["pending"]["question"]
        assert {"accept", "replan", "redesign", "finish"} <= {
            c["name"] for c in asked["commands"]}

        # ONE conversation: the orchestrator continued the discussor's.
        discussor = activity.latest(session, "discussor").thread_id
        turns = activity.turns(session, "orchestrator", limit=100)
        assert discussor and {t.thread_id for t in turns} == {discussor}
        first = min(turns, key=lambda t: t.index)
        assert first.sent["prompt"].startswith("THE DISCUSSION IS OVER")

        # Each step's final report was kept; the paper node ran last.
        reports = session / "artifacts" / "reports"
        assert (reports / "paper.md").exists()
        timeline = [v["node"] for v in activity.timeline(session)]
        assert "validator" in timeline and "paper" in timeline

        _answer(client, sid, "/finish")
        _until(client, sid, done=True)
        review = json.loads((session / "iterations" / "1" / "review.json").read_text())
        assert review["decision"] == "finish" and review["verdict"] == "done"
        saved = memory.load(session)
        assert saved["thread_id"] == discussor and saved["iterations"][0]["decision"] == "finish"
        assert [v for v in activity.timeline(session) if v.get("kind") == "iteration_end"]


def test_a_redesign_carries_the_review_and_the_conversation_into_the_next_run(tmp_path):
    from tests.test_supervisor import _answer, _client, _until
    from webui.tests.conftest import temp_repo

    with temp_repo() as tmp:
        repo = pathlib.Path(tmp)
        client = _client(repo, "manual", topology="hub")
        sid, session = _start(client, repo)
        _until(client, sid, purpose="finish_review")
        _answer(client, sid, "/redesign validate the data step harder")

        # The design phase again, then the redesigned agent's review gate.
        _until(client, sid, purpose="design_review")
        designer = activity.latest(session, "designer").sent["prompt"]
        assert "The orchestrator reviewed the last run" in designer
        assert "validate the data step harder" in designer
        _answer(client, sid, "/approve")

        _until(client, sid, purpose="finish_review")
        turns = activity.turns(session, "orchestrator", limit=200)
        second_run = [t for t in turns if "A RUN STARTS (iteration 2)" in (t.sent or {}).get(
            "prompt", "")]
        assert second_run, "iteration 2's first turn is told it is a new run"
        assert "EARLIER ITERATIONS" in second_run[0].sent["prompt"]
        assert len({t.thread_id for t in turns}) == 1, "still one conversation"
        _answer(client, sid, "/finish")
        _until(client, sid, done=True)
        first = json.loads((session / "iterations" / "1" / "review.json").read_text())
        assert first["decision"] == "redesign" and "harder" in first["note"]
        assert (session / "iterations" / "2" / "review.json").exists()


def test_auto_mode_acts_on_the_orchestrators_verdict_and_stops_at_the_limit(monkeypatch):
    from agent.backends import fake
    from tests.test_supervisor import _client, _until
    from webui.tests.conftest import temp_repo

    original = fake._orchestrate

    def critical(output_model, prompt):
        decision = original(output_model, prompt)
        if decision["action"] == "finish":
            decision.update(verdict="improve_agent",
                            design_feedback="give the eval node more context")
        return decision

    monkeypatch.setattr(fake, "_orchestrate", critical)
    with temp_repo() as tmp:
        repo = pathlib.Path(tmp)
        client = _client(repo, "auto", iterations=2, topology="hub")
        sid, session = _start(client, repo)
        _until(client, sid, done=True)
        decisions = [json.loads((session / "iterations" / str(n) / "review.json").read_text())
                     ["decision"] for n in (1, 2)]
        assert decisions == ["redesign", "finish"], decisions
        assert not (session / "iterations" / "3").exists()
