"""
WHAT:  The paper node every hub agent ends with: what it is told, what it is
       given, and what the validator checks it against.
WHY:   The format is the user's request -- ACL style, with appendices that
       keep the full results and settings -- and it lives in prompt text, so
       a careless edit would drop it without any test failing but these.

Run with:   python -m pytest tests/test_paper.py -q
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from agent.agentfolder.render import render  # noqa: E402

PLAN = {"summary": "s", "steps": [
    {"id": "1", "title": "Code", "detail": "", "check": "tests pass"},
    {"id": "2", "title": "Run the sweep", "detail": "", "check": "8 result files"}]}


def _paper_entry():
    from agent.bootstrap import blueprint
    from agent.bootstrap.spec import compile_design

    proposal = compile_design(blueprint.starting_design(PLAN, request="x"), PLAN)
    return {n["name"]: n for n in proposal["nodes"]}["paper"]


def test_the_paper_node_is_told_the_acl_format_and_the_appendices():
    entry = _paper_entry()
    assert entry["backend"] == "paper_writer" and entry["access"] == "write"
    prompt = render(entry["prompts"]["first"], {"artifacts_dir": "/runs/a",
                                                 "handoff": "write it"})
    for must in ("acl-style-files", "references.bib", "Limitations", "Ethics Statement",
                 "no page limit", "Full results", "Experimental settings",
                 "Reproducibility", "Run record", "failed or were dropped",
                 "/runs/a/paper", "Overleaf", "tectonic", "never write a citation from memory"):
        assert must.lower() in prompt.lower(), must
    assert "\\usepackage[final]{ acl }" in prompt, \
        "LaTeX braces are kept from being read as placeholders"
    fields = {f["name"] for f in entry["output"]}
    assert {"status", "report", "paper_dir", "pdf_path", "title", "abstract"} <= fields


def test_the_paper_is_written_from_the_step_reports_and_the_research(tmp_path):
    from agent.agentfolder.schema import OrchestratorNodeConfig
    from agent.work.templates.orchestrator_node import _work_handoff

    (tmp_path / "research").mkdir()
    (tmp_path / "research" / "round-1.md").write_text("LoRA: 2106.09685")
    report = tmp_path / "s1.md"
    report.write_text("S1 FINAL REPORT")
    state = {"outputs": {"s1_code": {"report_path": str(report)}},
             "artifacts_dir": str(tmp_path / "artifacts")}
    config = OrchestratorNodeConfig(validator="validator", paper="paper", review="review")
    text = _work_handoff("paper", {"handoff": "write the paper", "include": []}, {},
                         {"s1_code": {}, "paper": {}}, state, str(tmp_path), 100_000, config)
    assert "S1 FINAL REPORT" in text and "round-1.md" in text
    assert "THE RESULTS are under" in text


def test_the_validator_checks_a_paper_against_the_paper_checklist(tmp_path):
    from agent.work.templates.orchestrator_node import _validation_request

    roster = {"paper": {"kind": "paper", "steps": [], "role": "p", "access": "write"}}
    text = _validation_request("paper", {"handoff": "check it", "include": []},
                               {"outputs": {}, "artifacts_dir": "/runs/a", "plan": PLAN},
                               roster, str(tmp_path), 100_000)
    for must in ("traces to a file under /runs/a", "Limitations", "listed as excluded",
                 "from memory"):
        assert must in text, must


def test_the_planner_does_not_plan_the_paper():
    from agent.bootstrap.prompts import PLANNER_INSTRUCTIONS

    flat = " ".join(PLANNER_INSTRUCTIONS.lower().split())
    assert "the paper is not a step" in flat
