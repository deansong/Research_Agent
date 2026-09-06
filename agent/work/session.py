"""
WHAT:  Builds a GraphSession for a generated agent.
WHY:   Teaches terminal.py how to read WorkState without terminal.py importing
       WorkState.
CONCEPT: The adapter half of agent/session.py.
"""

from __future__ import annotations

from typing import Any

from agent.agentfolder.commands import build_registry
from agent.agentfolder.render import mermaid
from agent.agentfolder.load import AgentFolder
from agent.session import GraphSession
from agent.work.state import WorkState


def work_session(
    *,
    folder: AgentFolder,
    graph,
    thread_id: str,
    recursion_limit: int,
    initial_state: dict[str, Any],
) -> GraphSession:
    return GraphSession(
        title=f"agent: {folder.graph.name}",
        graph=graph,
        config={
            "configurable": {"thread_id": thread_id},
            "recursion_limit": recursion_limit,
        },
        registry=build_registry(folder),
        initial_state=initial_state,
        resume_payload=_resume_payload,
        state_report=lambda values, full: _state_report(folder, values, full),
        transcript=lambda values: list(values.get("transcript", [])),
        usage=lambda values: dict(values.get("usage", {})),
        topology=mermaid(folder),
    )


def _resume_payload(values: WorkState) -> dict | None:
    """What to re-show when picking up an interrupted session."""
    pending = values.get("pending") or {}
    if not pending.get("question"):
        return None
    return {
        "purpose": pending.get("purpose", ""),
        "question": pending.get("question", ""),
        "context": pending.get("context", ""),
    }


def _state_report(folder: AgentFolder, values: WorkState, full: bool) -> str:
    lines = [
        f"{'agent':20} {folder.graph.name}  ({folder.path})",
        f"{'task':20} {_short(values.get('task_brief', ''))}",
        f"{'waiting for':20} {(values.get('pending') or {}).get('purpose', '-')}",
        f"{'outcome':20} {values.get('outcome') or '(running)'}",
    ]

    counters = values.get("counters") or {}
    if counters:
        lines.append(f"{'counters':20} " + ", ".join(f"{k}={v}" for k, v in counters.items()))

    threads = values.get("threads") or {}
    if threads:
        lines.append(f"{'conversations':20} " + ", ".join(sorted(threads)))

    outputs = values.get("outputs") or {}
    lines.append("")
    lines.append("nodes that have run:")
    for name in folder.node_names():
        produced = outputs.get(name)
        mark = "yes" if produced else "no"
        lines.append(f"  {name:20} {mark}")

    if full:
        for name, produced in outputs.items():
            lines.append(f"\n{name} produced:")
            for key, value in produced.items():
                lines.append(f"  {key}: {_short(value, 300)}")

    return "\n".join(lines)


def _short(value: Any, limit: int = 60) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
