"""
WHAT:  The shape of everything the graph remembers, plus helpers to build and
       update it.
WHY:   LangGraph passes ONE state object between nodes.  Each node returns a
       partial update, and LangGraph merges it in.  Getting this file right is
       most of getting a graph right.
CONCEPT: State schema, channels, and reducers.

--------------------------------------------------------------------------
THE ONE RULE THAT EXPLAINS THIS WHOLE FILE
--------------------------------------------------------------------------
Only the TOP-LEVEL keys of the state schema are LangGraph "channels".  A
channel is what LangGraph knows how to merge.  By default a channel is
"last value wins": whatever a node returns for that key REPLACES what was
there.

You can change that by annotating the key with a reducer:

    transcript: Annotated[list[TranscriptEntry], operator.add]

which tells LangGraph "don't replace -- call operator.add(old, new)".

Here is the trap, and it is measurable:  a reducer nested INSIDE a
sub-TypedDict does nothing.  This does NOT accumulate --

    class Inner(TypedDict):
        items: Annotated[list, operator.add]     # <-- silently ignored
    class State(TypedDict):
        inner: Inner

-- because `inner` is the channel, and it is a last-value-wins channel, so
the whole dict (items included) is overwritten wholesale.

We group related fields into sub-TypedDicts anyway, because a flat state with
25 keys is miserable to read.  The price is that a node updating one field of
a section must hand back the WHOLE section.  That is exactly what
merge_section() below is for.  `transcript` is the one field important enough
to promote to a real channel with a real reducer.
"""

from __future__ import annotations

import operator
from pathlib import Path
from typing import Annotated, Any, Literal

from typing_extensions import TypedDict


# Bumped whenever the schema changes in a way old checkpoints cannot survive.
# storage.database_path() puts this in the filename, so a new version simply
# starts a new database instead of half-loading an old one.
STATE_VERSION = 2


class TranscriptEntry(TypedDict):
    """One turn of the human <-> discussor conversation."""

    role: Literal["human", "discussor", "system"]
    text: str
    cycle: int
    """Which task_cycle this belongs to.  Prompts filter on it so a second
    task in the same session does not inherit the first task's conversation,
    while /transcript can still show you everything."""


class DiscussionState(TypedDict, total=False):
    """What the discussor has worked out so far.

    The conversation itself is NOT here -- it lives in the top-level
    `transcript` channel, because only top-level keys can have reducers.
    """

    requirements: str
    """A running brief the discussor rewrites in full each turn."""

    advice: str
    """"keep_discussing" or "ready_to_plan".  A HINT that is printed to the
    human and never routed on.  The human decides when planning starts."""

    advice_reason: str


class PlanningState(TypedDict, total=False):
    plan: str
    generation: int
    """Incremented on every (re)plan.  Executors compare it against what they
    last saw to decide whether they need the full plan again."""
    feedback: str
    """Why we are replanning: orchestrator feedback, or guidance the human
    typed after /plan."""


class ExecutionState(TypedDict, total=False):
    workstream: str
    current_task: str
    result: dict[str, Any]
    git_status: str
    git_diff_stat: str


class HumanState(TypedDict, total=False):
    """What we are asking the human, and where to go with their reply."""

    question: str
    context: str

    purpose: str
    """Why we stopped: "discussion" | "orchestrator" | "next_task".
    nodes/human.py branches on it, and commands.py uses it to decide which
    slash commands are legal right now."""

    return_to: str
    """Which node to run next.  _route_after_human reads this."""

    last_answer: str
    """The most recent plain-text answer, for the node that asked."""


class ProviderState(TypedDict, total=False):
    """Conversation handles for the model provider.

    Deliberately separate from the workflow state above: LangGraph remembers
    the WORKFLOW, the provider remembers the CONVERSATION, and all we store is
    the id that lets us pick that conversation back up.  Keeping provider
    transcripts out of the checkpoint is what keeps checkpoints small.
    """

    role_threads: dict[str, str]
    """role name -> provider thread id (one long-lived thread per role)."""

    executor_threads: dict[str, str]
    """workstream -> provider thread id (one long-lived thread per stream)."""

    executor_seen_plan_generation: dict[str, int]
    """workstream -> the planning.generation that thread was last told about."""


class ControlState(TypedDict, total=False):
    event: str
    """Who just finished: "planner" | "executor" | "human" | ""."""
    action: str
    """What the orchestrator decided: "execute"|"replan"|"ask_human"|"finish"."""
    terminate: bool


class AgentState(TypedDict, total=False):
    """The graph's state.  Every key here is a channel."""

    state_version: int
    repo_path: str
    task_cycle: int
    user_request: str

    # ---- the one channel with a reducer ---------------------------------
    # Nodes return ONLY THE NEW ENTRIES, e.g. {"transcript": [entry]}.
    # LangGraph appends them for you.
    #
    #   COMMON BEGINNER BUG: returning the whole list, e.g.
    #       {"transcript": state["transcript"] + [entry]}
    #   That gets ADDED to what is already stored, so every entry appears
    #   twice, then four times, then eight.  Return the delta.
    transcript: Annotated[list[TranscriptEntry], operator.add]

    # ---- last-value-wins channels, each grouping related fields ----------
    discussion: DiscussionState
    planning: PlanningState
    execution: ExecutionState
    human: HumanState
    providers: ProviderState
    control: ControlState

    final_summary: str
    usage_by_role: dict[str, dict[str, Any]]


def merge_section(section: dict[str, Any] | None, **changes: Any) -> dict[str, Any]:
    """Return a copy of one state section with `changes` applied.

    This is the hand-rolled stand-in for a reducer on a nested field.  Because
    a section is one last-value-wins channel, a node that wants to change one
    field must return the whole section -- so it reads the old one, copies it,
    and overlays its changes:

        "planning": merge_section(state.get("planning"), plan=new_plan)

    Copying (rather than mutating state["planning"] in place) matters: the
    state object a node receives may be shared, and mutating it would corrupt
    the checkpoint.
    """
    merged = dict(section or {})
    merged.update(changes)
    return merged


def transcript_for_cycle(state: AgentState, cycle: int | None = None) -> str:
    """Render the conversation for one task cycle as plain text for a prompt.

    This is how the planner learns "what was discussed" -- requirement 2 says
    the plan is based on the discussion, so the planner gets the real dialogue
    (both sides), not just the discussor's summary of it.
    """
    if cycle is None:
        cycle = int(state.get("task_cycle", 1))

    lines = [
        f"{entry['role']}: {entry['text']}"
        for entry in state.get("transcript", [])
        if entry.get("cycle") == cycle
    ]
    return "\n\n".join(lines)


def create_initial_state(
    repo_path: Path,
    user_request: str,
    previous: dict[str, Any] | None = None,
) -> AgentState:
    """Build the state for a fresh task.

    `previous` is the last checkpoint's values, if any.  We carry over the
    things that are about the REPOSITORY (provider threads, plan generation,
    accumulated usage) and reset the things that are about the TASK.
    """
    previous = previous or {}

    previous_providers = dict(previous.get("providers", {}))
    previous_planning = dict(previous.get("planning", {}))

    return {
        "state_version": STATE_VERSION,
        "repo_path": str(repo_path),
        "task_cycle": int(previous.get("task_cycle", 0)) + 1,
        "user_request": user_request,
        # Seeding the conversation with the request means the planner sees it
        # in the transcript even if the human types /plan immediately.
        "transcript": [
            {
                "role": "human",
                "text": user_request,
                "cycle": int(previous.get("task_cycle", 0)) + 1,
            }
        ],
        "discussion": {"requirements": "", "advice": "keep_discussing", "advice_reason": ""},
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
            "last_answer": "",
        },
        "providers": {
            "role_threads": dict(previous_providers.get("role_threads", {})),
            "executor_threads": dict(previous_providers.get("executor_threads", {})),
            "executor_seen_plan_generation": dict(
                previous_providers.get("executor_seen_plan_generation", {})
            ),
        },
        "control": {"event": "", "action": "", "terminate": False},
        "final_summary": "",
        "usage_by_role": dict(previous.get("usage_by_role", {})),
    }
