from __future__ import annotations

from pathlib import Path
from typing import Any

from typing_extensions import TypedDict


class DiscussionState(TypedDict, total=False):
    history: list[dict[str, str]]
    requirements: str
    ready: bool
    last_human_answer: str


class PlanningState(TypedDict, total=False):
    plan: str
    generation: int
    feedback: str


class ExecutionState(TypedDict, total=False):
    workstream: str
    current_task: str
    result: dict[str, Any]
    git_status: str
    git_diff_stat: str


class HumanState(TypedDict, total=False):
    question: str
    context: str
    purpose: str
    return_to: str


class CodexState(TypedDict, total=False):
    role_threads: dict[str, str]
    executor_threads: dict[str, str]
    executor_seen_plan_generation: dict[str, int]


class ControlState(TypedDict, total=False):
    event: str
    action: str
    terminate: bool


class AgentState(TypedDict, total=False):
    repo_path: str
    task_cycle: int
    user_request: str

    discussion: DiscussionState
    planning: PlanningState
    execution: ExecutionState
    human: HumanState
    codex: CodexState
    control: ControlState

    final_summary: str
    usage_by_role: dict[str, dict[str, Any]]


def merge_section(section: dict[str, Any] | None, **changes: Any) -> dict[str, Any]:
    merged = dict(section or {})
    merged.update(changes)
    return merged


def create_initial_state(
    repo_path: Path,
    user_request: str,
    previous: dict[str, Any] | None = None,
) -> AgentState:
    previous = previous or {}

    previous_codex = dict(previous.get("codex", {}))
    previous_planning = dict(previous.get("planning", {}))

    return {
        "repo_path": str(repo_path),
        "task_cycle": int(previous.get("task_cycle", 0)) + 1,
        "user_request": user_request,
        "discussion": {
            "history": [],
            "requirements": "",
            "ready": False,
            "last_human_answer": "",
        },
        "planning": {
            "plan": "",
            "generation": int(previous_planning.get("generation", 0)),
            "feedback": "",
        },
        "execution": {
            "workstream": "main",
            "current_task": "",
            "result": {},
            "git_status": "",
            "git_diff_stat": "",
        },
        "human": {
            "question": "",
            "context": "",
            "purpose": "discussion",
            "return_to": "discussor",
        },
        "codex": {
            "role_threads": dict(previous_codex.get("role_threads", {})),
            "executor_threads": dict(previous_codex.get("executor_threads", {})),
            "executor_seen_plan_generation": dict(
                previous_codex.get("executor_seen_plan_generation", {})
            ),
        },
        "control": {
            "event": "",
            "action": "",
            "terminate": False,
        },
        "final_summary": "",
        "usage_by_role": dict(previous.get("usage_by_role", {})),
    }
