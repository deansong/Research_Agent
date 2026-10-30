"""
WHAT:  One open run in the web UI: its graph running on a worker thread, the
       discussor always there to talk to, and a feed of events.
WHY:   The two things the UI must guarantee live here.
       - The discussor is always alive: a message while the graph waits for
         the user answers it; a message while the team works is an aside --
         the discussor answers on the side and the work goes on; a message
         after a failure or a pause is an aside too.
       - The research state is always current: a watcher sends a `state`
         event whenever state/research.json changes, whoever changed it.
CONCEPT: The stage follows the research state: "bootstrap" until the
       execution has started (the state has its step table), then
       "execution". execute.json can be written before that, to choose the
       execution's models. Only the worker
       thread touches the graph's checkpoint; requests read the state from
       its files, and the status the worker left.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import traceback
from dataclasses import replace
from pathlib import Path
from typing import Callable

import research_backend as RB

from research_agent import autopilot, chat, fields, inbox
from research_agent import state as S
from research_agent.bootstrap import config as boot_config
from research_agent.bootstrap.config import RoleConfig
from research_agent.bootstrap import nodes as boot_nodes
from research_agent.bootstrap.session import Bootstrap
from research_agent.execute import config as exec_config
from research_agent.execute import nodes as exec_nodes
from research_agent.execute.session import Execution
from research_agent.web.events import EventBus

#: How often the watcher looks at the research state file, in seconds.
WATCH_SECONDS = 0.5
#: The newest events of a running call that the live view is sent.
LIVE_EVENTS = 300


class Busy(RuntimeError):
    """The run is working; this needs it to stop for the user first."""


class _Paused(Exception):
    """Raised between nodes when the user asked to pause."""


class RunHost:
    def __init__(self, folder: Path, *, roles=(), watch: bool = True):
        self.folder = Path(folder).expanduser().resolve()
        self.id = self.folder.name
        self.bus = EventBus()
        self.roles = list(roles)
        """--role style overrides for this server (e.g. all=fake in tests)."""
        self._lock = threading.RLock()
        self._worker: threading.Thread | None = None
        self._pause = threading.Event()
        self._switching = ""
        """Set when a role's model changed while its call waited: the run is
        paused to end the wait, and carries on by itself."""
        self._asides = 0
        self.error = ""
        self.current = ""
        """The model call under way, in words -- for asides and the UI."""
        self._status: dict = {}
        self._versions: dict = {}
        self.live: dict[str, dict] = {}
        """Session -> the call running in it: label, node, model, when it
        started, and the newest snapshot of what the model is doing."""
        self.stage = stage_of(S.Store(self.folder).read())
        self.session = self._open_execution() if self.stage == "execution" else self._open_bootstrap()
        self._refresh_status()
        self._closed = threading.Event()
        if watch:
            threading.Thread(target=self._watch, daemon=True, name=f"watch-{self.id}").start()

    # ---- opening the stage ------------------------------------------------------------

    def _open_bootstrap(self) -> Bootstrap:
        cfg = boot_config.load(self.folder, roles=self.roles)
        self.folder.mkdir(parents=True, exist_ok=True)
        boot_config.save(self.folder, cfg)
        run = boot_nodes.Run.from_config(self.folder, cfg)
        run.quiet, run.listener, run.interrupt = True, self._feed, self._pause
        return Bootstrap(run)

    def _open_execution(self, limits: dict | None = None, roles=()) -> Execution:
        cfg = exec_config.load(self.folder, roles=list(roles), limits=limits or {})
        exec_config.save(self.folder, cfg)
        discussor = boot_config.load(self.folder, roles=[r for r in self.roles
                                                         if r.split("=")[0] in ("all", "discussor")])
        run = exec_nodes.ExecRun.from_config(self.folder, cfg, discussor.roles["discussor"])
        run.quiet, run.listener, run.interrupt = True, self._feed, self._pause
        return Execution(run)

    # ---- what is there ----------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return bool(self._worker and self._worker.is_alive())

    def detail(self) -> dict:
        st = S.Store(self.folder).read()
        roles = {r: str(c) for r, c in self.session.run.roles.items()}
        return {"id": self.id, "folder": str(self.folder), "stage": self.stage,
                "field": fields.of_run(self.folder),
                "phase": (st.get("status") or {}).get("phase") or "discussing",
                "goal": (st.get("goal") or {}).get("question", ""),
                "busy": self.busy, "asides": self._asides, "current": self.current if self.busy else "",
                "error": self.error, "roles": roles,
                "can_execute": self.stage == "bootstrap" and S.approved(st, "design")
                and not self.busy,
                "autopilot": vars(autopilot.load(self.folder)),
                "held_idea": self.held_idea if not self._status.get("started") else "",
                **self._status}

    def _refresh_status(self) -> None:
        """Read where the graph stopped. Only from the worker, or while idle."""
        s = self.session
        turn = s.last_turn
        waiting = s.waiting_for_user if s.started else False
        self._status = {"started": s.started, "waiting": waiting, "outcome": turn.outcome,
                        "awaiting": getattr(s, "awaiting", "") if self.stage == "execution" else "",
                        # A node still to run that is not the user's: the graph
                        # was interrupted (a pause, a model switched mid-call),
                        # whatever the last outcome said. Measured: run
                        # 1134 kept outcome "stopped" with its discussor
                        # pending, so no Carry on and every message an aside.
                        "stopped_midway": s.started and not waiting and turn.waiting}

    def status_event(self) -> None:
        self.bus.emit("status", **self.detail())

    # ---- what the user does -----------------------------------------------------------

    # ---- a run set up before it starts ------------------------------------------------

    IDEA_FILE = "idea.txt"

    @property
    def held_idea(self) -> str:
        """The idea of a run created but not started yet ("" otherwise)."""
        path = self.folder / self.IDEA_FILE
        try:
            return path.read_text(encoding="utf-8").strip() if path.exists() else ""
        except OSError:
            return ""

    def hold(self, idea: str) -> None:
        """Create the run without starting it: nothing calls a model until
        Start, so the models, backups, skills and sources can be set first."""
        (self.folder / self.IDEA_FILE).write_text(idea.strip() + "\n", encoding="utf-8")
        self.bus.emit("note", text="The run is set up but not started. Choose the models and "
                                   "backups (Run tab), skills and sources (each node's window), "
                                   "then press Start.")
        self.status_event()

    def start_held(self) -> None:
        idea = self.held_idea
        if not idea:
            raise ValueError("This run has no idea waiting to start.")
        self.message(idea)

    def message(self, text: str) -> str:
        """The user says something: an answer, an aside, or the idea that
        starts the run. Returns which."""
        text = text.strip()
        if not text:
            raise ValueError("Say something.")
        with self._lock:
            if self.stage == "bootstrap" and not self._status.get("started") and not self.busy:
                (self.folder / self.IDEA_FILE).unlink(missing_ok=True)
                self.bus.emit("chat", role="user", text=text, kind="answer")
                self._run(lambda on: self.session.start(text, on_step=on))
                return "start"
            if not self.busy and self._status.get("waiting"):
                self.bus.emit("chat", role="user", text=text, kind="answer")
                self._run(lambda on: self.session.answer(text, on_step=on))
                return "answer"
            self.bus.emit("chat", role="user", text=text, kind="aside")
            self._aside(text)
            if not self.busy and self._status.get("stopped_midway"):
                self.bus.emit("note", text="The run stopped midway: press Carry on, and the team "
                                           "goes on with what you said.")
            return "aside"

    def approve(self, sections: list[str]) -> None:
        bad = [s for s in sections if s not in S.APPROVABLE]
        if self.stage != "bootstrap" or not sections or bad:
            raise ValueError(f"Approve which? {', '.join(S.APPROVABLE)} (in the bootstrap).")
        st = S.Store(self.folder).read()
        missing = [s for s in sections if not S.version(st, s)]
        if missing:
            raise ValueError(f"There is no {', '.join(missing)} to approve yet.")
        with self._lock:
            self._need_waiting()
            self.bus.emit("chat", role="user", text=f"(approved: {', '.join(sections)})",
                          kind="answer")
            self._run(lambda on: self.session.answer("", approve=sections, on_step=on))

    def go(self) -> None:
        with self._lock:
            self._need_waiting()
            if self.stage != "execution" or not self._status.get("awaiting"):
                raise ValueError("Nothing is waiting for your go.")
            self.bus.emit("chat", role="user", text=f"(go: {self._status['awaiting']})",
                          kind="answer")
            self._run(lambda on: self.session.answer("", go=True, on_step=on))

    def stop(self) -> str:
        """Execution: stop the jobs now and the work at its next step. The
        bootstrap has nothing running on its own: pause it."""
        with self._lock:
            if self.stage == "bootstrap":
                return self.pause()
            if self.busy:
                stopped = self.session.stop_now()
                self.bus.emit("note", text=f"Stopping: {stopped} running jobs stopped; the team "
                                           f"stops at its next step.")
                return "stopping"
            self._need_waiting()
            self.bus.emit("chat", role="user", text="(stop)", kind="answer")
            self._run(lambda on: self.session.answer("", stop=True, on_step=on))
            return "stopped"

    def pause(self) -> str:
        if not self.busy:
            raise ValueError("Nothing is running.")
        self._pause.set()
        waiting = any(c.get("notice") for c in self.live.values())
        self.bus.emit("note", text="Pausing now: the call was only waiting." if waiting
                      else "Pausing after the current step...")
        return "pausing"

    def resume(self) -> None:
        """Carry on after a failure or a pause."""
        with self._lock:
            if self.busy:
                raise Busy("The run is already working.")
            if not self._status.get("stopped_midway"):
                raise ValueError("The run is not stopped midway: say something instead.")
            go_on = self.session.retry if self.stage == "bootstrap" else self.session.resume
            self._run(lambda on: go_on(on_step=on))

    # ---- autopilot ----------------------------------------------------------------------

    def autopilot_settings(self) -> dict:
        exe = exec_config.load(self.folder)
        return {**vars(autopilot.load(self.folder)), "wall_hours": exe.wall_hours,
                "gpu_hours": exe.gpu_hours}

    def set_autopilot(self, on: bool, *, max_decisions: int | None = None,
                      wall_hours: float | None = None, gpu_hours: float | None = None) -> dict:
        """Tick or untick Autopilot. On, it needs the execution's limits --
        wall-clock and GPU hours -- set beforehand: nobody is there to stop
        it. Applies at the run's next stop; a run waiting now goes on."""
        exe = exec_config.load(self.folder)
        wall = exe.wall_hours if wall_hours is None else float(wall_hours)
        gpu = exe.gpu_hours if gpu_hours is None else float(gpu_hours)
        if on and (wall <= 0 or gpu <= 0):
            raise ValueError("Autopilot runs unattended: set the execution's limits first -- "
                             "wall-clock hours and GPU-hours, both above 0.")
        if (wall, gpu) != (exe.wall_hours, exe.gpu_hours):
            if wall < 0 or gpu < 0:
                raise ValueError("The limits cannot be negative.")
            exe = replace(exe, wall_hours=wall, gpu_hours=gpu)
            exec_config.save(self.folder, exe)
            if self.stage == "execution":
                self.session.run.limits = replace(self.session.run.limits, wall_hours=wall,
                                                  gpu_hours=gpu)
        s = autopilot.load(self.folder)
        s.on = bool(on)
        if max_decisions is not None:
            if int(max_decisions) < 1:
                raise ValueError("max_decisions is at least 1.")
            s.max_decisions = int(max_decisions)
        if on:
            s.streak, s.handed_back = 0, ""
        autopilot.save(self.folder, s)
        self.bus.emit("note", text=(f"Autopilot on: it decides for you, up to {s.max_decisions} "
                                    f"decisions in a row; limits {wall:g} h, {gpu:g} GPU-h."
                                    if on else "Autopilot off: the run waits for you again."))
        self.status_event()
        if on:
            self._autopilot_go_on()
        return self.autopilot_settings()

    def _carry_on(self, why: str) -> None:
        try:
            self.resume()
            self.bus.emit("note", text=f"Carrying on: {why}.")
        except (ValueError, Busy) as exc:
            self.bus.emit("note", text=f"Could not carry on by itself ({exc}): press Carry on.")

    def _autopilot_go_on(self) -> None:
        """Autopilot was ticked, or a stage just ended: if the run is waiting
        for the user, let the autopilot answer now; if the bootstrap is done,
        start the execution."""
        with self._lock:
            s = autopilot.load(self.folder)
            if self.busy or not s.on or s.handed_back or s.streak >= s.max_decisions:
                return              # off, or it handed back: the user answers
            if self._status.get("stopped_midway") and self.error:
                # Stopped by an error, not by the user: carry on, as one of
                # its decisions -- so an error that keeps coming back stops
                # at the limit, handed back.
                autopilot.counted(self.folder)
                self.bus.emit("note", text=f"Autopilot: the run stopped with an error "
                                           f"({self.error[:200]}); carrying on.")
                self.resume()
                return
            if self.stage == "bootstrap" and self._status.get("outcome") == "ready":
                try:
                    self.execute()
                except ValueError as exc:
                    self.bus.emit("note", text=f"Autopilot cannot start the execution: {exc}")
                return
            if self._status.get("waiting") and not self._status.get("outcome"):
                # Resumed with no words: the waiting step asks the autopilot.
                self._run(lambda on: self.session.answer("", on_step=on))

    def breakpoint(self, step: str, when: str, on: bool) -> None:
        if self.stage != "execution":
            raise ValueError("Breakpoints are for the execution stage.")
        steps = {s["id"] for s in (S.Store(self.folder).read().get("plan") or {}).get("steps") or []}
        if step not in steps or when not in ("before", "after"):
            raise ValueError("A breakpoint is a plan step, before or after.")
        inbox.post(self.folder, "breakpoint", step=step, when=when, on=on)
        self.bus.emit("note", text=f"Breakpoint {'set' if on else 'removed'}: {when} step {step} "
                                   f"(from the team's next step).")

    def step_action(self, step: str, action: str, why: str = "") -> str:
        """The user's call on a failed step: another attempt (also reopening a
        skipped one), or a pass with their reason. Taken at the team's next step; if the team waits for
        the user, it goes on now."""
        if self.stage != "execution":
            raise ValueError("Steps are for the execution stage.")
        rec = (S.Store(self.folder).read().get("execution") or {}).get("steps") or []
        status = next((r["status"] for r in rec if r["id"] == step), None)
        if action not in ("retry", "waive", "rewind"):
            raise ValueError("The action is retry, waive or rewind.")
        if action == "rewind":
            problems = exec_nodes.rewind_problems(self.folder, S.Store(self.folder).read(), step)
            if problems:
                raise ValueError(problems[0].removeprefix("rewind: "))
        elif status not in (exec_nodes.REOPENABLE if action == "retry" else ("fail", "stopped")):
            raise ValueError(f"Step {step} has not failed (it is {status or 'not a step'}).")
        if action == "waive" and not why.strip():
            raise ValueError("Say why the step passes anyway: it is kept as its verdict.")
        inbox.post(self.folder, action, step=step, why=why.strip())
        word = {"retry": "another attempt", "rewind": "start over from here"}.get(
            action, f"a pass ({why.strip()})")
        with self._lock:
            if not self.busy and self._status.get("waiting"):
                self.bus.emit("chat", role="user", text=f"(step {step}: {word})", kind="answer")
                self._run(lambda on: self.session.answer("", work=True, on_step=on))
                return "now"
        self.bus.emit("note", text=f"Step {step}: {word}, from the team's next step.")
        return "next step"

    def review(self, paper: bool = False) -> None:
        """The finished study reviewed again -- `paper`: its paper written
        again first: every step settled, the team waiting for the user."""
        with self._lock:
            if self.stage != "execution":
                raise ValueError("Only a finished execution is reviewed.")
            self._need_waiting()
            steps = (S.Store(self.folder).read().get("execution") or {}).get("steps") or []
            open_ = [r["id"] for r in steps if r["status"] not in exec_nodes.SETTLED]
            if open_:
                raise ValueError(f"Steps {', '.join(open_)} have not passed or been skipped yet.")
            if paper:
                self.bus.emit("note", text="The paper is written again, then reviewed.")
                self._run(lambda on: self.session.paper(on_step=on))
            else:
                self.bus.emit("note", text="The reviewer reviews the study again.")
                self._run(lambda on: self.session.review(on_step=on))

    def rewind_points(self) -> list[dict]:
        """Where the design stage can go back to, newest first."""
        if self.stage != "bootstrap":
            return []
        return self.session.points()

    def rewind(self, point: str) -> None:
        """The design stage back to `point`, exactly, and on from there."""
        with self._lock:
            if self.stage != "bootstrap":
                raise ValueError("Rewinding is for the design stage: go Back to design first.")
            if self.busy:
                raise Busy("The team is working: pause it first.")
            found = next((p for p in self.session.points() if p["id"] == point), None)
            if found is None:
                raise ValueError(f"No point {point!r} to go back to.")
            ap = autopilot.load(self.folder)
            if ap.on:       # going back is to see what happens: the user answers
                autopilot.save(self.folder, replace(ap, on=False))
            self.bus.emit("note", text=f"Rewound to {found['at'][:16].replace('T', ' ')} UTC: "
                                       f"{describe_point(found)}. The run goes on from there"
                                       + ("; the autopilot is off." if ap.on else "."))
            self._run(lambda on: self.session.rewind(point, on_step=on))

    def back_to_design(self) -> str:
        """The execution set aside and the run back in the design stage, in
        the same folder: the jobs stopped, the steps' outputs moved to
        artifacts/_before_redesign-<time>, the step table cleared (the
        history keeps it), the execution's checkpoint dropped, the autopilot
        off -- then the discussor is asked for the design again, from the
        brief already approved. Returns what was said for the user."""
        import shutil
        import sqlite3
        from datetime import datetime

        from research_agent.execute import jobs

        with self._lock:
            if self.stage != "execution":
                raise ValueError("The run is in the design stage already.")
            if self.busy:
                raise Busy("The team is working: pause or stop it first.")
            for job in jobs.records(self.folder):
                if jobs.status(job)["state"] == "running":
                    jobs.kill(self.folder, job["id"])
            st = S.Store(self.folder).read()
            steps = (st.get("execution") or {}).get("steps") or []
            done = ", ".join(f"step {r['id']} {r['status']}" for r in steps
                             if r.get("status") != "pending") or "no step had run"
            artifacts = self.folder / "artifacts"
            kept = artifacts / f"_before_redesign-{datetime.now():%Y%m%d-%H%M%S}"
            for folder in sorted(artifacts.glob("step_*")):
                kept.mkdir(parents=True, exist_ok=True)
                shutil.move(str(folder), str(kept / folder.name))
            ops = [{"op": "set", "section": "execution", "value": {}},
                   {"op": "merge", "section": "status", "value": {
                       "phase": "discussing", "next": "design again",
                       "summary": f"Back to the design stage from the execution ({done}); "
                                  f"its outputs are kept in {kept}."}},
                   {"op": "add", "section": "decisions", "value": {
                       "what": "Back to the design stage, to see the design made again",
                       "why": f"the execution so far: {done}", "by": "user"}}]
            new, changed = S.apply(st, ops, by="user")
            S.Store(self.folder).write(new, by="user", ops=ops, changed=changed)
            self.session.close()
            with sqlite3.connect(self.folder / "checkpoint.sqlite") as db:
                for table in ("checkpoints", "writes"):
                    db.execute(f"DELETE FROM {table} WHERE thread_id = 'execute'")
            inbox.take(self.folder)
            ap = autopilot.load(self.folder)
            if ap.on:       # the point is for the user to see the design
                autopilot.save(self.folder, replace(ap, on=False))
            self.session, self.stage = self._open_bootstrap(), "bootstrap"
            self._refresh_status()
            text = ("I went back to the design stage. Have the designer make the design again "
                    "from the approved brief, and show it to me before anything runs.")
            self.bus.emit("note", text=f"Back to the design stage: {done}. The steps' outputs "
                                       f"are kept in {kept.relative_to(self.folder)}"
                                       + ("; the autopilot is off." if ap.on else "."))
            self.bus.emit("chat", role="user", text=text, kind="answer")
            if self._status.get("waiting"):
                self._run(lambda on: self.session.answer(text, on_step=on))
            elif self._status.get("stopped_midway"):
                # The design stage stopped partway: the request waits in the
                # inbox for its orchestrator, and it carries on.
                inbox.post(self.folder, "request", text=text)
                self.resume()
            else:
                inbox.post(self.folder, "request", text=text)
                self.status_event()
            return text

    def execute(self, *, limits: dict | None = None, roles=()) -> None:
        """Start the execution stage of an approved run."""
        with self._lock:
            if self.stage != "bootstrap":
                raise ValueError("The execution has already started.")
            if self.busy:
                raise Busy("The bootstrap is working.")
            if not S.approved(S.Store(self.folder).read(), "design"):
                raise ValueError("The design is not approved yet.")
            session = self._open_execution(limits, roles=[*self.roles, *roles])
            self.session.close()
            self.session, self.stage = session, "execution"
            self._refresh_status()
            self.bus.emit("note", text="Execution started.")
            self._run(lambda on: self.session.start(on_step=on))

    # ---- one node: what it is and runs on -----------------------------------------------

    def node_info(self, node: str) -> dict:
        """A graph node's role, the model it runs on, its skills and access,
        its standing instructions, and (execution) its part of the design."""
        from research_agent.bootstrap import prompts as boot_prompts
        from research_agent.execute import prompts as exec_prompts

        st = S.Store(self.folder).read()
        boot, exe = boot_config.load(self.folder), exec_config.load(self.folder)
        out = {"node": node, "stage": self.stage, "role": "", "group": "", "steps": [],
               "skills": [], "access": "", "design": "", "instructions": "", "runs_on": {}}

        def runs_on(cfg):
            return {"provider": cfg.provider, "model": cfg.model or "(the provider's default)",
                    "effort": cfg.effort or "(default)", "options": cfg.options}

        def skills_word(choice):
            return [] if choice is None else [choice] if isinstance(choice, str) else list(choice)

        out["slots"] = self.skill_slots(node, boot, exe, st)
        if node == "discussor":
            out.update(role="discussor", group="bootstrap", runs_on=runs_on(boot.roles["discussor"]),
                       skills=skills_word(boot.skills.get("discussor")),
                       instructions=(exec_prompts if self.stage == "execution"
                                     else boot_prompts).instructions(self.folder)["discussor"])
            return out
        if self.stage == "bootstrap" and node in boot_config.ROLES:
            keys = [node]
            out.update(role=node, group="bootstrap", runs_on=runs_on(boot.roles[node]),
                       skills=[s for k in keys for s in skills_word(boot.skills.get(k))],
                       instructions=boot_prompts.instructions(self.folder)[node],
                       access="write" if node == "researcher" else "read_only")
            return out
        design = st.get("design") or {}
        by_role = {n["role"]: n for n in design.get("nodes") or []}
        found = None
        if node in ("orchestrator", "validator"):
            found = by_role.get(node)
            role = node
        elif node == "reviewer":
            out.update(role="reviewer", group="execution", runs_on=runs_on(exe.roles["reviewer"]),
                       skills=list(exe.reviewer_skills), access="read_only",
                       instructions=exec_prompts.instructions(self.folder)["reviewer"])
            return out
        else:
            found = next((n for n in design.get("nodes") or []
                          if n["name"] == node or f"node_{n['name']}" == node), None)
            role = ("worker" if found and found["role"] == "orchestrator"
                    else exec_nodes.role_of(found or {}))
        if found is None or role not in exe.roles:
            raise ValueError(f"No node {node!r} in this run's graph.")
        access = ("read_only" if role in ("orchestrator", "validator") and role == found["role"]
                  else exec_nodes.access_for(found))
        out.update(role=role, group="execution", runs_on=runs_on(exe.roles[role]),
                   steps=list(found.get("steps") or []) if role != "orchestrator" else [],
                   skills=list(found.get("skills") or []), access=access,
                   design=f"{found['name']} ({found['role']}): {found.get('instructions', '')}",
                   instructions=exec_prompts.instructions(self.folder)[role])
        return out

    # ---- which skills a node is given ---------------------------------------------------

    def skill_slots(self, node: str, boot=None, exe=None, st=None) -> list[dict]:
        """Where a node's skills are chosen. Each slot: key, label, the file it is kept in, the choice now
        ({mode: none|auto|chosen, skills}) and whether it is the design's."""
        boot = boot or boot_config.load(self.folder)
        exe = exe or exec_config.load(self.folder)
        st = st or S.Store(self.folder).read()

        def mode(choice):
            if choice is None:
                return {"mode": "none", "skills": []}
            if choice == "auto":
                return {"mode": "auto", "skills": []}
            return {"mode": "chosen", "skills": list(choice)}

        if node == "discussor" or (self.stage == "bootstrap" and node in boot_config.ROLES):
            keys = [node]
            return [{"key": k, "label": k.replace("_", " "), "group": "bootstrap",
                     "file": "config.json", **mode(boot.skills.get(k)), "from_design": False}
                    for k in keys]
        design = st.get("design") or {}
        if node == "reviewer":
            base = list(exe.reviewer_skills)
        elif node in ("orchestrator", "validator"):
            base = next((list(n.get("skills") or []) for n in design.get("nodes") or []
                         if n["role"] == node), [])
        else:
            base = next((list(n.get("skills") or []) for n in design.get("nodes") or []
                         if n["name"] == node or f"node_{n['name']}" == node), None)
            if base is None:
                return []
        chosen = exe.node_skills.get(node, "design")
        now = mode(base if chosen == "design" else chosen)
        return [{"key": node, "label": node, "group": "execution", "file": "execute.json",
                 **now, "from_design": chosen == "design", "design_skills": base}]

    def catalogue(self) -> list[dict]:
        folder = self.session.run.skills_dir
        if not (folder and Path(folder).is_dir()):
            return []
        return [{"name": s.name, "description": s.description}
                for s in RB.list_skills(folder)]

    def set_skills(self, node: str, key: str, mode: str, skills: list[str]) -> dict:
        """Choose a node's skills: none, auto (every skill's name and
        description; the model reads what it needs) or the chosen ones.
        mode "design" puts an execution node back on the design's. Used from
        the node's next call -- a conversation under way gets them with its
        next message."""
        slot = next((s for s in self.skill_slots(node) if s["key"] == key), None)
        if slot is None:
            raise ValueError(f"No skills to choose for {node!r} ({key!r}).")
        known = {s["name"] for s in self.catalogue()}
        unknown = [s for s in skills if s not in known]
        if unknown:
            raise ValueError(f"No skill called {', '.join(unknown)} in the skills folder.")
        if mode not in ("none", "auto", "chosen", "design"):
            raise ValueError("The mode is none, auto, chosen or design.")
        if mode == "chosen" and not skills:
            raise ValueError("Choose at least one skill, or pick none.")
        choice = None if mode == "none" else "auto" if mode == "auto" else list(skills)
        if slot["group"] == "bootstrap":
            if mode == "design":
                raise ValueError("Bootstrap nodes have no design to go back to.")
            cfg = boot_config.load(self.folder)
            cfg = replace(cfg, skills={**cfg.skills, key: choice})
            boot_config.save(self.folder, cfg)
            if self.stage == "bootstrap" or key == "discussor":
                self.session.run.skills = {**(self.session.run.skills or {}), key: choice}
        else:
            cfg = exec_config.load(self.folder)
            nodes = dict(cfg.node_skills)
            if mode == "design":
                nodes.pop(key, None)
            else:
                nodes[key] = choice
            cfg = replace(cfg, node_skills=nodes)
            exec_config.save(self.folder, cfg)
            if self.stage == "execution":
                self.session.run.limits = replace(self.session.run.limits, node_skills=nodes)
        word = {"none": "no skills", "auto": "auto (every skill listed)",
                "design": "the design's skills"}.get(mode, ", ".join(skills))
        self.bus.emit("note", text=f"{node} ({slot['label']}) now gets {word} (from its next call).")
        return next(s for s in self.skill_slots(node) if s["key"] == key)

    # ---- which model each role runs on ------------------------------------------------

    def models(self) -> dict:
        """The bootstrap's roles (with the discussor) and the execution's, as
        the run's config.json and execute.json have them."""
        boot = boot_config.load(self.folder)
        exe = exec_config.load(self.folder)

        def show(roles, group):
            # "now": what the role's next call runs on -- a backup while its
            # model is out of credits.
            run = self.session.run if self.session and group == self.stage else None
            return {r: {"provider": c.provider, "model": c.model or "", "effort": c.effort or "",
                        "backup": list(c.backup),
                        "now": str(run.current(r)) if run and r in run.roles else str(c)}
                    for r, c in roles.items()}
        return {"stage": self.stage, "groups": [
            {"name": "bootstrap", "file": "config.json", "roles": show(boot.roles, "bootstrap")},
            {"name": "execution", "file": "execute.json", "roles": show(exe.roles, "execution")}]}

    def research_sources(self) -> dict:
        """The literature sources the research skill may search in this run,
        and every source there is: what it covers, and whether its key is set
        (in this server's environment, which the researcher inherits)."""
        chosen = list(boot_config.load(self.folder).sources)
        return {"sources": chosen, "file": "config.json", "available": [
            {"name": name, "label": label, "about": about, "key": key,
             "keyed": not key or bool(os.environ.get(key)
                                      or (name == "s2" and os.environ.get("S2_API_KEY")))}
            for name, label, about, key in boot_config.SOURCES]}

    def set_research_sources(self, sources: list[str]) -> dict:
        """Choose the sources: saved in config.json and the run folder's
        .research_sources, so the researcher's next command uses them."""
        known = [name for name, *_ in boot_config.SOURCES]
        wanted = [s for s in known if s in set(sources)]
        unknown = sorted(set(sources) - set(known))
        if unknown:
            raise ValueError(f"No source {', '.join(unknown)}; they are {', '.join(known)}.")
        if not wanted:
            raise ValueError("Choose at least one source.")
        cfg = boot_config.load(self.folder)
        boot_config.save(self.folder, replace(cfg, sources=tuple(wanted)))
        self.bus.emit("note", text=f"Literature sources for the research: {', '.join(wanted)} "
                                   f"(from the researcher's next search).")
        return self.research_sources()

    def set_model(self, group: str, role: str, provider: str, model: str = "",
                  effort: str = "", backup: list[str] | None = None) -> dict:
        """Change one role's provider, model or effort: saved to the run's
        config file, and used from the next call. A discussor moved to another
        provider hands its conversation over (research_agent/chat.py)."""
        def changed(old: RoleConfig | None) -> RoleConfig:
            options = dict(old.options) if old else {}
            if old and old.provider != provider:
                options = {k: v for k, v in options.items() if k in ("timeout", "max_seconds")}
            kept = tuple(old.backup) if old and backup is None else tuple(
                x.strip() for x in backup or () if x.strip())
            return RoleConfig(provider, model or None, effort or None, options, kept)

        if provider not in RB.list_providers():
            raise ValueError(f"No provider {provider!r}; they are {', '.join(RB.list_providers())}.")
        for text in backup or ():
            if text.strip() and boot_config.spec_of(text).provider not in RB.list_providers():
                raise ValueError(f"Backup {text!r}: no provider "
                                 f"{boot_config.spec_of(text).provider!r}.")
            if (text.strip() and role in FILE_ROLES.get(group, ())
                    and not _reaches_files(boot_config.spec_of(text).provider)):
                raise ValueError(f"Backup {text!r}: {boot_config.spec_of(text).provider} has no "
                                 f"files or commands, and the {role} needs them.")
        if role in FILE_ROLES.get(group, ()) and not _reaches_files(provider):
            raise ValueError(f"{provider} has no files or commands, and the {role} needs them; "
                             f"give it to a provider that has (codex_strata runs the same "
                             f"model with Codex's tools).")
        if group == "bootstrap":
            if role not in boot_config.ROLES:
                raise ValueError(f"No bootstrap role {role!r}; they are {', '.join(boot_config.ROLES)}.")
            cfg = boot_config.load(self.folder)
            spec = changed(cfg.roles.get(role))
            boot_config.save(self.folder, replace(cfg, roles={**cfg.roles, role: spec}))
            live = self.stage == "bootstrap" or role == "discussor"
        elif group == "execution":
            if role not in exec_config.ROLES:
                raise ValueError(f"No execution role {role!r}; they are {', '.join(exec_config.ROLES)}.")
            cfg = exec_config.load(self.folder)
            spec = changed(cfg.roles.get(role))
            exec_config.save(self.folder, replace(cfg, roles={**cfg.roles, role: spec}))
            live = self.stage == "execution"
        else:
            raise ValueError("The group is bootstrap or execution.")
        if live:
            self.session.run.roles[role] = spec
            if self.busy and any(c.get("notice") for c in self.live.values()):
                # A call is waiting (out of credits, a full model): the new
                # model takes over now, not after the wait.
                self._switching = f"{role} now runs on {spec}"
                self._pause.set()
                self.bus.emit("note", text=f"{group} {role} now runs on {spec}: ending the wait, "
                                           f"then carrying on with it.")
                self.status_event()
                return {"role": role, "group": group, "now": str(spec)}
        then = f", then {', '.join(spec.backup)}" if spec.backup else ""
        self.bus.emit("note", text=f"{group} {role} now runs on {spec}{then} (from its next call).")
        self.status_event()
        return {"role": role, "group": group, "now": str(spec)}

    # ---- running ----------------------------------------------------------------------

    def _need_waiting(self) -> None:
        if self.busy:
            raise Busy("The team is working; this waits until it stops for you.")
        if not self._status.get("waiting"):
            raise ValueError("The run is not waiting for you.")

    def _run(self, drive: Callable) -> None:
        if self.busy:
            raise Busy("The run is already working.")
        self._pause.clear()
        self.error = ""

        def work():
            try:
                drive(self._on_step)
            except _Paused:
                self.bus.emit("note", text="Paused. Carry on when you are ready.")
            except RB.BackendCancelled:
                if not self._switching:
                    self.bus.emit("note", text="Paused while waiting. Change the role's model "
                                               "or give it a backup (Run tab), then Carry on.")
            except RB.BackendError as exc:
                self.error = str(exc)
                self.bus.emit("error", message=str(exc)[:3000])
            except Exception as exc:                     # shown, never swallowed
                self.error = f"{type(exc).__name__}: {exc}"
                self.bus.emit("error", message=self.error,
                              detail="".join(traceback.format_exc(limit=8)))
            finally:
                try:
                    self._refresh_status()
                finally:
                    self.current = ""
                    self.status_event()
                if self._switching:
                    # A role's model changed while its call waited: carry on
                    # with the new one, once this thread has ended.
                    me, why = threading.current_thread(), self._switching
                    self._switching = ""
                    threading.Thread(target=lambda: (me.join(), self._carry_on(why)),
                                     daemon=True).start()
                elif autopilot.load(self.folder).on and not self._pause.is_set():
                    # The bootstrap is done: the autopilot starts the
                    # execution, once this thread has ended.
                    me = threading.current_thread()
                    threading.Thread(target=lambda: (me.join(), self._autopilot_go_on()),
                                     daemon=True).start()

        self._worker = threading.Thread(target=work, daemon=True, name=f"run-{self.id}")
        self._worker.start()
        self.status_event()

    def _on_step(self, node: str, update: dict) -> None:
        if node == "human" and str(update.get("user", "")).startswith(autopilot.MARK):
            self.bus.emit("chat", role="user", text=update["user"], kind="autopilot")
            return
        if node == "discussor" and update.get("reply"):
            self.bus.emit("chat", role="discussor", text=update["reply"], kind="answer")
        else:
            self.bus.emit("step", node=node, text=_summary(node, update))
        research = update.get("research")
        if isinstance(research, dict):
            self._state_event(research)
        if self._pause.is_set():
            raise _Paused

    def _aside(self, text: str) -> None:
        self._asides += 1
        activity = (f"Working now: {self.current}." if self.current else "") + (
            f" The last run stopped with an error: {self.error}" if self.error else "")

        def answer():
            try:
                if self.stage == "execution":
                    out = self.session.aside(text)
                    noted = ([f"breakpoint {'on' if b.on else 'off'} {b.when} step {b.step}"
                              for b in out.breakpoints] + [f"request: {r}" for r in out.requests]
                             + (["stop"] if out.stop else []))
                else:
                    out = self.session.aside(text, activity=activity.strip())
                    noted = [f"request: {r.detail}" for r in out.requests]
                self.bus.emit("chat", role="discussor", text=out.reply, kind="aside", noted=noted)
            except Exception as exc:
                self.bus.emit("chat", role="discussor", kind="aside",
                              text=f"(The discussor could not answer: {str(exc)[:500]})")
            finally:
                self._asides -= 1
                self.status_event()

        threading.Thread(target=answer, daemon=True, name=f"aside-{self.id}").start()
        self.status_event()

    def _feed(self, kind: str, data: dict) -> None:
        if kind == "live":
            call = self.live.get(data.get("session") or "")
            snap = data.get("snapshot") or {}
            if call is not None and snap.get("notice"):
                # The call is waiting (credits, a full model): say so where
                # the person looks -- the chat, the status line, the node.
                call["notice"], call["until"] = snap["notice"], snap.get("until")
                self.current = f"{call['label']} ({call['model']}) -- waiting: {snap['notice'][:160]}"
                self.bus.emit("note", text=f"{call['label']}: {snap['notice']}")
                self.status_event()
            elif call is not None:
                call["snapshot"], call["updated"] = snap, time.time()
                call.pop("notice", None)
            return
        if kind == "call":
            session = data.get("session") or ""
            prompt = data.pop("prompt", "")       # kept here, not sent down the stream
            if data.get("state") == "start":
                self.current = f"{data['label']} ({data['model']})"
                self.live[session] = {"label": data["label"], "node": node_of(data["label"]),
                                      "session": session, "attempt": data.get("attempt", 1),
                                      "model": data.get("model", ""), "started": time.time(),
                                      "updated": time.time(), "snapshot": {}, "prompt": prompt}
            else:
                self.live.pop(session, None)
            self.bus.emit("call", **data, node=node_of(data.get("label", "")))
        else:
            self.bus.emit("note", **data)

    def running(self, node: str = "") -> list[dict]:
        """The calls under way (of one graph node), with their newest
        snapshot: the last events and the tails of what is being written."""
        out = []
        for call in list(self.live.values()):
            if node and call["node"] != node:
                continue
            snap = call.get("snapshot") or {}
            events = snap.get("events") or []
            out.append({**{k: v for k, v in call.items() if k != "snapshot"},
                        "elapsed": round(time.time() - call["started"], 1),
                        "idle": snap.get("idle"), "counts": snap.get("counts") or {},
                        "events_total": len(events), "events": events[-LIVE_EVENTS:],
                        "live": snap.get("live") or {}, "last": snap.get("last", "")})
        return out

    # ---- the research state, watched --------------------------------------------------

    def _state_event(self, st: dict) -> None:
        versions = st.get("versions") or {}
        if versions != self._versions:
            self._versions = dict(versions)
            self.bus.emit("state", versions=versions,
                          phase=(st.get("status") or {}).get("phase", ""))

    def _watch(self) -> None:
        """Changes the graph's steps do not report: an aside's, another
        process's (the CLI), a hand edit."""
        path = self.folder / "state" / "research.json"
        seen = path.stat().st_mtime_ns if path.exists() else 0
        while not self._closed.wait(WATCH_SECONDS):
            try:
                now = path.stat().st_mtime_ns if path.exists() else 0
                if now != seen:
                    seen = now
                    self._state_event(json.loads(path.read_text()))
            except (OSError, ValueError):
                continue

    def close(self) -> None:
        if self.busy:
            raise Busy("The run is working; pause or stop it first.")
        self._closed.set()
        self.session.close()


# ---- reading a run from its folder ------------------------------------------------------


NEXT_WORDS = {"designer": "the designer was about to design",
              "researcher": "the researcher was about to search",
              "orchestrator": "the orchestrator was about to decide",
              "discussor": "the discussor was about to answer", "human": "it waited for you"}


def describe_point(p: dict) -> str:
    """A rewind point in words: the brief and design as they were, what came next."""
    def part(name: str) -> str:
        v = (p.get("versions") or {}).get(name)
        if not v:
            return f"no {name}"
        approved = (p.get("approvals") or {}).get(name) == v
        return f"{name} v{v}{' approved' if approved else ''}"
    nxt = "; ".join(NEXT_WORDS.get(n, n) for n in p.get("next") or [])
    return f"{part('brief')}, {part('design')}" + (f" -- {nxt}" if nxt else "")


def stage_of(st: dict) -> str:
    """"execution" once it has started -- its step table is in the state."""
    return "execution" if (st.get("execution") or {}).get("steps") else "bootstrap"


def node_of(label: str) -> str:
    """The graph node a call belongs to, from its label: "researcher",
    "designer", "step 5 (trainer)", "validate step 5",
    "discussor (aside)", "reviewer"."""
    owner = re.fullmatch(r"step \S+ \((.+)\)", label or "")
    if owner:
        return owner.group(1)
    if (label or "").startswith("validate"):
        return "validator"
    return (label or "").split(" ")[0]


def activity(folder: Path, node: str = "", limit: int = 40) -> list[dict]:
    """The calls a node made, newest first, every event of each -- from
    <run>/activity/<session>.jsonl -- with a line on what went in and what
    came out (`said`, `outcome`, `to`), and the tokens, for the call list."""
    out = []
    for path in (Path(folder) / "activity").glob("*.jsonl"):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            rec["node"] = node_of(rec.get("label", ""))
            if not node or rec["node"] == node:
                out.append(rec)
    out.sort(key=lambda r: r.get("at") or "", reverse=True)
    out = out[:limit]
    turns: dict[str, list] = {}
    for rec in out:
        sid = rec.get("session") or ""
        if sid not in turns:
            try:
                turns[sid] = json.loads((Path(folder) / "rb" / "sessions" / sid / "session.json")
                                        .read_text()).get("turns") or []
            except (OSError, ValueError):
                turns[sid] = []
        turn = next((t for t in turns[sid] if t.get("turn") == rec.get("turn")), {})
        # The session has the whole answer; the activity line keeps 20,000 characters.
        rec.update(previews(turn.get("prompt") or "", turn.get("reply") or rec.get("reply") or ""),
                   tokens=turn.get("tokens"))
        if rec.get("problems"):
            rec["outcome"] = _line("not usable: " + "; ".join(rec["problems"]))
        elif not rec["outcome"] and turn.get("error"):
            rec["outcome"] = _line("failed: " + turn["error"])
    return out


_SECTION = re.compile(r"^=== (.+?) ===$", re.M)


def sections(prompt: str) -> list[tuple[str, str]]:
    """A brief's `=== TITLE ===` sections: (title, text); text before the
    first is titled ""."""
    found = list(_SECTION.finditer(prompt))
    out = [("", prompt[:found[0].start()].strip())] if found else [("", prompt.strip())]
    for i, m in enumerate(found):
        end = found[i + 1].start() if i + 1 < len(found) else len(prompt)
        out.append((m.group(1), prompt[m.end():end].strip()))
    return [(t, x) for t, x in out if t or x]


def _line(text: str, n: int = 160) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= n else flat[:n - 1].rstrip() + "…"


def previews(prompt: str, reply: str) -> dict:
    """What went in and what came out of one call, a line each, and where
    it handed to: for a list of calls that tells them apart."""
    said = ""
    if prompt.startswith("Your answer could not be used:"):
        said = "retry: " + prompt.split("\n", 2)[1].lstrip("- ") if "\n" in prompt else "retry"
    else:
        parts = dict(sections(prompt))
        aside = any(t.startswith("AN ASIDE") for t in parts)
        for title in ("THE USER SAYS", "THE LAST PIECE OF WORK", "TASK", "YOUR TASK", "THE STEP",
                      "NOW"):
            if parts.get(title):
                said = ("you (aside): " if aside else "you: " if title == "THE USER SAYS"
                        else "") + parts[title]
                break
        else:
            said = next((x for t, x in sections(prompt) if x), "")
    try:
        data = json.loads(reply)
    except ValueError:
        data = None
    to, outcome = "", ""
    if isinstance(data, dict):
        if data.get("reply"):
            outcome, to = data["reply"], data.get("hand_to") or ""
        elif data.get("action"):
            to = data["action"] + (f" {data['step']}" if data.get("step") else "")
            outcome = data.get("reason") or data.get("task") or ""
        elif data.get("verdict"):
            to, outcome = data["verdict"], data.get("summary") or data.get("reason") or ""
        elif data.get("state") and data.get("summary"):
            to, outcome = data["state"], data["summary"]
        else:
            outcome = next((v for v in data.values() if isinstance(v, str) and v.strip()), "") \
                or ", ".join(f"{len(v)} {k.replace('_', ' ')}" for k, v in data.items()
                             if isinstance(v, list) and v)
    else:
        outcome = reply
    return {"said": _line(said), "outcome": _line(outcome), "to": _line(to, 40)}


def _summary(node: str, update: dict) -> str:
    d = update.get("decision") or {}
    if node == "orchestrator" and d:
        step = f" {d.get('step')}" if d.get("step") else ""
        return f"orchestrator -> {d.get('action')}{step}: {d.get('reason', '')}"
    if update.get("last"):
        return str(update["last"]).splitlines()[0]
    if node == "human":
        return "the user answered"
    return node


def transcript(folder: Path) -> list[dict]:
    """The conversation with the discussor, across its sessions -- whoever
    drove it (the CLI or this UI)."""
    return chat.transcript(folder)


def run_summary(folder: Path, hosts: dict) -> dict | None:
    path = folder / "state" / "research.json"
    if not path.exists():
        return None
    try:
        st = json.loads(path.read_text())
    except ValueError:
        return None
    host = hosts.get(folder.name)
    return {"id": folder.name, "folder": str(folder),
            "goal": (st.get("goal") or {}).get("question", ""),
            "phase": (st.get("status") or {}).get("phase") or "discussing",
            "stage": stage_of(st),
            "updated": path.stat().st_mtime, "open": host is not None,
            "busy": bool(host and host.busy)}


#: Roles that read or change files, or run commands: a provider without them
#: (strata, a plain Responses API) cannot be one of them.
FILE_ROLES = {"bootstrap": ("researcher",),
              "execution": ("worker", "researcher", "writer", "validator", "reviewer")}


def _reaches_files(provider: str) -> bool:
    from research_backend.backends import load
    from research_backend.base import Access

    return load(provider).max_access != Access.NONE
