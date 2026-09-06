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
        self.turn = 0

    def run_structured(self, *, thread_id, repo_path, access, developer_instructions,
                       prompt, output_model):
        self.turn += 1
        fields = set(output_model.model_fields)

        if {"task_brief", "graph", "nodes"} <= fields:
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
        {"discussor": backend, "designer": backend}, paths, InMemorySaver(),
        max_attempts=max_attempts,
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


def test_a_valid_design_is_written_and_promoted():
    backend, graph, config, paths = _run([_MINIMAL_AGENT], ["/plan"])
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
    backend, graph, config, paths = _run([_MINIMAL_AGENT], ["/plan"])
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
        [_broken_design(), _broken_design(), _MINIMAL_AGENT], ["/plan"]
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
    backend, graph, config, paths = _run([_broken_design()], ["/plan"], max_attempts=3)
    values = graph.get_state(config).values

    assert backend.design_calls == 3, f"expected exactly 3 attempts, got {backend.design_calls}"
    assert values["human"]["purpose"] == "design_failed", values["human"]
    assert not values.get("outcome"), "should not claim to be ready"
    assert graph.get_state(config).next == ("human",), "should be waiting for the human"
    assert not paths.has_agent(), "a broken design must not be promoted"
    print("PASS  repair loop gives up after max_attempts and asks the human")


def test_use_falls_back_to_the_builtin_agent():
    backend, graph, config, paths = _run([_broken_design()], ["/plan", "/use"], max_attempts=2)
    values = graph.get_state(config).values
    assert values["outcome"] == "ready", values.get("outcome")
    assert not paths.has_agent(), "/use should not write a session agent"
    # cli.py sees no session agent and resolves the shipped default.
    print("PASS  /use gives up designing and falls back to the built-in agent")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\nAll bootstrap tests passed.")
