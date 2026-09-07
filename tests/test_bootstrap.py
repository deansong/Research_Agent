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
            if "rejected" in prompt:
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
    from agent.bootstrap.prompts import DESIGNER_INSTRUCTIONS, worked_example

    rule = DESIGNER_INSTRUCTIONS.lower()
    assert "no node judges its own success" in rule, "the rule is missing"
    assert "read_only verifier" in rule, "the verifier must be read_only"
    assert "self-assessment" in rule, "the failure mode must be named"
    assert "8 cases" in rule, "the branch limit is why this is per-stage"

    # The exact shape lives in the per-turn prompt, which has no size cliff.
    shape = worked_example()
    assert "THE VERIFIER LOOP" in shape, "the worked shape is missing"
    assert '"when": "redo", "to": "build_scorer"' in shape, \
        "redo must be shown pointing back at the WORKER, not onwards"
    assert '"default": "human_review"' in shape, \
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
    from agent.bootstrap.prompts import DESIGNER_INSTRUCTIONS, worked_example

    instructions = len(DESIGNER_INSTRUCTIONS)
    assert instructions < SAFE_INSTRUCTIONS_CHARS, (
        f"DESIGNER_INSTRUCTIONS is {instructions} chars, over the measured "
        f"{SAFE_INSTRUCTIONS_CHARS} cliff. Turns will HANG, not fail. Move it "
        f"into worked_example() -- the prompt has no such limit."
    )

    prompt = len(worked_example())
    assert prompt < 20_000, (
        f"the per-turn prompt is {prompt} chars. No cliff is known there, but "
        f"nothing this large has been verified either -- measure before raising."
    )
    print(f"PASS  instructions {instructions}/{SAFE_INSTRUCTIONS_CHARS} "
          f"({SAFE_INSTRUCTIONS_CHARS - instructions} left), prompt {prompt}")


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
