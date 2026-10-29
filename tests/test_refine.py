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
