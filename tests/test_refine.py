"""
WHAT:  Refining prompts from past runs (research_agent/refine): the cases
       read back from a run, the state each call saw, replaying a case with a
       candidate prompt, blind selection, lessons and adoption.

Run with:   python -m pytest tests/test_refine.py -q
"""

from __future__ import annotations

import pytest

from research_agent import state as S
from research_agent.bootstrap import nodes
from research_agent.bootstrap.config import ROLES, RoleConfig
from research_agent.bootstrap.session import Bootstrap
from research_agent.refine import cases as C
from tests.scripted import script  # noqa: F401 -- fixture
from tests.test_graph import DESIGN, FINDINGS, decide, say


@pytest.fixture
def past_run(tmp_path, script):
    """A finished bootstrap: research, a brief, a design that was sent back
    once (the validator owned a step), the user's approval."""
    run = nodes.Run(folder=tmp_path / "run", roles={r: RoleConfig("fake") for r in ROLES})
    bad = {**DESIGN, "nodes": [{**n, "steps": ["2"]} if n["role"] == "validator" else n
                               for n in DESIGN["nodes"]]}
    script.add("DiscussorOutput",
               say("Researching.", ("research", "LoRA baselines")),
               say("Brief ready. Approve?", brief="Compare LoRA ranks."),
               say("Designing.", approve=["brief"]),
               say("Design ready. Approve?"),
               say("Approved.", approve=["design"]), say("Done."))
    script.add("ResearcherOutput", FINDINGS)
    script.add("DesignOutput", bad, DESIGN)
    script.add("OrchestratorOutput", decide("research", questions=["q"]), decide("brief"),
               decide("design"))
    boot = Bootstrap(run)
    boot.start("LoRA rank on small data")
    boot.answer("Yes, that brief.")
    boot.answer("Looks good.")
    boot.close()
    return run.folder


def test_a_past_call_is_a_case_with_what_code_and_the_user_said(past_run):
    [case] = C.cases(past_run, "designer")
    assert case.id.endswith("/designer-001") and case.schema == "DesignOutput"
    assert "THE APPROVED BRIEF" in case.brief and "You are the DESIGNER" in case.instructions
    assert len(case.problems) == 1 and "owns steps 2" in case.problems[0]
    assert "the user said: Looks good." in case.aftermath
    assert "approved the design" in case.aftermath
    orchestrated = C.cases(past_run, "orchestrator")
    assert len(orchestrated) == 3 and not C.cases(past_run, "exec_orchestrator")
    assert C.collect([past_run], "designer", 2)[0].id == case.id


def test_the_state_a_call_saw_is_rebuilt_from_the_history(past_run):
    [case] = C.cases(past_run, "designer")
    then, exact = C.state_at(past_run, case.at, case.session)
    assert exact and S.approved(then, "brief") and not S.version(then, "design")
    now, exact = C.state_at(past_run, "9999")
    assert exact and now["versions"] == S.Store(past_run).read()["versions"]


def test_an_unknown_role_is_refused(past_run):
    with pytest.raises(ValueError, match="No role 'chef'"):
        C.collect([past_run], "chef")


# ---- replay, propose, select -------------------------------------------------------------

from research_agent import run as run_base  # noqa: E402
from research_agent.refine import propose as P  # noqa: E402
from research_agent.refine import replay as R  # noqa: E402
from research_agent.refine import select as SEL  # noqa: E402


def refiner(tmp_path):
    (tmp_path / "refine").mkdir(exist_ok=True)
    return run_base.Run(tmp_path / "refine", {"refiner": RoleConfig("fake")}, quiet=True)


def test_a_replay_sends_the_recorded_brief_with_the_candidate_instructions(past_run, script,
                                                                           tmp_path):
    [case] = C.cases(past_run, "designer")
    bad = {**DESIGN, "nodes": [{**n, "steps": ["2"]} if n["role"] == "validator" else n
                               for n in DESIGN["nodes"]]}
    script.add("DesignOutput", bad, DESIGN)
    out = R.replay(case, "You are a careful DESIGNER.", label="inv", home=tmp_path, model="fake")
    call = [c for c in script.calls if c["schema"] == "DesignOutput"][-2]
    assert call["prompt"] == case.brief and call["thread"] is None
    assert out.passed and out.attempts == 2
    assert any("owns steps 2" in p for p in out.first_problems), "the role's real check"
    script.add("DesignOutput", bad, bad, bad)
    failed = R.replay(case, "x", label="dom", home=tmp_path, model="fake")
    assert not failed.passed and "no usable answer" in failed.error


def test_the_proposers_see_the_cases_and_a_rewrite_is_checked(past_run, script, tmp_path):
    [case] = C.cases(past_run, "designer")
    base = R.Output(case=case.id, label="base", answer='{"summary": "x"}', attempts=1)
    script.add("Proposal", {"instructions": ""},
               {"instructions": "You are the DESIGNER. The validator owns no steps.",
                "notes": [{"change": "validator rule", "motivated_by": case.id}]})
    out = P.propose(refiner(tmp_path), "inv", role="designer", base="You are the DESIGNER.",
                    cases=[case], baseline={case.id: [base]}, lessons="- cue: x")
    sent = script.prompts("Proposal")
    assert "=== THE CURRENT INSTRUCTIONS ===\nYou are the DESIGNER." in sent[0]
    assert "owns steps 2" in sent[0] and "LESSONS FROM EARLIER" in sent[0]
    assert "Replayed with the base instructions -- passed the checks" in sent[0]
    assert "`instructions` is empty" in sent[1]
    assert out.instructions.endswith("owns no steps.")


def test_the_judge_sees_answers_not_labels_and_the_base_wins_ties(past_run, script, tmp_path):
    [case] = C.cases(past_run, "designer")
    outs = [R.Output(case=case.id, label=label, answer=f'{{"by": "{label}"}}', attempts=1)
            for label in ("base", "inv", "dom")]
    outs.append(R.Output(case=case.id, label="composed", error="no usable answer"))

    def pick(prompt):
        chunks = {c[0]: c for c in prompt.split("=== CANDIDATE ")[1:]}
        assert len(chunks) == 3 and "base" not in "".join(c.split("===")[0] for c in chunks.values()), \
            "candidates are shown by letter, not by which instructions made them"
        return {"why": "fidelity", "choice": next(i for i, c in chunks.items()
                                                  if '"by": "inv"' in c)}
    script.add("Choice", pick)
    p = SEL.judge(refiner(tmp_path), case, outs)
    assert p.winner == "inv" and set(p.shown.values()) == {"base", "inv", "dom"}, \
        "a candidate with no answer is not shown"
    picks = [SEL.Pick("c1", "inv"), SEL.Pick("c2", "base"), SEL.Pick("c3", "dom")]
    assert SEL.tally(picks)[0] == "base", "a tie keeps the base"
    picks.append(SEL.Pick("c4", "inv"))
    assert SEL.tally(picks) == ("inv", {"inv": 2, "base": 1, "dom": 1})
