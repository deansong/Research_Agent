"""
WHAT:  The bootstrap graph on scripted answers: a whole run from idea to an
       approved design, the approval gates, the step limit, leaving, and
       resuming from the checkpoint.
WHY:   The orchestrator decides the flow, so what code guarantees -- gates,
       limits, who sees what -- is what has to be pinned.

Run with:   python -m pytest tests/test_graph.py -q
"""

from __future__ import annotations

import json

import pytest

from research_agent import state as S
from research_agent.bootstrap import graph, nodes
from research_agent.bootstrap.session import Bootstrap
from research_agent.config import ROLES, RoleConfig
from tests.scripted import script  # noqa: F401 -- fixture


@pytest.fixture
def run(tmp_path):
    return nodes.Run(folder=tmp_path / "run", roles={r: RoleConfig("fake") for r in ROLES})


def say(reply, *requests, approve=(), **fields):
    return {"reply": reply, "approve": list(approve),
            "requests": [{"kind": k, "detail": d} for k, d in requests], **fields}


def decide(action, task="", **fields):
    return {"action": action, "reason": f"because {action}", "task": task or f"do {action}",
            "summary": f"after {action}", **fields}


FINDINGS = {"summary": "found baselines", "findings": [
    {"claim": "LoRA rank 8 is common", "source": "https://example.org/lora"}]}
HYPO = {"summary": "one hypothesis", "hypotheses": [
    {"statement": "Rank matters", "prediction": "r=64 beats r=4 by >=1 F1", "evidence": ["E1"]}],
    "rubric": [{"criterion": "Effect", "measure": "F1 gap", "threshold": ">=1"}],
    "protocol": {"metrics": ["F1"], "statistics": "paired t-test", "seeds": "5"}}
PLAN = {"summary": "two steps", "steps": [
    {"id": "1", "title": "Data", "detail": "load SST-2", "check": "rows counted"},
    {"id": "2", "title": "Train", "detail": "ranks 4..64", "check": "F1 logged",
     "depends_on": ["1"], "rubric": ["R1"]}]}
DESIGN = {"summary": "three nodes", "nodes": [
    {"name": "orchestrator", "role": "orchestrator", "instructions": "run it"},
    {"name": "trainer", "role": "runner", "steps": ["1", "2"], "access": "write",
     "instructions": "train", "validated_by": ["R1"]},
    {"name": "validator", "role": "validator", "instructions": "check"}]}


def test_a_whole_bootstrap_from_idea_to_an_approved_design(run, script):
    script.add("DiscussorOutput",
               say("Let me look into it.", ("research", "Find LoRA rank baselines"),
                   goal={"question": "Does LoRA rank matter?"}),
               say("Here are H1, R1 and the protocol. Approve?"),
               say("Planning now.", ("proceed", "Write the plan")),
               say("The plan has 2 steps. Approve?"),
               say("Designing.", ("proceed", "Design it")),
               say("Design ready. Approve?"),
               say("All done.", approve=["design"]),
               say("Bootstrap complete."))
    script.add("ResearcherOutput", FINDINGS)
    script.add("HypothesizeOutput", HYPO)
    script.add("PlanOutput", PLAN)
    script.add("DesignOutput", DESIGN)
    script.add("OrchestratorOutput",
               decide("research", questions=["LoRA rank results?"]),
               decide("hypothesize"),
               decide("ask_user", "Approve the hypotheses, rubric and protocol?"),
               decide("plan"),
               decide("ask_user", "Approve the plan?"),
               decide("design"),
               decide("ask_user", "Approve the design?"),
               decide("ready", note_for_discussor="All approved."))
    steps = []
    boot = Bootstrap(run)
    turn = boot.start("I want to study LoRA rank on small data",
                      on_step=lambda node, update: steps.append(node))
    assert turn.reply == "Here are H1, R1 and the protocol. Approve?" and turn.waiting
    assert steps == ["discussor", "orchestrator", "researcher", "orchestrator", "planner",
                     "orchestrator", "discussor"], "then it waits for the user"
    st = boot.research
    assert st["goal"]["question"] == "Does LoRA rank matter?"
    assert [e["id"] for e in st["evidence"]] == ["E1"] and st["hypotheses"][0]["id"] == "H1"

    turn = boot.answer("Looks good, go on", approve=["hypotheses", "rubric", "protocol"])
    assert turn.reply == "The plan has 2 steps. Approve?"
    turn = boot.answer("", approve=["plan"])
    assert turn.reply == "Design ready. Approve?"
    turn = boot.answer("Yes, approved.")
    assert turn.outcome == "ready" and turn.reply == "Bootstrap complete."

    st = boot.research
    assert all(S.approved(st, s) for s in S.APPROVABLE)
    assert [r["node"] for r in st["results"]] == ["researcher", "planner", "planner", "designer"]

    # Context: one long discussor conversation; every other call fresh.
    disc = [c for c in script.calls if c["schema"] == "DiscussorOutput"]
    assert disc[0]["thread"] is None and {c["thread"] for c in disc[1:]} == {disc[1]["thread"]}
    others = [c for c in script.calls if c["schema"] != "DiscussorOutput"]
    assert all(c["thread"] is None for c in others)
    assert all("I want to study LoRA rank" not in c["prompt"] for c in others), \
        "the user's words reach no one but the discussor"

    # Kept on disk: the state, its rendering, its history, every session.
    folder = run.folder
    saved = json.loads((folder / "state" / "research.json").read_text())
    assert saved["design"]["nodes"][1]["name"] == "trainer"
    assert "## Design (approved)" in (folder / "state" / "research.md").read_text()
    assert len(list((folder / "state" / "history").glob("*.json"))) > 10
    sessions = sorted(p.name for p in (folder / "rb" / "sessions").iterdir())
    assert sessions[:2] == ["designer-001", "discussor"] and "orchestrator-008" in sessions
    boot.close()


def test_the_orchestrator_cannot_skip_an_approval(run, script):
    script.add("DiscussorOutput", say("ok", ("other", "Design it now")), say("Need approval first."))
    script.add("OrchestratorOutput", decide("design"))
    turn = Bootstrap(run).start("idea")
    assert turn.reply == "Need approval first."
    told = [c for c in script.calls if c["schema"] == "DiscussorOutput"][1]["prompt"]
    assert "wanted to design, but first the user must approve: plan" in told
    assert not [c for c in script.calls if c["schema"] == "DesignOutput"]


def test_the_team_stops_to_ask_after_its_step_limit(run, script, monkeypatch):
    monkeypatch.setattr(graph, "MAX_STEPS", 2)
    script.add("DiscussorOutput", say("ok", ("research", "dig")), say("We have been busy."))
    script.add("OrchestratorOutput", decide("research", questions=["q"]),
               decide("research", questions=["q"]))
    script.add("ResearcherOutput", FINDINGS, FINDINGS)
    Bootstrap(run).start("idea")
    told = [c for c in script.calls if c["schema"] == "DiscussorOutput"][1]["prompt"]
    assert "taken 2 steps since the user last spoke" in told
    assert len([c for c in script.calls if c["schema"] == "OrchestratorOutput"]) == 2


def test_relaying_the_orchestrator_never_starts_more_work(run, script):
    # The discussor raises a request while telling the user something: it
    # waits for the user, instead of looping back to the orchestrator.
    script.add("DiscussorOutput", say("ok", ("research", "dig")),
               say("Question for you", ("research", "more")))
    script.add("OrchestratorOutput", decide("ask_user", "What budget?"))
    turn = Bootstrap(run).start("idea")
    assert turn.reply == "Question for you" and turn.waiting
    assert len([c for c in script.calls if c["schema"] == "OrchestratorOutput"]) == 1


def test_a_run_resumes_from_its_folder_and_can_be_left(run, script):
    script.add("DiscussorOutput", say("What budget?"), say("Noted: one GPU.",
                                                           constraints={"compute": "1 GPU"}))
    boot = Bootstrap(run)
    boot.start("idea")
    boot.close()

    again = Bootstrap(run)
    assert again.started and again.last_turn.reply == "What budget?"
    turn = again.answer("one GPU")
    assert turn.reply == "Noted: one GPU." and again.research["constraints"]["compute"] == "1 GPU"
    with pytest.raises(RuntimeError, match="already started"):
        again.start("another idea")
    left = again.exit()
    assert left.outcome == "exit" and not left.waiting


def test_a_failing_worker_is_reported_to_the_orchestrator(run, script):
    script.add("DiscussorOutput", say("ok", ("research", "dig")), say("The search failed."))
    bad = {"summary": "s", "findings": [{"claim": "x", "source": ""}]}
    script.add("ResearcherOutput", bad, bad, bad)
    script.add("OrchestratorOutput", decide("research", questions=["q"]),
               lambda prompt: decide("ask_user", "Retry?") if "researcher failed" in prompt
               else decide("research"))
    Bootstrap(run).start("idea")
    orch = [c for c in script.calls if c["schema"] == "OrchestratorOutput"]
    assert "researcher failed: researcher gave no usable answer" in orch[1]["prompt"]


def test_an_approval_alone_lets_the_work_move_on(run, script):
    script.add("DiscussorOutput", say("ok", ("research", "dig")), say("Approve H1?"),
               say("Thanks."), say("Planning is next."))
    script.add("ResearcherOutput", FINDINGS)
    script.add("HypothesizeOutput", HYPO)
    script.add("OrchestratorOutput", decide("research", questions=["q"]), decide("hypothesize"),
               decide("ask_user", "Approve?"), decide("ask_user", "Shall I plan?"))
    boot = Bootstrap(run)
    boot.start("idea")
    turn = boot.answer("", approve=["hypotheses", "rubric", "protocol"])
    assert turn.reply == "Planning is next."
    assert len([c for c in script.calls if c["schema"] == "OrchestratorOutput"]) == 4
    told = [c for c in script.calls if c["schema"] == "DiscussorOutput"][2]["prompt"]
    assert "(I approved: hypotheses, rubric, protocol.)" in told
