"""
WHAT:  The next run starts from a reviewed one: what carries over, what must
       be approved again, and that the review reaches the bootstrap's
       orchestrator, planner and designer.

Run with:   python -m pytest tests/test_carry.py -q
"""

from __future__ import annotations

import json

import pytest

from research_agent import cli
from research_agent import state as S
from research_agent.bootstrap import carry
from tests.scripted import script  # noqa: F401 -- fixture
from tests.test_execute_graph import REVIEW, approved_state
from tests.test_graph import DESIGN, decide, say


@pytest.fixture
def reviewed(tmp_path):
    """A finished run with a review."""
    old = tmp_path / "20261024-1200-lora"
    st = approved_state()
    st, _ = S.apply(st, [
        {"op": "set", "section": "execution", "value": {"started": "2026-10-24T12:00:00+00:00",
                                                        "steps": [{"id": "1", "title": "Data",
                                                                   "status": "pass"}]}},
        {"op": "add", "section": "results", "value": {"node": "builder", "summary": "done"}},
        {"op": "add", "section": "reviews", "value": {**REVIEW, "run": str(old)}},
        {"op": "merge", "section": "status", "value": {"phase": "done",
                                                       "summary": "H1 supported on two seeds."}}],
        by="execution")
    S.Store(old).write(st)
    (old / "config.json").write_text(json.dumps({"roles": {"discussor": {"provider": "fake"}}}))
    return old


def test_what_carries_over_and_what_is_approved_again(reviewed):
    new = carry.next_folder(reviewed)
    assert new.name == "20261024-1200-lora-next"
    st = carry.start_from(reviewed, new)
    assert carry.next_folder(reviewed).name == "20261024-1200-lora-next2"
    assert st["reviews"][0]["requests"][0]["what"] == "Run five seeds"
    assert st["plan"] and st["design"]["nodes"] and st["hypotheses"]
    assert not any(S.approved(st, s) for s in S.PARTS), "the revised design is approved again"
    assert not (st.get("execution") or {}).get("steps") and not st["results"]
    assert st["status"]["phase"] == "discussing"
    assert "asks for: [must] Run five seeds" in st["status"]["summary"]
    assert st["decisions"][-1]["what"] == "Start from run 20261024-1200-lora and its review V1"
    assert (new / "config.json").exists()
    assert S.Store(new).read() == st


def test_a_folder_that_is_not_a_run_is_refused(tmp_path):
    with pytest.raises(ValueError, match="not a run folder"):
        carry.start_from(tmp_path / "nothing", tmp_path / "new")


def test_bootstrap_from_a_reviewed_run(reviewed, tmp_path, script, monkeypatch, capsys):
    script.add("DiscussorOutput",
               say("The review asks for five seeds; the designer will revise the design.",
                   ("revise_design", "Answer review V1: five seeds")),
               say("The revised design is ready. Approve?"))
    script.add("OrchestratorOutput", decide("design", "Add seeds per V1"),
               decide("ask_user", "Approve the revised design?"))
    script.add("DesignOutput", {**DESIGN, "hypotheses": [
        {**DESIGN["hypotheses"][0], "evidence": []}]})
    monkeypatch.setattr("builtins.input", lambda prompt="": (_ for _ in ()).throw(EOFError))
    new = tmp_path / "next"
    assert cli.main(["bootstrap", "--from", str(reviewed), "--run", str(new),
                     "--role", "all=fake"]) == 0
    out = capsys.readouterr().out
    assert f"Starting from {reviewed}" in out and "The revised design is ready." in out
    first = script.prompts("DiscussorOutput")[0]
    assert "Continue the study from the previous run" in first
    assert "## Reviews" in first and "[must] Run five seeds" in first
    assert "[must] Run five seeds" in script.prompts("OrchestratorOutput")[0]
    assert "rating 5/10" in script.prompts("DesignOutput")[0], "the designer sees the review"
    st = S.Store(new).read()
    assert S.version(st, "design") > 1 and not S.approved(st, "design")
