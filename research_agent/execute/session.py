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
from research_agent import inbox
from research_agent.execute import graph, jobs, nodes

THREAD = {"configurable": {"thread_id": "execute"}, "recursion_limit": 10_000}


def ready(st: dict) -> list[str]:
    """Why a run cannot be executed yet, in words (empty: it can)."""
    problems = [] if S.approved(st, "design") else ["the design is not approved"]
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
               work: bool = False, on_step=None) -> Turn:
        """`work`: no words -- the team acts on what the inbox holds now."""
        return self._drive(Command(resume={"text": text, "go": go, "stop": stop, "work": work}),
                           on_step)

    def resume(self, on_step=None) -> Turn:
        """Carry on from where the run stopped (a failed call, Ctrl-C while
        jobs ran): the checkpoint stops just before that step."""
        return self._drive(None, on_step)

    def exit(self) -> Turn:
        return self._drive(Command(resume={"exit": True}), None)

    def aside(self, text: str):
        """The user writes while the team works (from another thread): the
        discussor answers from the state on disk -- the graph keeps its own
        copy -- and what it records goes to the inbox."""
        st = S.Store(self.run.folder).read()
        return nodes.aside(self.run, st, user=text, activity=self.activity(st))

    def activity(self, st: dict | None = None) -> str:
        """What is going on now, in words: steps under way, jobs, the budget."""
        st = st or S.Store(self.run.folder).read()
        lines = [f"Step {r['id']} ({r.get('owner', '')}): {r['status']}, attempt "
                 f"{r.get('attempts', 0)}, since {r.get('started') or '-'}"
                 + (f" -- {r['summary']}" if r.get("summary") else "")
                 for r in nodes.records(st).values()
                 if r["status"] in ("running", "waiting", "validating")]
        lines += [f"Job {jobs.describe(j)}" for j in jobs.records(self.run.folder)
                  if jobs.status(j)["state"] == "running"]
        lines.append(f"Budget: {graph.budget(self.run, st)[0]}")
        waiting = inbox.peek(self.run.folder)
        if waiting:
            lines.append("Waiting for the orchestrator's next decision: "
                         + "; ".join(f"{i['kind']} {i.get('text') or i.get('step') or ''}".strip()
                                     for i in waiting))
        return "\n".join(lines)

    def stop_now(self) -> int:
        """/stop while the team works: the jobs stop now, the graph at its next
        step. Returns how many jobs were stopped."""
        stopped = sum(jobs.kill(self.run.folder, j["id"]) for j in jobs.records(self.run.folder))
        inbox.post(self.run.folder, "stop", why="the user stopped the execution")
        return stopped

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
