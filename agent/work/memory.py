"""
WHAT:  The orchestrator's memory: one small file per session that outlives
       the design phase, every run, every iteration and every restart.
       <session>/orchestrator/memory.json
WHY:   The orchestrator IS the discussor, carried into the run. What it
       learned in the discussion, the plan it approved, what it decided in
       each run and why -- that is what lets it "know everything" without
       being sent everything. Its provider conversation holds the detail
       while it lasts; this holds what must survive the conversation being
       restarted when it grows too big (a roll), or being lost to a provider
       change.
CONCEPT: Not LangGraph state, on purpose: state belongs to one graph's
       thread, and each phase and iteration has its own. This is the one
       thing all of them share.

What is kept, and nothing more:

    thread_id, provider, model   the conversation to continue, and where
    requirements, plan_summary,  from the design phase: what was agreed
      rationale, plan_issues
    notes                        what the orchestrator chose to remember
    book                         the last step book: status and report path
                                 per node
    decisions                    its last decisions, one line each
    iterations                   per finished iteration: verdict, summary,
                                 what was decided
    rolled_at                    when its conversation was restarted, and why
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

FOLDER = "orchestrator"
FILE = "memory.json"

#: Bounds, so the file -- and the kickoff prompt built from it -- stays small.
MAX_NOTES = 40
MAX_DECISIONS = 60


def path(session_dir: str | Path) -> Path:
    return Path(session_dir) / FOLDER / FILE


def load(session_dir: str | Path) -> dict:
    """The memory, or {} when there is none (or it cannot be read)."""
    if not session_dir:
        return {}
    try:
        value = json.loads(path(session_dir).read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def save(session_dir: str | Path, memory: dict) -> None:
    """Write it whole, atomically. Never raises: losing a memory write must
    not stop a run -- the conversation still holds the detail."""
    if not session_dir:
        return
    target = path(session_dir)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(memory, indent=1, default=str) + "\n")
        os.replace(tmp, target)
    except OSError:
        pass


def update(session_dir: str | Path, **changes) -> dict:
    """Merge `changes` into the memory and save. Lists named `notes`,
    `decisions`, `iterations` and `rolled_at` are APPENDED to, not replaced."""
    memory = load(session_dir)
    for key, value in changes.items():
        if key in ("notes", "decisions", "iterations", "rolled_at"):
            items = list(memory.get(key) or []) + list(value or [])
            limit = {"notes": MAX_NOTES, "decisions": MAX_DECISIONS}.get(key)
            memory[key] = items[-limit:] if limit else items
        else:
            memory[key] = value
    save(session_dir, memory)
    return memory


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def handover_from_bootstrap(session_dir: str | Path, values: dict, *,
                            provider: str = "", model: str = "") -> dict:
    """What the design phase hands the run: the discussor's conversation, and
    what was agreed. Called when the design is approved, and again after
    every re-design (the plan may have changed).

    The thread is kept only when the memory has no newer one of its own:
    after the first run, the orchestrator's own conversation -- which has
    the discussion AND the runs in it -- is the one to continue."""
    memory = load(session_dir)
    design = values.get("design") or {}
    discussion = values.get("discussion") or {}
    plan = design.get("plan") or {}
    threads = (values.get("providers") or {}).get("role_threads") or {}
    changes: dict = {
        "session": Path(session_dir).name,
        "requirements": discussion.get("requirements", "") or memory.get("requirements", ""),
        "plan_summary": plan.get("summary", "") if isinstance(plan, dict) else "",
        "rationale": design.get("rationale", ""),
        "plan_issues": design.get("plan_issues", ""),
        "handed_over": now(),
    }
    thread = threads.get("discussor", "")
    if thread and not memory.get("thread_id"):
        changes.update(thread_id=thread, provider=provider, model=model,
                       thread_from="discussor")
    return update(session_dir, **changes)
