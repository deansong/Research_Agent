"""
WHAT:  The bootstrap graph: discussor <-> user, orchestrator -> researcher /
       planner / designer -> orchestrator, with the approval gates and limits
       enforced in code.
WHY:   The orchestrator LLM decides what the team does next; code makes sure
       it cannot plan before the hypotheses, rubric and protocol are approved,
       design before the plan is, or work for ever without the user.
CONCEPT: LangGraph. The graph state carries the research state (also mirrored
       to <run>/state/), the requests waiting for the orchestrator, and what
       the discussor last saw. The user is an interrupt; the checkpoint
       (<run>/checkpoint.sqlite) is what makes a run resumable.

    START -> discussor --(the user asked for work, or approved)--> orchestrator
                       --(otherwise)----------------> human (the user answers)
    orchestrator --research--> researcher --> orchestrator
                 --hypothesize/plan--> planner --> orchestrator
                 --design--> designer --> orchestrator
                 --ask_user / ready / limit--> discussor (tells the user) -> human
    human --> discussor, or END on /exit
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from typing_extensions import TypedDict

import research_backend as RB

from research_agent import state as S
from research_agent.bootstrap import nodes

#: Orchestrator decisions per user message before the user is asked again.
MAX_STEPS = 6

#: What each action needs approved first.
GATES = {
    "plan": ("hypotheses", "rubric", "protocol"),
    "design": ("plan",),
    "ready": ("design",),
}

WORKERS = {"research": "researcher", "hypothesize": "planner", "plan": "planner",
           "design": "designer"}


class BootstrapState(TypedDict, total=False):
    research: dict
    """The research state (research_agent/state.py)."""
    seen: dict
    """The section versions the discussor last saw."""
    pending: list
    """Requests from the discussor, waiting for the orchestrator."""
    decision: dict
    """The orchestrator's last decision: action, task, questions, note."""
    last: str
    """What the last piece of work did -- for the orchestrator."""
    user: str
    """The user's latest message, for the discussor."""
    note: str
    """What the orchestrator wants the discussor to tell or ask the user."""
    reply: str
    """The discussor's latest words to the user."""
    steps: int
    approved: bool
    """The user approved something with this message (e.g. /approve)."""
    outcome: str
    """"" while working; "ready" once the design is approved and nothing is
    open; "exit" when the user left."""


def blocked(st: dict, action: str) -> list[str]:
    """The sections `action` is waiting on: not approved, or not there."""
    return [s for s in GATES.get(action, ()) if not S.approved(st, s)]


def build(run: nodes.Run, checkpointer=None):
    """The compiled graph for one run."""
    store = S.Store(run.folder)

    def commit(st: dict, ops: list[dict], by: str) -> dict:
        if not ops:
            return st
        new, changed = S.apply(st, ops, by=by)
        store.write(new, by=by, ops=ops, changed=changed)
        return new

    def discussor(state: BootstrapState) -> dict:
        st = state.get("research") or S.new()
        turn = nodes.discussor(run, st, seen=state.get("seen") or {},
                               user=state.get("user", ""), note=state.get("note", ""))
        st = commit(st, turn.ops, "discussor")
        spoke = bool(state.get("user"))
        # The user asked for work, or approved something (which lets the
        # work move on): the orchestrator decides. Otherwise keep talking.
        approved = state.get("approved") or any(op["op"] == "approve" for op in turn.ops)
        go = spoke and bool(turn.requests or approved)
        return {"research": st, "seen": dict(st.get("versions") or {}), "approved": False,
                "pending": list(state.get("pending") or []) + turn.requests,
                "reply": turn.reply, "user": "", "note": "",
                # A new user message gives the orchestrator a fresh allowance.
                "steps": 0 if spoke else state.get("steps", 0),
                "_to": "orchestrator" if go else "human"}

    def orchestrator(state: BootstrapState) -> dict:
        st = state["research"]
        if S.approved(st, "design") and not state.get("pending"):
            # Done, whatever a model might still want to look into: seen in a
            # real run, an orchestrator with everything approved went on to
            # ask about sign-offs and research licences -- the execution
            # stage's work. Asking for a change still reopens the bootstrap.
            st = commit(st, [{"op": "merge", "section": "status", "value": {
                "phase": "ready", "next": "execution"}}], "orchestrator")
            note = ("Everything is approved: the bootstrap is done. Tell the user, and that "
                    "anything about carrying the plan out is for the execution stage.")
            return {"research": st, "note": note, "pending": [], "outcome": "ready",
                    "decision": {"action": "ready", "task": note, "questions": [],
                                 "reason": "the design is approved and nothing new was asked"}}
        if state.get("steps", 0) >= MAX_STEPS:
            note = (f"The team has taken {MAX_STEPS} steps since the user last spoke. "
                    f"Tell the user where things stand and ask how to continue.")
            return {"decision": {"action": "ask_user", "task": note}, "note": note,
                    "pending": state.get("pending") or []}
        out = nodes.orchestrator(run, st, requests=state.get("pending") or [],
                                 last=state.get("last", ""))
        st = commit(st, out.ops, "orchestrator")
        d = out.data
        decision = {"action": d.action, "task": d.task, "questions": list(d.questions),
                    "reason": d.reason}
        note = d.note_for_discussor or (d.task if d.action == "ask_user" else "")
        waiting = blocked(st, d.action)
        if waiting:
            note = (f"The orchestrator wanted to {d.action}, but first the user must approve: "
                    f"{', '.join(waiting)}. Show them and ask for approval. {note}").strip()
            decision = {"action": "ask_user", "task": note, "questions": [],
                        "reason": f"{d.action} is gated on {', '.join(waiting)}"}
        if decision["action"] == "ready":
            note = note or "The design is approved and nothing is open: the bootstrap is done."
        return {"research": st, "decision": decision, "note": note, "pending": [],
                "steps": state.get("steps", 0) + 1,
                "outcome": "ready" if decision["action"] == "ready" else ""}

    def worker(name: str):
        def run_it(state: BootstrapState) -> dict:
            st, d = state["research"], state["decision"]
            try:
                if name == "researcher":
                    out = nodes.researcher(run, st, task=d["task"], questions=d.get("questions", []))
                elif name == "planner":
                    out = (nodes.hypothesize if d["action"] == "hypothesize" else nodes.plan)(
                        run, st, task=d["task"])
                else:
                    out = nodes.design(run, st, task=d["task"])
            except RB.BackendCancelled:
                raise
            except RB.BackendError as exc:
                summary = f"{name} failed: {str(exc)[:600]}"
                st = commit(st, [{"op": "add", "section": "results", "value": {
                    "node": name, "job": d["action"], "asked": d["task"][:500],
                    "summary": summary}}], name)
                return {"research": st, "last": summary}
            st = commit(st, out.ops, name)
            return {"research": st, "last": f"{name} ({d['action']}): {out.summary}"}
        return run_it

    def human(state: BootstrapState) -> dict:
        answer: Any = interrupt({"reply": state.get("reply", ""),
                                 "outcome": state.get("outcome", "")})
        if isinstance(answer, str):
            answer = {"text": answer}
        if answer.get("exit"):
            return {"outcome": "exit", "_to": END}
        st = state["research"]
        approve = [{"op": "approve", "section": s} for s in answer.get("approve") or []]
        if approve:
            st = commit(st, approve, "user")
        text = answer.get("text", "")
        if approve and not text:
            text = f"(I approved: {', '.join(answer['approve'])}.)"
        return {"research": st, "user": text, "approved": bool(approve), "_to": "discussor"}

    graph = StateGraph(_Routed)
    graph.add_node("discussor", discussor)
    graph.add_node("orchestrator", orchestrator)
    for name in ("researcher", "planner", "designer"):
        graph.add_node(name, worker(name))
    graph.add_node("human", human)

    graph.add_edge(START, "discussor")
    graph.add_conditional_edges("discussor", lambda s: s["_to"], ["orchestrator", "human"])
    graph.add_conditional_edges(
        "orchestrator", lambda s: WORKERS.get(s["decision"]["action"], "discussor"),
        ["researcher", "planner", "designer", "discussor"])
    for name in ("researcher", "planner", "designer"):
        graph.add_edge(name, "orchestrator")
    graph.add_conditional_edges("human", lambda s: s["_to"], ["discussor", END])
    return graph.compile(checkpointer=checkpointer)


class _Routed(BootstrapState, total=False):
    _to: str
    """Where the node just run wants to go next (routing only)."""
