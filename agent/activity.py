"""
WHAT:  Keeps what a provider actually did during each node's turn.
WHY:   "still working (180s elapsed, 313 events, last one 7s ago)" is true and
       useless. Those 313 events are the commands it ran, their output, the
       files it touched and its own account of why -- and they were being
       counted and thrown away.
CONCEPT: Not LangGraph. A small append-only log per node turn, on disk.

--------------------------------------------------------------------------
WHY DISK AND NOT STATE
--------------------------------------------------------------------------
The obvious place is the graph's state, where every other per-node result
lives. It is the wrong place, for two reasons that only show up later:

1. **Size.** One executor turn produced 313 events; a full pytest run inside
   one of them is megabytes of output. LangGraph writes the WHOLE state on
   every super-step, so putting this in state means rewriting all of it, all
   over again, at every step of a twenty-node graph.

2. **It is not state.** Nothing routes on it, no prompt renders it, no node
   reads another node's events. It is a record for a human, and the test for
   whether something belongs in state is whether the graph needs it.

So: `<session>/activity/<node>/<n>.json`, one file per turn, newest number
highest. Cheap to write, trivial to read back, and it disappears with the
session like everything else under it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


#: Newest-first cap when listing. A node in a retry loop can run many times;
#: the recent turns are the ones anybody looks at.
DEFAULT_LIMIT = 20

#: The turn currently in flight, rewritten as it goes. A fixed name rather
#: than the next number, because it is not a turn yet -- it becomes one when
#: it finishes, and until then there must be exactly one of it.
IN_FLIGHT = "current.json"


@dataclass(frozen=True)
class Turn:
    """One recorded turn of one node."""

    node: str
    index: int
    path: Path
    started: str
    events: list[dict]
    usage: dict | None = None
    summary: dict | None = None
    progress: dict | None = None
    """How the turn is going, as the backend last reported it.

    Only present while it runs: elapsed, idle, the counts by kind, and
    `streamed` -- how many tokens of answer have been typed. That last one is
    the honest answer to "why is this taking so long" for a node whose output
    is a 12,000-token document, and it is not derivable from the events,
    because typed tokens are deliberately counted rather than kept.
    """

    partial: bool = False
    """True while the turn is still running.

    The reason this exists at all: the record used to be written only when a
    turn ENDED, so during a twenty-minute turn -- exactly when you want to
    know what is happening -- there was nothing to look at. A UI needs to say
    "still going" rather than presenting an unfinished turn as a finished one.
    """

    def counts(self) -> dict[str, int]:
        """How many of each kind of event -- the one-line version."""
        out: dict[str, int] = {}
        for event in self.events:
            kind = str(event.get("kind", "?"))
            # started/completed pairs describe ONE thing, so count the
            # completion and ignore the start. Otherwise every command appears
            # twice and the totals are quietly doubled.
            if event.get("phase") == "started" and kind != "error":
                continue
            out[kind] = out.get(kind, 0) + 1
        return out

    def as_dict(self) -> dict:
        return {
            "node": self.node,
            "index": self.index,
            "started": self.started,
            "counts": self.counts(),
            "partial": self.partial,
            "progress": self.progress,
            "events": self.events,
            "usage": self.usage,
            "summary": self.summary,
        }


def directory(session_dir: str | Path, node: str) -> Path:
    return Path(session_dir) / "activity" / _safe(node)


def write(session_dir: str | Path, node: str, events: list[dict], *,
          usage: dict | None = None, summary: dict | None = None) -> Path | None:
    """Append one turn's record. Returns the file, or None if there was nothing.

    Never raises. A node's real work must not fail because we could not write
    a log about it -- that would be the tail wagging the dog, and the failure
    would arrive as a mysterious node error rather than a missing file.
    """
    # An empty session_dir means "nowhere to write", and the guard belongs
    # HERE rather than at each call site. Without it, Path("") / "activity"
    # is a RELATIVE path, so a caller with no session -- a test, or a folder
    # driven outside one -- silently created ./activity/ in whatever directory
    # the process happened to be in.
    if not events or not str(session_dir).strip():
        return None
    try:
        folder = directory(session_dir, node)
        folder.mkdir(parents=True, exist_ok=True)
        index = _next_index(folder)
        path = folder / f"{index:04d}.json"
        path.write_text(json.dumps({
            "node": node,
            "index": index,
            "started": _now(),
            "usage": usage,
            "summary": summary,
            "events": events,
        }, indent=1) + "\n")
        return path
    except Exception:  # noqa: BLE001 -- see the docstring
        return None


def write_in_flight(session_dir: str | Path, node: str, events: list[dict],
                    **extra) -> Path | None:
    """Rewrite the record of the turn currently running.

    Called periodically while a turn is in flight, so the detail behind a
    "still working" line exists BEFORE the turn ends. Same never-raises
    contract as write(): a node's real work must not fail over a log.

    Rewriting the whole file each time rather than appending, because an
    append-only file that is being read concurrently can be read halfway
    through a line. A whole-file write is not atomic either, so readers
    tolerate a JSONDecodeError -- see turns().
    """
    if not events or not str(session_dir).strip():
        return None
    try:
        folder = directory(session_dir, node)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / IN_FLIGHT
        path.write_text(json.dumps({
            "node": node,
            "index": _next_index(folder),
            "started": _now(),
            "events": events,
            **extra,
        }, indent=1) + "\n")
        return path
    except Exception:  # noqa: BLE001 -- see the docstring
        return None


def clear_in_flight(session_dir: str | Path, node: str) -> None:
    """Drop the in-flight record once the turn has become a real one."""
    try:
        (directory(session_dir, node) / IN_FLIGHT).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def turns(session_dir: str | Path, node: str, *,
          limit: int = DEFAULT_LIMIT) -> list[Turn]:
    """Recorded turns for one node, newest first, in-flight turn included."""
    folder = directory(session_dir, node)
    if not folder.is_dir():
        return []

    out: list[Turn] = []
    numbered = sorted((p for p in folder.glob("*.json") if p.stem.isdigit()),
                      reverse=True)
    # The in-flight turn first: it is the newest, and it is the one somebody
    # watching a long turn actually came for.
    paths = [folder / IN_FLIGHT] + numbered[:limit]

    for path in paths:
        if not path.exists():
            continue
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            # A partial file can be caught mid-write. Skipping it is right:
            # the next poll gets a whole one, a second apart.
            continue
        out.append(Turn(
            node=node,
            index=int(raw.get("index", 0)),
            path=path,
            started=str(raw.get("started", "")),
            events=list(raw.get("events") or []),
            usage=raw.get("usage"),
            summary=raw.get("summary"),
            partial=path.name == IN_FLIGHT,
            progress={k: raw[k] for k in
                      ("elapsed", "idle", "streamed", "last") if k in raw} or None,
        ))
    return out


def latest(session_dir: str | Path, node: str) -> Turn | None:
    found = turns(session_dir, node, limit=1)
    return found[0] if found else None


def nodes_with_activity(session_dir: str | Path) -> list[str]:
    root = Path(session_dir) / "activity"
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def in_flight(session_dir: str | Path) -> Turn | None:
    """Whichever node is mid-turn right now, if any.

    The UI needs this because the "still working" line does not say which node
    it belongs to -- and asking the human to know is asking them to hold state
    the server already has. Only one node runs at a time (the validator
    refuses fan-out), so "the one in-flight record" is well defined.
    """
    for node in nodes_with_activity(session_dir):
        for turn in turns(session_dir, node, limit=1):
            if turn.partial:
                return turn
    return None


def _next_index(folder: Path) -> int:
    existing = [int(p.stem) for p in folder.glob("*.json") if p.stem.isdigit()]
    return (max(existing) + 1) if existing else 1


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe(name: str) -> str:
    """Node names are already `[a-z][a-z0-9_]*`, but this is a path."""
    return "".join(c if c.isalnum() or c in "-_." else "-" for c in name) or "node"


def arm_progress(backend, session_dir: str | Path, node: str) -> None:
    """Point a backend's progress callback at this node's in-flight record.

    A one-liner with a docstring because the ownership is the interesting
    part: the BACKEND knows what happened, and only the NODE knows where it
    belongs. Rather than teaching the backend about sessions, or threading a
    sink through the whole call chain, the node hands over a closure for the
    duration of its own call.
    """
    if not hasattr(backend, "on_progress"):
        return

    def flush(events, elapsed, idle, kinds, last, streamed=0) -> None:
        write_in_flight(session_dir, node, events,
                        elapsed=round(elapsed, 1), idle=round(idle, 1),
                        counts=kinds, last=last, streamed=streamed)

    backend.on_progress = flush


def disarm_progress(backend, session_dir: str | Path, node: str) -> None:
    """Unhook the callback and drop the in-flight file.

    Both matter. A backend instance is shared between roles, so a stale
    closure would write another node's events into this node's folder. And
    leaving the in-flight file behind would show a finished turn twice --
    once as itself and once as "still running".
    """
    if hasattr(backend, "on_progress"):
        backend.on_progress = None
    clear_in_flight(session_dir, node)
