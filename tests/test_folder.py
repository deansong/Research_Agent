"""
WHAT:  Tests for the agent-folder format -- schema, rendering and validation.
WHY:   Every rule here is a mistake a designer model will eventually make, so
       each one gets a test that proves we catch it and say something useful.
CONCEPT: None -- no LangGraph is involved, which is exactly the point. The
       format is testable without building a graph or calling a model.

Run with:   python tests/test_folder.py
"""

from __future__ import annotations

import copy
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from agent.agentfolder.load import AgentFolderError, load_agent_folder
from agent.agentfolder.render import render
from agent.agentfolder.validate import validate_folder

DEFAULT = pathlib.Path(__file__).resolve().parents[1] / "agent" / "builtin_agents" / "default"


def _folder(graph: dict, nodes: dict):
    """Write a throwaway folder and load it."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    (tmp / "graph.json").write_text(json.dumps(graph))
    (tmp / "nodes.json").write_text(json.dumps(nodes))
    return load_agent_folder(tmp)


def _default_pair():
    graph = json.loads((DEFAULT / "graph.json").read_text())
    nodes = json.loads((DEFAULT / "nodes.json").read_text())
    return copy.deepcopy(graph), copy.deepcopy(nodes)


def _codes(graph: dict, nodes: dict) -> set[str]:
    return {p.code for p in validate_folder(_folder(graph, nodes))}


def test_default_folder_is_valid():
    """The shipped agent must validate clean, or nothing else can be trusted."""
    problems = validate_folder(load_agent_folder(DEFAULT))
    assert problems == [], "\n".join(f"{p.code}: {p.where}: {p.message}" for p in problems)
    print("PASS  builtin_agents/default validates with zero problems")


def test_unreachable_node():
    """LangGraph compiles this happily. We must not."""
    graph, nodes = _default_pair()
    graph["nodes"].append({"name": "orphan", "kind": "agent"})
    nodes["orphan"] = copy.deepcopy(nodes["planner"])
    nodes["orphan"]["bump"] = []
    graph["edges"].append({"from": "orphan", "to": "__end__"})
    assert "unreachable" in _codes(graph, nodes)
    print("PASS  unreachable node caught (compile() would not)")


def test_end_unreachable():
    """The one that compiles fine and then dies with GraphRecursionError."""
    graph, nodes = _default_pair()
    # Remove every route to __end__: the orchestrator's default and /exit.
    graph["branches"][0]["default"] = "planner"
    nodes["human"]["commands"] = [
        c for c in nodes["human"]["commands"] if c["to"] != "__end__"
    ]
    # The 'finish' case goes to human with resume_to __end__, so drop that too.
    graph["branches"][0]["cases"] = [
        c for c in graph["branches"][0]["cases"] if c["when"] != "finish"
    ]
    codes = _codes(graph, nodes)
    assert "end_unreachable" in codes, codes
    print("PASS  unreachable END caught (compile() would not)")


def test_dead_end_node():
    graph, nodes = _default_pair()
    graph["edges"] = [e for e in graph["edges"] if e["from"] != "planner"]
    assert "dead_end" in _codes(graph, nodes)
    print("PASS  dead-end node caught (LangGraph silently treats it as END)")


def test_route_on_must_be_an_enum():
    graph, nodes = _default_pair()
    graph["branches"][0]["route_on"] = "task"       # a string field, not an enum
    assert "bad_route_field" in _codes(graph, nodes)

    graph, nodes = _default_pair()
    graph["branches"][0]["route_on"] = "nonexistent"
    assert "bad_route_field" in _codes(graph, nodes)
    print("PASS  route_on must name an enum output field")


def test_unhandled_enum_choice():
    """Because route_on is an enum, we can prove every value has somewhere to go."""
    graph, nodes = _default_pair()
    graph["branches"][0]["cases"] = [
        c for c in graph["branches"][0]["cases"] if c["when"] != "replan"
    ]
    assert "unhandled_case" in _codes(graph, nodes)
    print("PASS  an enum choice with no case and no default is caught")


def test_two_files_must_agree():
    graph, nodes = _default_pair()
    del nodes["planner"]
    assert "config_missing" in _codes(graph, nodes)

    graph, nodes = _default_pair()
    nodes["ghost"] = copy.deepcopy(nodes["planner"])
    assert "config_extra" in _codes(graph, nodes)
    print("PASS  graph.json and nodes.json name sets must match")


def test_bad_placeholder():
    graph, nodes = _default_pair()
    nodes["planner"]["prompts"]["first"] = "Plan this: {out.nosuchnode.field}"
    assert "bad_placeholder" in _codes(graph, nodes)

    graph, nodes = _default_pair()
    nodes["planner"]["prompts"]["first"] = "Use {var.never_set}"
    assert "bad_placeholder" in _codes(graph, nodes)
    print("PASS  placeholders referencing unknown nodes or vars are caught")


def test_ask_blocks_must_match_human_nodes():
    graph, nodes = _default_pair()
    del graph["edges"][0]["ask"]                     # discussor -> human, no ask
    assert "ask_missing" in _codes(graph, nodes)

    graph, nodes = _default_pair()
    graph["edges"][1]["ask"] = {"purpose": "x", "resume_to": "planner"}   # -> orchestrator
    assert "ask_stray" in _codes(graph, nodes)
    print("PASS  ask blocks are required into human nodes and rejected elsewhere")


def test_unknown_counter():
    graph, nodes = _default_pair()
    nodes["executor"]["refresh_on"] = "never_bumped"
    assert "unknown_counter" in _codes(graph, nodes)
    print("PASS  refresh_on must name a counter something bumps")


def test_reserved_output_field():
    graph, nodes = _default_pair()
    nodes["planner"]["output"].append({"name": "git_status", "type": "string"})
    assert "reserved_field" in _codes(graph, nodes)
    print("PASS  a node may not declare a capture-reserved output field")


def test_bad_json_and_bad_shape_report_the_file():
    tmp = pathlib.Path(tempfile.mkdtemp())
    (tmp / "graph.json").write_text("{not json")
    (tmp / "nodes.json").write_text("{}")
    try:
        load_agent_folder(tmp)
        raise AssertionError("expected AgentFolderError")
    except AgentFolderError as exc:
        assert "graph.json" in str(exc)

    tmp = pathlib.Path(tempfile.mkdtemp())
    (tmp / "graph.json").write_text(json.dumps({"format_version": 1, "name": "x",
                                                "entry": "a", "nodes": [{"name": "A-B",
                                                                         "kind": "agent"}]}))
    (tmp / "nodes.json").write_text("{}")
    try:
        load_agent_folder(tmp)
        raise AssertionError("expected AgentFolderError for the node name 'A-B'")
    except AgentFolderError as exc:
        assert "nodes.0.name" in str(exc), str(exc)
    print("PASS  bad JSON and bad node names name the file and the key")
    print("      (LangGraph itself accepts 'A-B' and '' as node names -- we do not)")


def test_renderer_leaves_json_examples_alone():
    """The reason we do not use str.format(): model-written prompts contain braces."""
    text = 'Reply like { "action": "execute" } for {task_brief}'
    out = render(text, {"task_brief": "ship it"})
    assert out == 'Reply like { "action": "execute" } for ship it', out

    # A placeholder that cannot resolve yet becomes empty, not an exception:
    # {out.executor.summary} is legitimately blank before the executor runs.
    assert render("[{out.executor.summary}]", {}) == "[]"
    print("PASS  renderer preserves JSON examples and tolerates unrun nodes")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\nAll folder tests passed.")
