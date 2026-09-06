"""
WHAT:  Turns a validated agent folder into a compiled LangGraph.
WHY:   This is the moment JSON becomes a running graph. It is also the whole
       reason a compiled graph's immutability does not matter: we never modify
       a graph, we build a new one.
CONCEPT: StateGraph, add_node, add_edge, add_conditional_edges -- the same four
       calls the hand-written graph makes, just in a loop over data.
"""

from __future__ import annotations

from typing import Mapping

from langgraph.graph import END, START, StateGraph

from agent.agentfolder.load import AgentFolder
from agent.agentfolder.outputs import build_output_model
from agent.agentfolder.schema import END_TARGET, AgentNodeConfig, HumanNodeConfig
from agent.backends.base import Access
from agent.work.state import WorkState
from agent.work.templates.agent_node import DEFAULT_ROUTE, make_agent_node
from agent.work.templates.human_node import make_human_node

# Appended to every write-access node's instructions by the loader, not the
# folder -- so a generated agent cannot opt out of it. The session checkpoint
# lives under .agent/, inside the executor's own write sandbox.
INFRASTRUCTURE_WARNING = (
    "\n\nIMPORTANT: the `.agent/` directory at the repository root is agent "
    "infrastructure -- it holds this session's checkpoint database and the "
    "agent definition you are running inside. Never read, modify, move or "
    "delete anything under `.agent/`, and never include it in a cleanup task."
)


def backends_needed(folder: AgentFolder) -> dict[str, Access]:
    """role name -> the highest access any node using that role asks for.

    Handed to build_backends() so the access check is driven by what the agent
    actually declares, rather than by a hardcoded table that cannot know what
    a generated folder invented.
    """
    needed: dict[str, Access] = {}
    for config in folder.agent_nodes().values():
        access = Access(config.access)
        current = needed.get(config.backend)
        if current is None or current.rank < access.rank:
            needed[config.backend] = access
    return needed


def compile_agent(
    folder: AgentFolder,
    *,
    backends: Mapping[str, object],
    checkpointer,
    registry,
):
    """Build and compile the graph described by `folder`.

    Assumes validate_folder() returned no problems. If it did not, LangGraph
    will raise something less helpful -- that is a programming error, not a
    user error, which is why the CLI validates first and prints a report.
    """
    builder = StateGraph(WorkState)

    transitions = _transitions(folder)

    # ---- nodes -----------------------------------------------------------
    for ref in folder.graph.nodes:
        config = folder.nodes[ref.name]

        if isinstance(config, HumanNodeConfig):
            builder.add_node(ref.name, make_human_node(ref.name, config, registry=registry))
            continue

        assert isinstance(config, AgentNodeConfig)
        instructions = config.instructions
        if config.access == "write":
            instructions += INFRASTRUCTURE_WARNING

        builder.add_node(
            ref.name,
            make_agent_node(
                ref.name,
                config.model_copy(update={"instructions": instructions}),
                backend=backends[config.backend],
                output_model=build_output_model(ref.name, config.output),
                transition=transitions.get(ref.name),
            ),
        )

    builder.add_edge(START, folder.graph.entry)

    # ---- plain edges -----------------------------------------------------
    for edge in folder.graph.edges:
        builder.add_edge(edge.from_, _target(edge.to))

    # ---- branches: one table lookup, no expression language --------------
    #
    # Every node writes one string into route[<its own name>]. The router just
    # reads it. That means the case list is consulted twice per step: once in
    # the node (to pick the `ask`, before it returns) and once here (to pick
    # the target). Both are pure and both read the same list, so they cannot
    # disagree -- and the alternative, letting the router call the model or
    # inspect outputs, would put logic somewhere LangGraph may re-run freely.
    for branch in folder.graph.branches:
        path_map = {case.when: _target(case.to) for case in branch.cases}
        path_map[DEFAULT_ROUTE] = _target(branch.default)
        builder.add_conditional_edges(branch.from_, _router(branch.from_), path_map)

    # ---- human nodes route to whatever they wrote ------------------------
    # Their targets are dynamic (a plain answer goes back to whoever asked, a
    # command goes wherever it says), so the path map is every node plus END
    # plus itself -- the self-entry is the "unrecognised command, ask again"
    # loop.
    all_targets = {ref.name: ref.name for ref in folder.graph.nodes}
    all_targets[END_TARGET] = END
    for ref in folder.graph.nodes:
        if isinstance(folder.nodes[ref.name], HumanNodeConfig):
            builder.add_conditional_edges(ref.name, _router(ref.name), dict(all_targets))

    return builder.compile(checkpointer=checkpointer)


def _transitions(folder: AgentFolder) -> dict[str, object]:
    """node name -> its single outgoing edge or branch (validated to be one)."""
    out: dict[str, object] = {}
    for edge in folder.graph.edges:
        out[edge.from_] = edge
    for branch in folder.graph.branches:
        out[branch.from_] = branch
    return out


def _target(name: str):
    return END if name == END_TARGET else name


def _router(node: str):
    """Read the routing key this node wrote. One line, no logic."""
    return lambda state: state.get("route", {}).get(node, DEFAULT_ROUTE)
