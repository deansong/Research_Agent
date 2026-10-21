"""
WHAT:  State for the bootstrap graph.
WHY:   Deliberately smaller than the work graph's, and deliberately built the
       OTHER way -- sub-TypedDicts plus merge_section rather than reducers.
CONCEPT: See agent/statelib.py for why both are right. Short version: this
       file knows its own key names; work/state.py does not.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any

from typing_extensions import TypedDict


class TranscriptEntry(TypedDict):
    role: str
    text: str


class DiscussionState(TypedDict, total=False):
    requirements: str
    advice: str
    advice_reason: str


class DesignState(TypedDict, total=False):
    proposal: dict
    """The designer's folder, as a PLAIN DICT.

    Not a Pydantic instance, on purpose. Measured: a Pydantic object stored in
    a loosely-annotated state field saves fine and then reloads as a plain
    dict, printing only a line on stderr -- a silent type change. Storing
    model_dump() and re-validating on read makes that a non-issue.
    """

    task_brief: str
    """Written by the designer FOR the generated agent. A rewrite of what was
    discussed, not a copy of the transcript: the new agent has no idea what
    "as we discussed" refers to."""

    plan: dict
    """The numbered steps, as a plain dict. Written to plan.json before the
    human sees them, and re-read from disk on approval so hand edits win."""

    plan_feedback: str
    """What the human asked to change about the plan."""

    valid: dict
    """The last agent that passed the validator -- at first the starting
    agent built from the plan (bootstrap/blueprint.py). The designer edits
    from it, and the validator falls back to it when the attempts run out,
    so a design never ends worse than this."""

    review_feedback: str
    """What the supervisor's review of the last run said to change. The
    designer is shown it, and starts from the last design that passed
    (`valid_spec`) when the plan has not changed."""

    valid_spec: dict
    """The design (graph + specs) that `valid` was compiled from."""

    plan_issues: list
    """What is wrong with the plan itself -- found by code (steps with no
    check, steps never split) or raised by the designer, which cannot fix
    the plan because the work nodes are fixed to its steps. Shown at the
    review stop."""

    problems: str
    attempt: int
    written_to: str
    rationale: str


class ResearchState(TypedDict, total=False):
    """The research agent's side of the discussion -- see nodes/researcher.py."""

    questions: list
    """What the discussor asked it, waiting to be researched."""
    rounds: int
    reports: list
    """One per round: questions, summary, findings, suggestions, file."""
    fresh: bool
    """A report the discussor has not read yet."""


class HumanState(TypedDict, total=False):
    question: str
    context: str
    purpose: str
    return_to: str
    last_answer: str
    auto_approve: bool
    """Set by the supervisor in auto mode: plan and design approvals are
    answered without stopping."""


class BootstrapState(TypedDict, total=False):
    repo_path: str
    session_dir: str
    user_request: str

    transcript: Annotated[list[TranscriptEntry], operator.add]

    discussion: DiscussionState
    research: ResearchState
    design: DesignState
    human: HumanState
    providers: dict[str, Any]
    usage_by_role: dict[str, Any]

    outcome: str
    """"" | "ready" | "aborted" -- read by cli.py to decide whether to run
    phase 2."""


def initial_bootstrap_state(*, repo_path: str, session_dir: str, request: str) -> dict:
    return {
        "repo_path": repo_path,
        "session_dir": session_dir,
        "user_request": request,
        "transcript": [{"role": "human", "text": request}],
        "discussion": {"requirements": "", "advice": "keep_discussing", "advice_reason": ""},
        "research": {"questions": [], "rounds": 0, "reports": [], "fresh": False},
        "design": {"proposal": {}, "task_brief": "", "problems": "", "attempt": 0,
                   "written_to": "", "rationale": "", "plan": {}, "plan_feedback": ""},
        "human": {"question": "", "context": "", "purpose": "discussion",
                  "return_to": "discussor", "last_answer": ""},
        "providers": {"role_threads": {}},
        "usage_by_role": {},
        "outcome": "",
    }


def transcript_text(state: BootstrapState) -> str:
    return "\n\n".join(
        f"{e.get('role', '?')}: {e.get('text', '')}" for e in state.get("transcript", [])
    )
