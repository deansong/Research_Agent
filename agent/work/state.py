"""
WHAT:  The state schema for any generated agent.
WHY:   Fixed on purpose -- it does NOT vary with the folder. That is what keeps
       checkpoints, /state and msgpack serialisation sane no matter what the
       designer invented.
CONCEPT: Reducers, and why they are unavoidable here.

--------------------------------------------------------------------------
THE LESSON THIS FILE EXISTS TO TEACH
--------------------------------------------------------------------------
agent/bootstrap/state.py uses merge_section() -- read a section, copy it,
overlay changes, return the whole thing. That works there, and it is what the
hand-written graph has always done.

It CANNOT work here, and the reason is worth understanding:

    merge_section() only works because the code KNOWS the keys.
    bootstrap/state.py can write providers["role_threads"] because
    "role_threads" is spelled in the source.

    Here the keys come from JSON. A node called `reviewer` writes
    threads["reviewer/"], and our code has never heard of `reviewer`.
    If that node returned the whole `threads` dict it would have to have read
    every other node's entry first -- and with two nodes writing in the same
    super-step, one would silently erase the other.

`operator.or_` moves the merge into the channel, which is where it belongs
when the keys are data. Each node returns ONLY its own entry:

    return {"threads": {"reviewer/": "thread-abc"}}

and LangGraph merges it into whatever is already there.

    THE RULE:  hand-merge when your code knows the keys;
               use a reducer when the keys come from data.

Caveat worth knowing: operator.or_ is a shallow merge and does NOT raise
InvalidUpdateError if two nodes write the same sub-key in one super-step --
last writer silently wins. That can only happen with fan-out, which the
validator currently forbids (at most one plain out-edge per node). If fan-out
is ever allowed, revisit this line.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any

from typing_extensions import TypedDict

# Recorded in the session's meta.json. A mismatch on resume refuses to load
# rather than half-reading an incompatible checkpoint.
WORK_STATE_VERSION = 1


class TranscriptEntry(TypedDict):
    role: str
    text: str


class WorkState(TypedDict, total=False):
    """State for a generated agent. Every key here is a LangGraph channel."""

    # ---- inputs: written once when the run starts, never updated ----------
    repo_path: str
    task_brief: str
    """The self-contained statement of the job, written by the bootstrap
    designer FOR this agent (or typed by the human under --pre-build-agent)."""
    artifacts_dir: str
    """Where this run should put anything it PRODUCES -- data, reports,
    caches, generated corpora. Source changes still go in the repository;
    this is for output that belongs to the run rather than to the project."""

    plan: dict
    """The approved numbered plan, as a plain dict.

    Kept whole in state, but a node never sees the whole thing: the templates
    render only that node's own steps into {my_steps}. Stored as a dict rather
    than a Pydantic model because a model in a loosely-typed state field
    reloads as a dict anyway, with only a line on stderr."""

    agent_dir: str
    """Provenance only. The folder itself is loaded from disk, never stored in
    state -- a Pydantic object in a loosely-typed state field reloads as a
    plain dict with only a line on stderr, which is a nasty silent bug."""

    # ---- reducer channels: keys come from the folder ----------------------
    transcript: Annotated[list[TranscriptEntry], operator.add]

    outputs: Annotated[dict[str, dict], operator.or_]
    """node name -> that node's last structured output, plus any `capture`
    fields. This is what {out.<node>.<field>} reads."""

    threads: Annotated[dict[str, str], operator.or_]
    """"<node>/<rendered thread_key>" -> provider conversation id.
    The discussor gets "discussor/"; an executor with
    thread_key "{out.planner.workstream}" gets "executor/main"."""

    thread_marks: Annotated[dict[str, int], operator.or_]
    """Same keys as `threads` -> the counter value when that conversation last
    ran. Compared against `counters` to decide first-vs-next prompt."""

    counters: Annotated[dict[str, int], operator.or_]
    """Named counters a node can `bump`. The default agent bumps
    "plan_revision", which is how the executor knows to resend the plan."""

    vars: Annotated[dict[str, str], operator.or_]
    """Values set by human commands, readable as {var.<name>}."""

    route: Annotated[dict[str, str], operator.or_]
    """node name -> the routing key it last produced. Every conditional edge in
    a generated graph is a table lookup on this. See work/compile.py."""

    usage: Annotated[dict[str, dict], operator.or_]

    # ---- last-value-wins: exactly one writer at a time --------------------
    pending: dict
    """{purpose, question, context, resume_to} -- what the human node should
    ask. Set by whichever transition led into it. This is the declarative form
    of today's human.question / human.purpose / human.return_to."""

    last_answer: str
    outcome: str
    """"" | "done" | "exit" | "new_task" -- why the run ended."""
    next_request: str
    """Set by a /new-style command; the CLI starts a fresh run with it."""


def initial_work_state(
    *,
    repo_path: str,
    task_brief: str,
    agent_dir: str,
    artifacts_dir: str = "",
    plan: dict | None = None,
    threads: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build the input for a fresh run of a generated agent.

    `threads` carries provider conversations over from a previous task in the
    same session. That is what preserves the prompt-cache saving that today's
    task-cycle carryover was for.
    """
    return {
        "repo_path": repo_path,
        "task_brief": task_brief,
        "agent_dir": agent_dir,
        "artifacts_dir": artifacts_dir,
        "plan": plan or {},
        "transcript": [{"role": "human", "text": task_brief}],
        "outputs": {},
        "threads": dict(threads or {}),
        "thread_marks": {},
        "counters": {},
        "vars": {},
        "route": {},
        "usage": {},
        "pending": {},
        "last_answer": "",
        "outcome": "",
        "next_request": "",
    }


def render_context(
    state: WorkState,
    *,
    argument: str = "",
    steps: list[str] | None = None,
) -> dict[str, Any]:
    """Build the mapping agentfolder.render.render() reads.

    One place that decides what a prompt can see, so the placeholder
    vocabulary in render.py and the state schema above cannot drift apart.
    """
    from agent.bootstrap.nodes.planner import outline, steps_for

    plan = state.get("plan") or {}

    return {
        "task_brief": state.get("task_brief", ""),
        # Only THIS node's steps, in full. Everyone else's collapse to a line.
        "my_steps": steps_for(plan, steps or []),
        "plan_outline": outline(plan),
        "repo_path": state.get("repo_path", ""),
        "artifacts_dir": state.get("artifacts_dir", ""),
        "last_answer": state.get("last_answer", ""),
        "transcript": format_transcript(state.get("transcript", [])),
        "argument": argument,
        "out": state.get("outputs", {}),
        "var": state.get("vars", {}),
    }


def format_transcript(entries) -> str:
    return "\n\n".join(f"{e.get('role', '?')}: {e.get('text', '')}" for e in entries)
