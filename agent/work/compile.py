"""
WHAT:  Turns a validated agent folder into a compiled LangGraph.
WHY:   This is the moment JSON becomes a running graph. It is also the whole
       reason a compiled graph's immutability does not matter: we never modify
       a graph, we build a new one.
CONCEPT: StateGraph, add_node, add_edge, add_conditional_edges -- the same four
       calls the hand-written graph makes, just in a loop over data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from langgraph.graph import END, START, StateGraph

from agent.agentfolder.load import AgentFolder
from agent.agentfolder.outputs import build_output_model
from agent.agentfolder.schema import (
    END_TARGET,
    AgentNodeConfig,
    HumanNodeConfig,
    OrchestratorNodeConfig,
)
from agent.backends.base import Access
from agent.work.state import WorkState
from agent.work.templates.agent_node import DEFAULT_ROUTE, make_agent_node
from agent.work.templates.human_node import make_human_node
from agent.work.templates.orchestrator_node import make_orchestrator_node

# Appended to every write-access node's instructions by the loader, not the
# folder -- so a generated agent cannot opt out of it. The session checkpoint
# lives inside the executor's own write sandbox.
def infrastructure_rules(artifacts_dir: str, session_dir: str = "") -> str:
    """The standing rules every write-access node gets, appended by us.

    Appended by the loader rather than written into the folder, so a generated
    agent cannot opt out of them -- and so they can mention paths the designer
    did not know about when it wrote the folder. `session_dir` is one of those:
    --session-dir can put the checkpoint anywhere, including somewhere the
    agent has write access and no reason to suspect is off limits.

    Each session is its own project (agent/storage.py): code in its
    workspace, a git repository that started empty, and everything a run
    PRODUCES in its artifacts -- the distinction a run once missed when it
    scattered data, caches and reports across someone's repository root.
    Nothing goes outside the session folder.
    """
    where = f"this session's folder, {session_dir}" if session_dir else "this session's folder"
    return (
        "\n\nWORKING RULES (these override anything above):\n"
        f"1. Everything you do stays in {where}. Its checkpoint, plan, agent "
        "and activity records are infrastructure: never read, modify, move or "
        "delete them. Never write anywhere outside the session folder.\n"
        "2. The CODE you write goes in your working directory: this session's "
        "own project, a git repository that started empty. Commit to it as you "
        "like.\n"
        f"3. Everything the run PRODUCES goes in:\n     {artifacts_dir}\n"
        "   Generated data, downloaded datasets and models, caches, results, "
        "reports, plots, logs and scratch work. Create subdirectories there as "
        "you like.\n"
        "4. Never revert, reset or delete work this run did not make -- "
        "another node of this agent made it. If something is in your way, "
        "leave it and say so in your answer."
    )


#: Told to every turn whose commands run in Codex's sandbox. Measured: it
#: hides the GPUs (no /dev/nvidia*), and a preflight run there reported "no
#: GPU" as a blocker on a machine with seven of them.
SANDBOX_GPU_NOTE = (
    "\n\nGPU NOTE: commands in this turn run inside Codex's sandbox, which hides "
    "this machine's GPUs -- nvidia-smi and torch.cuda fail here even though the "
    "GPUs exist. Never conclude or report that the machine has no GPU or driver. "
    "Leave GPU checks and GPU work to a step that runs outside the sandbox, and "
    "say that you did.")


def sandbox_note(backend, access: str) -> str:
    """The GPU note, for a turn that runs in Codex's sandbox: any Codex turn,
    unless its role has `gpu` on and the turn may write (see
    agent/backends/codex.py)."""
    if getattr(backend, "name", "") != "codex":
        return ""
    if getattr(backend, "gpu", False) and access == "write":
        return ""
    return SANDBOX_GPU_NOTE


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
    hub = folder.orchestrator()
    if hub and hub[1].backend not in needed:
        needed[hub[1].backend] = Access.READ_ONLY
    return needed


def hub_roster(folder: AgentFolder) -> dict[str, dict]:
    """A hub agent's nodes as its orchestrator sees them, in graph order:
    node -> {kind: work|validator|paper|human, steps, role, access}."""
    hub = folder.orchestrator()
    if hub is None:
        return {}
    _, config = hub
    roster: dict[str, dict] = {}
    for ref in folder.graph.nodes:
        node = folder.nodes.get(ref.name)
        if isinstance(node, HumanNodeConfig):
            roster[ref.name] = {"kind": "human", "steps": [], "role": "", "access": "none"}
        elif isinstance(node, AgentNodeConfig):
            kind = ("validator" if ref.name == config.validator
                    else "paper" if ref.name == config.paper else "work")
            roster[ref.name] = {"kind": kind, "steps": list(node.steps),
                                "role": node.backend, "access": node.access}
    return roster


def compile_agent(
    folder: AgentFolder,
    *,
    backends: Mapping[str, object],
    checkpointer,
    registry,
    artifacts_dir: str = "",
    session_dir: str = "",
    isolation: str = "",
):
    """Build and compile the graph described by `folder`.

    Assumes validate_folder() returned no problems. If it did not, LangGraph
    will raise something less helpful -- that is a programming error, not a
    user error, which is why the CLI validates first and prints a report.
    """
    builder = StateGraph(WorkState)

    transitions = _transitions(folder)
    roster = hub_roster(folder)

    # ---- nodes -----------------------------------------------------------
    for ref in folder.graph.nodes:
        config = folder.nodes[ref.name]

        if isinstance(config, OrchestratorNodeConfig):
            builder.add_node(ref.name, make_orchestrator_node(
                ref.name,
                config.model_copy(update={"instructions": config.instructions + isolation
                                          + sandbox_note(backends.get(config.backend),
                                                         "read_only")}),
                roster=roster, backend=backends[config.backend], session_dir=session_dir))
            continue

        if isinstance(config, HumanNodeConfig):
            builder.add_node(ref.name, make_human_node(ref.name, config, registry=registry,
                                                       session_dir=session_dir))
            continue

        assert isinstance(config, AgentNodeConfig)
        instructions = config.instructions
        if config.access == "write":
            instructions += infrastructure_rules(artifacts_dir, session_dir)
        # Every node, readers included: reading another session's files is
        # how one task's work turns up in another's.
        instructions += isolation
        instructions += sandbox_note(backends.get(config.backend), config.access)

        builder.add_node(
            ref.name,
            make_agent_node(
                ref.name,
                config.model_copy(update={"instructions": instructions}),
                backend=backends[config.backend],
                output_model=build_output_model(ref.name, config.output),
                transition=transitions.get(ref.name),
                session_dir=session_dir,
                hub_role=roster.get(ref.name, {}).get("kind", "") if roster else "",
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
        if isinstance(folder.nodes[ref.name], (HumanNodeConfig, OrchestratorNodeConfig)):
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
