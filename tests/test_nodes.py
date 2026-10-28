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


NODES = [{"name": "orchestrator", "role": "orchestrator", "instructions": "run it"},
         {"name": "trainer", "role": "worker", "steps": ["1", "2"], "access": "write",
          "skills": ["peft"], "instructions": "train", "validated_by": ["R1"]},
         {"name": "validator", "role": "validator", "instructions": "check"}]
DESIGN = {"summary": "two steps, three nodes", "hypotheses": [
    {"id": "H1", "plain": "Higher LoRA rank helps on small data.", "statement": "Rank matters",
     "prediction": "r=64 beats r=4 by >=1 F1", "evidence": ["E1"]}],
    "rubric": [{"id": "R1", "criterion": "Effect size", "measure": "F1 difference",
                "threshold": ">=1"}],
    "protocol": {"baselines": ["full fine-tuning"], "metrics": ["F1"],
                 "statistics": "paired t-test", "seeds": "5 seeds"},
    "steps": [{"id": "1", "title": "Data", "detail": "load SST-2", "check": "rows counted"},
              {"id": "2", "title": "Train", "detail": "ranks 4..64", "check": "F1 logged",
               "depends_on": ["1"], "rubric": ["R1"], "gate": True}],
    "nodes": NODES}


@pytest.fixture
def evidence():
    return _apply(S.new(), nodes.Outcome(ops=[{"op": "add", "section": "evidence", "value": {
        "claim": "c", "source": "s"}}]), "x")


def test_the_designer_writes_the_whole_design_in_one_call(run, script, evidence, monkeypatch):
    monkeypatch.setattr(nodes.Run, "catalogue", lambda self: [("peft", "LoRA")])
    script.add("DesignOutput", DESIGN)
    out = nodes.design(run, evidence, task="design it")
    st = _apply(evidence, out, "designer")
    assert st["hypotheses"][0]["id"] == "H1" and st["rubric"][0]["id"] == "R1"
    assert st["protocol"]["statistics"] == "paired t-test"
    assert [s["id"] for s in st["plan"]["steps"]] == ["1", "2"] and st["plan"]["steps"][1]["gate"]
    assert [n["name"] for n in st["design"]["nodes"]] == ["orchestrator", "trainer", "validator"]
    assert out.summary.startswith("a design of 2 steps and 3 nodes, 1 hypotheses, 1 criteria")
    brief = script.calls[0]["prompt"]
    assert "=== EVIDENCE (cite by id) ===" in brief and "- peft: LoRA" in brief


def test_a_bad_design_goes_back_with_what_is_wrong(run, script, evidence, monkeypatch):
    monkeypatch.setattr(nodes.Run, "catalogue", lambda self: [("peft", "LoRA")])
    bad = {**DESIGN,
           "hypotheses": [{**DESIGN["hypotheses"][0], "evidence": ["E9"]}],
           "steps": [DESIGN["steps"][0],
                     {"id": "3", "title": "Train", "detail": "d", "check": "c",
                      "depends_on": ["2"], "rubric": ["R7"]}],
           "nodes": [NODES[0], {"name": "Worker", "role": "worker", "steps": ["1"],
                                "skills": ["nope"], "instructions": "i",
                                "validated_by": ["checker"]}]}
    script.add("DesignOutput", bad, DESIGN)
    nodes.design(run, evidence, task="design it")
    retry = script.calls[1]
    assert retry["thread"] == "thread-1", "the retry continues the same call"
    for expected in ("cites E9, which is not an evidence id", 'step ids must be "1", "2"',
                     "serves R7", "depends on 2", "Worker is not lower_snake_case",
                     "exactly one validator", "no skill called nope",
                     "`validated_by` lists rubric ids (R1), not checker"):
        assert expected in retry["prompt"]

    script.add("DesignOutput", bad, bad, bad)
    with pytest.raises(nodes.NodeFailed, match="no usable answer in 3 attempts"):
        nodes.design(run, evidence, task="again")


def test_a_revision_replaces_the_design_and_what_it_leaves_out_goes(run, script, evidence,
                                                                    monkeypatch):
    monkeypatch.setattr(nodes.Run, "catalogue", lambda self: [("peft", "LoRA")])
    two = {**DESIGN, "hypotheses": [*DESIGN["hypotheses"],
                                    {**DESIGN["hypotheses"][0], "id": "H2", "statement": "Data"}],
           "rubric": [*DESIGN["rubric"], {**DESIGN["rubric"][0], "id": "R2", "criterion": "Old"}]}
    script.add("DesignOutput", two)
    st = _apply(evidence, nodes.design(run, evidence, task="first"), "designer")
    revised = {**DESIGN, "hypotheses": [{**DESIGN["hypotheses"][0], "statement": "Sharper"}],
               "rubric": [DESIGN["rubric"][0],
                          {**DESIGN["rubric"][0], "id": "R5", "criterion": "Cost"}]}
    script.add("DesignOutput", revised)
    st = _apply(st, nodes.design(run, st, task="revise"), "designer")
    assert [(h["id"], h["statement"], h["status"]) for h in st["hypotheses"]] == [
        ("H1", "Sharper", "proposed"), ("H2", "Data", "dropped")]
    assert [(r["id"], r["criterion"]) for r in st["rubric"]] == [("R1", "Effect size"),
                                                                 ("R5", "Cost")]
    script.add("DesignOutput", {**revised, "risks": [{"text": "slow"}]},
               {**revised, "risks": [{"text": "too slow"}]})
    st = _apply(st, nodes.design(run, st, task="again"), "designer")
    st = _apply(st, nodes.design(run, st, task="and again"), "designer")
    assert [r["text"] for r in st["risks"]] == ["too slow"], "a revision's risks replace the last"


def test_a_build_needs_no_hypotheses(run, script, evidence, monkeypatch):
    monkeypatch.setattr(nodes.Run, "catalogue", lambda self: [("peft", "LoRA")])
    script.add("DesignOutput", {**DESIGN, "hypotheses": []})
    st = _apply(evidence, nodes.design(run, evidence, task="build a dataset"), "designer")
    assert st["hypotheses"] == [] and st["plan"]["steps"]
    st, _ = S.apply(st, [{"op": "approve", "section": "design"}], by="user")
    assert S.approved(st, "design")


def test_no_brief_but_the_discussor_s_carries_the_users_words(run, script):
    st = S.new()
    st, _ = S.apply(st, [{"op": "merge", "section": "goal", "value": {"question": "Q?"}},
                         {"op": "set", "section": "plan", "value": {"steps": [
                             {"id": "1", "title": "a"}]}}], by="x")
    user = "my secret aside about the budget"
    briefs = [views.orchestrator(st, requests=[]),
              views.researcher(st, task="t"),
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
