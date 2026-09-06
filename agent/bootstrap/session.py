"""
WHAT:  Builds a GraphSession for the bootstrap graph.
WHY:   Teaches terminal.py how to read BootstrapState without importing it.
CONCEPT: The adapter half of agent/session.py. Compare agent/work/session.py --
       same shape, different schema, and the REPL cannot tell them apart.
"""

from __future__ import annotations

from typing import Any

from agent.bootstrap.commands import BOOTSTRAP_REGISTRY
from agent.bootstrap.state import BootstrapState, initial_bootstrap_state
from agent.session import GraphSession


def bootstrap_session(*, graph, thread_id, recursion_limit, repo_path, session_dir, request):
    return GraphSession(
        title="designing an agent for this task",
        graph=graph,
        config={
            "configurable": {"thread_id": thread_id},
            "recursion_limit": recursion_limit,
        },
        registry=BOOTSTRAP_REGISTRY,
        initial_state=initial_bootstrap_state(
            repo_path=repo_path, session_dir=session_dir, request=request
        ),
        resume_payload=_resume_payload,
        state_report=_state_report,
        transcript=lambda values: list(values.get("transcript", [])),
        usage=lambda values: dict(values.get("usage_by_role", {})),
    )


def _resume_payload(values: BootstrapState) -> dict | None:
    human = values.get("human") or {}
    if not human.get("question"):
        return None
    return {
        "purpose": human.get("purpose", ""),
        "question": human.get("question", ""),
        "context": human.get("context", ""),
    }


def _state_report(values: BootstrapState, full: bool) -> str:
    design = values.get("design") or {}
    lines = [
        f"{'phase':20} designing an agent",
        f"{'request':20} {_short(values.get('user_request', ''))}",
        f"{'waiting for':20} {(values.get('human') or {}).get('purpose', '-')}",
        f"{'design attempts':20} {design.get('attempt', 0)}",
        f"{'outcome':20} {values.get('outcome') or '(running)'}",
    ]

    proposal = design.get("proposal") or {}
    if proposal.get("graph"):
        graph = proposal["graph"]
        names = ", ".join(n.get("name", "?") for n in graph.get("nodes", []))
        lines.append(f"{'proposed agent':20} {graph.get('name', '?')}: {names}")
    if design.get("written_to"):
        lines.append(f"{'written to':20} {design['written_to']}")
    if design.get("problems"):
        lines.append("\nlast validation problems:")
        lines.append(design["problems"])
    if full and design.get("task_brief"):
        lines.append("\ntask brief for the new agent:")
        lines.append(design["task_brief"])

    return "\n".join(lines)


def _short(value: Any, limit: int = 60) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
