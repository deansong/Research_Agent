"""
WHAT:  Messages for a run's graph from outside it: what the user said while
       the team was working -- a request for the orchestrator, a decision, and
       in execution a breakpoint or a stop -- kept in <run>/inbox.json until
       the graph takes them.
WHY:   The user can talk to the discussor at any time, but the work runs in
       the graph and changes the research state there. So a conversation on
       the side never writes the state; it posts here, and the graph applies
       what it finds at its next step (a stop also while jobs run).
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
from pathlib import Path

KINDS = ("request", "breakpoint", "stop", "decision")
_threads = threading.Lock()


def post(run_dir, kind: str, **fields) -> None:
    if kind not in KINDS:
        raise ValueError(f"No inbox kind {kind!r}; the kinds are {', '.join(KINDS)}.")
    with _locked(run_dir) as path:
        items = _read(path)
        items.append({"kind": kind, **fields})
        _write(path, items)


def take(run_dir, kinds=KINDS) -> list[dict]:
    """The waiting messages of these kinds, removed from the inbox."""
    with _locked(run_dir) as path:
        items = _read(path)
        taken = [i for i in items if i["kind"] in kinds]
        if taken:
            _write(path, [i for i in items if i["kind"] not in kinds])
        return taken


def peek(run_dir) -> list[dict]:
    with _locked(run_dir) as path:
        return _read(path)


class _locked:
    def __init__(self, run_dir):
        self.path = Path(run_dir) / "inbox.json"

    def __enter__(self) -> Path:
        _threads.acquire()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path.with_suffix(".lock"), "w")
        fcntl.flock(self.handle, fcntl.LOCK_EX)
        return self.path

    def __exit__(self, *exc):
        fcntl.flock(self.handle, fcntl.LOCK_UN)
        self.handle.close()
        _threads.release()


def _read(path: Path) -> list[dict]:
    return json.loads(path.read_text()) if path.exists() else []


def _write(path: Path, items: list[dict]) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(items, indent=2) + "\n")
    os.replace(temp, path)
