"""
WHAT:  The supervisor: after each run of a designed agent, a review of the
       whole run, and the decision what to do next -- finish, re-plan the
       experiment, or redesign the agent and run again.
WHY:   One design and one run is a single shot. A run that measured the
       wrong thing, looped on one node six times, or skipped a seed should
       feed back into the next attempt, and somebody has to read the run to
       know. That is this graph's job; manual mode puts you in the loop,
       auto mode lets it act on its own review up to `max_iterations`.
CONCEPT: A third, small, hand-written StateGraph -- review -> decide -- driven
       by the same terminal and web drivers as the other two phases. The
       outer loop (design -> run -> supervise -> design ...) lives in the
       drivers, because each phase is its own graph on its own thread.

--------------------------------------------------------------------------
WHAT THE REVIEW READS
--------------------------------------------------------------------------
A report built by code (run_report) -- the brief, the plan with its checks,
how the run ended, every node visit from the timeline with the model that
ran it and the route it took, each node's last outputs (checkers' verdicts
and evidence included), the files in the artifacts directory, and earlier
iterations' reviews. The reviewer also has READ-ONLY access to the
repository, so it can open the result files themselves rather than trust
what a node said about them.

--------------------------------------------------------------------------
WHAT IS KEPT
--------------------------------------------------------------------------
<session>/iterations/<n>/ -- the plan, the agent, the brief, this run's
timeline, the report and the review. So a later iteration can be compared
with an earlier one, and "why did the agent change" has an answer on disk.
"""

from __future__ import annotations

import json
import shutil
from collections import Counter
from pathlib import Path
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import TypedDict

from agent import activity
from agent import commands as command_lib
from agent.backends.base import Access
from agent.commands import REGISTRY, SlashCommand
from agent.session import GraphSession
from agent.telemetry import usage_to_dict

ROLE = "supervisor"
ITERATIONS = "iterations"

#: What each verdict does in auto mode, and what /accept does in manual.
ACTIONS = {"done": "finish", "stop": "finish",
           "improve_experiment": "replan", "improve_agent": "redesign"}


class SupervisorReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Literal["done", "improve_experiment", "improve_agent", "stop"]
    """done -- the run answers the question; improve_experiment -- the PLAN
    needs changing; improve_agent -- the plan is right but the agent did it
    badly; stop -- another iteration will not help."""
    summary: str
    findings: list[str] = Field(default_factory=list, max_length=12)
    plan_feedback: str = ""
    """For improve_experiment: what the planner should change, concretely."""
    design_feedback: str = ""
    """For improve_agent (and after a re-plan): what the designer should change."""


SUPERVISOR_INSTRUCTIONS = """
You are the SUPERVISOR of a research agent. A designed agent has just run an
approved plan. Decide whether the result is good enough, and if not, what to
change before the next attempt.

You are READ-ONLY. Open the result files under the artifacts directory and the
code the run wrote; do not trust a node's own summary of them. The report
below tells you which node ran on which model, how often each one looped,
what each checker decided and on what evidence.

Judge three things, in this order:
1. THE RESULT. Did the run produce what the plan's steps say, passing their
   checks, and does it answer the claim? Missing seeds, a baseline that does
   not reproduce, numbers that cannot be right, a report that overstates
   what was measured -- these are failures even if every checker said ok.
2. THE EXPERIMENT. If the result is sound but the experiment cannot answer
   the question -- wrong metric, too few seeds, a missing ablation or
   baseline, a confound -- the PLAN must change: improve_experiment, and
   say in plan_feedback exactly what to add, drop or change.
3. THE AGENT. If the plan was right but the agent carried it out badly -- a
   node that looped many times, a checker that waved bad work through, a
   step on a model too weak for it, a node missing an input it needed, a
   run that stopped blocked -- improve_agent, and say in design_feedback
   exactly which node and what to change.

Verdicts: done, improve_experiment, improve_agent, stop. Use stop when another
iteration would not help (the data does not exist, the hardware is not
there, the same failure has repeated across iterations). Earlier
iterations' reviews are in the report: do not ask for the same change twice
if it did not work the first time.

Each finding is one concrete sentence with the evidence: a file, a number, a
node name. Feedback is instructions someone else will follow without you.
""".strip()


# ---------------------------------------------------------------------------
# the report the review reads
# ---------------------------------------------------------------------------

#: Bounds, so a long run cannot make the review's prompt unbounded.
_FIELD_CHARS = 700
_MAX_FILES = 150


def iteration_dir(session_dir: str | Path, n: int) -> Path:
    return Path(session_dir) / ITERATIONS / str(n)


def next_iteration(session_dir: str | Path) -> int:
    """1 + the number of iterations already reviewed in this session."""
    root = Path(session_dir) / ITERATIONS
    done = [int(p.name) for p in root.iterdir()
            if p.is_dir() and p.name.isdigit()] if root.is_dir() else []
    return (max(done) + 1) if done else 1


def run_visits(session_dir: str | Path) -> list[dict]:
    """The timeline since the last supervisor review: this run's visits."""
    visits = activity.timeline(session_dir)
    last = max((i for i, v in enumerate(visits) if v.get("node") == ROLE), default=-1)
    return visits[last + 1:]


def run_report(*, session_dir: str | Path, artifacts_dir: str | Path, plan: dict,
               brief: str, values: dict, iteration: int) -> str:
    """Everything the review needs about the run, as one text."""
    from agent.bootstrap.nodes.planner import steps_for

    steps = plan.get("steps") or []
    ids = [s["id"] for s in steps] + [x["id"] for s in steps for x in s.get("substeps") or []]
    parts = [f"ITERATION {iteration}",
             f"THE BRIEF THE AGENT RAN ON:\n{_clip(brief, 6000)}",
             f"THE PLAN (with each step's check):\n{steps_for(plan, ids) or '(no plan)'}",
             f"HOW THE RUN ENDED: outcome={values.get('outcome') or '(none)'}"]

    visits = run_visits(session_dir)
    if visits:
        per_node: dict[str, dict[str, Any]] = {}
        for v in visits:
            row = per_node.setdefault(v["node"], {"visits": 0, "routes": Counter(),
                                                  "models": set(), "seconds": 0.0,
                                                  "tokens": 0})
            row["visits"] += 1
            row["routes"][str(v.get("route", ""))] += 1
            if v.get("model") or v.get("provider"):
                row["models"].add(f"{v.get('provider', '')}/{v.get('model', '')}")
            row["seconds"] += float(v.get("seconds") or 0)
            row["tokens"] += int(v.get("output_tokens") or 0)
        lines = []
        for node, row in per_node.items():
            routes = ", ".join(f"{k}x{n}" for k, n in row["routes"].most_common() if k)
            lines.append(f"  {node}: {row['visits']} visit(s), routes [{routes}], "
                         f"model {', '.join(sorted(row['models'])) or '-'}, "
                         f"{row['seconds']:.0f}s, {row['tokens']} output tokens")
        order = " -> ".join(v["node"] for v in visits[-60:])
        parts.append("NODE VISITS (from the timeline):\n" + "\n".join(lines)
                     + f"\n\nORDER (last 60): {order}")

    outputs = values.get("outputs") or {}
    if outputs:
        lines = []
        for node, data in outputs.items():
            lines.append(f"### {node}")
            for key, value in (data or {}).items():
                lines.append(f"{key}: {_clip(value if isinstance(value, str) else json.dumps(value), _FIELD_CHARS)}")
        parts.append("EACH NODE'S LAST OUTPUT:\n" + "\n".join(lines))

    artifacts = Path(artifacts_dir)
    if artifacts.is_dir():
        files = sorted(p for p in artifacts.rglob("*") if p.is_file())
        listed = [f"  {p.relative_to(artifacts)}  ({p.stat().st_size:,} bytes)"
                  for p in files[:_MAX_FILES]]
        more = f"\n  ... and {len(files) - _MAX_FILES} more" if len(files) > _MAX_FILES else ""
        parts.append(f"FILES UNDER THE ARTIFACTS DIRECTORY ({artifacts}):\n"
                     + ("\n".join(listed) or "  (none)") + more)

    earlier = []
    for n in range(1, iteration):
        path = iteration_dir(session_dir, n) / "review.json"
        if path.exists():
            review = json.loads(path.read_text())
            earlier.append(f"  iteration {n}: {review.get('verdict')} -- "
                           f"{_clip(review.get('summary', ''), 400)}; decided: "
                           f"{review.get('decision', '?')}")
    if earlier:
        parts.append("EARLIER ITERATIONS:\n" + "\n".join(earlier))
    return "\n\n".join(parts)


def archive(*, session_dir: str | Path, agent_dir: str | Path, iteration: int,
            report: str, review: dict) -> Path:
    """Keep what this iteration was: plan, agent, brief, timeline, review."""
    target = iteration_dir(session_dir, iteration)
    target.mkdir(parents=True, exist_ok=True)
    session = Path(session_dir)
    for name in ("plan.json", "brief.md"):
        if (session / name).exists():
            shutil.copy2(session / name, target / name)
    # str() check first: Path("") is the CURRENT directory, and copying that
    # would archive whatever the process happens to be running in.
    if str(agent_dir).strip() and Path(agent_dir).is_dir():
        shutil.copytree(agent_dir, target / "agent", dirs_exist_ok=True)
    (target / "timeline.jsonl").write_text(
        "".join(json.dumps(v, default=str) + "\n" for v in run_visits(session_dir)))
    (target / "report.md").write_text(report)
    (target / "review.json").write_text(json.dumps(review, indent=2) + "\n")
    return target


def _clip(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + f" ... [{len(text) - limit} more]"


# ---------------------------------------------------------------------------
# the graph: review -> decide
# ---------------------------------------------------------------------------

class SupervisorState(TypedDict, total=False):
    repo_path: str
    session_dir: str
    agent_dir: str
    iteration: int
    max_iterations: int
    mode: str
    report: str
    review: dict
    decision: str
    """finish | replan | redesign -- what the driver does next."""
    feedback: str
    """What the planner or designer is told, when the decision is not finish."""
    question: str
    error: str


SUPERVISOR_REGISTRY: tuple[SlashCommand, ...] = tuple(
    c for c in REGISTRY if c.scope == "terminal"
) + (
    SlashCommand(name="accept", scope="graph", contexts=("supervisor_review",),
                 summary="Do what the review recommends"),
    SlashCommand(name="replan", scope="graph", contexts=("supervisor_review",),
                 argument="[what to change]",
                 summary="Change the plan, then redesign and run again"),
    SlashCommand(name="redesign", scope="graph", contexts=("supervisor_review",),
                 argument="[what to change]",
                 summary="Keep the plan, redesign the agent, and run again"),
    SlashCommand(name="finish", scope="graph", contexts=("supervisor_review",),
                 aliases=("exit", "quit", "q"),
                 summary="Stop here; keep everything as it is"),
)


def build_supervisor_graph(backend, checkpointer):
    builder = StateGraph(SupervisorState)
    builder.add_node("review", _make_review(backend))
    builder.add_node("decide", _decide)
    builder.add_edge(START, "review")
    builder.add_edge("review", "decide")
    builder.add_conditional_edges("decide", lambda s: "decide" if s.get("error") else "end",
                                  {"decide": "decide", "end": END})
    return builder.compile(checkpointer=checkpointer)


def _make_review(backend):
    def review(state: SupervisorState) -> dict:
        print(f"\n[supervisor] reviewing iteration {state.get('iteration', 1)}...")
        session_dir = state.get("session_dir", "")
        prompt = (f"{state.get('report', '')}\n\nThe repository is at "
                  f"{state.get('repo_path', '')}. Review the run and decide.")
        sent = activity.exchange(thread_id=None, instructions=SUPERVISOR_INSTRUCTIONS,
                                 prompt=prompt)
        activity.arm_progress(backend, session_dir, ROLE, sent=sent)
        try:
            run = backend.run_structured(thread_id=None, repo_path=state["repo_path"],
                                         access=Access.READ_ONLY,
                                         developer_instructions=SUPERVISOR_INSTRUCTIONS,
                                         prompt=prompt, output_model=SupervisorReview)
        finally:
            activity.disarm_progress(backend, session_dir, ROLE)
        data = run.data.model_dump(mode="json")
        activity.write(session_dir, ROLE, run.events, sent=sent,
                       summary=activity.scalars(run.data),
                       usage=usage_to_dict(run.usage) if run.usage else None,
                       backend=backend, thread_id=run.thread_id, output=data)
        # Marks the end of this run in the timeline: run_visits() reads
        # everything after the last supervisor visit as the next run.
        activity.record_visit(session_dir, ROLE, kind="supervisor",
                              iteration=state.get("iteration", 1),
                              verdict=data["verdict"],
                              provider=getattr(backend, "name", ""),
                              model=getattr(backend, "model", "") or "",
                              thread=run.thread_id)
        print(f"[supervisor] {data['verdict']}: {data['summary']}")
        for finding in data["findings"]:
            print(f"[supervisor]   - {finding}")
        return {"review": data}

    return review


def _decide(state: SupervisorState) -> dict:
    review = dict(state.get("review") or {})
    iteration = int(state.get("iteration", 1))
    limit = int(state.get("max_iterations", 3))
    suggested = ACTIONS.get(review.get("verdict", ""), "finish")
    at_limit = iteration >= limit

    if state.get("mode") == "auto":
        decision = "finish" if at_limit else suggested
        why = (f" (iteration limit {limit} reached)" if at_limit and suggested != "finish"
               else "")
        print(f"[supervisor] auto: {decision}{why}")
        return _decided(state, review, decision, "")

    question = (
        f"{state.get('error', '')}"
        f"Iteration {iteration} reviewed: {review.get('verdict')}.\n\n"
        f"{review.get('summary', '')}\n\n"
        + "".join(f"- {f}\n" for f in review.get("findings", []))
        + f"\nRecommended: {suggested}"
        + (f" -- but this is iteration {iteration} of {limit}" if at_limit else "")
        + ".\n\n/accept to do that, /replan [what] to change the plan, "
          "/redesign [what] to redesign the agent, or /finish."
    )
    payload = {"type": "human_input", "purpose": "supervisor_review",
               "question": question,
               "context": "\n\n".join(x for x in (
                   f"plan feedback: {review.get('plan_feedback')}" if review.get("plan_feedback") else "",
                   f"design feedback: {review.get('design_feedback')}" if review.get("design_feedback") else "",
               ) if x)}

    # ---- STOP. Everything below runs only after a resume. ----------------
    answer = str(interrupt(payload)).strip()
    parsed = command_lib.parse(answer, purpose="supervisor_review",
                               registry=SUPERVISOR_REGISTRY)
    if parsed.kind != "command":
        error = (parsed.error if parsed.kind == "rejected"
                 else "Answer with a command -- plain text is not a decision.")
        return {"error": error + "\n\n", "question": question}
    name = parsed.command.name
    decision = {"accept": suggested, "replan": "replan", "redesign": "redesign",
                "finish": "finish"}[name]
    return _decided(state, review, decision, parsed.text)


def _decided(state, review: dict, decision: str, note: str) -> dict:
    feedback = feedback_for(review, decision, note)
    review = {**review, "decision": decision, "note": note}
    archive(session_dir=state.get("session_dir", ""), agent_dir=state.get("agent_dir", ""),
            iteration=int(state.get("iteration", 1)), report=state.get("report", ""),
            review=review)
    return {"decision": decision, "feedback": feedback, "error": "", "review": review}


def feedback_for(review: dict, decision: str, note: str = "") -> str:
    """What the planner or designer is told for the next iteration."""
    if decision == "finish":
        return ""
    parts = [f"A supervisor reviewed the last run ({review.get('verdict')}): "
             f"{review.get('summary', '')}"]
    parts += [f"- {f}" for f in review.get("findings", [])]
    if decision == "replan" and review.get("plan_feedback"):
        parts.append(f"Change the plan: {review['plan_feedback']}")
    if review.get("design_feedback"):
        parts.append(f"Change the agent: {review['design_feedback']}")
    if note:
        parts.append(f"The person adds: {note}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# the session adapter the drivers use
# ---------------------------------------------------------------------------

def supervisor_session(*, graph, thread_id: str, initial_state: dict) -> GraphSession:
    def resume_payload(values: dict) -> dict | None:
        if not values.get("review") or values.get("decision"):
            return None
        return {"purpose": "supervisor_review",
                "question": values.get("question") or "Decide what to do next: "
                            "/accept, /replan, /redesign or /finish.",
                "context": ""}

    def state_report(values: dict, full: bool) -> str:
        review = values.get("review") or {}
        return (f"{'phase':20} supervising\n"
                f"{'iteration':20} {values.get('iteration')}\n"
                f"{'verdict':20} {review.get('verdict', '(reviewing)')}\n"
                f"{'decision':20} {values.get('decision') or '(waiting)'}")

    return GraphSession(
        title=f"supervisor: reviewing iteration {initial_state.get('iteration', 1)}",
        graph=graph,
        config={"configurable": {"thread_id": thread_id}, "recursion_limit": 25},
        registry=SUPERVISOR_REGISTRY,
        initial_state=initial_state,
        resume_payload=resume_payload,
        state_report=state_report,
        transcript=lambda values: [],
        usage=lambda values: {},
        topology="graph TD;\n  __start__ --> review;\n  review --> decide;\n"
                 "  decide -. decision .-> __end__;",
    )
