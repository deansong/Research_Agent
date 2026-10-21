"""
WHAT:  The starting design built from the plan (agent/bootstrap/blueprint.py),
       the compiler that turns a design into a folder (agent/bootstrap/spec.py),
       and the designer that drives both (agent/bootstrap/nodes/designer.py).
WHY:   The claim is that a design can no longer fail on bookkeeping and can
       never end worse than the starting one. So the tests feed plans shaped
       like the ones that broke the old designer on real sessions -- 13 bare
       steps, 27 substeps, gates, braces in the detail -- through the REAL
       writer and validator, and drive the designer through the real
       bootstrap graph with scripted answers, broken ones included.

Run with:   python -m pytest tests/test_blueprint.py -q
"""

from __future__ import annotations

import copy
import json
import os
import pathlib
import sys
import tempfile
import types

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

from agent import storage  # noqa: E402
from agent.backends.base import BackendTimeout, StructuredRun, Usage  # noqa: E402
from agent.bootstrap import blueprint as B  # noqa: E402
from agent.bootstrap.graph import build_bootstrap_graph  # noqa: E402
from agent.bootstrap.nodes.validator import _check  # noqa: E402
from agent.bootstrap.nodes.writer import make_writer  # noqa: E402
from agent.bootstrap.prompts import REPAIR_PREFIX  # noqa: E402
from agent.bootstrap.schemas import DesignerOutput  # noqa: E402
from agent.bootstrap.spec import SpecError, compile_design  # noqa: E402
from agent.bootstrap.state import initial_bootstrap_state  # noqa: E402


# ---- plans shaped like the real ones ---------------------------------------

def _four_stages(per_stage: int = 8) -> dict:
    """The research shape, at its maximum: four stages of eight substeps."""
    titles = ["Code", "Experiment design", "Run", "Analysis and report"]
    return {"summary": "four full stages", "steps": [
        {"id": str(i), "title": title, "detail": "", "substeps": [
            {"id": f"{i}.{j}", "title": f"{title} piece {j}",
             "detail": "writes results/{seed}.json", "check": "the file exists"}
            for j in range(1, per_stage + 1)]}
        for i, title in enumerate(titles, 1)]}


def _flat(count: int = 20) -> dict:
    """Many bare top-level steps -- what the planner actually produced on
    most real sessions here, not the four stages it is told to."""
    return {"summary": "flat", "steps": [
        {"id": str(i), "title": f"Step {i}" + (" run the panel" if i % 4 == 0 else ""),
         "detail": "", "substeps": []} for i in range(1, count + 1)]}


def _compiled(plan: dict) -> dict:
    return compile_design(B.starting_design(plan, request="compare two optimisers", topology="pipeline"), plan)


def _write_and_check(proposal: dict, plan: dict) -> str:
    DesignerOutput.model_validate(proposal)           # the writer's input shape
    with tempfile.TemporaryDirectory() as tmp:
        paths = types.SimpleNamespace(staging=f"{tmp}/agent.tmp", brief=f"{tmp}/brief.md")
        make_writer(paths)({"design": {"proposal": proposal}})
        return _check(pathlib.Path(paths.staging), plan)


@pytest.mark.parametrize("plan", [_four_stages(), _four_stages(2), _flat(20), _flat(3)],
                         ids=["4x8", "4x2", "20 flat", "3 flat"])
def test_a_plan_compiles_to_a_valid_agent(plan):
    """Valid by construction, within the folder's caps, for any plan shape."""
    blueprint = B.from_plan(plan, request="compare two optimisers")
    assert B.problems(blueprint, plan) == []
    proposal = _compiled(plan)

    graph = proposal["graph"]
    assert len(graph["nodes"]) <= 48 and len(graph["branches"]) <= 16
    work = [n for n in proposal["nodes"] if n.get("capture") == ["turn_changes"]]
    assert sorted(s for n in work for s in n["steps"]) == sorted(
        str(step["id"]) for step in plan["steps"]), "one work node per top-level step"
    assert _write_and_check(proposal, plan) == ""
    print(f"PASS  {len(graph['nodes'])} nodes, {len(graph['branches'])} branches, valid")


def test_every_piece_of_work_is_owned_exactly_once():
    plan = _four_stages()
    blueprint = B.from_plan(plan)
    owned = [leaf for n in blueprint.nodes for s in n.steps for leaf in B._covers(s, plan)]
    assert sorted(owned) == sorted(B.leaf_ids(plan))


def test_substeps_of_a_run_stage_become_runners():
    plan = _four_stages(2)
    shapes = {n.steps[0]: n.shape for n in B.from_plan(plan).nodes if n.steps}
    assert shapes["3"] == "runner" and shapes["1"] == "worker", shapes


def test_braces_in_the_plan_are_not_read_as_placeholders():
    """A step detail of "results/{seed}.json" is prose. Copied into a checker's
    prompt as-is it would be an unknown placeholder and fail validation."""
    plan = _four_stages(1)
    proposal = _compiled(plan)
    checker = next(n for n in proposal["nodes"] if n["name"].startswith("check_"))
    assert "{ seed }" in checker["prompts"]["first"]
    assert _write_and_check(proposal, plan) == ""


def test_a_gate_stops_after_the_checker_and_approve_continues():
    plan = _four_stages(2)
    plan["steps"][1]["substeps"][1]["gate"] = True          # 2.2
    blueprint = B.from_plan(plan)
    proposal = _compiled(plan)
    assert _write_and_check(proposal, plan) == ""

    owner = next(n.name for n in blueprint.nodes if n.steps == ["2"])
    checker = next(n.name for n in blueprint.nodes if owner in n.checks)
    branch = next(b for b in proposal["graph"]["branches"] if b["from"] == checker)
    ok = next(c for c in branch["cases"] if c["when"] == "ok")
    assert ok["to"] == B.REVIEW and ok["ask"]["resume_to"] == owner, ok

    review = next(n for n in proposal["nodes"] if n["name"] == B.REVIEW)
    approve = [c for c in review["commands"] if ok["ask"]["purpose"] in c["purposes"]]
    assert approve and approve[0]["to"] not in (B.REVIEW, "__end__"), approve


@pytest.mark.parametrize("change, expected", [
    (lambda nodes: nodes[0].steps.clear(), "owned by no node"),
    (lambda nodes: nodes[1].steps.append(nodes[0].steps[0]), "is owned by"),
    (lambda nodes: setattr(nodes[0], "name", "Bad Name"), "lowercase"),
    (lambda nodes: setattr(nodes[0], "name", "review"), "added automatically"),
    (lambda nodes: nodes[-1].checks.append("nope"), "EARLIER"),
    (lambda nodes: nodes[-1].steps.append("1.1"), "owns no steps"),
])
def test_problems_name_what_is_wrong(change, expected):
    plan = _four_stages(2)
    blueprint = B.from_plan(plan)
    nodes = [n.model_copy(deep=True) for n in blueprint.nodes]
    change(nodes)
    found = B.problems(blueprint.model_copy(update={"nodes": nodes}), plan)
    assert any(expected in line for line in found), found


# ---- the compiler: any graph, from specs -----------------------------------

def _start(plan):
    return B.starting_design(plan, request="compare two optimisers", topology="pipeline")


def test_a_loop_through_a_router_compiles_and_is_wired_into_the_prompts():
    """The shape the old chain could not express: run, then a router decides
    whether there is more to run. Code gives the router its enum from the
    cases, and the runner is shown why it was sent back."""
    plan = _four_stages(1)
    design = _start(plan)
    runner = next(n["name"] for n in design["nodes"] if n["role"] == "runner")
    checker = next(n["name"] for n in design["nodes"] if runner in n["reads"])
    graph = design["graph"]
    graph["nodes"].insert(5, {"name": "more_runs", "kind": "agent"})
    graph["edges"] = [{**e, "to": "more_runs"} if e["from"] == runner else e
                      for e in graph["edges"]]
    graph["branches"].append({"from": "more_runs", "route_on": "action", "default": "review",
                              "cases": [{"when": "more", "to": runner},
                                        {"when": "done", "to": checker}]})
    design["nodes"].insert(5, {"name": "more_runs", "role": "router", "steps": [],
                               "reads": [runner], "instructions": "Seeds 0-2.",
                               "outputs": [], "commands": []})

    proposal = compile_design(design, plan)
    assert _write_and_check(proposal, plan) == ""
    entries = {n["name"]: n for n in proposal["nodes"]}
    action = next(f for f in entries["more_runs"]["output"] if f["name"] == "action")
    assert action["choices"] == ["more", "done"]
    assert "{out.more_runs.next_task}" in entries[runner]["prompts"]["next"]
    assert "'more': the run goes to " + runner in entries["more_runs"]["prompts"]["first"]


@pytest.mark.parametrize("change, expected", [
    (lambda d: d["nodes"].pop(0), "has no spec"),
    (lambda d: d["nodes"][0]["steps"].append("2"), "must own exactly"),
    (lambda d: d["nodes"][-2]["steps"].append("1.1"), "owns no plan steps"),
    (lambda d: d["nodes"][0].update(name="renamed"), "is missing"),
    (lambda d: d["nodes"].insert(1, {**d["nodes"][0], "name": "extra_work",
                                     "steps": []}), "not one of the plan's work"),
    (lambda d: d["nodes"][0].update(reads=["nope"]), "not an agent node"),
    (lambda d: d["nodes"][-1].update(role="worker"), "role must be human"),
    (lambda d: d["nodes"].append({**d["nodes"][-2], "name": "ghost"}), "not in graph.nodes"),
])
def test_a_design_that_cannot_compile_says_why(change, expected):
    plan = _four_stages(2)
    design = _start(plan)
    change(design)
    with pytest.raises(SpecError) as caught:
        compile_design(design, plan)
    assert any(expected in line for line in caught.value.problems), caught.value.problems


# ---- the designer, through the real bootstrap graph --------------------------

MARK = '\n\n{\n "graph"'


class _Scripted:
    """Answers the discussor and planner; each designer answer is a function
    of the design shown at the end of its prompt."""

    name = "scripted"
    supports_repo_access = True
    from agent.backends.base import Access as _A
    max_access = _A.FULL

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts: list[str] = []

    def run_structured(self, *, thread_id, repo_path, access, developer_instructions,
                       prompt, output_model):
        fields = set(output_model.model_fields)
        if {"summary", "steps"} <= fields:
            data = output_model.model_validate(_four_stages(2))
        elif "reply" in fields:
            data = output_model.model_validate({"reply": "ok", "requirements": "reqs"})
        else:
            self.prompts.append(prompt)
            answer = self.answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            shown = json.loads(prompt[prompt.rindex(MARK) + 2:])
            data = output_model.model_validate(
                {"task_brief": "THE BRIEF", "rationale": "why", **answer(shown)})
        return StructuredRun(data=data, thread_id=thread_id or "t", is_new_thread=False,
                             usage=Usage(input_tokens=1))

    def close(self):
        return None


def _design(backend, *, max_attempts: int = 3):
    tmp = pathlib.Path(tempfile.mkdtemp())
    paths = storage.session_paths(tmp, "test")
    graph = build_bootstrap_graph({r: backend for r in ("discussor", "planner", "designer")},
                                  paths, InMemorySaver(), max_attempts=max_attempts,
                                  topology="pipeline")
    config = {"configurable": {"thread_id": "b"}, "recursion_limit": 200}
    graph.invoke(initial_bootstrap_state(repo_path=str(tmp), session_dir=str(paths.session),
                                         request="compare two optimisers"), config=config)
    for answer in ("/plan", "/approve"):
        graph.invoke(Command(resume=answer), config=config)
    return graph.get_state(config).values, paths


def _note(name, text):
    def answer(shown):
        for node in shown["nodes"]:
            if node["name"] == name:
                node["instructions"] = text
        return shown
    return answer


def test_the_designers_answer_is_compiled_into_the_agent():
    first = "s1_code"
    backend = _Scripted([_note(first, "Reuse utils/io.py.")])
    state, paths = _design(backend)

    assert state["human"]["purpose"] == "design_review", state["human"]
    nodes = json.loads((pathlib.Path(paths.agent_dir) / "nodes.json").read_text())
    assert "Reuse utils/io.py." in nodes[first]["instructions"]
    assert pathlib.Path(paths.brief).read_text().strip() == "THE BRIEF"
    assert "RESEARCH SKELETON" not in backend.prompts[0], "no worked example any more"


def test_a_repair_shows_the_design_and_names_the_problem():
    first = "s1_code"

    def broken(shown):
        for node in shown["nodes"]:
            if node["name"] == first:
                node["reads"] = ["nope"]
        return shown

    backend = _Scripted([broken, lambda shown: {**shown, "nodes": [
        {**n, "reads": []} if n["name"] == first else n for n in shown["nodes"]]}])
    state, _ = _design(backend)

    assert state["human"]["purpose"] == "design_review", state["human"]
    repair = backend.prompts[1]
    assert repair.startswith(REPAIR_PREFIX[:32])
    assert "nope" in repair, "the problem is named"
    assert '"s4_analysis_and_report"' in repair, \
        "the whole design as it stands is shown"
    assert len(repair) < 40_000, len(repair)


def test_when_the_attempts_run_out_the_starting_design_is_offered():
    """Never worse than the start: broken answers end at the review stop with
    the plan-built agent on disk, not at 'could not produce an agent'."""
    def broken(shown):
        shown["graph"]["nodes"].append({"name": "ghost", "kind": "agent"})
        return shown

    backend = _Scripted([broken, broken, broken])
    state, paths = _design(backend, max_attempts=2)

    assert state["human"]["purpose"] == "design_review", state["human"]
    assert "REJECTED CHANGES" in state["human"]["context"]
    assert "ghost" in state["human"]["context"]
    graph = json.loads((pathlib.Path(paths.agent_dir) / "graph.json").read_text())
    assert "ghost" not in [n["name"] for n in graph["nodes"]]


def test_a_failed_turn_is_an_attempt_not_a_crash():
    backend = _Scripted([BackendTimeout("no answer in 600s"), lambda shown: shown])
    state, _ = _design(backend)
    assert state["human"]["purpose"] == "design_review", state["human"]


# ---- the four goals, each as a rule --------------------------------------------

def test_every_work_node_must_be_checked():
    """Audit: nothing a worker does goes unexamined by an independent node."""
    plan = _four_stages(1)
    design = _start(plan)
    first = design["nodes"][0]["name"]
    checker = next(n for n in design["nodes"] if first in n["reads"])
    checker["reads"] = ["s2_experiment_design"]     # looks elsewhere
    with pytest.raises(SpecError) as caught:
        compile_design(design, plan)
    assert any("not in any checker" in line for line in caught.value.problems)


def test_a_light_worker_runs_on_the_cheaper_role_and_is_still_checked_by_checker():
    plan = _four_stages(1)
    design = _start(plan)
    first = design["nodes"][0]
    first["tier"] = "light"
    entries = {n["name"]: n for n in compile_design(design, plan)["nodes"]}
    assert entries[first["name"]]["backend"] == "coder_light"
    checker = next(n for n in entries.values() if n.get("backend") == "checker"
                   and "{out." + first["name"] + "." in n["prompts"]["first"])
    assert checker["access"] == "read_only"

    from agent.config import DEFAULT_ROLE_BACKENDS
    assert "coder_light" in DEFAULT_ROLE_BACKENDS, "a light tier must resolve to a model"


def test_a_checker_sees_what_each_turn_changed_and_must_give_evidence():
    plan = _four_stages(1)
    entries = {n["name"]: n for n in _compiled(plan)["nodes"]}
    worker, checker = entries["s1_code"], entries["check_1"]
    assert worker["capture"] == ["turn_changes"], "the change list is recorded by code"
    assert "{out.s1_code.turn_changes}" in checker["prompts"]["first"]
    evidence = next(f for f in checker["output"] if f["name"] == "evidence")
    assert evidence["required"] is True


def test_a_loop_back_is_feedback_not_input():
    """Context: a worker is not handed the outputs of the checker that sends it
    back -- as INPUT. It sees the complaint separately, when it comes back."""
    plan = _four_stages(2)
    entries = {n["name"]: n for n in _compiled(plan)["nodes"]}
    first = entries["s1_code"]
    assert "{out.s2_" not in first["prompts"]["first"], "a later worker leaked in"
    assert "{out.check_1.problem}" in first["prompts"]["next"]


def test_plan_issues_name_steps_without_checks():
    plan = _flat(7)
    issues = B.plan_issues(plan)
    assert any("no `check`" in i for i in issues), issues
    assert any("top-level steps" in i for i in issues), issues


# ---- audit records from a real compiled run ----------------------------------------

def test_a_run_records_model_conversation_output_changes_and_a_timeline():
    """The audit trail, end to end: compile a design, run it on the fake
    backend in a real git repository, and read back what was recorded."""
    import subprocess

    from agent import activity
    from agent.agentfolder.commands import build_registry
    from agent.agentfolder.load import load_agent_folder
    from agent.backends.fake import FakeBackend
    from agent.work.compile import backends_needed, compile_agent
    from agent.work.state import initial_work_state

    plan = _four_stages(1)
    with tempfile.TemporaryDirectory() as tmp:
        repo, session = pathlib.Path(tmp) / "repo", pathlib.Path(tmp) / "session"
        repo.mkdir(), session.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "a.py").write_text("x = 1\n")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c",
                        "user.name=t", "commit", "-qm", "init"], check=True)

        paths = types.SimpleNamespace(staging=str(session / "agent"), brief=str(session / "b"))
        make_writer(paths)({"design": {"proposal": _compiled(plan)}})
        folder = load_agent_folder(session / "agent")

        class Writing(FakeBackend):
            """Writes a file on the first worker's turn, so there is a change."""
            def run_structured(self, **kw):
                if "Code piece 1" in kw["prompt"] and "{" not in kw["prompt"][:1]:
                    (repo / "loader.py").write_text("def load(): ...\n")
                return super().run_structured(**kw)

        backend = Writing(model="fake-model")
        graph = compile_agent(folder, backends={r: backend for r in backends_needed(folder)},
                              checkpointer=InMemorySaver(), registry=build_registry(folder),
                              session_dir=str(session))
        state = initial_work_state(repo_path=str(repo), task_brief="b", plan=plan,
                                   agent_dir=str(session / "agent"),
                                   artifacts_dir=str(session / "artifacts"))
        graph.invoke(state, {"configurable": {"thread_id": "w"}, "recursion_limit": 100})

        turn = activity.latest(session, "s1_code")
        assert turn.provider == "fake" and turn.model == "fake-model"
        assert turn.thread_id, "the conversation is recorded"
        assert turn.output and "files" in turn.output, "the whole output is kept"
        assert "A loader.py" in turn.output["turn_changes"], turn.output["turn_changes"]

        visits = activity.timeline(session)
        order = [v["node"] for v in visits]
        assert order[:2] == ["s1_code", "check_1"], order
        assert visits[1]["route"] == "ok" and visits[0]["model"] == "fake-model"


def test_a_step_with_substeps_is_one_node_looping_one_substep_a_turn():
    """Fewer nodes without bigger turns: the node owns the step, and works
    through its substeps one per turn, each checked before the next."""
    plan = _four_stages(8)
    proposal = _compiled(plan)
    assert len(proposal["graph"]["nodes"]) == 9, [n["name"] for n in proposal["graph"]["nodes"]]

    entries = {n["name"]: n for n in proposal["nodes"]}
    worker, checker = entries["s1_code"], entries["check_1"]
    assert worker["steps"] == ["1"]
    assert worker["thread_key"] == "{out.check_1.next_substep}", "a fresh conversation per substep"
    assert "exactly ONE per turn" in worker["prompts"]["first"]
    assert any(f["name"] == "substep" for f in worker["output"])

    verdict = next(f for f in checker["output"] if f["name"] == "verdict")
    assert verdict["choices"][:2] == ["ok", "next"], verdict
    assert any(f["name"] == "next_substep" for f in checker["output"])
    branch = next(b for b in proposal["graph"]["branches"] if b["from"] == "check_1")
    assert {"when": "next", "to": "s1_code"} in branch["cases"]


def test_a_looping_node_without_its_next_case_is_rejected():
    plan = _four_stages(2)
    design = _start(plan)
    for branch in design["graph"]["branches"]:
        branch["cases"] = [c for c in branch["cases"] if c["when"] != "next"]
    with pytest.raises(SpecError) as caught:
        compile_design(design, plan)
    assert any("case `next`" in line for line in caught.value.problems)
