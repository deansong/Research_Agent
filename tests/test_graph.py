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
from research_agent.bootstrap.config import ROLES, RoleConfig
from tests.scripted import script  # noqa: F401 -- fixture


@pytest.fixture
def run(tmp_path):
    return nodes.Run(folder=tmp_path / "run", roles={r: RoleConfig("fake") for r in ROLES})


def say(reply, *requests, approve=(), **fields):
    """A discussor answer. It hands to the orchestrator when it has a request
    or an approval for it, unless `hand_to` says otherwise."""
    fields.setdefault("hand_to", "orchestrator" if requests or approve else "user")
    return {"reply": reply, "approve": list(approve),
            "requests": [{"kind": k, "detail": d} for k, d in requests], **fields}


def decide(action, task="", **fields):
    return {"action": action, "reason": f"because {action}", "task": task or f"do {action}",
            "summary": f"after {action}", **fields}


FINDINGS = {"summary": "found baselines", "findings": [
    {"claim": "LoRA rank 8 is common", "source": "https://example.org/lora"}]}
HYPO = {"summary": "one hypothesis", "hypotheses": [
    {"plain": "Higher LoRA rank helps on small data.", "statement": "Rank matters",
     "prediction": "r=64 beats r=4 by >=1 F1", "evidence": ["E1"]}],
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
               decide("ask_user", "Approve the design?"))
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
    assert sessions[:2] == ["designer-001", "discussor"] and sessions[-1] == "researcher-001"
    assert "orchestrator-007" in sessions and "orchestrator-008" not in sessions, \
        "with the design approved, no model is asked whether to finish"
    assert st["status"]["phase"] == "ready"
    told = script.prompts("DiscussorOutput")[-1]
    assert "**Start execution**" in told and f"execute --run {run.folder}" in told, \
        "the user is told how to start the execution"
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


def test_asking_for_a_change_after_ready_reopens_the_work(run, script):
    st, _ = S.apply(S.new(), [{"op": "set", "section": "design", "value": {"summary": "d"}}],
                    by="x")
    st, _ = S.apply(st, [{"op": "approve", "section": "design"}], by="user")
    S.Store(run.folder).write(st)
    script.add("DiscussorOutput", say("Done already.", approve=[]),
               say("I'll ask the designer.", ("revise_design", "Use two runners")),
               say("Revised; approve?"))
    script.add("OrchestratorOutput", decide("ask_user", "Approve the revised design?"))
    boot = Bootstrap(run)
    boot.start("hello")                         # just talking: no work starts
    turn = boot.answer("Please use two runners instead")
    assert turn.reply == "Revised; approve?"
    assert len(script.prompts("OrchestratorOutput")) == 1, "a request reopens the work"


def test_the_discussor_answers_a_clarifying_question_itself(run, script):
    script.add("DiscussorOutput",
               say("Researching first.", ("research", "survey methods")),
               say("Here is what the team found. Questions?"),
               say("F_s is the set-level F-score, see E3.", reason="a clarification"))
    script.add("OrchestratorOutput", decide("research", questions=["q"]),
               decide("ask_user", "Anything to clarify?"))
    script.add("ResearcherOutput", FINDINGS)
    boot = Bootstrap(run)
    boot.start("open-vocabulary emotion classification")
    turn = boot.answer("What does F_s mean?")
    assert turn.reply == "F_s is the set-level F-score, see E3."
    assert len(script.prompts("OrchestratorOutput")) == 2, "the team was not called again"


def test_a_request_can_wait_while_the_discussor_asks_first(run, script):
    script.add("DiscussorOutput",
               say("Which modality, text or video?", ("research", "survey OV-MER"),
                   hand_to="user"),
               say("Text it is; researching.", ("research", "text only")),
               say("ok?"))
    script.add("OrchestratorOutput", decide("ask_user", "ok?"))
    boot = Bootstrap(run)
    boot.start("open-vocabulary emotion classification")
    assert not script.prompts("OrchestratorOutput"), "held until the user answers"
    boot.answer("text")
    brief = script.prompts("OrchestratorOutput")[0]
    assert "- [research] survey OV-MER" in brief and "- [research] text only" in brief


def test_handing_over_with_nothing_to_hand_is_sent_back(run, script):
    script.add("DiscussorOutput", say("ok", hand_to="orchestrator"), say("What data?"))
    turn = Bootstrap(run).start("idea")
    assert turn.reply == "What data?"
    retry = script.prompts("DiscussorOutput")[1]
    assert 'hand_to is "orchestrator" but there is no request or approval' in retry


def test_the_discussor_discusses_and_still_sends_research():
    from research_agent.bootstrap.prompts import DISCUSSOR

    assert "AND send research to the team" in DISCUSSOR
    assert "When they ask what you think\n  or know, say it" in DISCUSSOR
    assert "Your knowledge is not evidence" in DISCUSSOR
    assert "never invent a title" in DISCUSSOR


def test_research_starts_open_from_the_newest_evidence():
    from datetime import date

    from research_agent.bootstrap import prompts, views

    assert "as candidates, never as limits" in prompts.DISCUSSOR
    assert "The first research on a topic is a landscape scan" in prompts.ORCHESTRATOR
    assert "suggestions, not limits" in prompts.RESEARCHER
    today = f"Today is {date.today().isoformat()}."
    assert views.researcher(S.new(), task="t").startswith(today)
    assert views.orchestrator(S.new(), requests=[]).startswith(today)


def test_the_user_writes_while_the_team_works(run, script):
    boot = Bootstrap(run)

    def researching(prompt):
        out = boot.aside("While you search: also look at text-only datasets.",
                         activity="the researcher is searching")
        assert out.reply == "Noted; the team will look at text-only datasets too."
        return FINDINGS

    script.add("DiscussorOutput", say("Researching.", ("research", "survey")), say("Found it."))
    script.add("ResearcherOutput", researching)
    script.add("BootstrapAside", {"reply": "Noted; the team will look at text-only datasets too.",
                                  "requests": [{"kind": "research",
                                                "detail": "Include text-only datasets"}]})
    script.add("OrchestratorOutput", decide("research", questions=["q"]),
               decide("ask_user", "Anything else?"))
    boot.start("open-vocabulary emotion")
    aside = script.prompts("BootstrapAside")[0]
    assert "AN ASIDE" in aside and "the researcher is searching" in aside
    second = script.prompts("OrchestratorOutput")[1]
    assert "- [research] Include text-only datasets" in second
    threads = [c["thread"] for c in script.calls
               if c["schema"] in ("DiscussorOutput", "BootstrapAside")]
    assert len(set(t for t in threads[1:])) == 1, "one conversation"


def test_moving_the_discussor_to_another_provider_hands_the_conversation_over(run, script):
    from research_agent import chat

    script.add("DiscussorOutput", say("Which dataset?"), say("SST-2, noted."))
    boot = Bootstrap(run)
    boot.start("LoRA rank on small data")
    run.roles["discussor"] = RoleConfig("claude_code", "opus")       # was fake
    boot.answer("SST-2 please")
    handed = script.prompts("DiscussorOutput")[1]
    assert "=== YOU TAKE OVER THIS CONVERSATION ===" in handed      # after the skills list
    assert handed.index("YOU TAKE OVER") < handed.index("=== THE USER SAYS ===")
    assert "[user] LoRA rank on small data" in handed and "[discussor] Which dataset?" in handed
    assert "=== THE USER SAYS ===\nSST-2 please" in handed
    assert "=== THE RESEARCH STATE ===" in handed, "the whole state, as for a first turn"
    sessions = sorted(p.name for p in (run.folder / "rb" / "sessions").iterdir())
    assert "discussor-fake-001" in sessions and "discussor" in sessions
    calls = [c for c in script.calls if c["schema"] == "DiscussorOutput"]
    assert calls[1]["thread"] is None, "a new conversation on the new provider"
    said = [(m["role"], m["text"]) for m in chat.transcript(run.folder)]
    assert said == [("user", "LoRA rank on small data"), ("discussor", "Which dataset?"),
                    ("user", "SST-2 please"), ("discussor", "SST-2, noted.")]


def test_changing_the_discussor_s_model_moves_its_conversation_on(run, script):
    script.add("DiscussorOutput", say("Which dataset?"), say("Noted."))
    boot = Bootstrap(run)
    boot.start("LoRA rank")
    run.roles["discussor"] = RoleConfig("fake", "smaller", "low")      # same provider
    boot.answer("SST-2")
    from research_backend import conversation

    session = conversation.open_session("discussor", home=run.rb_home)
    assert (session.model, session.effort) == ("smaller", "low")
    calls = [c for c in script.calls if c["schema"] == "DiscussorOutput"]
    assert calls[1]["thread"] == calls[0]["thread"] or calls[0]["thread"] is None, \
        "the same conversation, continued"


def _autopilot(folder, **fields):
    from research_agent import autopilot

    folder.mkdir(parents=True, exist_ok=True)
    autopilot.save(folder, autopilot.Settings(on=True, **fields))


def AUTO(answer, approve=(), **fields):
    return {"answer": answer, "why": "the user wants it done", "approve": list(approve), **fields}


def test_autopilot_takes_a_bootstrap_to_an_approved_design_alone(run, script):
    from research_agent import autopilot

    _autopilot(run.folder)
    script.add("DiscussorOutput",
               say("Let me look into it.", ("research", "Find LoRA rank baselines")),
               say("Here are H1, R1 and the protocol. Approve?"),
               say("Planning now.", ("proceed", "Write the plan")),
               say("The plan has 2 steps. Approve?"),
               say("Designing.", ("proceed", "Design it")),
               say("Design ready. Approve?"),
               say("All approved."),
               say("Bootstrap complete."))
    script.add("ResearcherOutput", FINDINGS)
    script.add("HypothesizeOutput", HYPO)
    script.add("PlanOutput", PLAN)
    script.add("DesignOutput", DESIGN)
    script.add("OrchestratorOutput",
               decide("research", questions=["LoRA rank results?"]), decide("hypothesize"),
               decide("ask_user", "Approve?"), decide("plan"), decide("ask_user", "Approve the plan?"),
               decide("design"), decide("ask_user", "Approve the design?"))
    script.add("AutopilotOutput",
               AUTO("Approved, plan it.", ["hypotheses", "rubric", "protocol"]),
               AUTO("The plan is fine.", ["plan"]),
               AUTO("Approve the design.", ["design"]))
    boot = Bootstrap(run)
    turn = boot.start("I want to study LoRA rank on small data")
    assert turn.outcome == "ready", "nobody answered: the autopilot did, three times"
    st = boot.research
    assert all(S.approved(st, s) for s in S.APPROVABLE)
    assert [d["by"] for d in st["decisions"]].count("autopilot") == 3
    said = [p for p in script.prompts("DiscussorOutput") if "[autopilot]" in p]
    assert len(said) == 3 and "[autopilot] Approved, plan it." in said[0]
    auto = [c for c in script.calls if c["schema"] == "AutopilotOutput"]
    assert "THE DISCUSSOR ASKS THE USER" in auto[0]["prompt"]
    assert "Here are H1, R1 and the protocol. Approve?" in auto[0]["prompt"]
    assert auto[0]["thread"] is None and auto[1]["thread"] and auto[2]["thread"] == auto[1]["thread"], \
        "the autopilot keeps one conversation of its own"
    assert autopilot.load(run.folder).streak == 3
    boot.close()


def test_autopilot_hands_back_after_its_limit(run, script):
    from research_agent import autopilot

    _autopilot(run.folder, max_decisions=1)
    script.add("DiscussorOutput", say("Approve H1?"), say("Thanks. Anything else?"),
               say("Noted. Shall I go on?"), say("Going on."))
    script.add("AutopilotOutput", AUTO("Yes, but keep it small."), AUTO("Yes."))
    boot = Bootstrap(run)
    turn = boot.start("idea")
    assert turn.waiting and turn.reply == "Thanks. Anything else?", "the second stop is the user's"
    assert "handed back to you after 1 decisions" in autopilot.load(run.folder).handed_back
    # The user answers: the count starts again, and the autopilot answers the
    # next stop -- then hands back once more.
    turn = boot.answer("Carry on.")
    assert turn.waiting and turn.reply == "Going on."
    assert len([c for c in script.calls if c["schema"] == "AutopilotOutput"]) == 2
    assert autopilot.load(run.folder).streak == 1 and autopilot.load(run.folder).handed_back
    boot.close()
