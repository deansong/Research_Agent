"""
WHAT:  The execution graph, built from the approved design: the discussor and
       the user, the orchestrator, one node per step owner in the design, a
       `wait` node for running jobs, and the validator.
WHY:   The orchestrator LLM chooses what runs next; code holds what it may
       choose (dependencies passed, attempts left, the user's go before a
       gated step, the budget), and only the validator's verdict passes a
       step.
CONCEPT: LangGraph, as the bootstrap, in the same checkpoint file under its
       own thread.

    START -> orchestrator --run/retry S--> dispatch --ready--> <owner of S> --done--> validator -> orchestrator
                                                    --missing--> orchestrator (twice: the user)
                                                         --launched--> wait --jobs done--> <owner of S>
                                                         --failed/blocked--> orchestrator
                          --skip S--> orchestrator (the terminal record written)
                          --ask_user / a gated step or breakpoint / stop / limit--> discussor -> human
                          --done--> reviewer --> discussor (the review goes to the next run)
    validator --a breakpoint after the step--> discussor
    human --> discussor --(go, or a request)--> orchestrator, else --> human
          --> orchestrator on /go; END on /exit
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from typing_extensions import TypedDict

import research_backend as RB

from research_agent import state as S
from research_agent import autopilot, inbox
from research_agent.execute import jobs, nodes


class ExecState(TypedDict, total=False):
    research: dict
    seen: dict
    """The section versions the discussor last saw."""
    pending: list
    """Requests from the discussor, waiting for the orchestrator."""
    decision: dict
    last: str
    """What the last piece of work did -- for the orchestrator."""
    user: str
    note: str
    reply: str
    decisions: int
    """Orchestrator decisions since the user last spoke."""
    awaiting: str
    """The step waiting for the user's go ("5"), or the breakpoint after a
    step the work is paused at ("after:4")."""
    step: str
    """The step being worked on."""
    report: dict
    """Its owner's last report."""
    wake: list
    """Job ids its owner is woken about."""
    nudged: bool
    """The user said go or stop with this message."""
    blocked: dict
    """Steps the dispatch would not start, and how many times in a row."""
    outcome: str
    stopped_by: str
    """"orchestrator" when it chose to stop: a question the autopilot may
    answer (pass, retry, amend). A stop by the user, the autopilot or a
    budget leaves it empty: final."""
    """"" while working; "done", "stopped" or "exit"."""


class _Routed(ExecState, total=False):
    _to: str


def budget(run: nodes.ExecRun, st: dict) -> tuple[str, str]:
    """(what is used and left, why it is over -- "" if it is not). The limits
    are the configuration's now, so raising them and resuming goes on."""
    started = (st.get("execution") or {}).get("started")
    hours = ((datetime.now(timezone.utc) - datetime.fromisoformat(started)).total_seconds() / 3600
             if started else 0.0)
    gpu = jobs.gpu_hours(run.folder)
    parts, over = [], ""
    for used, limit, label in ((hours, run.limits.wall_hours, "wall hours"),
                               (gpu, run.limits.gpu_hours, "GPU-hours")):
        parts.append(f"{used:.2f} {label} used" + (f" of {limit:g} ({limit - used:.2f} left)"
                                                   if limit else " (no limit)"))
        if limit and used >= limit and not over:
            over = f"the limit of {limit:g} {label} is used up ({used:.2f})"
    running = [j for j in jobs.records(run.folder) if jobs.status(j)["state"] == "running"]
    if running:
        parts.append(f"{len(running)} jobs running")
    return "; ".join(parts), over


def build(run: nodes.ExecRun, design: nodes.Design, checkpointer=None):
    store = S.Store(run.folder)
    limits = run.limits

    def commit(st: dict, ops: list[dict], by: str, session: str = "") -> dict:
        if not ops:
            return st
        new, changed = S.apply(st, ops, by=by)
        store.write(new, by=by, ops=ops, changed=changed, session=session)
        return new

    def go_ops(st: dict, awaiting: str) -> list[dict]:
        """The user's go: on the step's record, and as their decision -- which
        the discussor sees, even when /go did not pass through it."""
        after = awaiting.startswith("after:")
        step_id = awaiting.split(":", 1)[1] if after else awaiting
        step = nodes.plan_step(st, step_id) or {}
        record = (nodes.record_op(st, step_id, pause_after=False) if after else
                  nodes.record_op(st, step_id, go=True, pause_before=False))
        what = (f"Go on after step {step_id}" if after else
                f"Go for step {step_id}: {step.get('title', '')}".strip())
        return [record, {"op": "add", "section": "decisions", "value": {
            "what": what, "why": "the user said go", "by": "user"}}]

    def drain(st: dict) -> tuple[dict, list[str], str]:
        """What the user said while the team worked: (state, requests, why to
        stop -- "" if not)."""
        items = inbox.take(run.folder)
        steps = {s["id"] for s in (st.get("plan") or {}).get("steps") or []}
        ops, requests, stop = [], [], ""
        for item in items:
            if item["kind"] == "breakpoint" and item.get("step") in steps:
                ops.append(nodes.record_op(st, item["step"],
                                           **{f"pause_{item['when']}": item.get("on", True)}))
                st = commit(st, ops[-1:], "user")
            elif item["kind"] == "decision":
                st = commit(st, [{"op": "add", "section": "decisions", "value": {
                    "what": item["what"], "why": item.get("why", ""), "by": "user"}}], "user")
            elif item["kind"] == "request":
                requests.append(item["text"])
            elif item["kind"] == "stop":
                stop = item.get("why") or "the user stopped the execution"
            elif item["kind"] == "rewind" and item.get("step") in steps:
                if not nodes.rewind_problems(run.folder, st, item["step"]):
                    st = commit(st, nodes.rewind(run.folder, st, item["step"]), "user")
            elif item["kind"] == "go" and item.get("step") in steps:
                # A go given before the step waits for it (measured: run 1134's
                # "go" was an aside, and step 1 then waited for another).
                st = commit(st, [nodes.record_op(st, item["step"], go=True)], "user")
            elif item["kind"] == "skip" and item.get("step") in steps:
                st = commit(st, nodes.user_skip_ops(run.folder, st, [item]), "user")
            elif item["kind"] in ("retry", "waive") and item.get("step") in steps:
                retry = [item["step"]] if item["kind"] == "retry" else []
                waive = [{"step": item["step"], "why": item.get("why", "")}] \
                    if item["kind"] == "waive" else []
                rec = nodes.records(st).get(item["step"], {})
                if rec.get("status") in (nodes.REOPENABLE if retry else ("fail", "stopped")):
                    st = commit(st, nodes.user_step_ops(st, retry, waive), "user")
        return st, requests, stop

    def stop_all(st: dict, why: str) -> dict:
        """Stop the jobs and every step under way."""
        for job in jobs.records(run.folder):
            jobs.kill(run.folder, job["id"])
        for sid, r in nodes.records(st).items():
            if r["status"] in ("running", "waiting", "validating"):
                st = commit(st, [nodes.record_op(st, sid, status="stopped",
                                                 problems=r.get("problems", []) + [why])], "execution")
        return commit(st, [{"op": "merge", "section": "status", "value": {
            "phase": "stopped", "next": "the user decides"}}], "execution")

    # ---- the discussor and the user -------------------------------------------------

    def discussor(state: ExecState) -> dict:
        st = state["research"]
        turn = nodes.discussor(run, st, seen=state.get("seen") or {}, user=state.get("user", ""),
                               note=state.get("note", ""), awaiting=state.get("awaiting", ""))
        st = commit(st, turn.ops, "discussor")
        spoke = bool(state.get("user"))
        who = "autopilot" if state.get("user", "").startswith(autopilot.MARK) else "user"
        awaiting, outcome = state.get("awaiting", ""), state.get("outcome", "")
        if spoke and turn.go and awaiting:
            st = commit(st, go_ops(st, awaiting), "user")
            awaiting = ""
        if spoke and turn.rewind:
            st = commit(st, nodes.rewind(run.folder, st, turn.rewind), "user")
            outcome = ""
        if spoke and (turn.retry or turn.waive):
            st = commit(st, nodes.user_step_ops(st, turn.retry, turn.waive, by=who), who)
        if spoke and turn.skip and who == "user":
            st = commit(st, nodes.user_skip_ops(run.folder, st, turn.skip), "user")
        if spoke and turn.stop:
            st = stop_all(st, "the user stopped the execution")
            outcome = "stopped"
        # As in the bootstrap: passing on the orchestrator's message, it
        # answers the user; a go or a request moves the work on.
        go = spoke and not turn.stop and (turn.hand_to == "orchestrator" or turn.go
                                          or bool(turn.retry or turn.waive or turn.rewind
                                                   or turn.skip)
                                          or bool(state.get("nudged")))
        return {"research": st, "seen": dict(st.get("versions") or {}), "reply": turn.reply,
                "pending": list(state.get("pending") or []) + turn.requests, "user": "",
                "note": "", "nudged": False, "awaiting": awaiting, "outcome": outcome,
                "decisions": 0 if spoke else state.get("decisions", 0),
                "_to": "orchestrator" if go else "human"}

    def human(state: ExecState) -> dict:
        st = state["research"]
        # Unfinished, or stopped by the orchestrator's choice (a step out of
        # attempts): the autopilot answers. A stop by the user, the
        # autopilot or a budget is final.
        open_ = not state.get("outcome") or (state.get("outcome") == "stopped"
                                             and state.get("stopped_by") == "orchestrator")
        answer = autopilot_answer(state, st) if open_ else None
        if answer is None:
            answer = interrupt({"reply": state.get("reply", ""),
                                "outcome": state.get("outcome", "")})
            if isinstance(answer, str):
                answer = {"text": answer}
            if answer.get("text") or answer.get("go") or answer.get("stop"):
                autopilot.user_answered(run.folder)
        else:
            st = commit(st, answer.pop("ops"), "autopilot")
        if answer.get("exit"):
            return {"outcome": "exit", "_to": END}
        if answer.get("review"):
            return {"note": "", "_to": "reviewer"}
        if answer.get("paper"):
            return {"note": "", "_to": "paper"}
        text = answer.get("text", "")
        awaiting, nudged, outcome = state.get("awaiting", ""), False, state.get("outcome", "")
        if answer.get("work") and not text:
            # The page changed something the team should act on (another
            # attempt, a pass): straight to the orchestrator, as /go does.
            return {"awaiting": awaiting, "decisions": 0, "outcome": "", "_to": "orchestrator"}
        if answer.get("go") and awaiting and not text:
            # /go is a command, not a conversation: the work goes on, and the
            # discussor sees the go in the step table next time it speaks.
            st = commit(st, go_ops(st, awaiting), "user")
            return {"research": st, "awaiting": "", "decisions": 0, "outcome": "",
                    "_to": "orchestrator"}
        if answer.get("go") and awaiting:
            st = commit(st, go_ops(st, awaiting), "user")
            awaiting, nudged = "", True
        if answer.get("retry") or answer.get("waive"):
            # The autopilot's call on a failed step: applied by code, as the
            # user's is -- a retry or a pass said only in words does nothing.
            st = commit(st, nodes.user_step_ops(st, answer.get("retry") or [],
                                                answer.get("waive") or [], by="autopilot"),
                        "autopilot")
            nudged, outcome = True, ""
        if answer.get("stop"):
            st = stop_all(st, "the user stopped the execution")
            text, outcome = text or "(I stopped the execution.)", "stopped"
        return {"research": st, "user": text, "awaiting": awaiting, "nudged": nudged,
                "outcome": outcome, "stopped_by": "", "_to": "discussor"}

    def autopilot_answer(state: ExecState, st: dict) -> dict | None:
        """The user's answer by the autopilot, when it is on (None: ask the
        user)."""
        answer, note = autopilot.should_answer(run.folder)
        if note:
            run.say(note)
        if not answer:
            return None
        decision = state.get("decision") or {}
        if decision.get("action") == "ask_user" and decision.get("step") \
                and nodes.needed_by(st, decision["step"]):
            # Whether to give up a step later steps need -- or raise the limit
            # that stopped it -- is the user's (measured: run 1455-next).
            run.say(autopilot.hand_back(run.folder, (
                f"the team asks about step {decision['step']}, which steps "
                f"{', '.join(nodes.needed_by(st, decision['step']))} need: giving it up, or "
                f"raising a limit for it, is yours to decide")))
            return None
        out = autopilot.decide(run, st, stage="execution", reply=state.get("reply", ""),
                               awaiting=state.get("awaiting", ""))
        if out.hand_back.strip():
            run.say(autopilot.hand_back(run.folder, out.hand_back.strip()))
            return None
        autopilot.counted(run.folder)
        return {**autopilot.answer_for(out), "ops": [autopilot.decision_op(out)]}

    # ---- the orchestrator -------------------------------------------------------------

    def orchestrator(state: ExecState) -> dict:
        st, asked, stop = drain(state["research"])
        pending = list(state.get("pending") or []) + asked
        if stop:
            st = stop_all(st, stop)
            return {"research": st, "note": f"Execution stopped: {stop}. Running jobs were "
                                             f"stopped. Tell the user where things stand.",
                    "outcome": "stopped", "pending": pending, "_to": "discussor",
                    "decision": {"action": "stop", "step": "", "reason": stop}}
        used, over = budget(run, st)
        if over:
            st = stop_all(st, f"stopped: {over}")
            note = (f"Execution stopped: {over}. Running jobs were stopped. Tell the user "
                    f"where things stand; raising the limit (execute.json) and resuming "
                    f"lets it go on.")
            return {"research": st, "note": note, "outcome": "stopped", "_to": "discussor",
                    "decision": {"action": "stop", "step": "", "reason": over}}
        if state.get("decisions", 0) >= limits.max_decisions:
            note = (f"The team has made {limits.max_decisions} decisions since the user last "
                    f"spoke. Tell the user where things stand and ask how to continue.")
            return {"research": st, "note": note, "_to": "discussor", "pending": pending,
                    "decision": {"action": "ask_user", "step": "", "reason": "the limit"}}
        out = nodes.orchestrator(run, st, design, budget=used, requests=pending,
                                 last=state.get("last", ""))
        st = commit(st, out.ops, "orchestrator", out.session)
        d = out.data
        decision = {"action": d.action, "step": d.step, "reason": d.reason, "task": d.task}
        base = {"research": st, "decision": decision, "pending": [], "outcome": "",
                "stopped_by": "", "decisions": state.get("decisions", 0) + 1}
        note = d.note_for_discussor
        if d.action in ("run", "retry"):
            step = nodes.plan_step(st, d.step)
            rec = nodes.records(st)[d.step]
            if (needs_go(step) and not rec.get("go")) or rec.get("pause_before"):
                why = "a breakpoint" if rec.get("pause_before") else "the plan gates it"
                note = (f"Step {d.step} is next ({d.reason}). It waits for the user's go ({why}): "
                        f"say what it will do and cost, and ask. {note}").strip()
                return {**base, "awaiting": d.step, "note": note, "_to": "discussor"}
            return {**base, "step": d.step, "wake": [], "report": {}, "_to": "dispatch"}
        if d.action == "skip":
            folder = run.artifacts(d.step)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "SKIPPED.md").write_text(
                f"# Step {d.step} skipped\n\n{d.task}\n\nDecided by the orchestrator, "
                f"{nodes._now()}: {d.reason}\n")
            st = commit(st, [nodes.record_op(st, d.step, status="skipped", finished=nodes._now(),
                                             summary=f"skipped: {d.task}"[:1500],
                                             artifacts=["SKIPPED.md"])], "execution")
            return {**base, "research": st, "last": f"step {d.step} skipped: {d.task}",
                    "_to": "orchestrator"}
        if d.action == "done":
            st = commit(st, [{"op": "merge", "section": "status", "value": {
                "phase": "done", "next": "the paper"}}], "execution")
            note = note or "Every step has passed or been skipped: the execution is done."
            return {**base, "research": st, "note": note, "outcome": "done", "_to": "paper"}
        if d.action == "stop":
            st = stop_all(st, f"stopped by the orchestrator: {d.task}")
            return {**base, "research": st, "note": f"The orchestrator stopped the execution: "
                                                    f"{d.task} {note}".strip(),
                    "outcome": "stopped", "stopped_by": "orchestrator", "_to": "discussor"}
        return {**base, "note": note or d.task, "_to": "discussor"}    # ask_user

    def needs_go(step: dict) -> bool:
        return nodes.needs_go(step, limits.confirm)

    # ---- the dispatch: the step's inputs checked before it starts --------------------

    def dispatch(state: ExecState) -> dict:
        st, sid, d = state["research"], state["step"], state.get("decision") or {}
        node = design.owners[design.owner_of[sid]]
        try:
            out = nodes.dispatch(run, st, node=node, step=nodes.plan_step(st, sid),
                                 note=d.get("task", ""))
        except RB.BackendCancelled:
            raise
        except RB.BackendError as exc:
            return {"research": st, "_to": "orchestrator",
                    "last": f"step {sid} was not started: its dispatch failed: {str(exc)[:600]}"}
        st = commit(st, out.ops, "orchestrator", out.session)
        got = out.data
        blocked = dict(state.get("blocked") or {})
        if not got.ready:
            blocked[sid] = blocked.get(sid, 0) + 1
            missing = "".join(f"\n- {m}" for m in got.missing)
            last = f"step {sid} was not started: its dispatch found missing:{missing}"
            if blocked[sid] >= 2:
                # Twice in a row: what to do about it is the user's.
                note = (f"Step {sid} cannot start: what it needs is not there.{missing}\n"
                        f"Tell the user, and ask how to go on (redo the earlier work, change "
                        f"the step, or provide it).")
                return {"research": st, "last": last, "blocked": blocked, "note": note,
                        "_to": "discussor",
                        "decision": {"action": "ask_user", "step": sid, "reason": last}}
            return {"research": st, "last": last, "blocked": blocked, "_to": "orchestrator"}
        blocked.pop(sid, None)
        rec = nodes.records(st)[sid]
        attempt = rec.get("attempts", 0) + 1
        st = commit(st, [nodes.record_op(
            st, sid, status="running", attempts=attempt, started=nodes._now(),
            problems=rec.get("problems", []) if d.get("action") == "retry" else [],
            sessions=rec.get("sessions", []) + [nodes._session(sid, attempt)],
            dispatch={"inputs": [i.model_dump() for i in got.inputs],
                      "context": got.context})], "execution")
        return {"research": st, "blocked": blocked, "wake": [], "report": {},
                "_to": design.owner_of[sid]}

    # ---- a step's owner, the jobs, the validator ---------------------------------------

    def owner(name: str):
        node = design.owners[name]

        def run_it(state: ExecState) -> dict:
            st, sid = state["research"], state["step"]
            step = nodes.plan_step(st, sid)
            try:
                out = nodes.owner(run, st, node=node, step=step, wake=state.get("wake") or None,
                                  task=(state.get("decision") or {}).get("task", ""))
            except RB.BackendCancelled:
                raise
            except RB.BackendError as exc:
                why = f"{node['name']} failed: {str(exc)[:600]}"
                st = commit(st, [nodes.record_op(st, sid, status="fail", problems=[why])],
                            "execution")
                return {"research": st, "last": f"step {sid}: {why}", "_to": "orchestrator"}
            st = commit(st, out.ops, node["name"], out.session)
            rep = out.data
            rec = nodes.records(st)[sid]
            fields = {"summary": rep.summary[:1500], "jobs": list(dict.fromkeys(
                rec.get("jobs", []) + list(rep.jobs)))}
            if rep.artifacts:
                fields["artifacts"] = list(rep.artifacts)
            if rep.state == "launched":
                st = commit(st, [nodes.record_op(st, sid, status="waiting", **fields)], "execution")
                return {"research": st, "wake": list(rep.jobs), "_to": "wait"}
            if rep.state == "done" or (rep.state == "failed" and rep.artifacts):
                # Outputs are the validator's to judge, a failure the owner
                # reports included: measured, an owner failed its preflight
                # on a criterion the next two steps deliver, and spent the
                # step's last attempt.
                st = commit(st, [nodes.record_op(st, sid, status="validating", **fields)],
                            "execution")
                unverified = [o["value"]["source"] for o in out.ops
                              if o.get("section") == "evidence"
                              and o["value"].get("verified") is False]
                return {"research": st, "report": {**rep.model_dump(), "unverified": unverified},
                        "wake": [],
                        "_to": "validator"}
            problems = list(rep.problems) + [f"question for the user: {q}"
                                             for q in rep.questions_for_user]
            st = commit(st, [nodes.record_op(st, sid, status="fail", problems=problems[:20],
                                             **fields)], "execution")
            return {"research": st, "wake": [], "_to": "orchestrator",
                    "last": f"step {sid}: its owner reports {rep.state}: {rep.summary}"
                            + "".join(f"\n- {p}" for p in problems)}
        return run_it

    def wait(state: ExecState) -> dict:
        st, sid, ids = state["research"], state["step"], list(state.get("wake") or [])
        run.say(f"  . step {sid}: waiting on jobs {', '.join(ids)} (Ctrl-C leaves them "
                f"running; --resume picks them up)")
        while True:
            stops = inbox.take(run.folder, kinds=("stop",))
            if stops:
                st = stop_all(st, stops[0].get("why") or "the user stopped the execution")
                return {"research": st, "note": f"The user stopped the execution while step {sid} "
                                                f"was running; its jobs were stopped.",
                        "outcome": "stopped", "_to": "discussor"}
            found = [jobs.get(run.folder, i) for i in ids]
            if not any(j and jobs.status(j)["state"] == "running" for j in found):
                break
            _, over = budget(run, st)
            if over:
                st = stop_all(st, f"stopped: {over}")
                note = (f"Execution stopped while step {sid} was running: {over}. Its jobs were "
                        f"stopped. Tell the user; raising the limit and resuming lets it go on.")
                return {"research": st, "note": note, "outcome": "stopped", "_to": "discussor"}
            time.sleep(limits.poll_seconds)
        st = commit(st, [nodes.record_op(st, sid, gpu_hours=round(jobs.gpu_hours(run.folder, sid),
                                                                  3))], "execution")
        return {"research": st, "_to": design.owner_of[sid]}

    def validator(state: ExecState) -> dict:
        # What the user said while the step ran counts now: a breakpoint
        # after this step, or a stop.
        st, asked, stop = drain(state["research"])
        sid = state["step"]
        pending = list(state.get("pending") or []) + asked
        if stop:
            st = stop_all(st, stop)
            return {"research": st, "pending": pending, "outcome": "stopped", "_to": "discussor",
                    "note": f"Execution stopped before step {sid} was checked: {stop}."}
        node = design.owners[design.owner_of[sid]]
        try:
            out = nodes.validator(run, st, design, node=node, step=nodes.plan_step(st, sid),
                                  report=state.get("report") or {})
        except RB.BackendCancelled:
            raise
        except RB.BackendError as exc:
            why = f"the validator could not give a verdict: {str(exc)[:600]}"
            st = commit(st, [nodes.record_op(st, sid, status="fail", problems=[why])], "execution")
            return {"research": st, "last": f"step {sid}: {why}", "_to": "orchestrator"}
        was_approved = S.approved(st, "hypotheses")
        st = commit(st, out.ops, design.validator["name"], out.session)
        if was_approved and not S.approved(st, "hypotheses"):
            # Only their status changed (supported, refuted): a result, not a
            # change to what was approved.
            st = commit(st, [{"op": "approve", "section": "hypotheses"}], "execution")
        st = commit(st, [nodes.record_op(st, sid, gpu_hours=round(
            jobs.gpu_hours(run.folder, sid), 3))], "execution")
        rec = nodes.records(st)[sid]
        last = f"step {sid} verdict -- {rec['verdict']}" + "".join(
            f"\n- {p}" for p in rec.get("problems") or [])
        if rec["status"] == "pass" and rec.get("pause_after"):
            return {"research": st, "last": last, "awaiting": f"after:{sid}", "_to": "discussor",
                    "pending": pending,
                    "note": f"Step {sid} passed and the user asked to stop here (a breakpoint). "
                            f"Show them its results and ask whether to go on."}
        return {"research": st, "last": last, "pending": pending, "_to": "orchestrator"}

    def paper(state: ExecState) -> dict:
        st = state["research"]
        try:
            out = nodes.paper(run, st)
        except RB.BackendCancelled:
            raise
        except RB.BackendError as exc:
            return {"note": f"{state.get('note', '')} The paper could not be written: "
                            f"{str(exc)[:400]}".strip(), "_to": "reviewer"}
        st = commit(st, out.ops, "paper", out.session)
        pdf = (st.get("status") or {}).get("paper", "")
        said = (f"The paper is written: {pdf} (the user reads it with Paper at the top of the "
                f"page)." if pdf else
                f"The paper has no PDF: {'; '.join(out.data.problems) or out.data.summary}")
        return {"research": st, "note": f"{state.get('note', '')} {said}".strip(),
                "_to": "reviewer"}

    def reviewer(state: ExecState) -> dict:
        st = state["research"]
        try:
            out = nodes.reviewer(run, st)
        except RB.BackendCancelled:
            raise
        except RB.BackendError as exc:
            return {"note": f"{state.get('note', '')} The review could not be written: "
                            f"{str(exc)[:400]}".strip(), "_to": "discussor"}
        st = commit(st, out.ops, "reviewer", out.session)
        review = st["reviews"][-1]
        return {"research": st, "_to": "discussor", "note": (
            f"{state.get('note', '')} The reviewer reviewed the study (review {review['id']}, "
            f"{review['file']}): {review.get('decision', '')}, rating {review['rating']}/10, "
            f"confidence {review['confidence']}/5. "
            f"Tell the user its main points, and that the next run starts from it: "
            f"python -m research_agent bootstrap --from {run.folder}").strip()}

    graph = StateGraph(_Routed)
    graph.add_node("discussor", discussor)
    graph.add_node("human", human)
    graph.add_node("orchestrator", orchestrator)
    graph.add_node("dispatch", dispatch)
    for name in design.owners:
        graph.add_node(name, owner(name))
    graph.add_node("wait", wait)
    graph.add_node("validator", validator)
    graph.add_node("paper", paper)
    graph.add_node("reviewer", reviewer)

    owners = list(design.owners)
    graph.add_edge(START, "orchestrator")
    graph.add_conditional_edges("orchestrator", _route,
                                ["discussor", "orchestrator", "paper", "dispatch", *owners])
    graph.add_conditional_edges("dispatch", _route, ["orchestrator", "discussor", *owners])
    graph.add_conditional_edges("discussor", _route, ["orchestrator", "human"])
    graph.add_conditional_edges("human", _route, ["discussor", "orchestrator", "paper", "reviewer",
                                                  END])
    for name in owners:
        graph.add_conditional_edges(name, _route, ["wait", "validator", "orchestrator"])
    graph.add_conditional_edges("wait", _route, ["discussor", *owners])
    graph.add_conditional_edges("validator", _route, ["orchestrator", "discussor"])
    graph.add_conditional_edges("paper", _route, ["reviewer"])
    graph.add_edge("reviewer", "discussor")
    return graph.compile(checkpointer=checkpointer)


def _route(state: _Routed) -> str:
    return state["_to"]

