"""
WHAT:  Driving one bootstrap run: start it with an idea, answer the user's
       turns, approve sections, leave -- and resume it later from its folder.
WHY:   The CLI (and the tests) should not need to know LangGraph: they say
       what the user said, and get back what the discussor replied.
CONCEPT: One LangGraph thread per run, checkpointed in
       <run>/checkpoint.sqlite. The graph stops at the `human` interrupt each
       time the user is to speak; answer() resumes it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from research_agent import state as S
from research_agent.bootstrap import graph, nodes

THREAD = {"configurable": {"thread_id": "bootstrap"}, "recursion_limit": 200}


@dataclass
class Turn:
    """Where a run stopped: what the discussor said, and whether it is done."""

    reply: str
    outcome: str = ""
    """"" (waiting for the user), "ready" or "exit"."""
    waiting: bool = True


class Bootstrap:
    def __init__(self, run: nodes.Run):
        self.run = run
        run.folder.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(run.folder / "checkpoint.sqlite", check_same_thread=False)
        self.graph = graph.build(run, SqliteSaver(self._db))

    # ---- what is there -------------------------------------------------------------

    @property
    def started(self) -> bool:
        return bool(self.graph.get_state(THREAD).values)

    @property
    def research(self) -> dict:
        return self.graph.get_state(THREAD).values.get("research") or S.new()

    @property
    def last_turn(self) -> Turn:
        snap = self.graph.get_state(THREAD)
        return Turn(snap.values.get("reply", ""), snap.values.get("outcome", ""), bool(snap.next))

    # ---- driving it -------------------------------------------------------------------

    def start(self, idea: str, on_step: Callable[[str, dict], None] | None = None) -> Turn:
        if self.started:
            raise RuntimeError(f"{self.run.folder} has already started; resume it instead.")
        first = {"research": S.Store(self.run.folder).read(), "user": idea, "seen": {},
                 "pending": [], "steps": 0, "outcome": ""}
        return self._drive(first, on_step)

    def answer(self, text: str = "", *, approve=(), on_step=None) -> Turn:
        return self._drive(Command(resume={"text": text, "approve": list(approve)}), on_step)

    def exit(self) -> Turn:
        return self._drive(Command(resume={"exit": True}), None)

    def _drive(self, value, on_step) -> Turn:
        for chunk in self.graph.stream(value, THREAD, stream_mode="updates"):
            for node, update in chunk.items():
                if on_step and node != "__interrupt__" and isinstance(update, dict):
                    on_step(node, update)
        return self.last_turn

    def close(self) -> None:
        self._db.close()
