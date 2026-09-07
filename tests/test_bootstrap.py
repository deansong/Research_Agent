"""
WHAT:  Tests for phase 1 -- the graph that designs an agent.
WHY:   The repair loop is the part most likely to be subtly wrong: it has to
       terminate, it has to feed real problems back, and it has to hand over
       to the human rather than looping forever.
CONCEPT: Testing a graph whose nodes mostly do NOT call a model. Only the
       discussor and designer do; writer and validator are ordinary code, so
       these tests exercise real file writing and real validation.

Run with:   python tests/test_bootstrap.py
"""

from __future__ import annotations

import copy
import json
import os
import pathlib
import sys
import tempfile

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from agent import storage
from agent.backends.base import Access, StructuredRun, Usage
from agent.backends.fake import _MINIMAL_AGENT
from agent.bootstrap.prompts import REPAIR_PREFIX
from agent.bootstrap.graph import build_bootstrap_graph
from agent.bootstrap.state import initial_bootstrap_state


class ScriptedBackend:
    """Returns a scripted sequence of designs, so we can force failures."""

    name = "scripted"
    supports_repo_access = True
    max_access = Access.FULL

    def __init__(self, designs: list[dict]):
        self.designs = designs
        self.design_calls = 0
        self.repair_prompts: list[str] = []
        self.revise_prompts: list[str] = []
        self.turn = 0

    def run_structured(self, *, thread_id, repo_path, access, developer_instructions,
                       prompt, output_model):
        self.turn += 1
        fields = set(output_model.model_fields)

        if {"summary", "steps"} <= fields:
            if "Revise" in prompt:
                self.revise_prompts.append(prompt)
            data = output_model.model_validate(_PLAN)
        elif {"task_brief", "graph", "nodes"} <= fields:
            index = min(self.design_calls, len(self.designs) - 1)
            self.design_calls += 1
            # Matched on the actual marker, not on the word "rejected"
            # appearing somewhere in the prompt: the designer's first prompt
            # carries the worked example and the research skeleton, and the
            # moment either of those mentioned a rejected design, every first
            # attempt was counted as a repair.
            if prompt.startswith(REPAIR_PREFIX[:32]):
                self.repair_prompts.append(prompt)
            data = output_model.model_validate(self.designs[index])
        else:
            data = output_model.model_validate(
                {"reply": f"reply-{self.turn}", "question": "q?", "requirements": "reqs"}
            )

        return StructuredRun(data=data, thread_id=thread_id or f"t{self.turn}",
                             is_new_thread=thread_id is None,
                             usage=Usage(input_tokens=10))

    def close(self):
        pass


_PLAN = {
    "summary": "Two steps: look, then write.",
    "steps": [
        {"id": "1", "title": "Survey the repo", "detail": "find conventions",
         "substeps": []},
        {"id": "2", "title": "Write the thing", "detail": "",
         "substeps": [{"id": "2.1", "title": "draft", "detail": "first pass"},
                      {"id": "2.2", "title": "verify", "detail": ""}]},
    ],
}


def _broken_design() -> dict:
    """A design that is schema-valid but a broken graph: END is unreachable."""
    design = copy.deepcopy(_MINIMAL_AGENT)
    design["graph"]["name"] = "broken"
    # Remove the only route to __end__.
    design["nodes"][1]["commands"] = [
        {"name": "again", "to": "worker", "summary": "Have another go"}
    ]
    return design


def _run(designs: list[dict], answers: list[str], *, max_attempts: int = 3):
    tmp = pathlib.Path(tempfile.mkdtemp())
    paths = storage.session_paths(tmp, "test")
    backend = ScriptedBackend(designs)
    graph = build_bootstrap_graph(
        {"discussor": backend, "planner": backend, "designer": backend},
        paths, InMemorySaver(), max_attempts=max_attempts,
    )
    config = {"configurable": {"thread_id": "b"}, "recursion_limit": 100}
    graph.invoke(
        initial_bootstrap_state(repo_path=str(tmp), session_dir=str(paths.session),
                                request="do the thing"),
        config=config,
    )
    for answer in answers:
        graph.invoke(Command(resume=answer), config=config)
    return backend, graph, config, paths


def test_plan_is_the_only_door_to_the_designer():
    backend, graph, config, _ = _run([_MINIMAL_AGENT], ["more detail", "and more"])
    assert backend.design_calls == 0, "the designer ran without /plan"
    assert graph.get_state(config).next == ("human",)
    print("PASS  the designer cannot start on its own; only /plan starts it")


def test_a_valid_design_stops_for_review_before_it_runs():
    """The design phase must NOT flow straight into execution.

    It used to: the validator set outcome="ready" and the work graph started,
    so the first time you saw a generated agent was while it was already
    running -- with write access to your repository. The folder is on disk by
    then, which is exactly when looking at it and editing it is free.
    """
    backend, graph, config, paths = _run([_MINIMAL_AGENT], ["/plan", "/approve"])
    values = graph.get_state(config).values

    assert graph.get_state(config).next == ("human",), "should be parked on a question"
    assert values["human"]["purpose"] == "design_review", values["human"]
    assert not values.get("outcome"), "must not be ready until a human approves"
    assert paths.has_agent(), "the folder should exist and be editable now"
    assert not paths.is_approved(), "nothing approved it yet"
    # The summary shown at the gate must actually describe the agent.
    assert "fake-generated" in values["human"]["context"], values["human"]["context"]
    print("PASS  a valid design parks for review instead of executing")


def test_approving_a_design_records_it_durably():
    """Approval has to outlive the process.

    The gate lives in the bootstrap graph, but "may this run?" is asked again
    by cli.py and by the web server on every later start. Without a durable
    record, a resumed session sees a folder on disk, concludes it has an agent,
    and skips the gate -- which is the bug this test exists to prevent.
    """
    backend, graph, config, paths = _run(
        [_MINIMAL_AGENT], ["/plan", "/approve", "/approve"])
    values = graph.get_state(config).values

    assert values["outcome"] == "ready", values.get("outcome")
    assert paths.is_approved(), "approval must be on disk, not only in state"
    assert "fake-generated" in paths.approved.read_text()
    print("PASS  approving writes a marker that survives the process")


def test_a_valid_design_is_written_and_promoted():
    backend, graph, config, paths = _run(
        [_MINIMAL_AGENT], ["/plan", "/approve", "/approve"])
    values = graph.get_state(config).values

    assert values["outcome"] == "ready", values.get("outcome")
    assert paths.has_agent(), "agent.tmp was not promoted to agent/"
    assert not paths.staging.exists(), "staging directory should be gone after the rename"

    graph_json = json.loads((paths.agent_dir / "graph.json").read_text())
    nodes_json = json.loads((paths.agent_dir / "nodes.json").read_text())
    assert graph_json["name"] == "fake-generated"
    # The writer converts the designer's LIST of nodes into an object keyed by
    # name -- strict structured output cannot emit an open-ended dict.
    assert set(nodes_json) == {"worker", "review"}, nodes_json.keys()
    # And it splits agent fields from human fields by kind.
    assert "commands" in nodes_json["review"] and "backend" not in nodes_json["review"]
    assert "backend" in nodes_json["worker"] and "commands" not in nodes_json["worker"]
    print("PASS  a valid design is written, split by kind, and promoted to agent/")


def test_the_brief_is_handed_over():
    backend, graph, config, paths = _run([_MINIMAL_AGENT], ["/plan", "/approve"])
    brief = graph.get_state(config).values["design"]["task_brief"]
    assert brief, "no task_brief produced"
    assert paths.brief.exists(), "brief.md not written"
    # It lives BESIDE agent/, not inside: promoting an agent must not carry a
    # task-specific brief along with it.
    assert paths.brief.parent == paths.session
    print("PASS  the task brief is written beside the agent, not inside it")


def test_repair_loop_recovers():
    """Two bad designs then a good one: it should retry and succeed."""
    backend, graph, config, paths = _run(
        [_broken_design(), _broken_design(), _MINIMAL_AGENT],
        ["/plan", "/approve", "/approve"],
    )
    values = graph.get_state(config).values

    assert backend.design_calls == 3, f"expected 3 attempts, got {backend.design_calls}"
    assert len(backend.repair_prompts) == 2, backend.repair_prompts
    assert "end_unreachable" in backend.repair_prompts[0], \
        "the repair prompt must contain the actual problem"
    assert values["outcome"] == "ready"
    assert paths.has_agent()
    print("PASS  repair loop: 2 bad designs -> real problems fed back -> 3rd succeeded")


def test_repair_loop_gives_up_and_asks_the_human():
    """Always broken: it must stop, not loop, and hand control back."""
    backend, graph, config, paths = _run([_broken_design()], ["/plan", "/approve"],
                                         max_attempts=3)
    values = graph.get_state(config).values

    assert backend.design_calls == 3, f"expected exactly 3 attempts, got {backend.design_calls}"
    assert values["human"]["purpose"] == "design_failed", values["human"]
    assert not values.get("outcome"), "should not claim to be ready"
    assert graph.get_state(config).next == ("human",), "should be waiting for the human"
    assert not paths.has_agent(), "a broken design must not be promoted"
    print("PASS  repair loop gives up after max_attempts and asks the human")


def test_use_falls_back_to_the_builtin_agent():
    backend, graph, config, paths = _run([_broken_design()], ["/plan", "/approve", "/use"],
                                         max_attempts=2)
    values = graph.get_state(config).values
    assert values["outcome"] == "ready", values.get("outcome")
    assert not paths.has_agent(), "/use should not write a session agent"
    # cli.py sees no session agent and resolves the shipped default.
    print("PASS  /use gives up designing and falls back to the built-in agent")


def test_planning_stops_for_approval_before_designing():
    """/plan reaches the PLANNER, and the designer waits for /approve."""
    backend, graph, config, paths = _run([_MINIMAL_AGENT], ["/plan"])
    values = graph.get_state(config).values

    assert backend.design_calls == 0, "the designer ran before the plan was approved"
    assert values["human"]["purpose"] == "plan_review", values["human"]
    assert graph.get_state(config).next == ("human",)
    assert paths.plan.exists(), "plan.json was not written for review"

    plan = json.loads(paths.plan.read_text())
    assert [s["id"] for s in plan["steps"]] == ["1", "2"], plan
    print("PASS  planning stops for approval; plan.json is written first")


def test_editing_plan_json_by_hand_wins():
    """The gate is pointless if approval ignores your edits."""
    backend, graph, config, paths = _run([_MINIMAL_AGENT], ["/plan"])

    edited = json.loads(paths.plan.read_text())
    edited["steps"].append({"id": "3", "title": "MY EXTRA STEP", "detail": "added by hand",
                            "substeps": []})
    paths.plan.write_text(json.dumps(edited))

    graph.invoke(Command(resume="/approve"), config=config)
    approved = graph.get_state(config).values["design"]["plan"]

    titles = [s["title"] for s in approved["steps"]]
    assert "MY EXTRA STEP" in titles, f"hand edit was ignored: {titles}"
    print("PASS  /approve re-reads plan.json, so hand edits win")


def test_a_broken_hand_edit_falls_back_rather_than_crashing():
    backend, graph, config, paths = _run([_MINIMAL_AGENT], ["/plan"])
    paths.plan.write_text("{ this is not json")

    graph.invoke(Command(resume="/approve"), config=config)
    approved = graph.get_state(config).values["design"]["plan"]
    assert approved["steps"], "should have fallen back to the planner's own plan"
    print("PASS  invalid plan.json falls back instead of crashing")


def test_revise_sends_the_plan_back():
    backend, graph, config, paths = _run(
        [_MINIMAL_AGENT], ["/plan", "/revise make it shorter"])
    values = graph.get_state(config).values
    assert backend.design_calls == 0, "the designer ran during a revision"
    # Back at the gate with a fresh plan, having been told what to change.
    assert values["human"]["purpose"] == "plan_review"
    assert any("make it shorter" in p for p in backend.revise_prompts), backend.revise_prompts
    print("PASS  /revise returns to the planner with the human's note")


def test_a_node_only_sees_its_own_steps():
    """The context-control mechanism, which is the point of the whole change."""
    from agent.bootstrap.nodes.planner import outline, steps_for

    plan = _PLAN
    mine = steps_for(plan, ["2.1"])
    assert "draft" in mine and "first pass" in mine, mine
    assert "Survey the repo" not in mine, "a node saw a step it does not own"
    assert "verify" not in mine, "a node saw a sibling substep it does not own"

    # But it still knows the shape of the whole job, one line per step.
    every = outline(plan)
    assert "Survey the repo" in every and "Write the thing" in every
    assert "first pass" not in every, "the outline leaked step detail"
    print("PASS  a node sees its own steps in full and everything else as one line")


def test_designer_schema_survives_strict_mode():
    """Every model we send to a provider must satisfy strict structured output.

    Two rules, at every level: `required` lists every property, and
    `additionalProperties` is false. Notably that FORBIDS an open-ended
    dict[str, X] -- which is why the designer emits `sets` as name/value pairs
    and `nodes` as a list, and the writer converts both.

    This is a local check on purpose: each of these was originally found by a
    live API rejection, one round trip at a time.
    """
    from agent.backends._schema import strict_json_schema
    from agent.bootstrap.schemas import DesignerOutput, DiscussorOutput

    def audit(node, path="root", out=None):
        out = [] if out is None else out
        if isinstance(node, dict):
            if node.get("type") == "object" or "properties" in node:
                props, extra = node.get("properties"), node.get("additionalProperties")
                if props is None and extra not in (False, None):
                    out.append(f"open dict at {path}")
                elif props is not None:
                    if set(props) - set(node.get("required", [])):
                        out.append(f"{path}: properties missing from required")
                    if extra is not False:
                        out.append(f"{path}: additionalProperties is not false")
            for key, value in node.items():
                if key != "required":
                    audit(value, f"{path}.{key}", out)
        elif isinstance(node, list):
            for i, value in enumerate(node):
                audit(value, f"{path}[{i}]", out)
        return out

    for model in (DiscussorOutput, DesignerOutput):
        problems = sorted(set(audit(strict_json_schema(model))))
        assert not problems, f"{model.__name__}:\n  " + "\n  ".join(problems)

    print("PASS  designer and discussor schemas are strict-mode clean")



def _research_folder(tmp: pathlib.Path):
    """Write the research skeleton to disk as a real agent folder.

    Four of the ten node entries are REAL -- RESEARCH_NODES, exactly as the
    designer is shown them -- so this checks the exemplars themselves, not a
    paraphrase of them. Every {out.x.y} in those prompts has to name a field
    the node x really declares, and only writing them out and validating
    catches it. An exemplar with a broken placeholder teaches the designer to
    write broken placeholders, and its repair loop then fights the example.

    The other six are synthesised from RESEARCH_ROLES, the same reading of
    the skeleton the designer has to make: "b, c, d are the same with
    different names and steps".
    """
    from agent.agentfolder.schema import graph_document, node_entry
    from agent.bootstrap.nodes.writer import _entry_for
    from agent.bootstrap.prompts import (RESEARCH_GRAPH, RESEARCH_NODES,
                                         RESEARCH_ROLES)
    from agent.bootstrap.schemas import NodeProposal

    specs = {name: (access, backend)
             for name, access, backend, _ in RESEARCH_ROLES}
    outputs = {"report": [
        {"name": "summary", "type": "string", "required": True},
        {"name": "findings", "type": "string"},
    ]}

    nodes: dict[str, dict] = {}
    for entry in RESEARCH_GRAPH["nodes"]:
        name, kind = entry["name"], entry["kind"]

        # The b triple is a's entries with the names swapped -- the same
        # reading of "b, c, d are the same with different names and steps"
        # that the designer is asked to make.
        source = RESEARCH_NODES.get(name)
        if source is None and name.endswith("_b"):
            source = json.loads(
                json.dumps(RESEARCH_NODES[name[:-2] + "_a"])
                .replace("_a.", "_b."))

        if source is not None:
            # Through the designer's own strict model and then the REAL
            # writer, not a hand-rolled equivalent. That is what makes this
            # a test of the exemplars: they have to be emittable by the
            # designer (NodeProposal) and convertible to disk (_entry_for),
            # and `sets` alone differs between those two shapes -- pairs in
            # the proposal, an object on disk.
            proposal = NodeProposal.model_validate({"name": name, **source})
            nodes[name] = _entry_for(kind, proposal.model_dump())
            continue

        access, backend = specs[name]
        nodes[name] = node_entry("agent", {
            "backend": backend,
            "access": access,
            "steps": [],
            "instructions": f"You are {name}.",
            "output": outputs.get(name, [
                {"name": "summary", "type": "string", "required": True},
            ]),
            "prompts": {"first": "{my_steps}", "next": "{my_steps}"},
        })

    folder = tmp / "agent"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "graph.json").write_text(json.dumps(graph_document(RESEARCH_GRAPH)))
    (folder / "nodes.json").write_text(json.dumps(nodes))
    return folder


def test_a_step_carries_its_own_check_and_gate():
    """The two fields that turn a plan into a design.

    Before them, "how do we know this worked" lived in prose in `detail` if
    it was written down at all, and "a person must approve this" was nowhere
    -- so the designer had to infer both, and inferred neither. They are
    separate fields because they are separate questions: a check is what a
    machine can decide, a gate is what it must not.
    """
    from agent.bootstrap.nodes.planner import outline, steps_for
    from agent.bootstrap.schemas import PlanStep, PlannerOutput

    step = PlanStep.model_validate({
        "id": "4", "title": "Launch the sweep",
        "check": "8 files under results/sweep, each with a top1 field",
        "gate": True,
    })
    assert step.check.startswith("8 files")
    assert step.gate is True
    assert PlanStep.model_validate({"id": "1", "title": "x"}).gate is False, \
        "a gate must be opt-in -- it stops the whole run"

    plan = PlannerOutput.model_validate({
        "summary": "s",
        "steps": [
            {"id": "3", "title": "Write the sweep", "check": "pytest passes"},
            step.model_dump(),
        ],
    }).model_dump()

    # The outline is one line per step, and a gate still shows: where the run
    # STOPS for a person is topology, so every node should see it.
    assert "[human gate]" in outline(plan), outline(plan)
    assert "[human gate]" not in outline(plan).splitlines()[0], \
        "only the gated step is marked"

    # And the owning node is told how its own work will be judged.
    own = steps_for(plan, ["3"])
    assert "check: pytest passes" in own, own
    assert "human gate" not in own, "step 3 is not gated"
    print("PASS  a step carries its check and its gate, and both are rendered")


def test_a_gated_step_with_no_human_node_is_rejected():
    """The promise a design can silently break.

    `gate: true` is a person saying "ask me before going past this". A graph
    that owns the step and cannot reach a human node does not fail -- it runs
    straight through, and the approval nobody asked for is discovered later,
    if at all. The run LOOKS successful, which is the worst shape available.
    """
    from agent.agentfolder.load import load_agent_folder
    from agent.bootstrap.nodes.validator import _gate_problems

    plan = {"steps": [
        {"id": "3", "title": "Write the sweep", "check": "pytest"},
        {"id": "4", "title": "Launch the sweep", "gate": True},
    ]}

    with tempfile.TemporaryDirectory() as tmp:
        folder = load_agent_folder(_research_folder(pathlib.Path(tmp)))

        # The skeleton's run_exp_a owns step 4 and does reach `review`.
        assert _gate_problems(folder, plan) == "", _gate_problems(folder, plan)

        # Nobody owns step 9, so nobody will ever ask about it.
        orphan = {"steps": [{"id": "9", "title": "Publish", "gate": True}]}
        problem = _gate_problems(folder, orphan)
        assert "Step 9" in problem and "no node lists it" in problem, problem

        # And a gate is only satisfied by REACHING a human, not by one
        # existing somewhere -- a graph almost always has one for its exit,
        # so passing on that basis would make the check decorative.
        for node in folder.graph.nodes:
            if node.kind == "human":
                node.kind = "agent"
        blind = _gate_problems(folder, plan)
        assert "Step 4" in blind and "reaches a human node" in blind, blind
    print("PASS  a human-gated step must reach a human, not merely coexist")


def test_the_discussor_asks_about_the_shape_of_the_work_too():
    """The second half of the interview, which is what the graph is built from.

    The research questions settle what the experiment IS. These settle what
    the STEPS are -- and the answers become plan steps and then nodes, close
    to one each, so a vague answer here is a vague node two phases later.
    """
    from agent.bootstrap.prompts import DISCUSSOR_INSTRUCTIONS

    text = DISCUSSOR_INSTRUCTIONS.lower()
    for topic in ("data", "environment", "code", "experiments",
                  "checks", "gates"):
        assert topic in text, f"the discussor never asks about {topic}"

    # The pedantic one: "a few configurations" is what produces a graph that
    # cannot say when it is finished.
    assert "ablations" in text and "hyperparameters" in text and "seeds" in text
    assert "one question per turn" in text, "it must not interrogate in bulk"
    # The answers have to land somewhere the planner reads.
    assert "requirements" in text, "the planner reads that field, not reasoning"
    print("PASS  the discussor walks data, env, code, experiments, checks, gates")


def test_the_planner_is_told_what_a_check_is_and_is_not():
    """A check nobody could apply produces a verifier that rubber-stamps.

    Which is worse than no verifier: the graph gains a node, a turn and a
    branch, and still nothing decides whether the work counts.
    """
    from agent.bootstrap.prompts import PLANNER_INSTRUCTIONS

    text = PLANNER_INSTRUCTIONS.lower()
    assert "`check`" in text and "`gate`" in text
    assert "not \"the code is correct\"" in text or "not \"it works\"" in text, \
        "the planner needs the negative examples, not just the rule"
    assert "rubber-stamp" in text, "say what a vague check costs"
    assert "default false" in text, "a gate stops the whole run"
    print("PASS  the planner knows what a check is, and what a gate costs")


def test_every_design_phase_prompt_knows_this_is_ml_research():
    """All three phases, not just the designer.

    The failure this guards against is subtler than a missing rule: a
    discussor that never asks about compute produces a plan with no compute
    step, from which the designer builds a graph that runs out of GPU memory
    on its first expensive node -- and the run dies three phases away from
    the omission that caused it. So each phase has to know what kind of work
    this is, and each one has a different thing to get right.
    """
    from agent.bootstrap.prompts import (DESIGNER_INSTRUCTIONS,
                                         DISCUSSOR_INSTRUCTIONS,
                                         PLANNER_INSTRUCTIONS)

    discussor = DISCUSSOR_INSTRUCTIONS.lower()
    # The two people leave out, and the two that waste the most time.
    for essential in ("compute", "metric", "baseline", "seeds", "hardware"):
        assert essential in discussor, f"the discussor never asks about {essential}"
    assert "refute" in discussor, "a claim needs a way to be wrong"

    planner = PLANNER_INSTRUCTIONS.lower()
    assert "reproduce the baseline first" in planner, \
        "a number you cannot reproduce is not a comparison"
    assert "different steps" in planner, "running and analysing must split"
    assert "seeds" in planner and "config" in planner, "the variables must be pinned"
    assert "out of scope" in planner

    designer = DESIGNER_INSTRUCTIONS.lower()
    assert "never overwritten" in designer, "a re-run must add a result"
    assert "per seed" in designer, "results are per config and per seed"
    assert "runs something" in designer, "the expensive node must stand alone"

    print("PASS  discussor, planner and designer all know this is ML research")


def test_none_of_them_force_a_refactor_into_an_experiment():
    """The other half of the same instruction, and the easier one to forget.

    Pointing three prompts at machine-learning research is how every bug fix
    starts getting a baseline and a seed sweep. Each phase needs an explicit
    way out, or the research framing becomes a straitjacket for the work that
    is not research -- which is still most of the work in any repository.
    """
    from agent.bootstrap.prompts import (DESIGNER_INSTRUCTIONS,
                                         DISCUSSOR_INSTRUCTIONS,
                                         PLANNER_INSTRUCTIONS)

    for name, text in (("discussor", DISCUSSOR_INSTRUCTIONS),
                       ("planner", PLANNER_INSTRUCTIONS),
                       ("designer", DESIGNER_INSTRUCTIONS)):
        lowered = text.lower()
        assert "not research" in lowered or "not an experiment" in lowered, \
            f"the {name} has no way out of the research framing"
        assert "refactor" in lowered, \
            f"the {name} should name the ordinary case it must not distort"
    print("PASS  all three say what to do when the work is not an experiment")


def test_the_designer_is_told_when_to_use_an_orchestrator():
    """The choice the designer was never asked to make.

    It had a worked example that IS an orchestrator loop and a skeleton that
    is a fixed pipeline, and nothing telling it which shape suits which work.
    The deciding question is whether the plan fixes the ORDER: if it does, a
    pipeline; if the length of the work is unknown, an orchestrator.
    """
    from agent.bootstrap.prompts import CONTROL_FLOW

    text = CONTROL_FLOW.lower()
    assert "fixed pipeline" in text and "orchestrator" in text, text[:200]
    assert "not known in advance" in text, "the deciding question is missing"

    # The limits that make a central orchestrator impossible past a point --
    # measured, not guessed: BranchSpec.cases has max_length=8.
    assert "at most 8 cases" in text, "the branch limit must be stated"
    assert "seven workers" in text, "8 cases minus the finish case"
    assert "read_only" in text, "an orchestrator that edits files grades itself"

    # And the JSON, because prose about a shape is not a shape.
    assert '"route_on": "action"' in CONTROL_FLOW
    assert '"when": "finish"' in CONTROL_FLOW
    print("PASS  the designer is told which shape suits which work, with JSON")


def test_the_eight_case_limit_the_prompt_states_is_the_real_one():
    """A number in a prompt that nobody checks is a number that goes stale.

    This one is load-bearing: it is the reason the project uses a verifier
    per stage instead of one central orchestrator, and it is asserted in two
    prompts. If BranchSpec ever allowed more, both would be quietly wrong.
    """
    from agent.agentfolder.schema import BranchSpec

    limit = BranchSpec.model_fields["cases"].metadata
    caps = [getattr(m, "max_length", None) for m in limit]
    assert 8 in caps, f"BranchSpec.cases no longer caps at 8: {limit}"
    print("PASS  the 8-case limit in the prompts is the schema's real limit")


def test_the_designer_is_told_the_branch_uses_that_are_not_pass_fail():
    """Three branch uses that a pass/fail framing misses entirely.

    All three come from how experiments actually fail. A resource failure
    sent back to the node that wrote the code is the worst of them: the code
    is correct, rewriting it cannot free memory, and the loop will not
    converge because nothing in it changes the thing that is wrong.
    """
    from agent.bootstrap.prompts import CONTROL_FLOW

    text = CONTROL_FLOW.lower()

    # 1. resource vs code
    assert "out of memory" in text and "shrinks the configuration" in text
    assert "not to the node" in text, "a resource failure must not go to the coder"

    # 2. a sweep is a loop, and the filesystem is the only progress a prompt
    #    can read -- counters exist but are not renderable.
    assert "{artifacts_dir}" in CONTROL_FLOW, "the loop reads what already exists"
    assert "cannot be rendered" in text, "counters are not placeholders"

    # 3. an exit from the redo loop, which lives in the checker's own thread
    assert "spins until the step limit" in text
    assert "prompts.next" in text, "the attempt count lives in its conversation"
    print("PASS  resource failures, sweep loops and redo exits are all covered")


def test_the_claim_about_counters_is_still_true():
    """The prompt tells the designer counters cannot be rendered. Check it.

    This is the kind of statement that is true when written and silently
    false a release later, at which point the prompt is teaching a
    workaround for a limit that no longer exists.
    """
    from agent.agentfolder.render import SIMPLE_TOKENS
    from agent.work.state import WorkState

    assert "counters" in WorkState.__annotations__, "counters should still exist"
    assert not [t for t in SIMPLE_TOKENS if "counter" in t], SIMPLE_TOKENS
    # {var.*} reads `vars`, which only a human command's `sets` writes -- so
    # there is genuinely no way for a node to publish a count into a prompt.
    assert "vars" in WorkState.__annotations__
    print("PASS  counters really are unrenderable, as the prompt claims")


def test_every_field_the_designer_can_emit_is_in_its_prompt():
    """Nothing the designer is allowed to produce may be undiscoverable.

    This replaced a test that asserted particular entries survived in
    worked_example(), which broke the moment the budget was reallocated --
    and broke for the wrong reason: the fields it was really checking for had
    simply moved somewhere better. So it asks the question that actually
    matters instead. If a field is in NodeProposal, the designer can emit it,
    and it has to be either DEMONSTRATED in an example or DOCUMENTED in the
    reference. `record` was already missing when this was written.
    """
    from agent.bootstrap.prompts import (CONTROL_FLOW, PROMPT_REFERENCE,
                                         research_skeleton, worked_example)
    from agent.bootstrap.schemas import NodeProposal

    prompt = (worked_example() + PROMPT_REFERENCE + CONTROL_FLOW
              + research_skeleton())

    missing = [field for field in NodeProposal.model_fields
               if field != "name" and f'"{field}"' not in prompt]
    assert not missing, (
        f"the designer can emit {missing} and its prompt never mentions them. "
        f"Demonstrate them in an example, or add a line to PROMPT_REFERENCE."
    )
    print(f"PASS  all {len(NodeProposal.model_fields) - 1} emittable node "
          f"fields appear in the prompt")


def test_the_prompt_still_shows_the_shapes_that_need_showing():
    """Three JSON shapes prose cannot substitute for.

    Each is a thing the designer has to emit exactly right and would
    otherwise be guessing at: the orchestrator topology (every worker's edge
    going back), a human node's `commands`, and a branch with an `ask` on one
    of its cases.
    """
    from agent.bootstrap.prompts import (CONTROL_FLOW, research_skeleton,
                                         worked_example)

    example = worked_example()
    assert '"route_on": "action"' in example, "the orchestrator branch is gone"
    assert '"to": "orchestrator"' in example, \
        "workers must be shown returning to the orchestrator"

    skeleton = research_skeleton()
    assert '"commands"' in skeleton, "a human node's commands must be shown"
    assert '"sets"' in skeleton, "setting a var from a command must be shown"
    assert '"ask"' in skeleton, "a branch case with an ask must be shown"

    assert '"when": "finish"' in CONTROL_FLOW, "the orchestrator's exit case"
    print("PASS  orchestrator, commands and a branch ask are all shown as JSON")


def test_the_research_skeleton_is_a_valid_graph():
    """A skeleton in the prompt that does not validate teaches invalid graphs.

    This is the whole reason the skeleton is a Python structure rather than a
    block of prose with some JSON in it: prose cannot be run through the
    validator, so an error in it would surface as the designer's repair loop
    fighting an example we gave it.

    Checked at `strict=True`, the design-time setting -- which is where
    `self_assessment` is an error rather than a warning, and therefore where
    the skeleton's write -> run -> check -> write retry has to prove it is not
    a node grading itself.
    """
    from agent.agentfolder.load import load_agent_folder
    from agent.agentfolder.validate import validate_folder

    with tempfile.TemporaryDirectory() as tmp:
        folder = load_agent_folder(_research_folder(pathlib.Path(tmp)))
        problems = validate_folder(folder, strict=True)

        errors = [p for p in problems if not p.warning]
        assert not errors, [f"{p.code} at {p.where}: {p.message}" for p in errors]

        codes = {p.code for p in problems}
        assert "self_assessment" not in codes, "the retry loop grades itself"
        assert "no_verifier" not in codes, "check_* should count as verifiers"
        print(f"PASS  the research skeleton validates strictly "
              f"({len(problems)} warning(s))")


def test_the_research_skeleton_compiles_and_can_run():
    """Valid on paper is not the same as runnable.

    compile() is what catches an unreachable node or a dead end that the
    static checks miss, and it is cheap here because the backends are mocks --
    nothing is called, the graph is only built.
    """
    from unittest.mock import MagicMock

    from agent.agentfolder.commands import build_registry
    from agent.agentfolder.load import load_agent_folder
    from agent.work.compile import backends_needed, compile_agent

    with tempfile.TemporaryDirectory() as tmp:
        folder = load_agent_folder(_research_folder(pathlib.Path(tmp)))
        # Every role the folder asks for, so a typo in the skeleton's backend
        # names shows up as a KeyError here rather than at run time.
        assert set(backends_needed(folder)) == {"coder", "runner", "checker"}, \
            sorted(backends_needed(folder))
        compile_agent(
            folder,
            backends={role: MagicMock() for role in backends_needed(folder)},
            checkpointer=InMemorySaver(),
            registry=build_registry(folder),
        )
        print("PASS  the research skeleton compiles into a runnable graph")


def test_the_skeleton_offers_a_cheaper_backend_for_running_experiments():
    """The point of naming three roles instead of one.

    "runner" and "checker" exist so experiment runs can be pointed at a
    smaller model from .agent/config.json without editing the graph. If the
    skeleton put everything on one role that option would not exist, and the
    only way to get it would be to hand-edit every node.
    """
    from agent.bootstrap.prompts import RESEARCH_NODES, research_skeleton

    by_node = {name: entry.get("backend")
               for name, entry in RESEARCH_NODES.items()}
    assert by_node["run_exp_a"] == "runner", by_node
    assert by_node["check_a"] == "checker", by_node
    assert by_node["write_code_a"] == "coder", by_node
    assert by_node["run_exp_a"] != by_node["write_code_a"], \
        "running and writing must be separately configurable"

    # And the designer is told what those names are FOR -- an undocumented
    # role name is one the human never discovers and never configures.
    text = research_skeleton()
    assert "config.json" in text and "cheaper model" in text, \
        "the skeleton must say why the roles are separate"
    print("PASS  experiment runs are on their own configurable role")


def test_the_skeleton_says_it_is_a_starting_point():
    """A template that must be obeyed is worse than no template.

    It would produce a graph shaped like the example instead of like the
    work -- three experiment types crammed into two nodes because the example
    had two. So the skeleton has to say, in the prompt, that it is to be
    checked and changed.
    """
    from agent.bootstrap.prompts import research_skeleton

    text = research_skeleton().lower()
    assert "check it" in text or "check it fits" in text, text[:200]
    assert "rationale" in text, "it must be asked to report what it changed"
    assert "drop" in text, "it must be told it can remove nodes"
    assert "add nodes" in text, "it must be told it can add nodes"
    print("PASS  the skeleton presents itself as a starting point")


def test_designer_instructions_stay_under_the_measured_size_cliff():
    """A hard, measured limit -- not a style preference.

    Above roughly 6 KB of `developer_instructions`, a Codex turn NEVER
    COMPLETES. Not slower: it hangs, with no error. Bisected during a live run,
    and it is size rather than content -- 13,940 characters of ordinary prose
    hangs exactly the same way. The per-turn prompt has no such cliff, which is
    why the worked example lives there instead.

    So this file has a budget, and the failure mode for exceeding it is a
    design phase that silently never returns. Worth a test rather than a
    comment somebody edits past.
    """
    from agent.backends.codex import SAFE_INSTRUCTIONS_CHARS
    from agent.bootstrap.prompts import DESIGNER_INSTRUCTIONS

    size = len(DESIGNER_INSTRUCTIONS)
    assert size < SAFE_INSTRUCTIONS_CHARS, (
        f"DESIGNER_INSTRUCTIONS is {size} chars, over the {SAFE_INSTRUCTIONS_CHARS} "
        f"limit. Turns will hang rather than fail. Move material into the "
        f"per-turn prompt (worked_example) instead of growing this."
    )
    print(f"PASS  DESIGNER_INSTRUCTIONS is {size} chars, "
          f"{SAFE_INSTRUCTIONS_CHARS - size} under the cliff")


def test_the_designer_is_told_not_to_draft_and_not_to_explore():
    """Two rules that cost 35 minutes each to learn, and are one trim from
    being lost.

    The instructions sit a handful of characters under a hard limit, so the
    way to add anything is to shorten something -- and these are prose in the
    middle of prose. Both come from one measured run: the designer emitted a
    complete document with empty fields and a node literally named
    "placeholder" purely to narrate that it was about to start (every message
    is charged as the whole document, ~12,000 tokens), and it spent its first
    minutes on `git show` of a deletion commit, auditing a repository it does
    not need to read to lay out a graph.
    """
    from agent.bootstrap.prompts import DESIGNER_INSTRUCTIONS as text

    lowered = text.lower()
    assert "placeholder" in lowered and "once" in lowered, \
        "the rule against draft/placeholder documents is gone"
    assert "git history" in lowered, \
        "the rule against auditing the repository is gone"
    print("PASS  the designer is still told to answer once and not to explore")


def test_the_designer_is_told_not_to_let_a_node_check_itself():
    """A node that just failed is the worst judge of whether it failed.

    A real design came back with `smoke_validation` routing "retryable" to
    ITSELF, and three more nodes doing the same on their own `status` output.
    That is the same model, in the same conversation, grading its own work --
    it has every reason to report success. The fix is a separate read_only
    verifier that branches ok / redo / blocked.

    The 8-case branch limit is why this is per-STAGE rather than one central
    orchestrator: no single node can route back to twenty others.
    """
    from agent.bootstrap.prompts import (DESIGNER_INSTRUCTIONS,
                                         research_skeleton, worked_example)

    rule = DESIGNER_INSTRUCTIONS.lower()
    assert "no node judges its own success" in rule, "the rule is missing"
    assert "read_only verifier" in rule, "the verifier must be read_only"
    assert "self-assessment" in rule, "the failure mode must be named"
    assert "8 cases" in rule, "the branch limit is why this is per-stage"

    # The exact shape lives in the per-turn prompt, which has no size cliff.
    # Asserted across the whole prompt rather than inside worked_example(),
    # because the loop used to be spelled out twice -- once with invented
    # names in VERIFIER_LOOP, once with real ones and complete node entries
    # in the research skeleton. The second is strictly better, so the first
    # gave up its JSON; what matters is that the shape is shown SOMEWHERE.
    shape = worked_example() + research_skeleton()
    # Whitespace-normalised, because the JSON is rendered by json.dumps and
    # pinning its exact indentation makes this a test of the indent level.
    flat = " ".join(shape.split())
    assert "THE VERIFIER LOOP" in shape, "the rationale for the shape is missing"
    assert '"when": "redo", "to": "write_code_a"' in flat, \
        "redo must be shown pointing back at the WORKER, not onwards"
    assert '"default": "review"' in flat, \
        "an unhandled verdict must reach a human, not silently end the run"
    assert '"name": "review", "kind": "human"' in flat, \
        "and that target must be a human node"
    print("PASS  the designer is told to use a separate verifier, and shown one")


def test_the_designer_prompt_and_instructions_are_within_measured_limits():
    """Two different budgets, and only one of them is a proven cliff.

    `developer_instructions` above ~6 KB makes a Codex turn NEVER COMPLETE --
    bisected live, and it is size not content: 13,940 characters of ordinary
    prose hangs identically. That is why SAFE_INSTRUCTIONS_CHARS exists.

    The per-turn PROMPT has no known cliff. 11,830 characters was measured
    fine (34.2s), and a real 20-node design has since succeeded with a prompt
    of about that size. This asserts a generous ceiling rather than a measured
    one, so that a large addition has to be a deliberate decision.

    Note how little instruction headroom is left. That is the useful signal
    here: new designer guidance belongs in the prompt, or in the default agent
    that the worked example is read from -- not in this file.
    """
    from agent.backends.codex import SAFE_INSTRUCTIONS_CHARS
    from agent.bootstrap.prompts import (CONTROL_FLOW, DESIGNER_INSTRUCTIONS,
                                         PROMPT_REFERENCE, research_skeleton,
                                         worked_example)

    instructions = len(DESIGNER_INSTRUCTIONS)
    assert instructions < SAFE_INSTRUCTIONS_CHARS, (
        f"DESIGNER_INSTRUCTIONS is {instructions} chars, over the measured "
        f"{SAFE_INSTRUCTIONS_CHARS} cliff. Turns will HANG, not fail. Move it "
        f"into worked_example() -- the prompt has no such limit."
    )

    # Every piece designer.py appends, not just the first one. Measuring one
    # of four is how a budget is quietly overspent: worked_example() shrank
    # while the total grew, and a test on the part would have called that an
    # improvement.
    parts = {
        "worked_example": len(worked_example()),
        "PROMPT_REFERENCE": len(PROMPT_REFERENCE),
        "CONTROL_FLOW": len(CONTROL_FLOW),
        "research_skeleton": len(research_skeleton()),
    }
    prompt = sum(parts.values())
    assert prompt < 20_000, (
        f"the per-turn prompt is {prompt} chars ({parts}). No cliff is known "
        f"there, but nothing this large has been verified either -- measure "
        f"before raising."
    )
    print(f"PASS  instructions {instructions}/{SAFE_INSTRUCTIONS_CHARS} "
          f"({SAFE_INSTRUCTIONS_CHARS - instructions} left), "
          f"prompt {prompt}/20000 across {len(parts)} parts")


def test_the_designer_is_told_to_split_work_across_nodes():
    """The guidance used to contradict itself, and the wrong half won.

    It said "if a node owns more than about three steps, it is probably two
    nodes" AND "prefer the smallest graph that does the job -- three good nodes
    beat eight". A real design then put three plan steps and three separate
    output documents on one node, which ran past ten minutes and was killed
    with all its work discarded.

    These assertions are deliberately about the SUBSTANCE -- atomicity, and
    counting artefacts -- because that is the argument that makes splitting
    obviously right rather than a rule to be balanced against neatness.
    """
    from agent.bootstrap.prompts import DESIGNER_INSTRUCTIONS as text

    lowered = text.lower()
    assert "atomic" in lowered, "the designer must be told a node is all-or-nothing"
    assert "artefacts" in lowered or "artifacts" in lowered, \
        "the designer must be told to count what a node produces"
    assert "one top-level step per node" in lowered, "no explicit step budget"

    # And the line that pulled the other way must be gone.
    assert "three good nodes beat eight" not in lowered, \
        "the contradictory 'smallest graph' advice is back"
    print("PASS  the designer is told to split, with the reason, and not to un-split")


def test_an_agent_from_before_the_gate_is_grandfathered():
    """Adding the review gate must not break sessions that already worked.

    No marker on disk means "not approved", so without this an existing
    session would be sent back through the design phase -- and since its
    bootstrap graph had already finished, that means restarting the discussion
    from nothing. A change that improves the next run must not do that to the
    last one.

    The signal is exact: before the gate, the validator set outcome="ready" the
    moment a design passed. A bootstrap thread already saying "ready" was
    approved under the old rules.
    """
    from agent.config import AgentConfig, BackendConfig
    from agent.runtime import open_runtime

    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp)
        paths = storage.session_paths(repo, "old")
        # An agent on disk, as the old validator would have left it...
        paths.agent_dir.mkdir(parents=True, exist_ok=True)
        for name in ("graph.json", "nodes.json"):
            source = storage.builtin_agents_dir() / "default" / name
            (paths.agent_dir / name).write_text(source.read_text())
        assert paths.has_agent() and not paths.is_approved()

        cfg = AgentConfig(default=BackendConfig(provider="fake"), roles={})
        with open_runtime(cfg, paths) as runtime:
            # ...but no bootstrap checkpoint at all: nothing to grandfather on,
            # so the safe answer is to review it.
            assert runtime.resolve_folder(None) is None
            assert not paths.is_approved()

            # Now give it the state the old code would have left behind.
            saver = runtime.checkpointer
            config = {"configurable": {"thread_id": "old:bootstrap",
                                       "checkpoint_ns": ""}}
            saver.put(
                config,
                {"v": 1, "id": "x", "ts": "2026-01-01T00:00:00+00:00",
                 "channel_values": {"outcome": "ready"}, "channel_versions": {},
                 "versions_seen": {}},
                {"source": "loop", "step": 1, "parents": {}},
                {},
            )
            folder = runtime.resolve_folder(None)

        assert folder == paths.agent_dir, folder
        assert paths.is_approved(), "an already-ready session must be grandfathered"
        assert "grandfathered" in paths.approved.read_text()
        print("PASS  an agent that was already running keeps running")


def test_a_session_parked_at_the_gate_is_not_grandfathered():
    """The other half. A design waiting for review has no outcome yet, so it
    must keep its gate -- otherwise the feature would grandfather away the very
    thing it exists to do."""
    from agent.config import AgentConfig, BackendConfig
    from agent.runtime import open_runtime

    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp)
        paths = storage.session_paths(repo, "pending")
        paths.agent_dir.mkdir(parents=True, exist_ok=True)
        for name in ("graph.json", "nodes.json"):
            source = storage.builtin_agents_dir() / "default" / name
            (paths.agent_dir / name).write_text(source.read_text())

        cfg = AgentConfig(default=BackendConfig(provider="fake"), roles={})
        with open_runtime(cfg, paths) as runtime:
            runtime.checkpointer.put(
                {"configurable": {"thread_id": "pending:bootstrap",
                                  "checkpoint_ns": ""}},
                {"v": 1, "id": "y", "ts": "2026-01-01T00:00:00+00:00",
                 "channel_values": {"outcome": "",
                                    "human": {"purpose": "design_review"}},
                 "channel_versions": {}, "versions_seen": {}},
                {"source": "loop", "step": 2, "parents": {}},
                {},
            )
            assert runtime.resolve_folder(None) is None, "the gate must hold"
        assert not paths.is_approved()
        print("PASS  a design still awaiting review keeps its gate")

if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\nAll bootstrap tests passed.")
