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

import json
import sqlite3
from dataclasses import dataclass
from typing import Callable

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from research_agent import state as S
from research_agent.bootstrap import graph, nodes

THREAD = {"configurable": {"thread_id": "bootstrap"}, "recursion_limit": 200}
#: The nodes a rewind can go back to just before.
POINTS = {"planner", "designer", "researcher", "human"}


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
    def waiting_for_user(self) -> bool:
        """Stopped at the user's turn -- not midway, after a failure."""
        return "human" in (self.graph.get_state(THREAD).next or ())

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

    def retry(self, on_step=None) -> Turn:
        """Run again the step that failed (the checkpoint stops just before it)."""
        return self._drive(None, on_step)

    def exit(self) -> Turn:
        return self._drive(Command(resume={"exit": True}), None)

    # ---- going back ----------------------------------------------------------------

    def points(self) -> list[dict]:
        """Where `rewind` can go, newest first: one point per brief and design
        (approved or not) and what was about to run, at the time it was first
        reached -- across every branch a rewind started."""
        found: dict[str, dict] = {}
        # Newest first, so the earliest checkpoint of a state is the one kept:
        # after a rewind the same state is reached again, later, and should
        # still show at the time you know it by.
        for snap in self.graph.get_state_history(THREAD):
            research = snap.values.get("research")
            # Points worth going back to: just before the designer or the
            # researcher ran, or where the run waited for the user.
            if not research or not set(snap.next or ()) & POINTS:
                continue
            versions, approvals = research.get("versions") or {}, research.get("approvals") or {}
            # One point per brief and design -- their text, not only their
            # numbers, as a branch reuses the numbers -- whether approved, and
            # what ran next: the rest of the state changes at every turn.
            key = json.dumps([[research.get(p), approvals.get(p) == versions.get(p)]
                              for p in ("brief", "design")]
                             + [research.get("plan"), list(snap.next or ())], sort_keys=True)
            found[key] = {"id": snap.config["configurable"]["checkpoint_id"],
                          "at": snap.created_at, "next": list(snap.next or ()),
                          "versions": versions, "approvals": approvals,
                          "summary": (research.get("status") or {}).get("summary", "")}
        return sorted(found.values(), key=lambda p: p["at"], reverse=True)

    def rewind(self, point: str, on_step=None) -> Turn:
        """Back to `point` (a checkpoint of `points`) exactly: the research
        state as it was then, and on from there -- what was about to run then
        runs now. The states after it stay in the history."""
        config = {**THREAD, "configurable": {**THREAD["configurable"], "checkpoint_id": point}}
        snap = self.graph.get_state(config)
        research = snap.values.get("research") if snap and snap.values else None
        if not research:
            raise ValueError(f"No point {point!r} to go back to.")
        S.Store(self.run.folder).write(research, by="user", changed=list(research), ops=[
            {"op": "rewind", "to": point, "as_of": snap.created_at}])
        return self._drive(None, on_step, config=config)

    def _drive(self, value, on_step, config=None) -> Turn:
        for chunk in self.graph.stream(value, config or THREAD, stream_mode="updates"):
            for node, update in chunk.items():
                if on_step and node != "__interrupt__" and isinstance(update, dict):
                    on_step(node, update)
        return self.last_turn

    def aside(self, text: str, activity: str = ""):
        """The user writes while the team works (from another thread): the
        discussor answers from the state on disk."""
        return nodes.aside(self.run, S.Store(self.run.folder).read(), user=text, activity=activity)

    def close(self) -> None:
        self._db.close()
