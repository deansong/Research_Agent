"""
WHAT:  The `orchestrator` node template -- the hub of a hub agent. Each turn
       it reads what just happened and decides what happens next: send a
       node work, send a node's output to the validator, ask the person,
       look something up, or finish.
WHY:   A pipeline agent's checker could only send work back to the step it
       had just checked. A fault found in step 3 that started in step 1 was
       retried in step 3 until a loop guard gave up. Here one mind -- the
       discussor, carried into the run -- sees every step, is told where the
       validator thinks a fault started, and sends the work THERE.
CONCEPT: A LangGraph node like make_agent_node, but its prompt is built by
       code from a step book rather than from a template, and its output
       model is built from the agent's own node names. Routing is still one
       table lookup: it writes route[<its name>] and compile.py's path map
       (every node, and the end) does the rest.

--------------------------------------------------------------------------
CONTEXT: WHAT IT KEEPS, AND WHAT IT IS SENT
--------------------------------------------------------------------------
It keeps a STEP BOOK (state["orchestrator"]["book"]): per node, its status
and -- once validated -- its final report. Not its logs, not its diffs, not
the validator's evidence: those stay with the node that produced them, in
its own conversation and its activity records, and come back only when it
asks for them (`include`, resolved by agent/work/context.py).

Each turn it is sent the event that just happened, in full -- a node's
report, a verdict, a person's answer -- plus the book. The first turn of a
run, or of a fresh conversation, also gets the brief, the plan with its
checks, the roster, its memory (agent/work/memory.py) and the book with
every finished step's report. Budgets come from the model's context window
(backends/models.py), not from small character caps.

Its conversation is the discussor's, continued (via memory). When it fills
`roll_percent` of the window, the next turn starts a fresh one, briefed from
memory and the book.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from agent import activity
from agent.activity import arm_progress, disarm_progress
from agent.agentfolder.schema import END_TARGET, OrchestratorNodeConfig
from agent.backends.base import Access
from agent.backends.delegate import Delegated
from agent.backends.models import context_window, prompt_budget_chars
from agent.supervisor import ACTIONS as SUPERVISOR_ACTIONS
from agent.supervisor import clip_middle
from agent.telemetry import record_usage, usage_to_dict
from agent.work import context as pulls
from agent.work import memory as memory_lib
from agent.work.state import WorkState

ACTIONS = ("dispatch", "validate", "ask_human", "inspect", "finish")
STATUSES = ("todo", "doing", "validating", "done", "failed", "stale", "blocked")
VERDICTS = ("none", "done", "improve_experiment", "improve_agent", "stop")
THREAD_KEY = "orchestrator/"

#: How many ledger lines each turn shows, newest last.
HISTORY = 8
#: How many `inspect` turns in a row before it must act.
MAX_INSPECTS = 2


ORCHESTRATOR_INSTRUCTIONS = """
You are the ORCHESTRATOR of this experiment. You discussed it with the person,
the plan was approved and an agent was built from it; now you run that agent,
turn by turn, until the work is done and checked. You are READ-ONLY: you never
change files yourself. You may read files to understand a problem.

Each turn you are told what just happened -- a node reported, the validator
judged, the person answered -- and you choose ONE action:

  dispatch   send `target` its next piece of work. `handoff` is what it is
             told: what to do now, what to fix, what to keep. Name a substep
             when the step has them. thread=fresh starts it a new
             conversation (a new substep; or it is stuck), continue keeps it.
  validate   send `subject`'s latest output to the validator. Do this after
             every piece of work before relying on it; `handoff` says what to
             focus on. The validator reads the files itself.
  ask_human  ask the person (`handoff` is the question). For a decision only
             they can make, or when you are stuck -- not for routine progress.
  inspect    read some detail first (`include`), then decide next turn.
  finish     the work is done and checked -- or cannot be done. Fill verdict,
             summary and findings: this is your review of the whole run.

WHERE A PROBLEM STARTED. When the validator fails a step, read its
suspected_origin and evidence and send the fix to the node where the fault
STARTED, which is not always the node just checked: a wrong number in step 3
may come from step 1's data. Steps after a fixed step are marked stale; send
them again, in order, once the fix passes.

CONTEXT. You get each step's final report, not its logs. To see more, use
`include` ({node, what}: report | output | changes | log | prompt | turn) --
with inspect for yourself, or with dispatch/validate to pass it on. Pass on
only what the receiver needs.

Keep `note` for what you must remember later (a decision, a number, a
warning); it survives when this conversation is restarted. Update
`step_status` only when you know better than the book.

THE PAPER. When every step is done and validated, dispatch the paper node; it
writes the paper from the step reports and the results. Validate the paper
like any step. Finish after that.

FINISH verdicts -- your review of the run:
  done                the result answers the question (negative results
                      count), every step checked
  improve_experiment  the PLAN must change (metric, baseline, seeds, a
                      confound): say exactly what in plan_feedback
  improve_agent       the plan is right but the agent did it badly (a node
                      too weak, a missing input): say which in design_feedback
  stop                another attempt would not help (data or hardware
                      missing, the same failure repeating)
Each finding is one concrete sentence with its evidence: a file, a number, a
node.
""".strip()


# ---------------------------------------------------------------------------
# the decision it answers with
# ---------------------------------------------------------------------------

def orchestrator_output_model(targets: list[str], spokes: list[str]) -> type[BaseModel]:
    """Built from this agent's node names, so `target` can only be a real one.

    `targets`: what it can dispatch (work nodes in plan order, then the paper
    node). `spokes`: every model node, the validator included -- what it can
    ask to see.
    """
    target_type = Literal[tuple(targets + ["none"])]  # type: ignore[misc]
    pull_node = Literal[tuple(spokes)]  # type: ignore[misc]

    pull = create_model(
        "ContextPull", __config__=ConfigDict(extra="forbid"),
        node=(pull_node, Field(description="whose detail")),
        what=(Literal[pulls.WHATS], Field(description="which detail")),  # type: ignore[misc]
    )
    change = create_model(
        "StepStatus", __config__=ConfigDict(extra="forbid"),
        node=(target_type, ...),
        status=(Literal[STATUSES], ...),  # type: ignore[misc]
    )
    return create_model(
        "OrchestratorDecision", __config__=ConfigDict(extra="forbid"),
        action=(Literal[ACTIONS], Field(description=" | ".join(ACTIONS))),  # type: ignore[misc]
        target=(target_type, Field("none", description="dispatch: who does the work")),
        subject=(target_type, Field("none", description="validate: whose output; "
                                                        "dispatch: whose failure this fixes")),
        reason=(str, Field("", description="one sentence: why this action")),
        handoff=(str, Field("", description="what the receiver is told; for ask_human, "
                                            "the question")),
        include=(list[pull], Field(default_factory=list, max_length=6)),
        thread=(Literal["continue", "fresh"], "continue"),
        step_status=(list[change], Field(default_factory=list, max_length=12)),
        note=(str, Field("", description="what to remember, if anything")),
        verdict=(Literal[VERDICTS], Field("none", description="finish only")),  # type: ignore[misc]
        summary=(str, Field("", description="finish: the run in a few sentences")),
        findings=(list[str], Field(default_factory=list, max_length=12)),
        plan_feedback=(str, ""),
        design_feedback=(str, ""),
    )


# ---------------------------------------------------------------------------
# the node
# ---------------------------------------------------------------------------

def make_orchestrator_node(
    name: str,
    config: OrchestratorNodeConfig,
    *,
    roster: dict[str, dict],
    backend,
    session_dir: str = "",
):
    """`roster`: node -> {kind: work|validator|paper|human, steps, role,
    access}, in graph order. Work nodes are dispatched in that order by
    default, so it is the plan's."""
    work = [n for n, r in roster.items() if r["kind"] == "work"]
    targets = work + ([config.paper] if config.paper else [])
    spokes = [n for n, r in roster.items() if r["kind"] in ("work", "validator", "paper")]
    output_model = orchestrator_output_model(targets, spokes)
    instructions = ORCHESTRATOR_INSTRUCTIONS + (
        f"\n\nFOR THIS EXPERIMENT:\n{config.instructions.strip()}" if config.instructions.strip()
        else "")

    def orchestrator_node(state: WorkState) -> dict[str, Any]:
        if config.announce:
            print(f"\n[{name}] {config.announce}")
        own = dict(state.get("orchestrator") or {})
        book = _book(own.get("book"), targets)
        memory = memory_lib.load(session_dir)

        # ---- step 1: what just happened ------------------------------------
        event = _event(state, own, book, config, roster)
        _apply_event(event, own, book)

        # ---- step 2: which conversation ------------------------------------
        provider = getattr(backend, "name", "") or ""
        model = getattr(backend, "model", "") or ""
        thread_id = (state.get("threads") or {}).get(THREAD_KEY)
        owner = (state.get("thread_providers") or {}).get(THREAD_KEY)
        rolled = ""
        if thread_id and owner and provider and owner != provider:
            rolled = f"it ran on {owner} and now runs on {provider}"
        elif thread_id and float(own.get("fill") or 0) >= config.roll_percent:
            rolled = f"its conversation was {own.get('fill')}% full"
        if rolled:
            print(f"[{name}] starting a fresh conversation: {rolled}.")
            thread_id = None
        turns = int(own.get("turns") or 0)
        kickoff = thread_id is None or turns == 0 or rolled

        # ---- step 3: the prompt, within the model's budget -----------------
        window = config.context_window or own.get("window") or None
        budget = prompt_budget_chars(provider, model, window)
        state_line = _state_line(turns + 1, event, book, targets)
        parts = []
        if kickoff:
            parts.append(_kickoff(state, memory, roster, book, config, name,
                                  continued=bool(thread_id), budget=budget))
        parts.append(_turn_text(event, state, book, own, config, budget=budget))
        if own.get("inspected"):
            parts.append(f"WHAT YOU ASKED TO SEE:\n{own['inspected']}")
        parts.append(state_line + "\nDecide the next action.")
        prompt = "\n\n".join(p for p in parts if p)
        sent = {"template": "first" if kickoff else "next", "prompt": prompt,
                "instructions": instructions}

        # ---- step 4: ask the model -----------------------------------------
        started = time.monotonic()
        arm_progress(backend, session_dir, name, sent=sent)

        def call(thread, text):
            return backend.run_structured(
                thread_id=thread, repo_path=state["repo_path"], access=Access.READ_ONLY,
                developer_instructions=instructions, prompt=text, output_model=output_model)

        try:
            try:
                run = call(thread_id, prompt)
            except Delegated:
                provider = getattr(backend, "name", "") or ""
                prompt = "\n\n".join(p for p in (
                    _kickoff(state, memory, roster, book, config, name, continued=False,
                             budget=budget),
                    _turn_text(event, state, book, own, config, budget=budget),
                    state_line + "\nDecide the next action.") if p)
                sent = {**sent, "template": "first", "prompt": prompt, "delegated": True}
                arm_progress(backend, session_dir, name, sent=sent)
                run = call(None, prompt)
        finally:
            disarm_progress(backend, session_dir, name)
        decision = run.data.model_dump(mode="json")

        # ---- step 5: decide, with the guards in code -----------------------
        own.update(turns=turns + 1, inspected="")
        if run.usage is not None:
            usage = usage_to_dict(run.usage)
            win = context_window(provider, model, config.context_window
                                 or usage.get("model_context_window"))
            own["window"] = win
            own["fill"] = round(100 * (usage.get("last_input_tokens") or 0) / win, 1)
        for change in decision.get("step_status") or []:
            if change["node"] in book:
                book[change["node"]]["status"] = change["status"]
        update: dict[str, Any] = {}
        route, ledger = _decide(name, decision, event, own, book, config, roster, state,
                                session_dir, budget, update)

        record = activity.write(
            session_dir, name, run.events,
            usage=usage_to_dict(run.usage) if run.usage else None,
            summary={k: v for k, v in decision.items() if isinstance(v, (str, int, float, bool))},
            sent=sent, backend=backend, thread_id=run.thread_id, output=decision)
        activity.record_visit(
            session_dir, name, kind="orchestrator", action=ledger["action"],
            target=ledger.get("target", ""), reason=decision.get("reason", "")[:300],
            provider=provider, model=model, role=config.backend, thread=run.thread_id,
            prompt=sent["template"], turn=record.stem if record else "", route=route,
            seconds=round(time.monotonic() - started, 1), fill=own.get("fill"),
            rolled=rolled, output_tokens=getattr(run.usage, "output_tokens", None))
        print(f"[{name}] {ledger['digest']}")

        own["book"] = book
        update.update({
            "orchestrator": own,
            "route": {name: route},
            "threads": {THREAD_KEY: run.thread_id},
            "thread_providers": {THREAD_KEY: provider},
            "usage": {THREAD_KEY: usage_to_dict(run.usage)},
            "ledger": [{**ledger, "node": name, "kind": "decision",
                        "turn": record.stem if record else ""}],
            "transcript": [{"role": name, "text": ledger["digest"]}],
        })
        if event["kind"] == "human":
            # Consumed: never shown twice.
            update["last_answer"] = ""
            update["vars"] = {"answer": "", "gate": ""}

        changes: dict[str, Any] = {
            "thread_id": run.thread_id, "provider": provider, "model": model,
            "fill": own.get("fill"), "turns": own.get("turns"), "window": own.get("window"),
            "book": {n: {k: v for k, v in b.items() if k != "report"} for n, b in book.items()},
            "decisions": [f"{memory_lib.now()} {ledger['digest']}"],
        }
        if decision.get("note", "").strip():
            changes["notes"] = [decision["note"].strip()]
        if rolled:
            changes["rolled_at"] = [f"{memory_lib.now()}: {rolled}"]
        memory_lib.update(session_dir, **changes)

        record_usage(state.get("usage", {}), THREAD_KEY, run.usage)
        return update

    return orchestrator_node


# ---------------------------------------------------------------------------
# the step book
# ---------------------------------------------------------------------------

def _book(saved: dict | None, targets: list[str]) -> dict[str, dict]:
    book = {n: dict(b) for n, b in (saved or {}).items() if n in targets}
    for n in targets:
        book.setdefault(n, {"status": "todo", "visits": 0, "failures": 0})
    return {n: book[n] for n in targets}


def _todo(book: dict, targets: list[str]) -> str:
    """The first node, in order, whose work is not done -- what comes next."""
    return next((n for n in targets if book[n].get("status") != "done"), "none")


# ---------------------------------------------------------------------------
# what just happened
# ---------------------------------------------------------------------------

def _event(state, own, book, config, roster) -> dict:
    """The one thing the orchestrator is being told about this turn."""
    ledger = state.get("ledger") or []
    seen = int(own.get("seen") or 0)
    fresh = [e for e in ledger[seen:] if e.get("kind") != "decision"]
    own["seen"] = len(ledger) + 1          # + the line this turn appends
    if own.get("asked"):
        answer = (state.get("last_answer") or (state.get("vars") or {}).get("answer") or "").strip()
        gate = (state.get("vars") or {}).get("gate") == "approved"
        return {"kind": "human", "node": config.review, "purpose": own.get("asked"),
                "answer": answer, "gate": gate, "subject": own.get("asked_about", "")}
    if not fresh:
        return {"kind": "start", "node": "start"}
    last = fresh[-1]
    node = last.get("node", "")
    data = (state.get("outputs") or {}).get(node) or {}
    kind = roster.get(node, {}).get("kind", "work")
    if kind == "validator":
        return {"kind": "validation", "node": node, "subject": last.get("subject", ""),
                "verdict": data.get("verdict", ""), "data": data}
    return {"kind": "paper" if kind == "paper" else "work", "node": node, "subject": node,
            "data": data, "status": data.get("status", "")}


def _apply_event(event, own, book) -> None:
    """Update the book from the event, before the model sees it."""
    kind = event["kind"]
    if kind in ("work", "paper") and event["node"] in book:
        entry = book[event["node"]]
        # "working": a substep's progress, more to come. Anything else: the
        # node says its step is finished -- not done until validated.
        entry["status"] = "doing" if event.get("status") == "working" else "finished"
        entry["summary"] = str(event["data"].get("summary") or "")[:400]
        if event["data"].get("report_path"):
            entry["report_path"] = event["data"]["report_path"]
    elif kind == "validation" and event.get("subject") in book:
        entry = book[event["subject"]]
        data = event["data"]
        verdict = str(data.get("verdict") or "")
        entry["verdict"] = verdict
        if verdict == "pass":
            # Collapses: the evidence stays with the validator's own record;
            # the book keeps that it passed, and the step's report.
            entry.update(status="done", failures=0, problem="")
            own["last_validation"] = {}
        else:
            entry["status"] = "failed"
            entry["failures"] = int(entry.get("failures") or 0) + (verdict == "fail")
            entry["problem"] = str(data.get("problem") or "")[:500]
            own["last_validation"] = {
                "subject": event["subject"], "verdict": verdict,
                **{k: data.get(k, "") for k in ("problem", "suspected_origin", "suspected_step",
                                                "evidence", "suggested_fix")}}
    elif kind == "human":
        if event.get("gate") and own.get("asked") == "gate":
            own["approved_gates"] = sorted(set(own.get("approved_gates") or [])
                                           | set(own.get("gate_ids") or []))
        # A guard that asked is reset by the answer: the person said go on.
        if own.get("asked") == "guard":
            own["turn_base"] = int(own.get("turns") or 0)
            for entry in book.values():
                entry["visits"] = 0
                entry["failures"] = 0
        own["asked"] = ""


# ---------------------------------------------------------------------------
# the prompt
# ---------------------------------------------------------------------------

def _kickoff(state, memory, roster, book, config, name, *, continued: bool, budget: int) -> str:
    """Everything a conversation needs to take over the run: sent on the
    first turn of a run, and of every fresh conversation."""
    from agent.bootstrap.nodes.planner import steps_for

    plan = state.get("plan") or {}
    steps = plan.get("steps") or []
    ids = [s["id"] for s in steps] + [x["id"] for s in steps for x in s.get("substeps") or []]
    iteration = int((state.get("supervision") or {}).get("iteration") or 1)
    opening = ("THE DISCUSSION IS OVER. The plan was approved and an agent was built "
               "from it. From now on you are its ORCHESTRATOR, and your answers are "
               "decisions in the new format." if continued and iteration == 1 else
               f"A RUN STARTS (iteration {iteration}). You are its ORCHESTRATOR.")
    roster_lines = []
    for node, info in roster.items():
        if info["kind"] == "human":
            continue
        role = {"work": "work", "validator": "THE VALIDATOR", "paper": "THE PAPER"}[info["kind"]]
        owns = f", steps {', '.join(info['steps'])}" if info.get("steps") else ""
        roster_lines.append(f"  {node} -- {role}{owns}; {info['access']} access, "
                            f"role {info['role']}")
    roster_lines.append(f"  {config.review} -- the person (ask_human)")

    parts = [opening,
             f"THE BRIEF:\n{state.get('task_brief', '')}"]
    if memory.get("requirements"):
        parts.append(f"WHAT WAS AGREED IN THE DISCUSSION:\n{memory['requirements']}")
    if memory.get("rationale"):
        parts.append(f"WHY THE AGENT WAS BUILT THIS WAY:\n{memory['rationale']}")
    parts.append(f"THE APPROVED PLAN, with each step's check:\n{steps_for(plan, ids) or '(none)'}")
    parts.append("THE NODES:\n" + "\n".join(roster_lines))
    if memory.get("iterations"):
        parts.append("EARLIER ITERATIONS:\n" + "\n".join(
            f"  {i.get('n')}: {i.get('verdict')} -- {i.get('summary', '')} "
            f"(decided: {i.get('decision', '?')})" for i in memory["iterations"]))
    if memory.get("notes"):
        parts.append("YOUR NOTES:\n" + "\n".join(f"  - {n}" for n in memory["notes"]))
    if any(b.get("status") != "todo" for b in book.values()):
        # A fresh conversation in the middle of a run: the finished steps'
        # reports are the run so far.
        reports = []
        for node, entry in book.items():
            if entry.get("status") in ("done", "finished") and entry.get("report_path"):
                text = pulls.report_of(state, node)
                if text:
                    reports.append(f"--- {node} (report) ---\n{text}")
        if reports:
            parts.append("THE FINISHED STEPS' REPORTS:\n"
                         + clip_middle("\n\n".join(reports), int(budget * 0.4)))
        recent = [e for e in state.get("ledger") or []][-HISTORY:]
        if recent:
            parts.append("RECENT EVENTS:\n" + "\n".join(f"  {e.get('digest', '')}" for e in recent))
    return clip_middle("\n\n".join(parts), int(budget * 0.7))


def _turn_text(event, state, book, own, config, *, budget: int) -> str:
    """The event, in full, and the step book."""
    kind = event["kind"]
    lines = []
    if kind == "start":
        lines.append("NOTHING HAS RUN YET. Dispatch the first piece of work.")
    elif kind in ("work", "paper"):
        data = event["data"]
        text = pulls.output_text(data, skip=("report", "report_path", "git_status"))
        report = pulls.report_of(state, event["node"])
        lines.append(f"{event['node'].upper()} REPORTED ({event.get('status') or 'finished'}):\n"
                     + clip_middle(text, int(budget * 0.25)))
        if report:
            lines.append(f"ITS FINAL REPORT:\n{clip_middle(report, int(budget * 0.25))}")
    elif kind == "validation":
        data = event["data"]
        if data.get("verdict") == "pass":
            # A pass collapses to one line: its evidence stays in the
            # validator's own record (include {validator, output} to see it).
            lines.append(f"THE VALIDATOR PASSED {event.get('subject') or '?'}."
                         + (f" Note: {data['problem']}" if data.get("problem") else ""))
        else:
            lines.append(f"THE VALIDATOR JUDGED {event.get('subject') or '?'}: "
                         f"{data.get('verdict', '?')}\n"
                         + clip_middle(pulls.output_text(data), int(budget * 0.25)))
    elif kind == "human":
        what = {"gate": "approved the gate" if event.get("gate") else "answered your gate question",
                "finish_review": "answered your proposal to finish",
                "guard": "answered your question about a limit"}.get(event.get("purpose"),
                                                                     "answered")
        lines.append(f"THE PERSON {what.upper()}:\n{event.get('answer') or '(no text)'}")

    book_lines = []
    for node, entry in book.items():
        extra = []
        if entry.get("summary"):
            extra.append(entry["summary"][:160])
        if entry.get("problem") and entry.get("status") == "failed":
            extra.append(f"problem: {entry['problem'][:200]}")
        if entry.get("report_path") and entry.get("status") == "done":
            extra.append(f"report: {entry['report_path']}")
        count = f" visits {entry.get('visits', 0)}/{config.max_visits}"
        if entry.get("failures"):
            count += f", failed {entry['failures']}/{config.max_failures}"
        book_lines.append(f"  {node}: {entry.get('status', 'todo')}{count}"
                          + (f" -- {' | '.join(extra)}" if extra else ""))
    lines.append("THE STEP BOOK:\n" + "\n".join(book_lines))
    left = config.max_turns - (int(own.get("turns") or 0) - int(own.get("turn_base") or 0))
    if left <= 10:
        lines.append(f"Only {left} decision(s) left before the person is asked.")
    validation = own.get("last_validation") or {}
    if validation and kind != "validation":
        lines.append(f"OPEN FAILURE: {validation.get('subject')} -- {validation.get('problem')} "
                     f"(origin: {validation.get('suspected_origin') or '?'})")
    return "\n\n".join(lines)


def _state_line(turn, event, book, targets) -> str:
    """A fixed, machine-readable summary. For debugging -- and the fake
    backend reads it to play a plausible orchestrator offline."""
    data = event.get("data") or {}
    return (f"[state] turn={turn} last={event.get('node', '')} kind={event['kind']} "
            f"subject={event.get('subject') or 'none'} verdict={data.get('verdict') or 'none'} "
            f"origin={data.get('suspected_origin') or 'none'} todo={_todo(book, targets)}")


# ---------------------------------------------------------------------------
# acting on the decision
# ---------------------------------------------------------------------------

def _decide(name, decision, event, own, book, config, roster, state, session_dir,
            budget, update) -> tuple[str, dict]:
    """Turn the model's decision into a route, a handoff and a ledger line,
    enforcing the guards. Returns (route, ledger line)."""
    from agent.bootstrap.blueprint import gated_ids

    action = decision.get("action", "ask_human")
    target = decision.get("target") or "none"
    subject = decision.get("subject") or "none"
    reason = decision.get("reason", "")
    last_dispatch = own.get("last_dispatch", "")

    def ask(question: str, purpose: str = "orchestrator", about: str = "",
            context: str = "") -> tuple[str, dict]:
        own["asked"] = "guard" if purpose == "guard" else purpose
        own["asked_about"] = about
        update["pending"] = {"purpose": purpose, "resume_to": name, "question": question,
                             "context": context}
        return config.review, {"action": "ask_human", "target": config.review,
                               "digest": f"ask the person: {question.splitlines()[0][:200]}"}

    # ---- normalise ----------------------------------------------------------
    if action == "dispatch" and target == "none" and subject != "none":
        target = subject
    if action == "validate" and subject == "none":
        subject = target if target != "none" else last_dispatch or "none"
    if action == "inspect":
        own["inspects"] = int(own.get("inspects") or 0) + 1
        if own["inspects"] > MAX_INSPECTS:
            action = "ask_human"
            decision["handoff"] = (decision.get("handoff") or
                                   f"I have looked at the detail {MAX_INSPECTS} times without "
                                   f"deciding. {reason} How should I proceed?")
    else:
        own["inspects"] = 0

    turns_used = int(own.get("turns") or 0) - int(own.get("turn_base") or 0)
    if turns_used >= config.max_turns and action != "finish":
        return ask(f"I have made {turns_used} decisions in this run, the limit "
                   f"({config.max_turns}). Last: {action} {target}. {reason}\n\n"
                   f"/continue [guidance] to carry on, or /exit.", "guard")

    # ---- each action --------------------------------------------------------
    if action == "inspect":
        own["inspected"] = pulls.resolve(session_dir, state, decision.get("include"),
                                         int(budget * 0.5))
        return name, {"action": "inspect", "target": name,
                      "digest": "inspect: " + ", ".join(
                          f"{p['node']}.{p['what']}" for p in decision.get("include") or [])}

    if action == "ask_human":
        return ask(decision.get("handoff") or reason or "How should I proceed?")

    if action == "finish":
        paper = config.paper
        verdict = decision.get("verdict") or "none"
        if verdict == "none":
            verdict = "done"
        status = book.get(paper, {}).get("status")
        if paper and status != "done" and verdict != "stop":
            # No run ends without its paper -- written, and validated.
            reason = f"(finish asked for; the paper is not done yet) {reason}"
            if status == "finished":
                action, subject = "validate", paper
                decision["handoff"] = ("Check the paper: every number traces to a result "
                                       "file, every citation exists, Limitations is there, "
                                       "and the appendices hold the full results and "
                                       "settings.")
            else:
                action, target = "dispatch", paper
                decision["handoff"] = decision.get("handoff") or "Write the paper."
        else:
            return _finish(name, decision, verdict, own, config, state, update, ask)

    if action == "validate":
        if subject not in book:
            return ask(f"I wanted to validate {subject!r}, which is not a node that does "
                       f"work. {reason}")
        validator = config.validator
        last_subject = own.get("validated_last", "")
        fresh = not (decision.get("thread") == "continue" and last_subject == subject)
        own["validated_last"] = subject
        book[subject]["status"] = "validating"
        text = _validation_request(subject, decision, state, roster, session_dir, budget)
        update["handoff"] = {"to": validator, "text": text, "fresh": fresh,
                             "subject": subject, "turn": own["turns"]}
        return validator, {"action": "validate", "target": validator, "subject": subject,
                           "digest": f"validate {subject}: {reason}"}

    # ---- dispatch -----------------------------------------------------------
    if target == config.validator:
        decision["action"] = "validate"
        decision["subject"] = subject if subject != "none" else last_dispatch or "none"
        decision["target"] = "none"
        return _decide(name, decision, event, own, book, config, roster, state, session_dir,
                       budget, update)
    if target not in book:
        return ask(f"I chose to {action} {target!r}, which is not a node I can send work to. "
                   f"{reason}\n\nTell me how to proceed, or /exit.")
    entry = book[target]
    if int(entry.get("visits") or 0) >= config.max_visits:
        return ask(f"{target} has been sent work {entry['visits']} times in this run, the "
                   f"limit ({config.max_visits}). Its last problem: "
                   f"{entry.get('problem') or '(none recorded)'}\n\n{reason}\n\n"
                   f"/continue [guidance] to allow more, or /exit.", "guard", target)
    if int(entry.get("failures") or 0) >= config.max_failures:
        return ask(f"{target} has failed validation {entry['failures']} times in a row. "
                   f"Last problem: {entry.get('problem') or '?'}\n\n{reason}\n\n"
                   f"/continue [guidance] to try again, or /exit.", "guard", target)
    owned = roster.get(target, {}).get("steps") or []
    gates = sorted(g for g in set(gated_ids(state.get("plan") or {}))
                   - set(own.get("approved_gates") or [])
                   # A node owning step 2 owns its substeps' gates (2.3).
                   if any(g == s or g.startswith(f"{s}.") for s in owned))
    if gates:
        own["gate_ids"] = gates
        return ask(f"{target} is about to start, and step(s) {', '.join(gates)} need your "
                   f"approval first.\n\nWhat it would be told: {decision.get('handoff') or '-'}"
                   f"\n\n/approve [note] to let it start, /continue [guidance] to have me "
                   f"reconsider, or /exit.", "gate", target)

    # Work on an earlier step makes later finished ones stale: they were
    # built on what is about to change.
    order = list(book)
    for later in order[order.index(target) + 1:]:
        if book[later].get("status") in ("done", "finished", "failed"):
            book[later]["status"] = "stale"
    entry["status"] = "doing"
    entry["visits"] = int(entry.get("visits") or 0) + 1
    own["last_dispatch"] = target
    text = _work_handoff(target, decision, own, book, state, session_dir, budget, config)
    update["handoff"] = {"to": target, "text": text,
                         "fresh": decision.get("thread") == "fresh",
                         "subject": subject if subject != "none" else "", "turn": own["turns"]}
    return target, {"action": "dispatch", "target": target,
                    "digest": f"dispatch -> {target}: {reason}"}


def _work_handoff(target, decision, own, book, state, session_dir, budget, config) -> str:
    """What the dispatched node is told: the orchestrator's instructions, the
    validator's findings when this is the fix, and anything it chose to pass
    on."""
    parts = [f"FROM THE ORCHESTRATOR:\n{decision.get('handoff') or 'Carry on with your step.'}"]
    validation = own.get("last_validation") or {}
    if validation and (target == validation.get("suspected_origin")
                       or target == validation.get("subject")
                       or decision.get("subject") == validation.get("subject")):
        parts.append(
            f"THE VALIDATOR FOUND A PROBLEM in {validation.get('subject')}'s work"
            + (f" (step {validation['suspected_step']})" if validation.get("suspected_step") else "")
            + f":\n{validation.get('problem', '')}\n\nEvidence: {validation.get('evidence', '')}"
            + (f"\n\nSuggested fix: {validation['suggested_fix']}"
               if validation.get("suggested_fix") else ""))
    if target == config.paper:
        parts.append(_paper_inputs(state, book, config.paper, session_dir, budget))
    pulled = pulls.resolve(session_dir, state, decision.get("include"), int(budget * 0.4))
    if pulled:
        parts.append(f"CONTEXT PASSED ON TO YOU:\n{pulled}")
    return "\n\n".join(parts)


def _validation_request(subject, decision, state, roster, session_dir, budget) -> str:
    """Everything the validator needs to judge one node's work -- generous,
    because a verdict on too little evidence is worse than none. After the
    verdict, none of it is carried forward."""
    from agent.bootstrap.nodes.planner import steps_for

    data = (state.get("outputs") or {}).get(subject) or {}
    steps = roster.get(subject, {}).get("steps") or []
    turn = activity.latest_finished(session_dir, subject) if session_dir else None
    asked = steps_for(state.get("plan") or {}, steps) or "(no plan steps)"
    if roster.get(subject, {}).get("kind") == "paper":
        asked = PAPER_CHECKS.format(artifacts=state.get("artifacts_dir", ""))
    parts = [
        f"VALIDATE THE WORK OF: {subject}",
        f"FOCUS (from the orchestrator): {decision.get('handoff') or 'its latest output'}",
        f"WHAT IT WAS ASKED TO DO, with each check:\n{asked}",
        f"WHAT IT REPORTED:\n{clip_middle(pulls.output_text(data, skip=('report',)), int(budget * 0.15))}",
    ]
    report = pulls.report_of(state, subject)
    if report:
        parts.append(f"ITS FINAL REPORT:\n{clip_middle(report, int(budget * 0.2))}")
    changes = str(data.get("turn_changes") or "")
    if changes:
        parts.append(f"FILES ITS LAST TURN CHANGED:\n{clip_middle(changes, int(budget * 0.1))}")
    log = activity.log_tail(turn)
    if log:
        parts.append(f"WHAT ITS COMMANDS PRINTED (the end of each):\n"
                     f"{clip_middle(log, int(budget * 0.2))}")
    pulled = pulls.resolve(session_dir, state, decision.get("include"), int(budget * 0.2))
    if pulled:
        parts.append(f"MORE CONTEXT:\n{pulled}")
    parts.append(f"The workspace is {state.get('repo_path', '')}; results are under "
                 f"{state.get('artifacts_dir', '')}. Open the files -- do not take the report's "
                 f"word for them.")
    return "\n\n".join(parts)


#: What the validator checks a paper against.
PAPER_CHECKS = """An ACL-style paper of this run, in {artifacts}/paper. Check:
- every number in it traces to a file under {artifacts} -- open them;
- every citation in references.bib exists (it was found in the design
  phase's research, or a lookup confirms it); none written from memory;
- the body has Introduction, Related Work, Method, Experimental Setup,
  Results, Analysis, Limitations (required) and Ethics Statement;
- the appendices hold the full results (every configuration, seed and run,
  failed ones included), the full settings, and how to reproduce it: every
  result file is either covered by an appendix table or listed as excluded,
  with the reason;
- its claims match the evidence -- a null or negative result said plainly;
- main.tex uses the ACL style (or says why it could not), and main.pdf
  exists if a LaTeX engine is installed."""


def _paper_inputs(state, book, paper, session_dir, budget) -> str:
    """What the paper is written from: every step's final report, and where
    the results and the research are."""
    from pathlib import Path

    reports = []
    for node in book:
        text = pulls.report_of(state, node)
        if text and node != paper:
            reports.append(f"--- {node} ---\n{text}")
    research = sorted(Path(session_dir, "research").glob("round-*.md")) if session_dir else []
    parts = ["THE STEP REPORTS:\n" + (clip_middle("\n\n".join(reports), int(budget * 0.4))
                                     or "(none saved)")]
    if research:
        parts.append("THE DESIGN PHASE'S RESEARCH (papers found and checked):\n"
                     + "\n".join(f"  {p}" for p in research))
    parts.append(f"THE RESULTS are under {state.get('artifacts_dir', '')}.")
    return "\n\n".join(parts)


def _finish(name, decision, verdict, own, config, state, update, ask) -> tuple[str, dict]:
    """The orchestrator's review of the run -- what the supervisor used to
    write -- and, depending on the mode, a question or the end."""
    supervision = state.get("supervision") or {}
    mode = supervision.get("mode") or "off"
    iteration = int(supervision.get("iteration") or 1)
    limit = int(supervision.get("max_iterations") or 1)
    final = {"verdict": verdict, "summary": decision.get("summary") or decision.get("reason", ""),
             "findings": list(decision.get("findings") or []),
             "plan_feedback": decision.get("plan_feedback", ""),
             "design_feedback": decision.get("design_feedback", "")}
    own["final"] = final
    suggested = SUPERVISOR_ACTIONS.get(verdict, "finish")
    digest = f"finish ({verdict}): {final['summary'][:200]}"
    if mode == "manual":
        at_limit = iteration >= limit
        question = (f"The run is over: {verdict}.\n\n{final['summary']}\n\n"
                    + "".join(f"- {f}\n" for f in final["findings"])
                    + f"\nRecommended: {suggested}"
                    + (f" -- but this is iteration {iteration} of {limit}" if at_limit else "")
                    + ".\n\n/accept to do that, /replan [what] to change the plan, "
                      "/redesign [what] to redesign the agent, /finish to stop here, or "
                      "/continue [what] to keep working.")
        context = "\n\n".join(x for x in (
            f"plan feedback: {final['plan_feedback']}" if final["plan_feedback"] else "",
            f"design feedback: {final['design_feedback']}" if final["design_feedback"] else "",
        ) if x)
        route, ledger = ask(question, "finish_review", context=context)
        return route, {**ledger, "action": "finish", "digest": digest}
    update["outcome"] = "auto" if mode == "auto" else "done"
    return END_TARGET, {"action": "finish", "target": END_TARGET, "digest": digest}
