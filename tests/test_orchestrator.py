"""
WHAT:  A hub agent: one orchestrator deciding each turn, one validator, a
       paper at the end. Its folder format, its routing, its guards, and
       what it is -- and is not -- sent.
WHY:   The point of the redesign is two behaviours a pipeline could not
       have: a fault found in step 2 that started in step 1 goes back to
       step 1, and the orchestrator carries step reports forward rather than
       logs. Both are asserted here, on the fake backend, offline.

Run with:   python -m pytest tests/test_orchestrator.py -q
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys
from dataclasses import replace

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

from agent import activity  # noqa: E402
from agent.agentfolder.commands import build_registry  # noqa: E402
from agent.agentfolder.load import load_agent_folder  # noqa: E402
from agent.agentfolder.validate import validate_folder  # noqa: E402
from agent.backends.fake import FakeBackend  # noqa: E402
from agent.work import memory  # noqa: E402
from agent.work.compile import backends_needed, compile_agent  # noqa: E402
from agent.work.state import initial_work_state, render_context  # noqa: E402

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "hub_agent"
PLAN = {"summary": "two steps", "steps": [
    {"id": "1", "title": "Get the data", "detail": "", "check": "the data file exists"},
    {"id": "2", "title": "Evaluate", "detail": "", "check": "a numbers table"},
]}


def _folder(tmp_path, edit=None):
    target = tmp_path / "agent"
    shutil.copytree(FIXTURE, target)
    if edit:
        nodes = json.loads((target / "nodes.json").read_text())
        graph = json.loads((target / "graph.json").read_text())
        edit(graph, nodes)
        (target / "nodes.json").write_text(json.dumps(nodes))
        (target / "graph.json").write_text(json.dumps(graph))
    return load_agent_folder(target)


def _run(tmp_path, folder=None, *, backend=None, plan=PLAN, supervision=None, resume=()):
    folder = folder or _folder(tmp_path)
    for name in ("repo", "session", "artifacts"):
        (tmp_path / name).mkdir(exist_ok=True)
    backend = backend or FakeBackend()
    graph = compile_agent(folder, backends={r: backend for r in backends_needed(folder)},
                          checkpointer=InMemorySaver(), registry=build_registry(folder),
                          artifacts_dir=str(tmp_path / "artifacts"),
                          session_dir=str(tmp_path / "session"))
    config = {"configurable": {"thread_id": "t"}, "recursion_limit": 200}
    out = graph.invoke(initial_work_state(
        repo_path=str(tmp_path / "repo"), task_brief="Does X beat Y?", agent_dir="x",
        artifacts_dir=str(tmp_path / "artifacts"), plan=plan, supervision=supervision),
        config)
    for answer in resume:
        out = graph.invoke(Command(resume=answer), config)
    return graph, config, out


def _decisions(out):
    return [e["digest"].split(":")[0] for e in out["ledger"] if e["kind"] == "decision"]


# ---- the folder ------------------------------------------------------------

def test_the_hub_folder_loads_validates_and_round_trips(tmp_path):
    from agent.agentfolder.render import mermaid
    from agent.agentfolder.schema import node_entry
    from webui.editing import graph_view

    folder = _folder(tmp_path)
    assert validate_folder(folder, strict=True) == []
    name, config = folder.orchestrator()
    assert (name, config.validator, config.paper, config.review) == (
        "orchestrator", "validator", "paper", "review")
    assert node_entry("orchestrator", config.model_dump()) == {
        "validator": "validator", "paper": "paper", "review": "review"}, \
        "defaults are not written out"
    assert "orchestrator -. dispatch .-> s1_data" in mermaid(folder)
    view = graph_view(folder, PLAN)
    assert {(e["from"], e["to"], e["label"]) for e in view["edges"]} >= {
        ("orchestrator", "s1_data", "dispatch"), ("orchestrator", "validator", "validate"),
        ("orchestrator", "review", "ask"), ("orchestrator", "__end__", "finish")}
    assert backends_needed(folder)["orchestrator"].value == "read_only"


@pytest.mark.parametrize("edit, code", [
    (lambda g, n: g["edges"].__setitem__(0, {"from": "s1_data", "to": "s2_eval"}), "hub_spoke"),
    (lambda g, n: g.__setitem__("entry", "s1_data"), "hub_entry"),
    (lambda g, n: n["validator"].__setitem__("access", "write"), "hub_validator"),
    (lambda g, n: n["validator"]["output"][0].__setitem__("choices", ["ok", "redo"]),
     "hub_validator"),
    (lambda g, n: n["orchestrator"].__setitem__("review", "paper"), "hub_review"),
])
def test_the_hub_rules_are_enforced(tmp_path, edit, code):
    problems = validate_folder(_folder(tmp_path, edit), strict=True)
    assert code in {p.code for p in problems}, problems


def test_an_old_checker_agent_still_validates_without_hub_rules():
    folder = load_agent_folder(pathlib.Path(__file__).resolve().parents[1]
                               / "agent" / "builtin_agents" / "default")
    assert folder.orchestrator() is None
    assert not [p for p in validate_folder(folder) if p.code.startswith("hub_")]


# ---- a run -----------------------------------------------------------------

def test_a_run_dispatches_validates_each_step_then_writes_the_paper(tmp_path):
    _, _, out = _run(tmp_path)
    assert _decisions(out) == [
        "dispatch -> s1_data", "validate s1_data", "dispatch -> s2_eval", "validate s2_eval",
        "dispatch -> paper", "validate paper", "finish (done)"], _decisions(out)
    assert out["outcome"] == "done"
    book = out["orchestrator"]["book"]
    assert {n: b["status"] for n, b in book.items()} == {
        "s1_data": "done", "s2_eval": "done", "paper": "done"}
    reports = sorted(p.name for p in (tmp_path / "artifacts" / "reports").iterdir())
    assert reports == ["paper.md", "s1_data.md", "s2_eval.md"]
    assert out["orchestrator"]["final"]["verdict"] == "done"


def test_a_failure_goes_back_to_where_it_started_and_later_steps_go_stale(tmp_path, monkeypatch):
    """The validator fails s2 and says the fault is s1's. The run goes back to
    s1 -- not to s2 -- with the validator's findings, then re-runs s2."""
    original = FakeBackend.run_structured
    failed = []

    def judging(self, **kw):
        run = original(self, **kw)
        if kw["output_model"].__name__ == "ValidatorOutput" and "OF: s2_eval" in kw["prompt"] \
                and not failed:
            failed.append(True)
            run = replace(run, data=run.data.model_copy(update={
                "verdict": "fail", "problem": "the eval reads an empty data file",
                "suspected_origin": "s1_data", "evidence": "data.csv has 0 rows",
                "suggested_fix": "re-download the data"}))
        return run

    monkeypatch.setattr(FakeBackend, "run_structured", judging)
    _, _, out = _run(tmp_path)
    decisions = _decisions(out)
    assert decisions[:6] == ["dispatch -> s1_data", "validate s1_data", "dispatch -> s2_eval",
                             "validate s2_eval", "dispatch -> s1_data", "validate s1_data"], \
        decisions
    assert decisions[6] == "dispatch -> s2_eval", "the stale step runs again"
    s1 = activity.turns(str(tmp_path / "session"), "s1_data")
    fix_prompt = next(t for t in s1 if t.index == 2).sent["prompt"]
    assert "the eval reads an empty data file" in fix_prompt
    assert "data.csv has 0 rows" in fix_prompt and "re-download the data" in fix_prompt
    stale_seen = [t.sent["prompt"] for t in activity.turns(str(tmp_path / "session"),
                                                            "orchestrator", limit=50)
                  if "s2_eval: stale" in (t.sent or {}).get("prompt", "")]
    assert stale_seen, "s2 was marked stale while s1 was being fixed"


def test_after_a_pass_only_the_report_is_carried_forward(tmp_path, monkeypatch):
    original = FakeBackend.run_structured

    def reporting(self, **kw):
        run = original(self, **kw)
        name = kw["output_model"].__name__
        if name == "S1DataOutput":
            run = replace(run, data=run.data.model_copy(
                update={"report": "S1 REPORT: 1000 rows fetched"}))
        if name == "ValidatorOutput":
            run = replace(run, data=run.data.model_copy(
                update={"evidence": "VALIDATOR EVIDENCE: counted"}))
        return run

    monkeypatch.setattr(FakeBackend, "run_structured", reporting)
    _run(tmp_path)
    prompts = {t.index: t.sent["prompt"] for t in activity.turns(
        str(tmp_path / "session"), "orchestrator", limit=50)}
    after_work = prompts[2]           # s1 reported
    after_pass = prompts[3]           # the validator passed s1
    assert "S1 REPORT: 1000 rows fetched" in after_work
    assert "VALIDATOR EVIDENCE" not in after_pass, "a pass collapses to one line"
    assert "PASSED s1_data" in after_pass
    later = "\n".join(p for i, p in prompts.items() if i > 3)
    assert "VALIDATOR EVIDENCE" not in later and "$ ls -la" not in later, \
        "no logs and no validation evidence carried forward"
    # The validator, though, was given the log.
    request = activity.turns(str(tmp_path / "session"), "validator", limit=50)[-1].sent["prompt"]
    assert "WHAT ITS COMMANDS PRINTED" in request and "python -m pytest -q" in request


def test_the_handoff_reaches_only_its_addressee():
    state = {"handoff": {"to": "s2_eval", "text": "fix the loader"}}
    assert render_context(state, node="s2_eval")["handoff"] == "fix the loader"
    assert render_context(state, node="s1_data")["handoff"] == ""
    assert render_context(state)["handoff"] == ""


def test_a_fresh_handoff_starts_a_new_conversation(tmp_path):
    _run(tmp_path)
    validator = activity.turns(str(tmp_path / "session"), "validator", limit=50)
    assert {t.sent["template"] for t in validator} == {"first"}, \
        "the fake asks for a fresh judge for each subject"


def test_a_gated_step_waits_for_approval(tmp_path):
    plan = json.loads(json.dumps(PLAN))
    plan["steps"][1]["gate"] = True
    graph, config, out = _run(tmp_path, plan=plan)
    snapshot = graph.get_state(config)
    assert snapshot.next == ("review",), "parked at the person"
    assert out["pending"]["purpose"] == "gate" and "2" in out["pending"]["question"]
    out = graph.invoke(Command(resume="/approve go ahead"), config)
    assert "dispatch -> s2_eval" in _decisions(out)
    assert out["outcome"] == "done"


def test_finish_waits_for_the_person_in_manual_mode(tmp_path):
    graph, config, out = _run(tmp_path, supervision={"mode": "manual", "iteration": 1,
                                                     "max_iterations": 3})
    assert out["pending"]["purpose"] == "finish_review"
    assert "/redesign" in out["pending"]["question"]
    out = graph.invoke(Command(resume="/redesign the validator is too lenient"), config)
    assert out["outcome"] == "redesign"
    assert out["vars"]["note"] == "the validator is too lenient"


# ---- the guards, one decision at a time -------------------------------------

def _decide(decision, *, book=None, own=None, plan=PLAN, session=""):
    from agent.agentfolder.schema import OrchestratorNodeConfig
    from agent.work.templates.orchestrator_node import _book, _decide as decide

    config = OrchestratorNodeConfig(validator="validator", paper="paper", review="review",
                                    max_visits=2, max_failures=2)
    roster = {"s1_data": {"kind": "work", "steps": ["1"], "role": "coder", "access": "write"},
              "s2_eval": {"kind": "work", "steps": ["2"], "role": "coder", "access": "write"},
              "validator": {"kind": "validator", "steps": [], "role": "v", "access": "read_only"},
              "paper": {"kind": "paper", "steps": [], "role": "p", "access": "write"},
              "review": {"kind": "human", "steps": [], "role": "", "access": "none"}}
    full = _book(book, ["s1_data", "s2_eval", "paper"])
    own = {"turns": 1, **(own or {})}
    update: dict = {}
    base = {"action": "dispatch", "target": "none", "subject": "none", "reason": "r",
            "handoff": "h", "include": [], "thread": "continue", "step_status": [],
            "note": "", "verdict": "none", "summary": "", "findings": [],
            "plan_feedback": "", "design_feedback": ""}
    route, line = decide("orchestrator", {**base, **decision}, {"kind": "start"}, own, full,
                         config, roster, {"plan": plan, "repo_path": "/r"}, session, 100_000,
                         update)
    return route, line, update, full, own


def test_a_node_sent_work_too_often_asks_the_person():
    route, _, update, _, _ = _decide({"action": "dispatch", "target": "s1_data"},
                                     book={"s1_data": {"status": "failed", "visits": 2}})
    assert route == "review" and update["pending"]["purpose"] == "guard"


def test_repeated_failures_ask_the_person():
    route, _, update, _, _ = _decide({"action": "dispatch", "target": "s2_eval"},
                                     book={"s2_eval": {"status": "failed", "failures": 2,
                                                       "problem": "still wrong"}})
    assert route == "review" and "still wrong" in update["pending"]["question"]


def test_an_unknown_target_asks_rather_than_guessing():
    route, _, update, _, _ = _decide({"action": "validate", "subject": "none"})
    assert route == "review"


def test_dispatching_the_validator_means_validate():
    route, line, update, _, _ = _decide({"action": "dispatch", "target": "none",
                                         "subject": "s1_data"}, own={"last_dispatch": "s1_data"})
    route, line, update, _, _ = _decide({"action": "validate", "target": "s1_data"})
    assert route == "validator" and update["handoff"]["subject"] == "s1_data"


def test_finish_before_the_paper_writes_the_paper_and_then_checks_it():
    route, _, update, _, _ = _decide({"action": "finish", "verdict": "done"},
                                     book={"s1_data": {"status": "done"},
                                           "s2_eval": {"status": "done"}})
    assert route == "paper" and "THE STEP REPORTS" in update["handoff"]["text"]
    route, _, update, _, _ = _decide({"action": "finish", "verdict": "done"},
                                     book={"s1_data": {"status": "done"},
                                           "s2_eval": {"status": "done"},
                                           "paper": {"status": "finished"}})
    assert route == "validator" and update["handoff"]["subject"] == "paper"
    route, _, update, _, _ = _decide({"action": "finish", "verdict": "stop"})
    assert route == "__end__", "stop needs no paper"


# ---- context and memory ----------------------------------------------------

def test_pulls_bring_a_nodes_detail_back_within_budget(tmp_path):
    from agent.work.context import resolve

    session = str(tmp_path)
    activity.write(session, "s1_data", [
        {"kind": "command", "phase": "completed", "command": "make data", "exit_code": 2,
         "output": "x" * 50_000 + "\nERROR: disk full"}], sent={"prompt": "do step 1"})
    state = {"outputs": {"s1_data": {"summary": "fetched", "turn_changes": "M data.py"}}}
    text = resolve(session, state, [{"node": "s1_data", "what": "log"},
                                    {"node": "s1_data", "what": "changes"},
                                    {"node": "s1_data", "what": "prompt"},
                                    {"node": "s2_eval", "what": "output"}], budget=20_000)
    assert "ERROR: disk full" in text and "[exit 2]" in text, "the end of the log survives"
    assert "M data.py" in text and "do step 1" in text
    assert "--- output of s2_eval ---\n(nothing)" in text
    assert len(text) < 25_000


def test_the_orchestrator_continues_its_conversation_and_remembers(tmp_path):
    folder = _folder(tmp_path)
    for name in ("repo", "session", "artifacts"):
        (tmp_path / name).mkdir()
    graph = compile_agent(folder, backends={r: FakeBackend() for r in backends_needed(folder)},
                          checkpointer=InMemorySaver(), registry=build_registry(folder),
                          artifacts_dir=str(tmp_path / "artifacts"),
                          session_dir=str(tmp_path / "session"))
    out = graph.invoke(initial_work_state(
        repo_path=str(tmp_path / "repo"), task_brief="b", agent_dir="x",
        artifacts_dir=str(tmp_path / "artifacts"), plan=PLAN,
        threads={"orchestrator/": "discussor-thread-1"},
        thread_providers={"orchestrator/": "fake"}),
        {"configurable": {"thread_id": "t"}, "recursion_limit": 200})
    turns = activity.turns(str(tmp_path / "session"), "orchestrator", limit=50)
    assert {t.thread_id for t in turns} == {"discussor-thread-1"}
    first = min(turns, key=lambda t: t.index).sent["prompt"]
    assert first.startswith("THE DISCUSSION IS OVER"), first[:80]
    saved = memory.load(str(tmp_path / "session"))
    assert saved["thread_id"] == "discussor-thread-1" and saved["decisions"]
    assert saved["book"]["s1_data"]["status"] == "done"
    assert out["outcome"] == "done"


def test_a_full_conversation_or_another_provider_starts_fresh(tmp_path):
    from agent.agentfolder.schema import OrchestratorNodeConfig
    from agent.work.templates.orchestrator_node import make_orchestrator_node

    seen = []

    class Recording(FakeBackend):
        def run_structured(self, **kw):
            seen.append(kw["thread_id"])
            return super().run_structured(**kw)

    config = OrchestratorNodeConfig(validator="validator", review="review", roll_percent=50)
    roster = {"s1_data": {"kind": "work", "steps": ["1"], "role": "c", "access": "write"},
              "validator": {"kind": "validator", "steps": [], "role": "v", "access": "read_only"},
              "review": {"kind": "human", "steps": [], "role": "", "access": "none"}}
    node = make_orchestrator_node("orchestrator", config, roster=roster, backend=Recording(),
                                  session_dir=str(tmp_path))
    base = {"repo_path": str(tmp_path), "plan": PLAN, "ledger": [], "outputs": {}}
    node({**base, "threads": {"orchestrator/": "old"}, "thread_providers": {"orchestrator/": "fake"},
          "orchestrator": {"turns": 3, "fill": 80}})
    node({**base, "threads": {"orchestrator/": "old"},
          "thread_providers": {"orchestrator/": "codex"}, "orchestrator": {"turns": 3}})
    node({**base, "threads": {"orchestrator/": "old"}, "thread_providers": {"orchestrator/": "fake"},
          "orchestrator": {"turns": 3, "fill": 10}})
    assert seen == [None, None, "old"]
    assert len(memory.load(str(tmp_path)).get("rolled_at", [])) == 2
