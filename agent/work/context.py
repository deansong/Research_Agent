"""
WHAT:  The orchestrator's way of reaching into a node's detail: one node's
       report, output, file changes, command log, prompt or whole turn, as
       text, on request.
WHY:   The orchestrator carries a step book, not transcripts -- each step's
       final report once it passed, or just "finished". Everything else (the
       logs, the diffs, what the node was told) stays with the node that
       produced it. When a later problem points back there, this brings it
       out again, for the orchestrator's own next turn, for the node it sends
       work to, or for the validator.
CONCEPT: Not LangGraph. Code resolves the pull, not the model: the model
       names WHAT it wants ({node, what}); this reads it from state and from
       the activity records on disk.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent import activity
from agent.supervisor import clip_middle

#: What a pull may ask for.
WHATS = ("report", "output", "changes", "log", "prompt", "turn")

#: One pulled item is never more than this share of the budget it is
#: resolved into, so one huge log cannot crowd out the rest.
ITEM_SHARE = 0.5


def _text(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, indent=1, default=str)


def report_of(state: dict, node: str) -> str:
    """A node's final report: the saved file if there is one, else the
    `report` field of its last output."""
    data = (state.get("outputs") or {}).get(node) or {}
    path = data.get("report_path")
    if path and Path(path).is_file():
        try:
            return Path(path).read_text().strip()
        except OSError:
            pass
    return str(data.get("report") or "").strip()


def output_text(data: dict, *, skip: tuple[str, ...] = ()) -> str:
    """Every field of one output, one per paragraph."""
    lines = []
    for key, value in (data or {}).items():
        if key in skip or value in (None, "", [], {}):
            continue
        lines.append(f"{key}: {_text(value)}")
    return "\n".join(lines)


def resolve_one(session_dir: str, state: dict, node: str, what: str) -> str:
    """One pull, as text, or "" when there is nothing to show."""
    outputs = state.get("outputs") or {}
    data = outputs.get(node) or {}
    if what == "report":
        return report_of(state, node)
    if what == "output":
        return output_text(data)
    if what == "changes":
        return str(data.get("turn_changes") or data.get("git_diff") or "")
    turn = activity.latest_finished(session_dir, node) if session_dir else None
    if what == "log":
        return activity.log_tail(turn)
    if what == "prompt":
        return str(((turn.sent if turn else None) or {}).get("prompt") or "")
    if what == "turn":
        if turn is None:
            return ""
        counts = ", ".join(f"{n} {k}" for k, n in turn.counts().items())
        files = sorted({c.get("path", "") for e in turn.events if e.get("kind") == "file_change"
                        for c in e.get("changes") or []} - {""})
        said = [e.get("text", "") for e in turn.events if e.get("kind") == "message"]
        return "\n".join(x for x in (
            f"turn {turn.index} on {turn.provider}/{turn.model}: {counts}",
            f"files changed: {', '.join(files)}" if files else "",
            f"commands:\n{activity.log_tail(turn, lines=8)}" if turn.events else "",
            f"its last message: {said[-1]}" if said else "",
        ) if x)
    return ""


def resolve(session_dir: str, state: dict, pulls, budget: int) -> str:
    """Every pull, each under its own heading, within `budget` characters.

    `pulls` are {node, what} dicts (or models with those attributes). A pull
    of a node that never ran, or of nothing, says so rather than vanishing --
    the orchestrator asked for it and should know it got nothing.
    """
    items = []
    seen = set()
    for pull in pulls or []:
        node = getattr(pull, "node", None) or (pull.get("node") if isinstance(pull, dict) else "")
        what = getattr(pull, "what", None) or (pull.get("what") if isinstance(pull, dict) else "")
        if not node or what not in WHATS or (node, what) in seen:
            continue
        seen.add((node, what))
        items.append((node, what, resolve_one(session_dir, state, node, what)))
    if not items:
        return ""
    each = max(int(budget / len(items)), int(budget * ITEM_SHARE / len(items)), 2000)
    blocks = [f"--- {what} of {node} ---\n" + (clip_middle(text, each) if text else "(nothing)")
              for node, what, text in items]
    return "\n\n".join(blocks)
