"""
WHAT:  The `agent` node template -- one structured model turn, configured by
       nodes.json.
WHY:   This single function replaces today's discussor, planner, orchestrator
       and executor. Everything that used to differ between those four files
       is now a field in JSON.
CONCEPT: A LangGraph node is a function (state) -> partial update. This one is
       built by a factory that closes over the node's config, exactly like
       make_planner(backend) does in the hand-written graph.
"""

from __future__ import annotations

import time
from typing import Any

from agent import activity
from agent.activity import arm_progress, disarm_progress
from agent.agentfolder.render import render
from agent.agentfolder.schema import END_TARGET, AgentNodeConfig, BranchSpec, EdgeSpec
from agent.backends.delegate import Delegated
from agent.backends.base import Access
from agent.git_utils import fingerprint, snapshot, turn_changes
from agent.telemetry import record_usage, usage_to_dict
from agent.work.state import WorkState, render_context

# Written into `route` when a node's routing value matched no case. The
# compiled path_map sends it to the branch's default.
DEFAULT_ROUTE = "__default__"


def make_agent_node(
    name: str,
    config: AgentNodeConfig,
    *,
    backend,
    output_model,
    transition: EdgeSpec | BranchSpec | None,
    session_dir: str = "",
    hub_role: str = "",
):
    """Build the node function for one `kind: "agent"` entry.

    `transition` is this node's single outgoing edge or its branch. The node
    needs it for one reason: if the transition it takes carries an `ask`, the
    node must write `pending` so the human node knows what to show. That is
    why the branch is evaluated TWICE -- once here (to pick the ask) and once
    in the router (to pick the target). Both lookups are pure and read the same
    `cases` list; see the comment in compile.py.

    `hub_role` is set when the node is a spoke of a hub agent: "work",
    "validator" or "paper". Such a node also takes a fresh conversation when
    the orchestrator says so, appends its line to the ledger, and -- a work
    or paper node that finished -- saves its final report.
    """

    def agent_node(state: WorkState) -> dict[str, Any]:
        if config.announce:
            print(f"\n[{name}] {config.announce}")

        # ---- step 1: which provider conversation is this? -----------------
        context = render_context(state, steps=list(config.steps), node=name)
        thread_suffix = render(config.thread_key, context) if config.thread_key else ""
        thread_id_key = f"{name}/{thread_suffix}"
        thread_id = state.get("threads", {}).get(thread_id_key)
        handoff = state.get("handoff") or {}
        if hub_role and handoff.get("to") == name and handoff.get("fresh"):
            # The orchestrator asked for a clean start: a new substep, or a
            # judge that must not remember its last verdict.
            thread_id = None

        # A conversation belongs to the provider that started it. Moved to
        # another (Runs on, mid-run): start a new one, with the first prompt
        # -- the new model has seen none of the old conversation. A run from
        # before providers were recorded: the node's last recorded turn says.
        provider = getattr(backend, "name", "") or ""
        owner = state.get("thread_providers", {}).get(thread_id_key)
        if thread_id and owner is None and session_dir:
            from agent import activity as _activity

            last = _activity.latest(session_dir, name)
            owner = (getattr(last, "provider", "") or None) if last else None
        if thread_id and owner and provider and owner != provider:
            print(f"[{name}] now runs on {provider}: starting a new conversation "
                  f"(its {owner} one cannot carry over).")
            thread_id = None

        # ---- step 2: first prompt or next prompt? -------------------------
        # THE ONE RULE: `first` when there is no conversation yet, OR when the
        # counter named by refresh_on has moved since this conversation last
        # ran. Otherwise `next`. That reproduces both of the hand-written
        # agent's behaviours -- "no thread yet" and "the plan was revised".
        counters = state.get("counters", {})
        current_mark = int(counters.get(config.refresh_on, 0)) if config.refresh_on else 0
        last_mark = state.get("thread_marks", {}).get(thread_id_key)

        use_first = thread_id is None or (
            bool(config.refresh_on) and last_mark != current_mark
        )
        template = config.prompts.first if use_first else (
            config.prompts.next or config.prompts.first
        )
        prompt = render(template, context)

        # What this turn ASKS, kept so it can be read back. A prompt is a
        # template rendered against the state of the moment; re-rendering it
        # later shows today's state, not what the model read. Which template
        # was used matters too -- a retry loop that is not converging often
        # turns out to be re-sending `first` every time.
        sent = {
            "template": "first" if use_first else "next",
            "prompt": prompt,
            "instructions": config.instructions,
        }

        # ---- step 3: call the model ---------------------------------------
        # Armed per call, because only the node knows which node and which
        # session this turn belongs to. See CodexBackend.on_progress for why
        # per-call is safe here (no fan-out) -- and note it is cleared in the
        # finally, so a backend shared with another node never keeps a stale
        # callback pointing at this one.
        # Taken BEFORE the turn, so turn_changes is this node's own edits and
        # not everything every earlier node has changed too.
        before = fingerprint(state["repo_path"]) if "turn_changes" in config.capture else None
        started = time.monotonic()
        arm_progress(backend, session_dir, name, sent=sent)

        def call(thread, text):
            return backend.run_structured(
                thread_id=thread,
                repo_path=state["repo_path"],
                access=Access(config.access),
                developer_instructions=config.instructions,
                prompt=text,
                output_model=output_model,
            )

        try:
            try:
                run = call(thread_id, prompt)
            except Delegated:
                # Out of credits, and this role delegates (you chose so, under
                # Runs on): the same turn on the delegate, as a NEW
                # conversation with the first prompt -- it has seen none of
                # the old one. See agent/backends/delegate.py.
                thread_id, use_first = None, True
                provider = getattr(backend, "name", "") or ""
                prompt = render(config.prompts.first, context)
                sent = {"template": "first", "prompt": prompt,
                        "instructions": config.instructions, "delegated": True}
                arm_progress(backend, session_dir, name, sent=sent)
                run = call(None, prompt)
        finally:
            disarm_progress(backend, session_dir, name)
        data = run.data.model_dump()

        # ---- step 4: capture anything the model does not report -----------
        if config.capture:
            git = snapshot(state["repo_path"])
            if "git_status" in config.capture:
                data["git_status"] = git.status
            if "git_diff" in config.capture:
                data["git_diff"] = git.diff_stat
            if "turn_changes" in config.capture:
                data["turn_changes"] = (turn_changes(state["repo_path"], before)
                                        or "(no files changed)")

        # ---- step 4b: keep what the provider actually did ----------------
        # Written to disk rather than into state -- see agent/activity.py for
        # why. No guard needed: activity.write does nothing when there is no
        # session_dir, which is the case for a folder driven outside a session.
        record = activity.write(
            session_dir, name, run.events,
            usage=usage_to_dict(run.usage) if run.usage else None,
            summary={k: v for k, v in data.items()
                     if isinstance(v, (str, int, float, bool))},
            sent=sent, backend=backend, thread_id=run.thread_id, output=data,
        )

        _print_summary(name, config, data)

        # ---- step 5: decide where we are going, and what to ask -----------
        route_value, ask = _resolve_transition(transition, data)

        activity.record_visit(
            session_dir, name, kind="agent", steps=list(config.steps),
            provider=getattr(backend, "name", ""), model=getattr(backend, "model", "") or "",
            role=config.backend, thread=run.thread_id,
            prompt="first" if use_first else "next",
            turn=record.stem if record else "", route=route_value,
            seconds=round(time.monotonic() - started, 1),
            output_tokens=getattr(run.usage, "output_tokens", None),
        )

        update: dict[str, Any] = {
            # Each of these returns ONLY this node's own entry. The reducers on
            # these channels merge them; returning the whole dict would erase
            # every other node's. See work/state.py.
            "outputs": {name: data},
            "threads": {thread_id_key: run.thread_id},
            "thread_providers": {thread_id_key: provider},
            "route": {name: route_value},
            "usage": {thread_id_key: _usage_dict(run.usage)},
        }

        if config.refresh_on:
            update["thread_marks"] = {thread_id_key: current_mark}
        if config.bump:
            update["counters"] = {
                counter: int(counters.get(counter, 0)) + 1 for counter in config.bump
            }
        after = render_context({**state, "outputs": {**state.get("outputs", {}), name: data}},
                               steps=list(config.steps), node=name)
        if hub_role:
            update["ledger"] = [_ledger_line(name, hub_role, data,
                                             record.stem if record else "",
                                             handoff.get("subject", ""))]
            saved = _save_report(state.get("artifacts_dir", ""), name, hub_role, data)
            if saved:
                data["report_path"] = saved
                update["outputs"] = {name: data}

        if config.record:
            # Re-render with this turn's output already visible, so a node can
            # record something it just produced.
            text = render(config.record, after).strip()
            if text:
                update["transcript"] = [{"role": name, "text": text}]
        if ask is not None:
            update["pending"] = {
                "purpose": ask.purpose,
                "resume_to": ask.resume_to,
                "question": render(ask.question, after),
                "context": render(ask.context, after),
            }

        record_usage(state.get("usage", {}), thread_id_key, run.usage)
        return update

    return agent_node


#: A hub node's final report is saved here, under the run's artifacts.
REPORTS_DIR = "reports"


def _ledger_line(name: str, hub_role: str, data: dict, turn: str, subject: str) -> dict:
    """One short line for the orchestrator's ledger, made by code -- a model
    is not asked to summarise its own work twice."""
    if hub_role == "validator":
        digest = f"{data.get('verdict', '?')} on {subject or '?'}"
        if data.get("verdict") != "pass":
            if data.get("problem"):
                digest += f": {data['problem']}"
            if data.get("suspected_origin") not in (None, "", subject):
                digest += f" (origin: {data['suspected_origin']})"
    else:
        digest = f"{data.get('status') or 'reported'}: {data.get('summary') or ''}".strip()
    return {"node": name, "kind": "validation" if hub_role == "validator" else hub_role,
            "subject": subject if hub_role == "validator" else name,
            "verdict": str(data.get("verdict", "")) if hub_role == "validator" else "",
            "digest": digest[:300], "turn": turn}


def _save_report(artifacts_dir: str, name: str, hub_role: str, data: dict) -> str:
    """Write a finished node's final report to <artifacts>/reports/<node>.md.

    The report is what outlives the node's turn: the orchestrator carries it
    forward instead of the node's logs, and the paper is written from it.
    Only once the node says it is finished -- a substep's progress note is
    not a report."""
    report = str(data.get("report") or "").strip()
    if hub_role == "validator" or not report or not artifacts_dir:
        return ""
    if data.get("status") not in (None, "", "finished"):
        return ""
    from pathlib import Path

    path = Path(artifacts_dir) / REPORTS_DIR / f"{name}.md"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report + "\n")
    except OSError:
        return ""
    return str(path)


def _resolve_transition(transition, data: dict) -> tuple[str, Any]:
    """Return (route key, ask spec or None) for the transition being taken."""
    if transition is None:
        return END_TARGET, None

    if isinstance(transition, EdgeSpec):
        # An unconditional edge: the route key is unused by the compiled graph
        # (a plain add_edge needs no router) but we still write it so /state
        # shows where each node went.
        return transition.to, transition.ask

    value = str(data.get(transition.route_on, ""))
    for case in transition.cases:
        if case.when == value:
            return value, case.ask

    # No case matched, so the run takes `default` -- which the designer is
    # told to point at a human node, precisely so an unexpected value reaches
    # a person instead of quietly ending the run.
    #
    # `default` has nowhere to put an `ask`, though, and returning None here
    # meant the human node fell back to whatever `pending` still held: the
    # PREVIOUS question, from earlier in the run, with the previous
    # `resume_to`. So the person was asked something stale and their answer
    # went somewhere unrelated. Blank would have been better; this is better
    # still, because the only thing worth saying is what actually happened.
    return DEFAULT_ROUTE, _unrouted_ask(transition, value)


def _unrouted_ask(transition, value: str):
    """An ask block for a branch that fell through to its default."""
    from agent.agentfolder.schema import AskSpec

    handled = ", ".join(repr(case.when) for case in transition.cases) or "nothing"
    return AskSpec(
        purpose="unrouted",
        # Back to the node that produced the unexpected value: it is the only
        # place that can produce a different one.
        resume_to=transition.from_,
        question=(
            f"{transition.from_} returned {transition.route_on}="
            f"{value!r}, which has no case (handled: {handled}).\n\n"
            f"Tell me how to proceed, or /exit."
        ),
        context="",
    )


def _usage_dict(usage) -> dict:
    from agent.telemetry import usage_to_dict

    return usage_to_dict(usage)


def _print_summary(name: str, config: AgentNodeConfig, data: dict) -> None:
    """Show the human what happened, without dumping the whole payload."""
    for field in config.output:
        value = data.get(field.name)
        if not value:
            continue
        if field.type == "enum":
            print(f"[{name}] {field.name} = {value}")
        elif field.type == "string" and len(str(value)) > 200:
            print(f"\n[{name}] {field.name}:")
            print(value)
    if data.get("git_diff"):
        print(f"\n[{name}] diff:")
        print(data["git_diff"])
