from __future__ import annotations

from pathlib import Path
from typing import Any

from langgraph.types import Command

from agent.config import DEFAULT_RECURSION_LIMIT
from agent.state import create_initial_state



def drive_graph(graph, repo_path: Path, session_id: str) -> None:
    config = {
        "configurable": {"thread_id": session_id},
        "recursion_limit": DEFAULT_RECURSION_LIMIT,
    }

    snapshot = graph.get_state(config)

    if snapshot.values and snapshot.next:
        result = _resume_existing(graph, config, snapshot)
    else:
        request = input(f"\nRepository: {repo_path}\n\nWhat do you want to build/change?\n\nyou> ").strip()
        if not request:
            print("No request supplied.")
            return

        initial_state = create_initial_state(
            repo_path=repo_path,
            user_request=request,
            previous=dict(snapshot.values) if snapshot.values else {},
        )
        result = graph.invoke(initial_state, config=config)

    while True:
        payload = _interrupt_payload(result)
        if payload is None:
            break

        answer = _ask_user(graph, config, payload)
        result = graph.invoke(Command(resume=answer), config=config)

    print("\nAgent session ended.")
    final_snapshot = graph.get_state(config)
    if final_snapshot.values.get("usage_by_role"):
        print_usage_table(final_snapshot.values["usage_by_role"])


def _resume_existing(graph, config: dict[str, Any], snapshot):
    human = snapshot.values.get("human", {})
    if human.get("question"):
        print(f"\nResuming LangGraph session: {config['configurable']['thread_id']}")
        answer = _ask_user(
            graph,
            config,
            {
                "question": human.get("question", ""),
                "context": human.get("context", ""),
                "purpose": human.get("purpose", ""),
            },
        )
        return graph.invoke(Command(resume=answer), config=config)

    return graph.invoke(None, config=config)


def _interrupt_payload(result: dict[str, Any]) -> dict[str, Any] | None:
    interrupts = result.get("__interrupt__")
    if not interrupts:
        return None

    item = interrupts[0]
    value = getattr(item, "value", item)
    return value if isinstance(value, dict) else {"question": str(value), "context": ""}


def _ask_user(graph, config: dict[str, Any], payload: dict[str, Any]) -> str:
    while True:
        _show_prompt(payload)
        print("Commands: /usage  /quit")
        answer = input("\nyou> ").strip()

        if answer == "/usage":
            snapshot = graph.get_state(config)
            print_usage_table(snapshot.values.get("usage_by_role", {}))
            continue

        return answer


def _show_prompt(payload: dict[str, Any]) -> None:
    print("\n======================================")
    print("HUMAN INPUT")
    print("======================================")

    context = str(payload.get("context", "")).strip()
    if context:
        print(context)
        print()

    print(str(payload.get("question", "Your input is required.")).strip())
    print()


def print_usage_table(usage_map: dict[str, dict[str, Any]]) -> None:
    if not usage_map:
        print("No token usage recorded yet.")
        return

    print("\nTOKEN / CACHE USAGE")
    print("-" * 78)

    for role, usage in usage_map.items():
        line = (
            f"{role:24} "
            f"input={usage.get('last_input_tokens', 0):>8,} "
            f"cached={usage.get('last_cached_input_tokens', 0):>8,} "
            f"cache={usage.get('cache_percent', 0):>5}%"
        )
        if usage.get("context_percent") is not None:
            line += f" context={usage['context_percent']:>5}%"
        print(line)
