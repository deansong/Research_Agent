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


def test_one_incomplete_answer_is_one_problem_not_forty():
    """The report a real failure produced, and why it made things worse.

    The designer returned 20 node names, no edges, no branches and a single
    node entry. That is one fact -- it stopped early -- and it came back as
    19 config_missing + 8 dead_end + 15 unreachable = 42 errors. The repair
    prompt is built from this list, so the designer's next attempt arrived
    flooded with derived noise, and the human's screen was 42 lines of
    wreckage rather than the cause.
    """
    graph, nodes = _default_pair()
    graph["nodes"] += [{"name": f"stage_{i}", "kind": "agent"} for i in range(6)]

    problems = validate_folder(_folder(graph, nodes))
    codes = [p.code for p in problems]

    assert codes.count("incomplete_design") == 1, codes
    assert "config_missing" not in codes, "the per-node list is the noise"
    # Suppressed because their answers follow from the gap, not from a second
    # mistake -- a node with no entry has no edges either.
    assert "dead_end" not in codes and "unreachable" not in codes, codes

    message = next(p.message for p in problems if p.code == "incomplete_design")
    assert "6 have no entry" in message, message
    assert "'stage_0'" in message, "name enough of them to recognise the gap"
    print(f"PASS  an answer that stopped early is 1 problem, not "
          f"{len(graph['nodes'])}")


def test_a_couple_of_missing_entries_are_still_named_individually():
    """Below the threshold nothing changes, and that is deliberate.

    Two missing entries is an omission, not a truncation. Naming them is the
    most useful thing to say, and collapsing them into "the design is
    incomplete" would be throwing away the answer.
    """
    graph, nodes = _default_pair()
    graph["nodes"].append({"name": "extra", "kind": "agent"})
    graph["edges"].append({"from": "extra", "to": "__end__"})

    codes = [p.code for p in validate_folder(_folder(graph, nodes))]
    assert codes.count("config_missing") == 1, codes
    assert "incomplete_design" not in codes, codes
    print("PASS  one missing entry is still reported as itself")


def test_one_command_name_may_serve_two_gates():
    """What a real design asked for and was refused.

    /approve at a design review goes to the implementation node; /approve at
    a findings review goes to "__end__". Same word, same meaning to whoever
    types it, two transitions -- which is exactly what `purposes` is for. The
    duplicate check rejected it on the name alone, so the design could not be
    expressed at all, and the natural workaround (/approve_design,
    /approve_report) makes the person learn the graph's internals.
    """
    graph, nodes = _default_pair()
    nodes["human"]["commands"] = [
        {"name": "approve", "to": "executor", "purposes": ["design_review"],
         "summary": "Approve the design and start work"},
        {"name": "approve", "to": "__end__", "purposes": ["findings"],
         "summary": "Accept the findings and finish"},
        {"name": "exit", "to": "__end__", "summary": "Stop"},
    ]

    codes = _codes(graph, nodes)
    assert "duplicate_command" not in codes, codes

    # And each gate resolves to its own transition.
    from agent.agentfolder.commands import build_registry
    from agent.commands import lookup

    registry = build_registry(_folder(graph, nodes))
    design = lookup("approve", registry, purpose="design_review")
    findings = lookup("approve", registry, purpose="findings")
    assert design is not None and findings is not None
    assert design.summary != findings.summary, (design, findings)
    assert "design" in design.summary.lower(), design.summary
    print("PASS  /approve can mean two things at two gates")


def test_a_command_name_reused_in_the_same_context_is_still_refused():
    """The clash worth refusing: which transition you get depends on order.

    Two rows with the same name and an overlapping context is ambiguous, and
    ambiguity resolved by list position is the kind of bug that works until
    somebody reorders a JSON file.
    """
    graph, nodes = _default_pair()
    nodes["human"]["commands"] = [
        {"name": "approve", "to": "executor", "purposes": ["design_review"]},
        {"name": "approve", "to": "planner", "purposes": ["design_review", "findings"]},
        {"name": "exit", "to": "__end__"},
    ]
    problems = [p for p in validate_folder(_folder(graph, nodes))
                if p.code == "duplicate_command"]
    assert problems, "an overlapping context must still be a problem"
    assert "design_review" in problems[0].message, problems[0].message

    # "Valid everywhere" overlaps with everything, including a named context.
    graph, nodes = _default_pair()
    nodes["human"]["commands"] = [
        {"name": "approve", "to": "executor"},
        {"name": "approve", "to": "planner", "purposes": ["findings"]},
        {"name": "exit", "to": "__end__"},
    ]
    assert "duplicate_command" in _codes(graph, nodes), \
        "a row valid everywhere collides with a row for one purpose"
    print("PASS  the same name in overlapping contexts is still rejected")


def test_a_graph_has_room_for_a_verifier_per_stage():
    """The arithmetic that broke a real design.

    Every stage now grows a read_only checker, and each checker spends one
    BRANCH. The cap was 8 branches, so at most 8 checked stages -- and a
    13-step plan needs more. The designer did not get a clear refusal; it
    emitted 20 node names and almost nothing else, an answer shaped like the
    graph it could not express.
    """
    from agent.agentfolder.schema import GraphFile
    from agent.bootstrap.schemas import DesignerOutput, GraphProposal

    for model, field, wanted in ((GraphFile, "nodes", 30),
                                 (GraphFile, "branches", 16),
                                 (GraphProposal, "nodes", 30),
                                 (GraphProposal, "branches", 16),
                                 (DesignerOutput, "nodes", 30)):
        caps = [getattr(m, "max_length", None)
                for m in model.model_fields[field].metadata]
        assert wanted in caps, f"{model.__name__}.{field} caps at {caps}, want {wanted}"

    # 13 stages of worker + checker, one human, is 27 nodes and 13 branches.
    # It has to fit, because that is a plan the planner is allowed to write.
    assert 13 * 2 + 1 <= 30 and 13 <= 16
    print("PASS  a 13-stage plan with a checker per stage fits the caps")


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
