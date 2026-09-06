"""
WHAT:  Everything about a folder that Pydantic cannot check -- graph shape and
       cross-file references.
WHY:   LangGraph's compile() catches less than you would hope, and the gaps are
       exactly the mistakes a model makes.
CONCEPT: Graph algorithms over the SPEC, not over a compiled graph -- so they
       run before compile() and before any backend is constructed, which means
       a bad design costs nothing.

MEASURED against langgraph 1.2.11, so you know which checks are ours to make:

    compile() DOES catch   unknown edge target, unknown branch target, no
                           entrypoint from START, "__start__" and "a:b" as
                           node names, and duplicate add_node calls.
    compile() does NOT     unreachable nodes, an unreachable END (compiles
                           fine, then dies with GraphRecursionError at run
                           time), dead-end nodes (silently act as END), or the
                           node names "" and "A-B".

Every check runs and every problem is reported together, so one repair round
can fix all of them instead of playing whack-a-mole one error at a time.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Mapping, Sequence

from agent.agentfolder.load import AgentFolder
from agent.agentfolder.render import find_tokens, token_is_known
from agent.agentfolder.schema import (
    END_TARGET,
    RESERVED_OUTPUT_FIELDS,
    AgentNodeConfig,
    AskSpec,
    HumanNodeConfig,
)
from agent.backends.base import Access


@dataclass(frozen=True)
class Problem:
    code: str
    """Stable and machine-readable, so tests and the designer can key on it."""
    where: str
    """Human-readable location, e.g. "node 'executor'"."""
    message: str
    """One sentence the designer model can act on."""
    warning: bool = False


def format_problems(problems: Sequence[Problem]) -> str:
    """Render problems for a human or for the designer's repair prompt."""
    if not problems:
        return "No problems found."
    lines = []
    for problem in problems:
        tag = "warning" if problem.warning else "error"
        lines.append(f"  [{tag}: {problem.code}] {problem.where}: {problem.message}")
    return "\n".join(lines)


def validate_folder(
    folder: AgentFolder,
    *,
    backends: Mapping[str, object] | None = None,
) -> list[Problem]:
    """Return every problem with `folder`. Empty list means it is safe to compile.

    `backends` is optional: when given, each node's declared access is checked
    against what its configured backend can actually do. When omitted (the
    bootstrap validator, which runs before backends are built) that one check
    is skipped -- build_backends performs it again anyway.
    """
    problems: list[Problem] = []
    graph = folder.graph

    names = [ref.name for ref in graph.nodes]
    name_set = set(names)
    kinds = {ref.name: ref.kind for ref in graph.nodes}

    # ---- names and the two files agreeing -------------------------------
    seen: set[str] = set()
    for name in names:
        if name in seen:
            problems.append(Problem("duplicate_node", f"node {name!r}",
                                    "declared more than once in graph.json."))
        seen.add(name)

    for name in name_set - set(folder.nodes):
        problems.append(Problem("config_missing", f"node {name!r}",
                                "listed in graph.json but has no entry in nodes.json."))
    for name in set(folder.nodes) - name_set:
        problems.append(Problem("config_extra", f"node {name!r}",
                                "configured in nodes.json but not listed in graph.json. "
                                "(Duplicate JSON keys collapse silently -- check for two "
                                "entries with the same name.)"))

    for name, config in folder.nodes.items():
        kind = kinds.get(name)
        if kind == "human" and not isinstance(config, HumanNodeConfig):
            problems.append(Problem("kind_mismatch", f"node {name!r}",
                                    'kind is "human" but its config looks like an agent node.'))
        elif kind == "agent" and not isinstance(config, AgentNodeConfig):
            problems.append(Problem("kind_mismatch", f"node {name!r}",
                                    'kind is "agent" but its config looks like a human node.'))

    # ---- the vocabularies placeholders may draw on -----------------------
    counters = {c for cfg in folder.agent_nodes().values() for c in cfg.bump}
    variables = {
        key
        for cfg in folder.nodes.values()
        if isinstance(cfg, HumanNodeConfig)
        for command in cfg.commands
        for key in command.sets
    }

    # ---- targets exist ---------------------------------------------------
    def check_target(target: str, where: str, field: str) -> None:
        if target != END_TARGET and target not in name_set:
            problems.append(Problem("unknown_target", where,
                                    f"{field} points at {target!r}, which is not a node."))

    if graph.entry not in name_set:
        problems.append(Problem("unknown_target", "graph.json",
                                f"entry is {graph.entry!r}, which is not a node."))

    for edge in graph.edges:
        check_target(edge.from_, f"edge from {edge.from_!r}", "from")
        check_target(edge.to, f"edge {edge.from_}->{edge.to}", "to")
    for branch in graph.branches:
        check_target(branch.from_, f"branch on {branch.from_!r}", "from")
        check_target(branch.default, f"branch on {branch.from_!r}", "default")
        for case in branch.cases:
            check_target(case.to, f"branch on {branch.from_!r} case {case.when!r}", "to")

    # ---- outgoing transitions -------------------------------------------
    edges_by_source: dict[str, list] = {}
    for edge in graph.edges:
        edges_by_source.setdefault(edge.from_, []).append(edge)
    branch_by_source = {branch.from_: branch for branch in graph.branches}

    for name in name_set:
        out_edges = edges_by_source.get(name, [])
        branch = branch_by_source.get(name)

        if out_edges and branch:
            problems.append(Problem("conflicting_outgoing", f"node {name!r}",
                                    "has both a plain edge and a branch. Use one or the other."))
        if len(out_edges) > 1:
            problems.append(Problem("fan_out", f"node {name!r}",
                                    "has more than one plain outgoing edge. Parallel branches "
                                    "are not supported yet; use a branch to choose one."))
        if not out_edges and not branch and kinds.get(name) != "human":
            problems.append(Problem("dead_end", f"node {name!r}",
                                    "has no outgoing edge or branch, so the run stops there. "
                                    'Add an edge, or route it to "__end__" explicitly.'))

    # ---- reachability ----------------------------------------------------
    reachable, reaches_end = _reachability(folder, edges_by_source, branch_by_source)
    for name in sorted(name_set - reachable):
        problems.append(Problem("unreachable", f"node {name!r}",
                                f"nothing leads to it from the entry node {graph.entry!r}."))
    if not reaches_end:
        problems.append(Problem("end_unreachable", "graph.json",
                                'no path from the entry node reaches "__end__", so the agent '
                                "can never finish. It would run until the step limit."))

    # ---- branches switch on a real enum ----------------------------------
    for branch in graph.branches:
        config = folder.nodes.get(branch.from_)
        if not isinstance(config, AgentNodeConfig):
            continue
        field = next((f for f in config.output if f.name == branch.route_on), None)
        if field is None:
            problems.append(Problem("bad_route_field", f"branch on {branch.from_!r}",
                                    f"route_on names {branch.route_on!r}, which is not an "
                                    f"output field of that node."))
            continue
        if field.type != "enum":
            problems.append(Problem("bad_route_field", f"branch on {branch.from_!r}",
                                    f"route_on names {branch.route_on!r}, which is type "
                                    f"{field.type!r}. Only enum fields can be branched on, "
                                    f"so that every possible value can be checked."))
            continue
        handled = {case.when for case in branch.cases}
        for choice in field.choices:
            if choice not in handled and branch.default == END_TARGET:
                problems.append(Problem("unhandled_case", f"branch on {branch.from_!r}",
                                        f"choice {choice!r} has no case, so it would end the "
                                        f"run. Add a case or set an explicit default."))
        for case in branch.cases:
            if case.when not in field.choices:
                problems.append(Problem("unhandled_case", f"branch on {branch.from_!r}",
                                        f"case {case.when!r} is not one of the choices for "
                                        f"{branch.route_on!r} ({', '.join(field.choices)})."))

    # ---- ask blocks match human nodes ------------------------------------
    def check_ask(ask: AskSpec | None, target: str, where: str) -> None:
        target_is_human = kinds.get(target) == "human"
        if target_is_human and ask is None:
            problems.append(Problem("ask_missing", where,
                                    f"goes to the human node {target!r} but has no ask block, "
                                    f"so there would be no question to show."))
        if not target_is_human and ask is not None:
            problems.append(Problem("ask_stray", where,
                                    f"has an ask block but does not go to a human node."))
        if ask is not None:
            check_target(ask.resume_to, where, "ask.resume_to")

    for edge in graph.edges:
        check_ask(edge.ask, edge.to, f"edge {edge.from_}->{edge.to}")
    for branch in graph.branches:
        for case in branch.cases:
            check_ask(case.ask, case.to,
                      f"branch on {branch.from_!r} case {case.when!r}")

    # ---- placeholders, reserved fields, counters -------------------------
    for name, config in folder.nodes.items():
        if isinstance(config, AgentNodeConfig):
            _check_agent_node(problems, name, config, name_set, variables, counters, folder)
        elif isinstance(config, HumanNodeConfig):
            _check_human_node(problems, name, config, name_set, variables, check_target)

    for ask, where in _all_asks(graph):
        for text in (ask.question, ask.context):
            _check_tokens(problems, where, text, name_set, variables, allow_argument=False)

    # ---- can the configured backends actually do this? -------------------
    if backends is not None:
        for name, config in folder.agent_nodes().items():
            backend = backends.get(config.backend)
            if backend is None:
                continue
            needed = Access(config.access)
            if not needed <= backend.max_access:
                problems.append(Problem("access_unsupported", f"node {name!r}",
                                        f"needs {config.access} access but its backend "
                                        f"{config.backend!r} tops out at "
                                        f"{backend.max_access.value}."))

    # ---- warnings --------------------------------------------------------
    has_exit = any(
        command.to == END_TARGET
        for cfg in folder.nodes.values()
        if isinstance(cfg, HumanNodeConfig)
        for command in cfg.commands
    )
    if not has_exit and any(k == "human" for k in kinds.values()):
        problems.append(Problem("no_exit", "graph.json",
                                "no human command routes to \"__end__\", so there is no way to "
                                "stop the agent by hand.", warning=True))

    return problems


def _check_agent_node(problems, name, config, name_set, variables, counters, folder):
    for field in config.output:
        if field.name in RESERVED_OUTPUT_FIELDS:
            problems.append(Problem("reserved_field", f"node {name!r}",
                                    f"declares an output field named {field.name!r}, which is "
                                    f"reserved for the `capture` mechanism."))
        if field.type == "enum" and not field.choices:
            problems.append(Problem("bad_route_field", f"node {name!r}",
                                    f"output field {field.name!r} is an enum with no choices."))

    for label, text in (("prompts.first", config.prompts.first),
                        ("prompts.next", config.prompts.next),
                        ("thread_key", config.thread_key),
                        ("record", config.record)):
        _check_tokens(problems, f"node {name!r} {label}", text, name_set, variables,
                      allow_argument=False)

    if config.refresh_on and config.refresh_on not in counters:
        problems.append(Problem("unknown_counter", f"node {name!r}",
                                f"refresh_on names the counter {config.refresh_on!r}, but no "
                                f"node bumps it, so it would never change."))


def _check_human_node(problems, name, config, name_set, variables, check_target):
    seen: set[str] = set()
    for command in config.commands:
        for label in (command.name, *command.aliases):
            if label in seen:
                problems.append(Problem("duplicate_command", f"node {name!r}",
                                        f"/{label} is defined more than once."))
            seen.add(label)
        check_target(command.to, f"node {name!r} command /{command.name}", "to")
        _check_tokens(problems, f"node {name!r} command /{command.name} record",
                      command.record, name_set, variables, allow_argument=True)
        for value in command.sets.values():
            _check_tokens(problems, f"node {name!r} command /{command.name} sets",
                          value, name_set, variables, allow_argument=True)


def _check_tokens(problems, where, text, name_set, variables, *, allow_argument):
    for token in find_tokens(text or ""):
        if token == "argument" and not allow_argument:
            problems.append(Problem("bad_placeholder", where,
                                    "{argument} is only available in a command's record or "
                                    "sets, not in a node prompt."))
            continue
        if not token_is_known(token, node_names=name_set, var_names=variables):
            problems.append(Problem("bad_placeholder", where,
                                    f"{{{token}}} is not a placeholder this format knows."))


def _all_asks(graph):
    for edge in graph.edges:
        if edge.ask:
            yield edge.ask, f"edge {edge.from_}->{edge.to} ask"
    for branch in graph.branches:
        for case in branch.cases:
            if case.ask:
                yield case.ask, f"branch on {branch.from_!r} case {case.when!r} ask"


def _reachability(folder, edges_by_source, branch_by_source):
    """BFS from the entry node. Returns (reachable node names, reaches __end__).

    This is the check that matters most: LangGraph compiles a graph that can
    never reach END without complaint, and only fails at run time with a
    GraphRecursionError that tells you nothing about which edge is missing.
    """
    kinds = {ref.name: ref.kind for ref in folder.graph.nodes}
    reachable = {folder.graph.entry}
    reaches_end = False
    queue = deque([folder.graph.entry])

    while queue:
        current = queue.popleft()
        targets: list[str] = []

        for edge in edges_by_source.get(current, []):
            targets.append(edge.to)
            if edge.ask:
                targets.append(edge.ask.resume_to)

        branch = branch_by_source.get(current)
        if branch:
            targets.append(branch.default)
            for case in branch.cases:
                targets.append(case.to)
                if case.ask:
                    targets.append(case.ask.resume_to)

        # A human node's exits are its commands, which live in nodes.json.
        if kinds.get(current) == "human":
            config = folder.nodes.get(current)
            if isinstance(config, HumanNodeConfig):
                targets.extend(command.to for command in config.commands)

        for target in targets:
            if target == END_TARGET:
                reaches_end = True
            elif target not in reachable:
                reachable.add(target)
                queue.append(target)

    return reachable, reaches_end
