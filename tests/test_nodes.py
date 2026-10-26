"""
WHAT:  The bootstrap nodes, each against a scripted backend: what it is sent,
       what lands in the research state, and what happens to a bad answer.
WHY:   The point of the redesign is context: the discussor keeps the
       conversation, every other node starts clean with a brief from the
       state. So the briefs and sessions are what is tested.

Run with:   python -m pytest tests/test_nodes.py -q
"""

from __future__ import annotations

import pytest

from research_agent import state as S
from research_agent.bootstrap import nodes, verify, views
from research_agent.bootstrap.config import ROLES, RoleConfig
from tests.scripted import script  # noqa: F401 -- fixture


@pytest.fixture
def run(tmp_path):
    return nodes.Run(folder=tmp_path, roles={r: RoleConfig("fake") for r in ROLES})


def _apply(st, outcome, by):
    return S.apply(st, outcome.ops, by=by)[0]


DISCUSS = {"reply": "What GPU budget do you have?", "goal": {"question": "Does LoRA rank matter?"},
           "constraints": {"compute": "1 A100"}, "decisions": [{"what": "Use SST-2", "why": "small"}],
           "requests": [{"kind": "research", "detail": "Find LoRA baselines on SST-2"}]}


def test_the_discussor_keeps_one_conversation_and_records_what_the_user_settled(run, script):
    script.add("DiscussorOutput", DISCUSS, {"reply": "Noted."})
    st = S.new()
    turn = nodes.discussor(run, st, seen={}, user="I want to study LoRA rank")
    assert turn.reply == "What GPU budget do you have?"
    assert turn.requests == [{"kind": "research", "detail": "Find LoRA baselines on SST-2"}]
    st = _apply(st, turn, "discussor")
    assert st["goal"]["question"] == "Does LoRA rank matter?"
    assert st["constraints"]["compute"] == "1 A100"
    assert st["decisions"][0]["by"] == "user"

    first = script.calls[0]["prompt"]
    assert "=== THE USER SAYS ===\nI want to study LoRA rank" in first
    assert "=== THE RESEARCH STATE ===" in first, "the first turn sees the whole state"
    nodes.discussor(run, st, seen=dict(st["versions"]), user="one A100, two weeks")
    second = script.calls[1]
    assert second["thread"] == "thread-1", "the same conversation continues"
    assert "THE RESEARCH STATE" not in second["prompt"] and "CHANGED SINCE" not in second["prompt"]
    assert turn.session == "discussor"


def test_other_nodes_start_clean_every_time(run, script):
    decide = {"action": "research", "reason": "no evidence yet", "task": "Find baselines",
              "questions": ["LoRA results on SST-2?"], "summary": "Just started."}
    script.add("OrchestratorOutput", decide, decide)
    st = S.new()
    one = nodes.orchestrator(run, st, requests=[{"kind": "research", "detail": "baselines"}])
    two = nodes.orchestrator(run, st, requests=[])
    assert (one.session, two.session) == ("orchestrator-001", "orchestrator-002")
    assert [c["thread"] for c in script.calls] == [None, None], "a new conversation each call"
    assert "- [research] baselines" in script.calls[0]["prompt"]
    st = _apply(st, one, "orchestrator")
    assert st["status"]["summary"] == "Just started." and st["status"]["next"] == "research"


def test_the_researcher_s_sources_are_checked(run, script, monkeypatch):
    monkeypatch.setattr(verify, "_paper", lambda tools, arxiv: arxiv == "2106.09685")
    st = _apply(S.new(), nodes.Outcome(ops=[{"op": "add", "section": "hypotheses",
                                              "value": {"statement": "Rank matters"}}]), "x")
    script.add("ResearcherOutput", {
        "summary": "LoRA is standard.",
        "findings": [{"claim": "LoRA matches fine-tuning", "source": "arXiv:2106.09685",
                      "bears_on": ["H1"]},
                     {"claim": "Made up", "source": "arXiv:2199.00001"},
                     {"claim": "SST-2 has 67k train", "source": "glue/sst2"}],
        "novelty": [{"hypothesis": "H1", "note": "studied for GPT-3, not small models"}],
        "unanswered": ["cost per run on an A100"]})
    out = nodes.researcher(run, st, task="baselines", questions=["LoRA on SST-2?"])
    st = _apply(st, out, "researcher")
    assert [e["verified"] for e in st["evidence"]] == [True, False, None]
    assert st["hypotheses"][0]["novelty"].startswith("studied for GPT-3")
    assert [(q["id"], q["text"]) for q in st["open_questions"]] == [
        ("Q1", "LoRA on SST-2?"), ("Q2", "cost per run on an A100")]
    assert out.summary.startswith("3 findings (1 sources verified, 1 not found)")
    assert st["results"][0]["node"] == "researcher" and st["results"][0]["session"] == "researcher-001"
    call = script.calls[0]
    assert call["access"].value == "write" and call["workdir"].endswith("research")
    assert "=== THE QUESTIONS -- answer each in `answers` by its id ===\n- Q1 LoRA on SST-2?" \
        in call["prompt"]


HYPO = {"summary": "two hypotheses", "hypotheses": [
    {"statement": "Rank matters", "prediction": "r=64 beats r=4 by >=1 F1", "evidence": ["E1"]}],
    "rubric": [{"criterion": "Effect size", "measure": "F1 difference", "threshold": ">=1"}],
    "protocol": {"baselines": ["full fine-tuning"], "metrics": ["F1"], "statistics": "paired t-test",
                 "seeds": "5 seeds", "stop_criteria": "all ranks run"}}


def test_a_bad_answer_goes_back_with_what_is_wrong(run, script):
    bad = {**HYPO, "hypotheses": [{**HYPO["hypotheses"][0], "evidence": ["E9"]}]}
    script.add("HypothesizeOutput", bad, HYPO)
    st = _apply(S.new(), nodes.Outcome(ops=[{"op": "add", "section": "evidence", "value": {
        "claim": "c", "source": "s"}}]), "x")
    out = nodes.hypothesize(run, st, task="write hypotheses")
    retry = script.calls[1]
    assert retry["thread"] == "thread-1", "the retry continues the same call"
    assert "cites E9, which is not an evidence id" in retry["prompt"]
    st = _apply(st, out, "planner")
    assert st["hypotheses"][0]["id"] == "H1" and st["rubric"][0]["id"] == "R1"
    assert st["protocol"]["statistics"] == "paired t-test"

    script.add("HypothesizeOutput", bad, bad, bad)
    with pytest.raises(nodes.NodeFailed, match="no usable answer in 3 attempts"):
        nodes.hypothesize(run, st, task="again")


def test_a_draft_s_own_numbering_means_new_items(run, script):
    st = _apply(S.new(), nodes.Outcome(ops=[
        {"op": "add", "section": "evidence", "value": {"claim": "c", "source": "s"}},
        {"op": "add", "section": "hypotheses", "value": {"statement": "old"}}]), "x")
    revised = {**HYPO, "hypotheses": [
        {**HYPO["hypotheses"][0], "id": "H1", "statement": "old, sharpened"},
        {**HYPO["hypotheses"][0], "id": "H7", "statement": "brand new"}],
        "rubric": [{**HYPO["rubric"][0], "id": "R3"}]}
    script.add("HypothesizeOutput", revised)
    st = _apply(st, nodes.hypothesize(run, st, task="revise"), "planner")
    assert len(script.calls) == 1, "no retry"
    assert [(h["id"], h["statement"]) for h in st["hypotheses"]] == [
        ("H1", "old, sharpened"), ("H2", "brand new")]
    assert [r["id"] for r in st["rubric"]] == ["R1"]


def test_the_plan_is_checked_against_the_rubric(run, script):
    st = _apply(S.new(), nodes.Outcome(ops=[{"op": "add", "section": "rubric", "value": {
        "criterion": "Effect"}}]), "x")
    bad = {"summary": "s", "steps": [
        {"id": "1", "title": "Data", "detail": "d", "check": "c"},
        {"id": "3", "title": "Train", "detail": "d", "check": "", "depends_on": ["2"],
         "rubric": ["R7"]}]}
    good = {"summary": "two steps", "steps": [
        {"id": "1", "title": "Data", "detail": "d", "check": "c"},
        {"id": "2", "title": "Train", "detail": "d", "check": "c", "depends_on": ["1"],
         "rubric": ["R1"], "gate": True}]}
    script.add("PlanOutput", bad, good)
    out = nodes.plan(run, st, task="plan it")
    problems = script.calls[1]["prompt"]
    for expected in ('step ids must be "1", "2"', "has no check", "serves R7", "depends on 2"):
        assert expected in problems
    st = _apply(st, out, "planner")
    assert [s["id"] for s in st["plan"]["steps"]] == ["1", "2"] and st["plan"]["steps"][1]["gate"]


def test_the_design_covers_every_step_once(run, script):
    st = _apply(S.new(), nodes.Outcome(ops=[
        {"op": "add", "section": "rubric", "value": {"criterion": "Effect"}},
        {"op": "set", "section": "plan", "value": {"steps": [
            {"id": "1", "title": "a"}, {"id": "2", "title": "b"}]}}]), "x")
    node = {"name": "orchestrator", "role": "orchestrator", "instructions": "run it"}
    bad = {"summary": "s", "nodes": [node,
                                     {"name": "Worker", "role": "worker", "steps": ["1"],
                                      "skills": ["nope"], "instructions": "i"}]}
    good = {"summary": "three nodes", "nodes": [
        node, {"name": "worker", "role": "worker", "steps": ["1", "2"], "access": "write",
               "skills": ["peft"], "instructions": "i", "validated_by": ["R1"]},
        {"name": "validator", "role": "validator", "instructions": "check"}]}
    script.add("DesignOutput", bad, good)
    out = nodes.design(run, st, task="design it")
    problems = script.calls[1]["prompt"]
    for expected in ("Worker is not lower_snake_case", "exactly one validator",
                     "plan step 2 has no node", "no skill called nope"):
        assert expected in problems
    st = _apply(st, out, "designer")
    assert [n["name"] for n in st["design"]["nodes"]] == ["orchestrator", "worker", "validator"]


def test_no_brief_but_the_discussor_s_carries_the_users_words(run, script):
    st = S.new()
    st, _ = S.apply(st, [{"op": "merge", "section": "goal", "value": {"question": "Q?"}},
                         {"op": "set", "section": "plan", "value": {"steps": [
                             {"id": "1", "title": "a"}]}}], by="x")
    user = "my secret aside about the budget"
    briefs = [views.orchestrator(st, requests=[]),
              views.researcher(st, task="t"),
              views.planner(st, job="hypothesize", task="t"),
              views.planner(st, job="plan", task="t"),
              views.designer(st, task="t", skills=[("peft", "LoRA")])]
    assert all(user not in b and "Question: Q?" in b for b in briefs)
    assert user in views.discussor_turn(st, {}, user=user)
    assert "- peft: LoRA" in briefs[-1]


def test_the_researcher_is_never_sent_back_for_a_wrong_reference(run, script):
    # Seen in a real run: no hypotheses yet, yet novelty for H1; answers under
    # Q5, which did not exist. Ten minutes of research must not be redone for that.
    st = S.new()
    script.add("ResearcherOutput", lambda prompt: {
        "summary": "survey", "findings": [{"claim": "c", "source": "glue/sst2"}],
        "novelty": [{"hypothesis": "H1", "note": "dual encoders now win"}],
        "answers": [{"id": "Q1", "answer": "the first answer"},
                    {"id": "Q5", "answer": "datasets: GoEmotions, ISEAR"}]})
    out = nodes.researcher(run, st, task="survey", questions=["Which methods?", "Which methods?"])
    assert len(script.calls) == 1, "no retry"
    brief = script.calls[0]["prompt"]
    assert "None yet: leave `novelty` empty" in brief
    st = _apply(st, out, "researcher")
    questions = {q["id"]: q for q in st["open_questions"]}
    assert questions["Q1"]["text"] == "Which methods?" and questions["Q1"]["status"] == "answered"
    assert len(questions) == 2, "a repeated question is filed once"
    assert questions["Q2"]["answer"] == "datasets: GoEmotions, ISEAR"
    assert "under Q5" in questions["Q2"]["text"]
    assert st["evidence"][1]["claim"] == "Novelty: dual encoders now win"
    assert st["evidence"][1]["verified"] is None
    assert out.summary.endswith("(Kept, though filed under unknown ids: H1, Q5.)")


def test_rubric_criteria_can_be_retired(run, script):
    st = _apply(S.new(), nodes.Outcome(ops=[
        {"op": "add", "section": "evidence", "value": {"claim": "c", "source": "s"}},
        *[{"op": "add", "section": "rubric", "value": {"criterion": f"old {i}"}} for i in range(3)]]),
        "x")
    unknown = {**HYPO, "retire_rubric": ["R9"]}
    empty = {**HYPO, "rubric": [{"id": "R1", "criterion": " ", "measure": "", "threshold": ""}]}
    good = {**HYPO, "rubric": [{**HYPO["rubric"][0], "id": "R1"}], "retire_rubric": ["R2", "R3"]}
    script.add("HypothesizeOutput", unknown, empty, good)
    out = nodes.hypothesize(run, st, task="retire the stale criteria")
    assert "retire_rubric names R9, which is not a rubric id (there are: R1, R2, R3)" in script.calls[1]["prompt"]
    assert "rubric criterion R1 is empty" in script.calls[2]["prompt"]
    st = _apply(st, out, "planner")
    assert [(r["id"], r["criterion"]) for r in st["rubric"]] == [("R1", "Effect size")]
    assert "(retired R2, R3)" in out.summary
