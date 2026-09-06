"""
WHAT:  The bootstrap graph -- hand-written, fixed, and the map of phase 1.
WHY:   Deliberately NOT expressed as a folder. One hand-written graph in the
       codebase is what a reader needs in order to understand what a folder is
       describing, and two of its nodes call no model at all.
CONCEPT: StateGraph, conditional edges, and a repair loop.

--------------------------------------------------------------------------
THE GRAPH
--------------------------------------------------------------------------

    START
      |
      v
  discussor ------> human <---------------------+
  (what do you        |                         |
   want, and what     |  you type...            |
   shape of agent)    |                         |
                      |                         |
        +-------+-----+------+--------+         |
        |       |            |        |         |
    plain text /plan      /discuss  /exit       |
        |       |            |        |         |
        v       v            +--------+        END
    discussor  designer                         ^
                  |                             |
                  v                             |
              writer  (no model: json.dump)     |
                  |                             |
                  v                             |
             validator  (no model: check disk)  |
                  |                             |
       +----------+-----------+                 |
       |          |           |                 |
      ok        retry      give up              |
       |          |           |                 |
       v          v           v                 |
      END     designer      human --------------+
                            (/retry /discuss /use /exit)

The one rule to notice, same as the original agent: `discussor` has a single
plain edge to `human`. No model output can start the design. Only /plan can.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from agent.bootstrap.nodes import (
    human_input,
    make_designer,
    make_discussor,
    make_validator,
    make_writer,
)
from agent.bootstrap.state import BootstrapState


def build_bootstrap_graph(backends, paths, checkpointer, *, max_attempts: int):
    builder = StateGraph(BootstrapState)

    builder.add_node("discussor", make_discussor(backends["discussor"]))
    builder.add_node("human", human_input)
    builder.add_node("designer", make_designer(backends["designer"]))
    builder.add_node("writer", make_writer(paths))
    builder.add_node("validator", make_validator(paths, max_attempts=max_attempts))

    builder.add_edge(START, "discussor")

    # THE gate: the discussor's only exit is the human.
    builder.add_edge("discussor", "human")

    builder.add_conditional_edges(
        "human",
        _route_after_human,
        {"discussor": "discussor", "designer": "designer", "end": END},
    )

    # design -> materialise -> check. Three nodes rather than one, so each is
    # small enough to read and so a failed design still leaves an inspectable
    # folder in agent.tmp/.
    builder.add_edge("designer", "writer")
    builder.add_edge("writer", "validator")

    builder.add_conditional_edges(
        "validator",
        _route_after_validator,
        {"designer": "designer", "human": "human", "end": END},
    )

    return builder.compile(checkpointer=checkpointer)


def _route_after_human(state: BootstrapState) -> str:
    if state.get("outcome"):
        return "end"
    return state.get("human", {}).get("return_to", "discussor")


def _route_after_validator(state: BootstrapState) -> str:
    """Success ends phase 1; otherwise repair, or hand back to the human.

    The node already decided this and wrote it into state -- `outcome` when it
    passed, `human.purpose == "design_failed"` when it gave up. The router only
    reads. Keeping routers logic-free is what makes the graph readable, and it
    matters more than usual here because LangGraph may call a router more than
    once.

    Note designer -> writer -> validator -> designer is 3 super-steps per
    attempt inside ONE invoke() with no interrupt between them. At the default
    recursion_limit of 1000 that is nothing, but do not "tidy" the limit down
    to 10.
    """
    if state.get("outcome") == "ready":
        return "end"
    if state.get("human", {}).get("purpose") == "design_failed":
        return "human"
    return "designer"
