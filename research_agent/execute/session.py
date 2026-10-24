"""
WHAT:  Driving one run's execution: start it once the design is approved,
       answer the user's turns, give the go for a gated step, stop, leave --
       and resume it later, jobs and all.
WHY:   The CLI and the tests say what the user said and get back what the
       discussor replied, as with the bootstrap.
CONCEPT: The same checkpoint file as the bootstrap (<run>/checkpoint.sqlite),
       another thread ("execute"). The graph is built from the design in the
       research state each time it is opened.
"""

from __future__ import annotations

import sqlite3
from typing import Callable

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from research_agent import state as S
from research_agent.bootstrap.session import Turn
from research_agent.execute import graph, nodes

THREAD = {"configurable": {"thread_id": "execute"}, "recursion_limit": 10_000}


def ready(st: dict) -> list[str]:
    """Why a run cannot be executed yet, in words (empty: it can)."""
    problems = [f"the {s} is not approved" for s in S.APPROVABLE if not S.approved(st, s)]
    if not (st.get("plan") or {}).get("steps"):
        problems.append("there is no plan")
    return problems


class Execution:
    def __init__(self, run: nodes.ExecRun):
        self.run = run
        st = S.Store(run.folder).read()
        problems = ready(st)
        if problems:
            raise ValueError(f"{run.folder} cannot be executed yet: " + "; ".join(problems)
                             + ". Finish the bootstrap first (bootstrap --resume).")
        self.design = nodes.Design.read(st, skills=run.known_skills())
        self._db = sqlite3.connect(run.folder / "checkpoint.sqlite", check_same_thread=False)
        self.graph = graph.build(run, self.design, SqliteSaver(self._db))

    # ---- what is there -------------------------------------------------------------

    @property
    def started(self) -> bool:
        return bool(self.graph.get_state(THREAD).values)

    @property
    def research(self) -> dict:
        values = self.graph.get_state(THREAD).values
        return values.get("research") or S.Store(self.run.folder).read()

    @property
    def awaiting(self) -> str:
        return self.graph.get_state(THREAD).values.get("awaiting", "")

    @property
    def last_turn(self) -> Turn:
        snap = self.graph.get_state(THREAD)
        return Turn(snap.values.get("reply", ""), snap.values.get("outcome", ""), bool(snap.next))

    @property
    def waiting_for_user(self) -> bool:
        return "human" in (self.graph.get_state(THREAD).next or ())

    # ---- driving it -----------------------------------------------------------------

    def start(self, on_step: Callable[[str, dict], None] | None = None) -> Turn:
        if self.started:
            raise RuntimeError(f"{self.run.folder}'s execution has started; resume it instead.")
        st = self._prepare(S.Store(self.run.folder).read())
        first = {"research": st, "seen": dict(st.get("versions") or {}), "pending": [],
                 "decisions": 0, "outcome": "", "awaiting": ""}
        return self._drive(first, on_step)

    def answer(self, text: str = "", *, go: bool = False, stop: bool = False,
               on_step=None) -> Turn:
        return self._drive(Command(resume={"text": text, "go": go, "stop": stop}), on_step)

    def resume(self, on_step=None) -> Turn:
        """Carry on from where the run stopped (a failed call, Ctrl-C while
        jobs ran): the checkpoint stops just before that step."""
        return self._drive(None, on_step)

    def exit(self) -> Turn:
        return self._drive(Command(resume={"exit": True}), None)

    def _drive(self, value, on_step) -> Turn:
        for chunk in self.graph.stream(value, THREAD, stream_mode="updates"):
            for node, update in chunk.items():
                if on_step and node != "__interrupt__" and isinstance(update, dict):
                    on_step(node, update)
        return self.last_turn

    def _prepare(self, st: dict) -> dict:
        """The step table, the budget, and the folders."""
        run = self.run
        for name in ("work", "artifacts", "jobs"):
            (run.folder / name).mkdir(parents=True, exist_ok=True)
        if (st.get("execution") or {}).get("steps"):
            return st
        steps = [{"id": s["id"], "title": s["title"], "owner": self.design.owners[
            self.design.owner_of[s["id"]]]["name"]} for s in st["plan"]["steps"]]
        ops = [{"op": "set", "section": "execution", "value": {
                    "started": nodes._now(), "wall_hours": run.limits.wall_hours,
                    "gpu_hours": run.limits.gpu_hours, "steps": steps}},
               {"op": "merge", "section": "status", "value": {
                   "phase": "executing", "next": "the first step"}}]
        new, changed = S.apply(st, ops, by="execution")
        S.Store(run.folder).write(new, by="execution", ops=ops, changed=changed)
        return new

    def close(self) -> None:
        self._db.close()
