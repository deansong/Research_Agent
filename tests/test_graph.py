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
DESIGN = {"summary": "two steps, three nodes", "hypotheses": [
    {"id": "H1", "plain": "Higher LoRA rank helps on small data.", "statement": "Rank matters",
     "prediction": "r=64 beats r=4 by >=1 F1", "evidence": ["E1"]}],
    "rubric": [{"id": "R1", "criterion": "Effect", "measure": "F1 gap", "threshold": ">=1"}],
    "protocol": {"metrics": ["F1"], "statistics": "paired t-test", "seeds": "5"},
    "steps": [
        {"id": "1", "title": "Data", "detail": "load SST-2", "check": "rows counted"},
        {"id": "2", "title": "Train", "detail": "ranks 4..64", "check": "F1 logged",
         "depends_on": ["1"], "rubric": ["R1"]}],
    "nodes": [
        {"name": "orchestrator", "role": "orchestrator", "instructions": "run it"},
        {"name": "trainer", "role": "worker", "steps": ["1", "2"], "access": "write",
         "instructions": "train", "validated_by": ["R1"]},
        {"name": "validator", "role": "validator", "instructions": "check"}]}


def test_a_whole_bootstrap_from_idea_to_an_approved_design(run, script):
    script.add("DiscussorOutput",
               say("Let me look into it.", ("research", "Find LoRA rank baselines"),
                   goal={"question": "Does LoRA rank matter?"}),
               say("Rank 8 is common. Brief: compare ranks 4-64 on SST-2. Approve?",
                   brief="Compare LoRA ranks 4 to 64 on SST-2, two seeds."),
               say("Brief approved.", approve=["brief"]),
               say("Design ready: H1, R1, two steps, three nodes. Approve?"),
               say("All done.", approve=["design"]),
               say("Bootstrap complete."))
    script.add("ResearcherOutput", FINDINGS)
    script.add("DesignOutput", DESIGN)
    script.add("OrchestratorOutput",
               decide("research", questions=["LoRA rank results?"]),
               decide("brief", "Cover the rank baselines"),
               decide("design"))
    steps = []
    boot = Bootstrap(run)
    turn = boot.start("I want to study LoRA rank on small data",
                      on_step=lambda node, update: steps.append(node))
    assert turn.reply.startswith("Rank 8 is common") and turn.waiting
    assert steps == ["discussor", "orchestrator", "researcher", "orchestrator", "discussor"]
    told = script.prompts("DiscussorOutput")[1]
    assert "=== WHAT THE RESEARCHER REPORTED ===" in told and "found baselines" in told, \
        "the discussor gets everything the team found, to explain"
    assert "Cover the rank baselines" in told and "write the design brief in `brief`" in told
    assert boot.research["brief"]["text"].startswith("Compare LoRA ranks")

    steps.clear()
    turn = boot.answer("Yes, that's the brief.", on_step=lambda node, update: steps.append(node))
    assert turn.reply.startswith("Design ready") and turn.waiting
    assert steps == ["human", "discussor", "orchestrator", "designer", "orchestrator", "discussor"]
    designed = script.prompts("DesignOutput")[0]
    assert "=== THE APPROVED BRIEF -- design from this ===\nCompare LoRA ranks" in designed
    assert "found baselines" not in designed, "the researcher's report is not the designer's"
    st = boot.research
    assert S.approved(st, "brief")
    assert st["goal"]["question"] == "Does LoRA rank matter?"
    assert [e["id"] for e in st["evidence"]] == ["E1"] and st["hypotheses"][0]["id"] == "H1"
    assert [s["id"] for s in st["plan"]["steps"]] == ["1", "2"] and st["rubric"][0]["id"] == "R1"
    assert not S.approved(st, "design")
    told = script.prompts("DiscussorOutput")[3]
    assert "The designer delivered design v1" in told and "It is theirs to judge" in told, \
        "a new design goes to the user: no model judges whether it is ready"
    assert st["status"]["summary"] == "Design v1 is ready for the user's review."

    turn = boot.answer("Yes, approved.")
    assert turn.outcome == "ready" and turn.reply == "Bootstrap complete."

    st = boot.research
    assert S.approved(st, "design")
    assert all(S.approved(st, s) for s in S.PARTS), "one approval covers every part"
    assert [r["node"] for r in st["results"]] == ["researcher", "designer"]

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
    assert len(list((folder / "state" / "history").glob("*.json"))) > 5
    sessions = sorted(p.name for p in (folder / "rb" / "sessions").iterdir())
    assert sessions[:2] == ["designer-001", "discussor"] and sessions[-1] == "researcher-001"
    assert "orchestrator-003" in sessions and "orchestrator-004" not in sessions, \
        "with the design approved, no model is asked whether to finish"
    assert st["status"]["phase"] == "ready"
    told = script.prompts("DiscussorOutput")[-1]
    assert "**Start execution**" in told and f"execute --run {run.folder}" in told, \
        "the user is told how to start the execution"
    boot.close()


def test_the_orchestrator_cannot_skip_an_approval(run, script):
    script.add("DiscussorOutput", say("ok", ("other", "Finish now")), say("Need approval first."))
    script.add("OrchestratorOutput", decide("ready"))
    turn = Bootstrap(run).start("idea")
    assert turn.reply == "Need approval first." and turn.outcome != "ready"
    told = [c for c in script.calls if c["schema"] == "DiscussorOutput"][1]["prompt"]
    assert "wanted to ready, but first the user must approve: design" in told


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


def test_no_design_before_the_user_approves_the_brief(run, script):
    script.add("DiscussorOutput", say("ok", ("research", "dig")),
               say("Here is the brief. Approve it?", brief="Build a small benchmark."),
               say("Thanks."), say("Design ready. Approve?"))
    script.add("ResearcherOutput", FINDINGS)
    script.add("DesignOutput", DESIGN)
    script.add("OrchestratorOutput", decide("research", questions=["q"]), decide("design"),
               decide("design"))
    boot = Bootstrap(run)
    boot.start("idea")
    assert not script.prompts("DesignOutput"), "the orchestrator's design waits for a brief"
    assert "=== WHAT THE RESEARCHER REPORTED ===" in script.prompts("DiscussorOutput")[1]
    turn = boot.answer("", approve=["brief"])
    assert turn.reply == "Design ready. Approve?"
    told = script.prompts("DiscussorOutput")[2]
    assert "(I approved: brief.)" in told
    assert "Build a small benchmark." in script.prompts("DesignOutput")[0]


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


def test_the_instructions_are_minimal_and_keep_what_code_needs():
    from datetime import date

    from research_agent.bootstrap import prompts, views
    from research_agent.execute import prompts as exec_prompts

    assert set(prompts.INSTRUCTIONS) == {"discussor", "orchestrator", "researcher", "designer"}
    for text in (*prompts.INSTRUCTIONS.values(), *exec_prompts.INSTRUCTIONS.values()):
        assert len(text) < 1200, "a role's instructions stay short"
    def said(text):
        return " ".join(text.split())

    assert "never invent a title" in said(prompts.DISCUSSOR)
    assert '`approve` "brief" or "design"' in said(prompts.DISCUSSOR)
    assert "names its source" in said(prompts.RESEARCHER)
    assert "what you leave out goes" in said(prompts.DESIGNER)
    today = f"Today is {date.today().isoformat()}."
    assert views.researcher(S.new(), task="t").startswith(today)
    assert views.orchestrator(S.new(), requests=[]).startswith(today)
    assert views.designer(S.new(), task="t", skills=[]).startswith(today)


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
               say("Brief: compare ranks. Approve?", brief="Compare LoRA ranks."),
               say("Brief approved; designing."),
               say("Design ready. Approve?"),
               say("Revising the design.", ("revise_design", "Five seeds")),
               say("Revised design ready. Approve?"),
               say("All approved."),
               say("Bootstrap complete."))
    script.add("ResearcherOutput", FINDINGS)
    script.add("DesignOutput", DESIGN, DESIGN)
    script.add("OrchestratorOutput",
               decide("research", questions=["LoRA rank results?"]), decide("brief"),
               decide("design"), decide("design"))
    script.add("AutopilotOutput",
               AUTO("That brief is right.", ["brief"]),
               AUTO("Use five seeds."),
               AUTO("Approve the design.", ["design"]))
    boot = Bootstrap(run)
    turn = boot.start("I want to study LoRA rank on small data")
    assert turn.outcome == "ready", "nobody answered: the autopilot did, three times"
    st = boot.research
    assert S.approved(st, "design")
    assert S.approved(st, "brief")
    assert [d["by"] for d in st["decisions"]].count("autopilot") == 3
    said = [p for p in script.prompts("DiscussorOutput") if "[autopilot]" in p]
    assert len(said) == 3 and "[autopilot] Use five seeds." in said[1]
    auto = [c for c in script.calls if c["schema"] == "AutopilotOutput"]
    assert "THE DISCUSSOR ASKS THE USER" in auto[0]["prompt"]
    assert "Brief: compare ranks. Approve?" in auto[0]["prompt"]
    assert auto[0]["thread"] is None and auto[1]["thread"], \
        "the autopilot keeps one conversation of its own"
    assert autopilot.load(run.folder).streak == 3
    assert len(script.prompts("DesignOutput")) == 2 and S.version(st, "design") == 2
    assert len(script.prompts("OrchestratorOutput")) == 4, "no model judges a new design"
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
