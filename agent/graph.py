from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from agent.codex_backend import CodexBackend
from agent.nodes import (
    human_input,
    make_discussor,
    make_executor,
    make_orchestrator,
    make_planner,
)
from agent.state import AgentState



def build_graph(backend: CodexBackend, checkpointer):
    builder = StateGraph(AgentState)

    builder.add_node("discussor", make_discussor(backend))
    builder.add_node("human", human_input)
    builder.add_node("planner", make_planner(backend))
    builder.add_node("orchestrator", make_orchestrator(backend))
    builder.add_node("executor", make_executor(backend))

    builder.add_edge(START, "discussor")
    builder.add_conditional_edges(
        "discussor",
        _route_after_discussor,
        {"planner": "planner", "human": "human"},
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
    builder.add_edge("executor", "orchestrator")
    builder.add_conditional_edges(
        "human",
        _route_after_human,
        {"discussor": "discussor", "orchestrator": "orchestrator", "end": END},
    )

    return builder.compile(checkpointer=checkpointer)


def _route_after_discussor(state: AgentState) -> str:
    return "planner" if state.get("discussion", {}).get("ready", False) else "human"


def _route_after_human(state: AgentState) -> str:
    if state.get("control", {}).get("terminate", False):
        return "end"
    return state.get("human", {}).get("return_to", "discussor")


def _route_after_orchestrator(state: AgentState) -> str:
    action = state.get("control", {}).get("action", "")
    return {
        "execute": "executor",
        "replan": "planner",
        "ask_human": "human",
        "finish": "human",
    }.get(action, "end")
