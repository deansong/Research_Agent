"""
WHAT:  Wires the nodes together into the graph and defines how it branches.
WHY:   This is the map of the whole system.  If you read one file to
       understand the agent, read this one.
CONCEPT: StateGraph, nodes, edges, conditional edges, compile(checkpointer=).

--------------------------------------------------------------------------
THE GRAPH
--------------------------------------------------------------------------

    START
      |
      v
  discussor ----------> human <----------------------+
  (asks you one          |  ^                        |
   question, keeps       |  |                        |
   a requirements        |  +---- /discuss ----------+
   brief)                |
                         |  you type... 
                         |
        +----------------+----------------+---------------+
        |                |                |               |
   plain text          /plan            /replan         /exit
        |                |                |               |
        v                v                v               v
    discussor         planner          planner           END
                         |
                         v
                   orchestrator <----------+
                         |                 |
        +--------+-------+--------+        |
        |        |       |        |        |
    execute   replan  ask_human  finish    |
        |        |       |        |        |
        v        v       v        v        |
    executor  planner  human    human      |
        |                                  |
        +----------------------------------+

THE ONE RULE TO NOTICE:  there is no arrow from `discussor` straight to
`planner`.  The discussor ALWAYS goes to `human`.  The only way into the
planner is for you to type /plan.  That is requirement 2, enforced by the
shape of the graph rather than by a flag a model gets to set.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from agent import roles
from agent.nodes import (
    human_input,
    make_discussor,
    make_executor,
    make_orchestrator,
    make_planner,
)
from agent.state import AgentState


def build_graph(backends, checkpointer):
    """Assemble and compile the graph.

    `backends` maps a ROLE NAME to the provider adapter serving it, e.g.
        {"discussor": <CodexBackend>, "executor": <ClaudeCodeBackend>, ...}
    That per-role indirection is requirement 3: each node is handed its own
    backend, so they can be different providers with different models.

    `checkpointer` saves state after every step -- it is the reason you can
    ctrl-C and pick up where you left off.
    """
    # StateGraph is the BUILDER.  Note that the thing compile() returns is
    # immutable: you cannot add a node to a running graph.  (That constraint
    # is why docs/AUTO_GRAPH_DESIGN.md builds a *child* graph instead.)
    builder = StateGraph(AgentState)

    # ---- nodes -----------------------------------------------------------
    # A node is just a function: state in, partial state update out.
    builder.add_node("discussor", make_discussor(backends[roles.DISCUSSOR]))
    builder.add_node("human", human_input)
    builder.add_node("planner", make_planner(backends[roles.PLANNER]))
    builder.add_node("orchestrator", make_orchestrator(backends[roles.ORCHESTRATOR]))
    builder.add_node("executor", make_executor(backends[roles.EXECUTOR]))

    # ---- fixed edges: "always go here next" ------------------------------
    builder.add_edge(START, "discussor")

    # THE requirement-2 edge.  This used to be a conditional edge that could
    # jump to the planner when the model set discussion.ready.  Now the
    # discussor's only exit is the human.
    builder.add_edge("discussor", "human")

    builder.add_edge("executor", "orchestrator")

    # ---- conditional edges: "ask this function where to go" --------------
    # The function returns a KEY, and the dict maps keys to node names.  The
    # indirection looks redundant here, but it is what lets LangGraph draw the
    # graph and validate targets without executing your router.
    builder.add_conditional_edges(
        "human",
        _route_after_human,
        {
            "discussor": "discussor",
            "planner": "planner",
            "orchestrator": "orchestrator",
            "end": END,
        },
    )
    builder.add_edge("planner", "orchestrator")
    builder.add_conditional_edges(
        "orchestrator",
        _route_after_orchestrator,
        {
            "executor": "executor",
            "planner": "planner",
            "human": "human",
            "end": END,
        },
    )

    # compile() freezes the topology.  Passing the checkpointer here is what
    # turns "a function pipeline" into "a resumable, interruptible workflow".
    return builder.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------------------
# Routers.  Each is a plain function of state -> key.  Keep them pure and
# trivial: routing logic that needs to *think* belongs in a node.
# ---------------------------------------------------------------------------

def _route_after_human(state: AgentState) -> str:
    """Where to go after the human answered.

    nodes/human.py already decided this and wrote it into human.return_to;
    all we do here is read it.  Splitting "decide" (node) from "route"
    (router) is the LangGraph convention and keeps routers side-effect free.
    """
    if state.get("control", {}).get("terminate", False):
        return "end"
    return state.get("human", {}).get("return_to", "discussor")


def _route_after_orchestrator(state: AgentState) -> str:
    """Turn the orchestrator's chosen action into a destination.

    Note "finish" goes to `human`, not END: finishing a task means asking you
    what to do next, and you end the session yourself with /exit.  The "end"
    fallback below only fires on an action we do not recognise.
    """
    action = state.get("control", {}).get("action", "")
    return {
        "execute": "executor",
        "replan": "planner",
        "ask_human": "human",
        "finish": "human",
    }.get(action, "end")
