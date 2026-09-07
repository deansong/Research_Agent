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


def turns(session_dir: str | Path, node: str, *,
          limit: int = DEFAULT_LIMIT) -> list[Turn]:
    """Recorded turns for one node, newest first."""
    folder = directory(session_dir, node)
    if not folder.is_dir():
        return []

    out: list[Turn] = []
    for path in sorted(folder.glob("*.json"), reverse=True)[:limit]:
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        out.append(Turn(
            node=node,
            index=int(raw.get("index", 0)),
            path=path,
            started=str(raw.get("started", "")),
            events=list(raw.get("events") or []),
            usage=raw.get("usage"),
            summary=raw.get("summary"),
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


def _next_index(folder: Path) -> int:
    existing = [int(p.stem) for p in folder.glob("*.json") if p.stem.isdigit()]
    return (max(existing) + 1) if existing else 1


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe(name: str) -> str:
    """Node names are already `[a-z][a-z0-9_]*`, but this is a path."""
    return "".join(c if c.isalnum() or c in "-_." else "-" for c in name) or "node"
