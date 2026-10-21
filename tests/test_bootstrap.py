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
        self.design_prompts: list[str] = []
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
            self.design_prompts.append(prompt)
            # Matched on the actual marker, not on the word "rejected"
            # appearing somewhere in the prompt: the designer's first prompt
            # carries the worked example and the research skeleton, and the
            # moment either of those mentioned a rejected design, every first
            # attempt was counted as a repair.
            if prompt.startswith(REPAIR_PREFIX[:32]):
                self.repair_prompts.append(prompt)
            design = self.designs[index]
            if callable(design):
                # A function of the design the prompt ends with -- the
                # starting design, or the one being repaired.
                # With no starting design (no plan to build one from) it is
                # handed the fake backend's canned agent instead.
                marker = '\n\n{\n "graph"'
                design = design(json.loads(prompt[prompt.rindex(marker) + 2:])
                                if marker in prompt else copy.deepcopy(_MINIMAL_AGENT))
            data = output_model.model_validate(
                {"task_brief": "the brief", "rationale": "", **design})
        else:
            data = output_model.model_validate(
                {"reply": f"reply-{self.turn}", "requirements": "reqs",
                 "questions": [{"topic": "Code", "question": "q?",
                                "suggestion": "s", "why": "w"}]}
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
         "check": "notes.md names three conventions", "substeps": []},
        {"id": "2", "title": "Write the thing", "detail": "", "gate": True,
         "check": "pytest passes",
         "substeps": [{"id": "2.1", "title": "draft", "detail": "first pass"},
                      {"id": "2.2", "title": "verify", "detail": ""}]},
    ],
}


def _accept(shown: dict) -> dict:
    """The designer's simplest valid answer: the starting design, unchanged
    but for its name -- the same thing the fake backend answers."""
    shown["graph"]["name"] = "fake-generated"
    return shown


def _broken_design():
    """A design that is schema-valid but a broken graph: END is unreachable --
    every command that would end the run is taken away."""
    def broken(shown: dict) -> dict:
        shown = copy.deepcopy(shown)
        shown["graph"]["name"] = "broken"
        for node in shown["nodes"]:
            node["commands"] = [c for c in node.get("commands", [])
                                if c["to"] != "__end__"]
        return shown
    return broken


def _run(designs: list[dict], answers: list[str], *, max_attempts: int = 3):
    tmp = pathlib.Path(tempfile.mkdtemp())
    paths = storage.session_paths(tmp, "test")
    backend = ScriptedBackend(designs)
    graph = build_bootstrap_graph(
        {"discussor": backend, "planner": backend, "designer": backend},
        paths, InMemorySaver(), max_attempts=max_attempts,
        # The repair loop is exercised on the pipeline, whose graph the
        # designer writes; a hub's graph is fixed by code.
        topology="pipeline",
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
    backend, graph, config, _ = _run([_accept], ["more detail", "and more"])
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
    backend, graph, config, paths = _run([_accept], ["/plan", "/approve"])
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
        [_accept], ["/plan", "/approve", "/approve"])
    values = graph.get_state(config).values

    assert values["outcome"] == "ready", values.get("outcome")
    assert paths.is_approved(), "approval must be on disk, not only in state"
    assert "fake-generated" in paths.approved.read_text()
    print("PASS  approving writes a marker that survives the process")


def test_a_valid_design_is_written_and_promoted():
    backend, graph, config, paths = _run(
        [_accept], ["/plan", "/approve", "/approve"])
    values = graph.get_state(config).values

    assert values["outcome"] == "ready", values.get("outcome")
    assert paths.has_agent(), "agent.tmp was not promoted to agent/"
    assert not paths.staging.exists(), "staging directory should be gone after the rename"

    graph_json = json.loads((paths.agent_dir / "graph.json").read_text())
    nodes_json = json.loads((paths.agent_dir / "nodes.json").read_text())
    assert graph_json["name"] == "fake-generated"
    # The writer converts the designer's LIST of nodes into an object keyed by
    # name -- strict structured output cannot emit an open-ended dict.
    # One fixed work node per leaf plan step, plus checkers and `review`.
    from agent.bootstrap.blueprint import work_nodes

    assert {w["name"] for w in work_nodes(_PLAN)} | {"review"} <= set(nodes_json), \
        nodes_json.keys()
    # And it splits agent fields from human fields by kind.
    assert "commands" in nodes_json["review"] and "backend" not in nodes_json["review"]
    work = work_nodes(_PLAN)[0]["name"]
    assert "backend" in nodes_json[work] and "commands" not in nodes_json[work]
    print("PASS  a valid design is written, split by kind, and promoted to agent/")


def test_the_brief_is_handed_over():
    backend, graph, config, paths = _run([_accept], ["/plan", "/approve"])
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
        [_broken_design(), _broken_design(), _accept],
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


def test_repair_loop_gives_up_on_the_edits_and_offers_the_starting_agent():
    """Always broken: it must stop, not loop -- and end no worse than it began.

    The designer edits an agent built from the plan that already passed, so
    when its edits are still rejected at the limit, that agent is what the
    human is asked to review, with the rejected changes listed.
    """
    backend, graph, config, paths = _run([_broken_design()], ["/plan", "/approve"],
                                         max_attempts=3)
    values = graph.get_state(config).values

    assert backend.design_calls == 3, f"expected exactly 3 attempts, got {backend.design_calls}"
    assert values["human"]["purpose"] == "design_review", values["human"]
    assert "REJECTED CHANGES" in values["human"]["context"]
    assert "end_unreachable" in values["human"]["context"], "say what was wrong"
    assert not values.get("outcome"), "should not claim to be ready"
    assert graph.get_state(config).next == ("human",), "should be waiting for the human"
    assert paths.has_agent(), "the starting agent is on disk to review"
    graph_json = json.loads((paths.agent_dir / "graph.json").read_text())
    assert graph_json["name"] != "broken", "the broken edit must not be promoted"
    print("PASS  repair loop gives up on the edits and offers the starting agent")


def test_repair_loop_gives_up_and_asks_the_human():
    """With no starting agent to fall back to, it stops and hands control back."""
    from unittest.mock import patch

    with patch("agent.bootstrap.nodes.designer._starting_design", return_value=({}, {})):
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
    from unittest.mock import patch

    with patch("agent.bootstrap.nodes.designer._starting_design", return_value=({}, {})):
        backend, graph, config, paths = _run([_broken_design()],
                                             ["/plan", "/approve", "/use"], max_attempts=2)
    values = graph.get_state(config).values
    assert values["outcome"] == "ready", values.get("outcome")
    assert not paths.has_agent(), "/use should not write a session agent"
    # cli.py sees no session agent and resolves the shipped default.
    print("PASS  /use gives up designing and falls back to the built-in agent")


def test_planning_stops_for_approval_before_designing():
    """/plan reaches the PLANNER, and the designer waits for /approve."""
    backend, graph, config, paths = _run([_accept], ["/plan"])
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
    backend, graph, config, paths = _run([_accept], ["/plan"])

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
    backend, graph, config, paths = _run([_accept], ["/plan"])
    paths.plan.write_text("{ this is not json")

    graph.invoke(Command(resume="/approve"), config=config)
    approved = graph.get_state(config).values["design"]["plan"]
    assert approved["steps"], "should have fallen back to the planner's own plan"
    print("PASS  invalid plan.json falls back instead of crashing")


def test_revise_sends_the_plan_back():
    backend, graph, config, paths = _run(
        [_accept], ["/plan", "/revise make it shorter"])
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
    """A real agent folder for a four-stage plan, as the designer starts from.

    Built by the same code the designer uses (blueprint.starting_design and
    spec.compile_design) and written by the REAL writer, so the gate tests
    run against the kind of folder a design actually produces. Its runner
    owns 3.1 and every path reaches the `review` human node.
    """
    import types

    from agent.bootstrap.blueprint import starting_design
    from agent.bootstrap.nodes.writer import make_writer
    from agent.bootstrap.spec import compile_design

    plan = {"summary": "four stages", "steps": [
        {"id": "1", "title": "Code", "substeps": [
            {"id": "1.1", "title": "Write the dataloader"},
            {"id": "1.2", "title": "Write the method"}]},
        {"id": "2", "title": "Experiment design", "substeps": [
            {"id": "2.1", "title": "Write the config"}]},
        {"id": "3", "title": "Run", "substeps": [
            {"id": "3.1", "title": "our method"}]},
        {"id": "4", "title": "Analysis and report", "substeps": [
            {"id": "4.1", "title": "Write the report"}]},
    ]}
    proposal = compile_design(starting_design(plan, topology="pipeline"), plan)
    paths = types.SimpleNamespace(staging=str(tmp / "agent"), brief=str(tmp / "brief.md"))
    make_writer(paths)({"design": {"proposal": proposal}})
    return tmp / "agent"


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
        {"id": "1", "title": "Code", "check": "pytest"},
        {"id": "3", "title": "Run", "gate": True,
         "substeps": [{"id": "3.1", "title": "Launch the sweep"}]},
    ]}

    with tempfile.TemporaryDirectory() as tmp:
        folder = load_agent_folder(_research_folder(pathlib.Path(tmp)))

        # Stage 3 is gated and NOTHING owns the bare id "3" -- nodes own
        # substeps. The gate is satisfied through the runner, which owns 3.1
        # and reaches `review`. Without that reading every gate a person ticks
        # on a stage would reject the design.
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
        assert "Step 3" in blind and "reaches a human node" in blind, blind
    print("PASS  a human-gated step must reach a human, not merely coexist")


def test_the_discussor_asks_about_the_shape_of_the_work_too():
    """The second half of the interview, which is what the graph is built from.

    The research questions settle what the experiment IS. These settle what
    the STEPS are -- and the answers become plan steps and then nodes, close
    to one each, so a vague answer here is a vague node two phases later.
    """
    from agent import skills
    from agent.bootstrap.prompts import DISCUSSOR_INSTRUCTIONS

    # What it is SENT: the persona, and the checklist skill in its prompt.
    text = (DISCUSSOR_INSTRUCTIONS + "\n" + skills.experiment_design()).lower()
    for topic in ("data", "environment", "code", "experiment",
                  "checks", "gates"):
        assert topic in text, f"the discussor never asks about {topic}"

    # The pedantic one: "a few configurations" is what produces a graph that
    # cannot say when it is finished.
    assert "ablations" in text and "hyperparameters" in text and "seeds" in text
    # All at once, each with a suggestion: one question per turn made a dozen
    # questions a dozen round trips.
    flat = " ".join(text.split())
    assert "all in this one turn" in flat and "suggested answer" in flat
    assert "(assumed)" in text, "an unanswered question keeps its suggestion"
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


def _designer_turn() -> str:
    """Everything the designer is sent, whitespace-normalised.

    Instructions AND prompt, because which of the two a rule sits in is an
    artefact of one measured limit -- developer_instructions hang a turn past
    ~6 KB, prompts have no known cliff -- and material has moved across that
    line four times now. Both arrive in the same turn, so a test that pins a
    rule to one of them fails on a reorganisation that changed nothing about
    what the designer is told.
    """
    from agent.bootstrap.prompts import (CONTROL_FLOW, DESIGNER_INSTRUCTIONS,
                                         PROMPT_REFERENCE, RESEARCH_CONTEXT,
                                         VERIFIER_LOOP)

    return " ".join((DESIGNER_INSTRUCTIONS + RESEARCH_CONTEXT + VERIFIER_LOOP
                     + PROMPT_REFERENCE + CONTROL_FLOW).lower().split())


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

    # Whitespace-normalised, like the designer half below and for the same
    # reason: these are wrapped prose. This assertion set has now failed three
    # times on a reflow that changed nothing about what the model is told --
    # the last one split "OUT of scope" across two lines.
    flat = lambda text: " ".join(text.lower().split())

    from agent import skills

    discussor = flat(DISCUSSOR_INSTRUCTIONS + " " + skills.experiment_design())
    # The two people leave out, and the two that waste the most time.
    for essential in ("compute", "metric", "baseline", "seeds", "hardware"):
        assert essential in discussor, f"the discussor never asks about {essential}"
    assert "refute" in discussor, "a claim needs a way to be wrong"

    planner = flat(PLANNER_INSTRUCTIONS)
    assert "reproduce the baseline first" in planner, \
        "a number you cannot reproduce is not a comparison"
    # Running and analysing are separate STAGES now, which is a stronger
    # guarantee than the sentence that used to assert it: the plan's shape
    # keeps them apart rather than a paragraph asking nicely. Merging them is
    # what makes the second one quietly not happen.
    # Anchored to the stage TABLE, by the descriptor after each name, because
    # the bare names also appear in the worked substep example below it -- the
    # first version of this assertion matched there and let a merged
    # "3. Run and analyse" through.
    for stage in ("1. code what has to be written",
                  "2. experiment design what will be run",
                  "3. run running it, and keeping every result",
                  "4. analysis what the numbers mean"):
        assert stage in planner, f"the planner no longer names stage {stage!r}"
    assert "seeds" in planner and "config" in planner, "the variables must be pinned"
    assert "out of scope" in planner

    # Over the whole turn rather than one field, because these have also
    # failed on material moving between the instructions and the prompt --
    # which likewise changes nothing about what the designer is told.
    designer = _designer_turn()
    assert "never overwritten" in designer, "a re-run must add a result"
    assert "per config and seed" in designer, "results are per config and seed"
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

    from agent import skills

    sent = DISCUSSOR_INSTRUCTIONS + " " + skills.experiment_design()
    for name, lowered in (("discussor", " ".join(sent.lower().split())),
                          ("planner", " ".join(PLANNER_INSTRUCTIONS.lower().split())),
                          ("designer", _designer_turn())):
        assert "not research" in lowered or "not an experiment" in lowered, \
            f"the {name} has no way out of the research framing"
        assert "refactor" in lowered, \
            f"the {name} should name the ordinary case it must not distort"
    print("PASS  all three say what to do when the work is not an experiment")


def test_the_designer_is_told_it_is_building_a_langgraph():
    """It was never told what it was producing for.

    The format was described as if it were bespoke -- edges, branches,
    "__end__" -- when it is a thin declarative layer over StateGraph, and the
    model has a great deal of knowledge about StateGraph that it was being
    kept from using.

    More than a label: four of the rules were arbitrary-sounding assertions
    until the runtime explained them. A node is atomic BECAUSE the checkpoint
    is per node; a human node does no work BECAUSE interrupt() re-runs it
    from the first line; a loop needs an exit BECAUSE the recursion limit
    kills it rather than hanging.
    """
    from agent.bootstrap.prompts import PIPELINE_DESIGNER_INSTRUCTIONS as DESIGNER_INSTRUCTIONS

    text = DESIGNER_INSTRUCTIONS
    assert "LangGraph" in text or "LANGGRAPH" in text, "name the framework"

    # The mapping, so the model can use what it knows about each call.
    for api in ("add_node", "add_edge", "add_conditional_edges", "END",
                "interrupt()", "Command(resume=...)"):
        assert api in text, f"{api} is not mapped to anything in the format"

    lowered = " ".join(text.lower().split())
    # The four consequences, each of which justifies a rule elsewhere.
    assert "one shared state" in lowered
    assert "checkpointed after every node" in lowered, \
        "this is why a node is atomic"
    assert "re-runs the node" in lowered, \
        "this is why a human node does no work of its own"
    assert "recursion_limit" in lowered, "this is why a loop needs an exit"
    print("PASS  the designer knows it is building a LangGraph StateGraph")


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
    assert "remembers what it already rejected" in " ".join(text.split()), \
        "the attempt count lives in its conversation"
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

    If a field is in NodeSpec, the designer can emit it, and its instructions
    have to say what it means. The bulky fields -- prompts, outputs, access --
    are not the designer's any more; bootstrap/spec.py derives them.
    """
    from agent.bootstrap.prompts import PIPELINE_DESIGNER_INSTRUCTIONS as DESIGNER_INSTRUCTIONS
    from agent.bootstrap.schemas import NodeSpec

    missing = [field for field in NodeSpec.model_fields
               if field != "name" and f"`{field}`" not in DESIGNER_INSTRUCTIONS]
    assert not missing, (
        f"the designer can emit {missing} and its instructions never say what "
        f"they mean.")
    print(f"PASS  all {len(NodeSpec.model_fields) - 1} spec fields are documented")


def test_the_prompt_still_shows_the_shapes_that_need_showing():
    """Three JSON shapes prose cannot substitute for.

    Each is a thing the designer has to emit exactly right and would
    otherwise be guessing at: the orchestrator topology (every worker's edge
    going back), a human node's `commands`, and a branch with an `ask` on one
    of its cases.
    """
    from agent.bootstrap.prompts import CONTROL_FLOW

    # The orchestrator shape now lives only in CONTROL_FLOW. The default
    # agent's graph.json used to carry it too, and carrying it twice was what
    # the prompt budget was being spent on.
    assert '"route_on": "action"' in CONTROL_FLOW, "the orchestrator branch is gone"
    assert '"to": "orchestrator"' in CONTROL_FLOW, \
        "workers must be shown returning to the orchestrator"

    # The other two are shown in the STARTING DESIGN, which ends every
    # design prompt -- built from the session's own plan.
    from agent.bootstrap.blueprint import starting_design

    shown = json.dumps(starting_design(_PLAN, topology="pipeline"))
    assert '"commands"' in shown, "a human node's commands must be shown"
    assert '"sets"' in shown, "setting a var from a command must be shown"
    assert '"ask"' in shown, "a branch case with an ask must be shown"

    assert '"when": "finish"' in CONTROL_FLOW, "the orchestrator's exit case"
    print("PASS  orchestrator, commands and a branch ask are all shown as JSON")


def test_the_prompt_size_test_covers_the_part_that_actually_varies():
    """The static budget was guarding the smaller half.

    The four static sections are 18 KB and the test asserted they stay under
    20 KB. On a real session the assembled prompt was 40 KB: the transcript
    and the plan are session-derived, and together they were bigger than
    everything the test measured. Nothing was wrong with the 20 KB figure --
    it was answering a different question from the one it looked like it was
    answering.
    """
    from agent.bootstrap.nodes.designer import TRANSCRIPT_BUDGET, _conversation

    # The transcript is the only unbounded input: it grows with every turn
    # for the rest of the session, so it is the one that needs a ceiling.
    entries = [{"role": "human", "text": "x" * 5000} for _ in range(20)]
    kept = _conversation({"transcript": entries})
    assert len(kept) < TRANSCRIPT_BUDGET + 2000, len(kept)
    assert "earliest message(s) are omitted" in kept, \
        "a prompt that has quietly lost context is worse than a short one"

    # Newest kept, because the plan already carries what the early
    # conversation produced.
    entries = [{"role": "human", "text": f"message {i}"} for i in range(5)]
    kept = _conversation({"transcript": entries})
    assert "message 4" in kept and "earliest" not in kept, kept
    print(f"PASS  the transcript is capped at {TRANSCRIPT_BUDGET} chars, "
          f"and says when it trimmed")


def test_the_designer_is_actually_sent_the_plan():
    """The one input the designer exists to work from, and it was missing.

    Measured on a real 13-step session: 0 of the 13 plan step titles appeared
    anywhere in the designer's prompt, which nonetheless ended "design the
    agent from the plan above". So it was inventing stages from the
    conversation, and every `steps: [...]` it wrote named ids it had never
    read -- which makes {my_steps}, the per-step `check` and the human gates
    all point at nothing.

    Driven through the real graph rather than by calling the renderer,
    because the renderer was never the broken part: the prompt simply did not
    interpolate it.
    """
    backend, graph, config, _ = _run([_accept], ["/plan", "/approve"])

    assert backend.design_prompts, "the designer never ran"
    prompt = backend.design_prompts[0]

    for step in _PLAN["steps"]:
        assert step["title"] in prompt, f"step {step['id']} is not in the prompt"
    assert "2.1" in prompt and "draft" in prompt, "substep ids must be there too"

    # The two fields that turn the plan into a design, not just its titles.
    assert "check: notes.md names three conventions" in prompt, prompt[:400]
    assert "human gate" in prompt, "a gated step must be visible as gated"

    # And the brief is sent ONCE. It is byte-identical to the transcript's
    # first entry, and both were being interpolated -- 6.5 KB twice on the
    # measured run.
    assert prompt.count("do the thing") <= 1, \
        "the request is in the transcript already; do not send it twice"
    print("PASS  the designer receives the plan it is told to design from")


def test_the_gate_check_is_wired_into_the_validator():
    """A test that calls a function nothing else calls proves nothing.

    _gate_problems shipped with no caller: the line meant to invoke it was
    lost in a failed edit, and its own test passed because it called the
    function directly. So this one goes through _check -- the function the
    validator node actually uses -- and the earlier test keeps the detail.
    """
    from agent.bootstrap.nodes.validator import _check

    with tempfile.TemporaryDirectory() as tmp:
        folder = _research_folder(pathlib.Path(tmp))

        # A gated step nobody owns: nothing will ever ask about it.
        gated = {"steps": [{"id": "99", "title": "Publish the results",
                            "gate": True}]}
        problem = _check(folder, gated)
        assert "Step 99" in problem, problem
        assert "Publish the results" in problem, problem

        # And with no gates the same folder passes, so the check above is
        # about the gate and not about the folder.
        assert _check(folder, {"steps": [{"id": "99", "title": "x"}]}) == ""
    print("PASS  the gate check runs from _check, where the validator calls it")


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
        f"per-turn prompt instead of growing this."
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
    from agent.bootstrap.prompts import PIPELINE_DESIGNER_INSTRUCTIONS as text

    lowered = " ".join(text.lower().split())
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
    from agent.bootstrap.blueprint import starting_design
    from agent.bootstrap.prompts import VERIFIER_LOOP
    from agent.bootstrap.prompts import PIPELINE_DESIGNER_INSTRUCTIONS as DESIGNER_INSTRUCTIONS

    rule = DESIGNER_INSTRUCTIONS.lower()
    assert "no node judges its own success" in rule, "the rule is missing"
    assert "read_only verifier" in rule, "the verifier must be read_only"
    assert "self-assessment" in rule, "the failure mode must be named"
    assert "8 cases" in rule, "the branch limit is why this is per-stage"

    # The exact shape is shown in the STARTING DESIGN that ends the prompt.
    assert "THE VERIFIER LOOP" in VERIFIER_LOOP, "the rationale for the shape is missing"
    design = starting_design(_PLAN, topology="pipeline")
    roles = {n["name"]: n["role"] for n in design["nodes"]}
    for branch in design["graph"]["branches"]:
        redo = [c["to"] for c in branch["cases"] if c["when"].startswith("redo")]
        assert redo and all(roles[t] in ("worker", "runner") for t in redo), \
            "redo must point back at the WORKER, not onwards"
        assert roles[branch["default"]] == "human", \
            "an unhandled verdict must reach a human, not silently end the run"
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
                                         PROMPT_REFERENCE, VERIFIER_LOOP)

    instructions = len(DESIGNER_INSTRUCTIONS)
    assert instructions < SAFE_INSTRUCTIONS_CHARS, (
        f"DESIGNER_INSTRUCTIONS is {instructions} chars, over the measured "
        f"{SAFE_INSTRUCTIONS_CHARS} cliff. Turns will HANG, not fail. Move it "
        f"into the per-turn prompt -- it has no such limit."
    )

    # Every piece designer.py appends, not just the first one. Measuring one
    # of four is how a budget is quietly overspent: one part shrank while
    # the total grew, and a test on that part would have called it an
    # improvement.
    parts = {
        "VERIFIER_LOOP": len(VERIFIER_LOOP),
        "PROMPT_REFERENCE": len(PROMPT_REFERENCE),
        "CONTROL_FLOW": len(CONTROL_FLOW),
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

    The cutting now happens in two places, and both are checked: the PLANNER
    cuts the work into substeps that each end in an artefact, and the
    DESIGNER may not re-cut it -- code fixes one work node per leaf step, so
    a merged node is not something a design can even express.
    """
    from agent.bootstrap.prompts import PLANNER_INSTRUCTIONS
    from agent.bootstrap.prompts import PIPELINE_DESIGNER_INSTRUCTIONS as DESIGNER_INSTRUCTIONS

    designer = " ".join(DESIGNER_INSTRUCTIONS.lower().split())
    assert "one work node per top-level plan step" in designer, "the contract is unstated"
    assert "one substep per turn" in designer, "a step's substeps are turns, not nodes"
    assert "cannot rename, merge, split" in designer, "re-cutting must be ruled out"

    planner = " ".join(PLANNER_INSTRUCTIONS.lower().split())
    assert "each step becomes one node" in planner, "the planner must know it cuts nodes"
    assert "at most six top-level steps" in planner, "or the agent grows to 30 nodes"
    assert "ends in a file" in planner, "count artefacts, not topics"

    # And the line that pulled the other way must be gone.
    assert "three good nodes beat eight" not in designer + planner, \
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


# ---- a substep carries its own check and gate ------------------------------
#
# The four-stage plan puts the STAGES at the top level, so the unit a single
# node owns -- and therefore the unit a verifier applies a check to -- is a
# substep. These three tests are the three places that fact has to be honoured;
# each one fails on its own if only the schema field is added.


def test_a_substep_carries_its_check_to_the_node_that_owns_it():
    """The schema field alone buys nothing: `steps_for` is what puts the check
    in front of the node, and a node told how its work will be judged can aim
    at that instead of guessing."""
    from agent.bootstrap.nodes.planner import steps_for

    plan = {"steps": [{"id": "1", "title": "Code", "substeps": [
        {"id": "1.1", "title": "unified dataloader",
         "check": "pytest tests/test_loader.py passes"},
        {"id": "1.2", "title": "our method", "check": "trains one step"},
    ]}]}

    own = steps_for(plan, ["1.1"])

    assert "unified dataloader" in own
    assert "check: pytest tests/test_loader.py passes" in own
    # And only its own: the whole point of substeps is that three nodes do not
    # each receive all three checks.
    assert "trains one step" not in own
    print("PASS  a substep's check reaches the node that owns it, and only it")


def test_a_gate_on_a_substep_shows_in_the_outline():
    """The outline collapses each stage to one line, and it is the only thing
    every OTHER node is shown. A gate invisible there is a stop in the run that
    nothing downstream can see coming."""
    from agent.bootstrap.nodes.planner import outline

    plan = {"steps": [
        {"id": "1", "title": "Code"},
        {"id": "3", "title": "Run", "substeps": [
            {"id": "3.1", "title": "our method", "gate": True},
            {"id": "3.2", "title": "baselines"},
        ]},
    ]}

    lines = outline(plan).splitlines()

    assert len(lines) == 2, "the outline is one line per stage"
    assert "[human gate]" not in lines[0]
    assert "[human gate]" in lines[1], "a gated substep left the stage unmarked"
    print("PASS  a gate on a substep marks its stage in the outline")


def test_a_gated_substep_with_no_human_node_is_rejected():
    """The same promise `test_a_gated_step_with_no_human_node_is_rejected`
    covers, one level down -- and this is the level that matters, because what
    a person actually approves ("start the long training run") is a substep.

    Walking only the top level lets every gate that matters through unchecked,
    and the run then LOOKS successful, which is the worst shape available.
    """
    from agent.agentfolder.load import load_agent_folder
    from agent.bootstrap.nodes.validator import _gate_problems

    with tempfile.TemporaryDirectory() as tmp:
        folder = load_agent_folder(_research_folder(pathlib.Path(tmp)))

        owned = {"steps": [{"id": "3", "title": "Run", "substeps": [
            {"id": "3.1", "title": "Launch the sweep", "gate": True}]}]}
        assert _gate_problems(folder, owned) == "", "a gated substep an owning node reaches"

        orphan = {"steps": [{"id": "9", "title": "Publish", "substeps": [
            {"id": "9.9", "title": "Publish", "gate": True}]}]}
        problem = _gate_problems(folder, orphan)
        assert "9.9" in problem and "no node lists it" in problem, problem
    print("PASS  a gated SUBSTEP is checked against the graph, not just a step")


def test_a_gate_on_a_whole_stage_is_satisfied_by_its_substeps_owners():
    """Nodes own SUBSTEPS, so nothing owns a bare stage id.

    A person ticking "human gate" on stage 3 in the plan editor is asking to
    approve before the running starts -- an entirely reasonable thing to want.
    Looking for a node whose `steps` contains "3" finds none, and the design
    phase then rejects every design with a complaint about an id that was
    never meant to be owned directly.
    """
    from agent.agentfolder.load import load_agent_folder
    from agent.bootstrap.nodes.validator import _gate_problems

    with tempfile.TemporaryDirectory() as tmp:
        folder = load_agent_folder(_research_folder(pathlib.Path(tmp)))

        stage = {"steps": [{"id": "3", "title": "Run", "gate": True,
                            "substeps": [{"id": "3.1", "title": "our method"}]}]}
        assert _gate_problems(folder, stage) == "", _gate_problems(folder, stage)

        # And it is still not satisfied by a stage nobody's nodes touch at all.
        nowhere = {"steps": [{"id": "8", "title": "Publish", "gate": True,
                              "substeps": [{"id": "8.1", "title": "upload"}]}]}
        assert "no node lists it" in _gate_problems(folder, nowhere)
    print("PASS  a gate on a stage is satisfied through the nodes owning its substeps")


def test_the_approval_screen_shows_the_gates_and_the_checks():
    """What /approve is actually approving.

    The gate is where the run will STOP and wait for a person; the check is
    the sentence a verifier node gets built to apply. Neither was shown on
    this screen, so somebody could approve a plan without seeing the two
    fields that decide what the agent does when something goes wrong -- and
    with four stages the gate is nearly always on a substep.
    """
    from agent.bootstrap.nodes.planner import render_plan

    text = render_plan({"steps": [
        {"id": "1", "title": "Code", "substeps": [
            {"id": "1.1", "title": "dataloader", "check": "pytest passes"}]},
        {"id": "3", "title": "Run", "substeps": [
            {"id": "3.1", "title": "our method", "gate": True}]},
    ]})

    assert "check: pytest passes" in text, "a substep's check is not shown"
    assert "[human gate]" in text, "the gate is not shown"
    # On the gated line, not floating somewhere else in the screen.
    gated = [ln for ln in text.splitlines() if "[human gate]" in ln]
    assert len(gated) == 1 and "our method" in gated[0], gated
    print("PASS  the approval screen shows both gates and checks")


def test_the_design_phase_nodes_record_what_they_were_sent():
    """Through the real node, not through activity.exchange.

    activity.py's own tests all pass with the call site deleted -- a helper
    can be perfect and never run, and this project has shipped exactly that
    before. The design-phase nodes are hand-written rather than built from
    the node template, so wiring one does not wire the others: each has to be
    checked where it actually calls the backend.
    """
    import tempfile

    from agent import activity
    from agent.backends.fake import FakeBackend
    from agent.bootstrap.nodes import make_discussor

    with tempfile.TemporaryDirectory() as tmp:
        node = make_discussor(FakeBackend(model="m"))
        node({
            "repo_path": tmp,
            "session_dir": tmp,
            "user_request": "compare two things",
            "transcript": [],
            "providers": {},
            "usage_by_role": {},
        })

        turn = activity.turns(tmp, "discussor")[0]

        assert turn.sent, "the discussor recorded no prompt"
        assert "compare two things" in turn.sent["prompt"]
        # No thread yet on the opening turn, which is what `first` means.
        assert turn.sent["template"] == "first"
        assert turn.sent["instructions"], "the developer message was not kept"
        # And what it returned, which was recorded nowhere before.
        assert turn.summary.get("reply"), turn.summary
    print("PASS  a design-phase node records the prompt it sent and the answer")


def test_the_discussor_asks_everything_at_once_with_a_suggestion_each():
    """One message, every open question numbered, each with the answer it will
    assume -- so the human answers only what they disagree with."""
    from agent.bootstrap.nodes.discussor import _ask
    from agent.bootstrap.schemas import DiscussorQuestion

    text = _ask([
        DiscussorQuestion(topic="Experiment design", question="Which dataset?",
                          suggestion="AG News, test split", why="sets the data node"),
        DiscussorQuestion(topic="Run", question="How many seeds?",
                          suggestion="3", why="decides how long the run is"),
    ])
    assert "1. [Experiment design] Which dataset?" in text
    assert "   Suggested: AG News, test split" in text
    assert "2. [Run] How many seeds?" in text and "   Suggested: 3" in text
    assert "/plan" in text and "suggestion stands" in text
    assert _ask([]) == ""


def test_the_hub_designer_is_told_the_shape_is_fixed_and_what_it_designs():
    """A hub's graph is written by code; the designer shapes what each node
    is told. Its instructions must say both, and stay under Codex's limit."""
    from agent.backends.codex import SAFE_INSTRUCTIONS_CHARS
    from agent.bootstrap.prompts import DESIGNER_INSTRUCTIONS

    flat = " ".join(DESIGNER_INSTRUCTIONS.lower().split())
    for rule in ("one work node per top-level plan step", "cannot add, remove, rename",
                 "validator", "the orchestrator", "where the fault started",
                 "acl-style paper", "plan_issues", "`instructions`", "`tier`",
                 "`outputs`"):
        assert rule in flat, rule
    assert len(DESIGNER_INSTRUCTIONS) < SAFE_INSTRUCTIONS_CHARS * 0.6
