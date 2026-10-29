"""
WHAT:  The execution stage's nodes as plain functions -- the discussor, the
       orchestrator, a step's owner, the validator -- and the approved design
       read as the graph's nodes (`Design`).
WHY:   The design is data: one orchestrator, one read-only validator, and
       owners (worker, writer) of the plan's steps. Code interprets
       it; the models do the work inside each node. What a model may do is
       checked here: an orchestrator can only make the moves code opened, an
       owner's files must exist, only a validator's pass passes a step.
CONCEPT: Sessions, in <run>/rb:
    discussor               the bootstrap's conversation, continued
    orchestrator-NNN        fresh for each decision
    step05-a1               a step's owner, one per attempt -- continued when
                            its jobs finish, so it remembers what it launched
    validate05-a1-NNN       fresh for each verdict
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from research_agent import run as run_base
from research_agent import state as S
from research_agent.bootstrap.config import RoleConfig
from research_agent.bootstrap.nodes import DISCUSSOR_SESSION, Outcome, design_problems
from research_agent import chat, inbox, prompt_files
from research_agent.execute import config, jobs, prompts, schemas, views

NodeFailed = run_base.NodeFailed

#: Graph node names the design's own names must not take.
RESERVED = {"discussor", "orchestrator", "validator", "wait", "human", "paper", "reviewer"}
#: Where a step's status says it is finished, one way or another.
SETTLED = {"pass", "skipped"}
#: What the user can give another attempt: a step that failed or was
#: stopped -- or skipped, which reopens the steps skipped after it.
REOPENABLE = ("fail", "stopped", "skipped")


_KEEP = object()


@dataclass
class ExecRun(run_base.Run):
    """One run's execution: the bootstrap's run folder, with the execution
    roles, the limits, and the bootstrap's discussor."""

    limits: config.ExecConfig | None = None

    def __post_init__(self):
        super().__post_init__()
        self.instructions = {**prompts.instructions(self.folder), **self.instructions}
        if self.limits is None:
            self.limits = config.defaults()

    @classmethod
    def from_config(cls, folder: Path, cfg: config.ExecConfig, discussor: RoleConfig, *,
                    progress=False, trace=False, discussor_skills=_KEEP) -> "ExecRun":
        if discussor_skills is _KEEP:
            from research_agent.bootstrap import config as boot_config

            try:
                discussor_skills = boot_config.load(folder).skills.get("discussor")
            except ValueError:
                discussor_skills = None
        return cls(folder, {**cfg.roles, "discussor": discussor}, skills_dir=cfg.skills_dir,
                   skills={"discussor": discussor_skills}, progress=progress, trace=trace,
                   limits=cfg)

    @property
    def work(self) -> Path:
        return self.folder / "work"

    def artifacts(self, step_id: str) -> Path:
        return self.folder / "artifacts" / views.folder_name(step_id)

    def paths(self, step_id: str) -> dict:
        return {"run": str(self.folder), "work": str(self.work),
                "artifacts": str(self.artifacts(step_id)), "python": self.limits.python}

    @property
    def jobs_command(self) -> str:
        return f"{sys.executable} {jobs.__file__} --run {self.folder}"

    def known_skills(self) -> set[str]:
        return {name for name, _ in self.catalogue()}


# ---- the design, read as the graph's nodes ------------------------------------------------


@dataclass
class Design:
    orchestrator: dict
    validator: dict
    owners: dict[str, dict]
    """Graph node name -> the design node."""
    owner_of: dict[str, str]
    """Plan step id -> its owner's graph node name."""

    @classmethod
    def read(cls, st: dict, *, skills: set[str] = frozenset()) -> "Design":
        """The approved design as nodes. Raises ValueError with its problems --
        the bootstrap checked it, but the file may have been edited since."""
        design = st.get("design") or {}
        design = {**design, "nodes": [{**n, "role": role_of(n)} for n in design.get("nodes") or []]}
        steps = [s["id"] for s in (st.get("plan") or {}).get("steps") or []]
        problems = design_problems(design, steps=steps, skills=set(skills),
                                   rubric={r["id"] for r in st.get("rubric") or []})
        problems += [f"{n['name']} is a {n['role']}: an execution node is orchestrator, "
                     f"validator, worker or writer"
                     for n in design.get("nodes") or []
                     if n["role"] not in ("orchestrator", "validator", *config.OWNERS)]
        if problems:
            raise ValueError("The design cannot be run:\n" + "\n".join(f"- {p}" for p in problems))
        nodes = design["nodes"]
        # Steps the design gives its orchestrator (a preflight, say) are done
        # by a step owner made from that node: the orchestrator itself only
        # decides. The validator owns none: it cannot write.
        owners = {graph_name(n["name"]): n if n["role"] in config.OWNERS else {**n, "role": "worker"}
                  for n in nodes if n["role"] in config.OWNERS
                  or (n["role"] == "orchestrator" and n.get("steps"))}
        return cls(orchestrator=next(n for n in nodes if n["role"] == "orchestrator"),
                   validator=next(n for n in nodes if n["role"] == "validator"),
                   owners=owners,
                   owner_of={s: name for name, n in owners.items() for s in n.get("steps") or []})


def role_of(node: dict) -> str:
    """A design node's role -- a "runner" of an earlier design is a worker."""
    return config.RETIRED.get(node.get("role", ""), node.get("role", ""))


def graph_name(name: str) -> str:
    return f"node_{name}" if name in RESERVED else name


_DESIGN = object()


def skills_for(run: ExecRun, node: dict, key: str = ""):
    """A node's skills: the user's choice for it (execute.json "nodes", by
    graph node), else the design's -- None, "auto", or the names that are in
    the folder."""
    choice = (run.limits.node_skills or {}).get(key or graph_name(node["name"]), _DESIGN)
    if choice is _DESIGN:
        choice = list(node.get("skills") or [])
    if choice is None or choice == "auto":
        return choice if run.known_skills() else None
    known = run.known_skills()
    return [s for s in choice if s in known] or None


def access_for(node: dict) -> str:
    """A step owner runs commands -- on the GPUs, on the network, writing
    outside its workspace (artifacts, jobs) -- which research_backend calls
    `full`, whatever access the design gave it: measured, a preflight owner
    designed read_only ran in a sandbox with no network, GPUs or writes, and
    reported the machine as having none. Only the validator is read-only."""
    return "full"


# ---- the steps: what may happen next -----------------------------------------------------


def records(st: dict) -> dict[str, dict]:
    return {r["id"]: r for r in (st.get("execution") or {}).get("steps") or []}


def plan_step(st: dict, step_id: str) -> dict | None:
    return next((s for s in (st.get("plan") or {}).get("steps") or [] if s["id"] == step_id), None)


def record_op(st: dict, step_id: str, **fields) -> dict:
    """The op that changes one step's record."""
    steps = [dict(r, **fields) if r["id"] == step_id else r
             for r in (st.get("execution") or {}).get("steps") or []]
    return {"op": "merge", "section": "execution", "value": {"steps": steps}}


def open_moves(st: dict, max_attempts: int) -> dict:
    """{"run": [...], "retry": [...], "skip": [...], "done": bool} -- what the
    orchestrator may do now. ask_user and stop are always open."""
    rec = records(st)
    moves = {"run": [], "retry": [], "skip": [], "done": bool(rec) and all(
        r["status"] in SETTLED for r in rec.values())}
    for s in (st.get("plan") or {}).get("steps") or []:
        r = rec.get(s["id"])
        if r is None:
            continue
        ready = all(rec.get(d, {}).get("status") in SETTLED for d in s.get("depends_on") or [])
        if r["status"] == "pending" and ready:
            moves["run"].append(s["id"])
        if r["status"] in ("fail", "stopped") and ready and \
                r.get("attempts", 0) < max_attempts + r.get("extra_attempts", 0):
            moves["retry"].append(s["id"])
        if (r["status"] == "pending" and ready) or (r["status"] in ("fail", "stopped")
                                                    and not dependents(st, s["id"])):
            # A failed step that others need is the user's call, not a skip --
            # nor is skipping the step after it: measured, both skipped round
            # a failed preflight, and the next step ran without its inputs.
            # A pending step is skipped only once its own inputs are settled
            # (a branch that does not apply).
            moves["skip"].append(s["id"])
    return moves


def dependents(st: dict, step_id: str) -> list[str]:
    """The steps that need `step_id`, directly or through others, in plan order."""
    steps = (st.get("plan") or {}).get("steps") or []
    found = {step_id}
    for s in steps:
        if found & set(s.get("depends_on") or []):
            found.add(s["id"])
    return [s["id"] for s in steps if s["id"] in found and s["id"] != step_id]


def describe_moves(moves: dict, st: dict, max_attempts: int) -> list[str]:
    rec = records(st)
    lines = [f"run {s}" for s in moves["run"]]
    lines += [f"retry {s} (attempt {rec[s].get('attempts', 0) + 1} of "
              f"{max_attempts + rec[s].get('extra_attempts', 0)})"
              for s in moves["retry"]]
    if moves["skip"]:
        lines.append(f"skip one of: {', '.join(moves['skip'])}")
    lines += ["ask_user", "stop"] + (["done"] if moves["done"] else [])
    return lines


def needs_go(step: dict, confirm: str) -> bool:
    return confirm == "all" or (confirm == "gated" and bool(step.get("gate")))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _session(step_id: str, attempt: int) -> str:
    return f"step{views.folder_name(step_id)[5:]}-a{attempt}"


# ---- discussor ------------------------------------------------------------------------------


@dataclass
class DiscussorTurn(Outcome):
    reply: str = ""
    requests: list[str] = field(default_factory=list)
    hand_to: str = "user"
    go: bool = False
    stop: bool = False
    retry: list[str] = field(default_factory=list)
    waive: list[dict] = field(default_factory=list)
    rewind: str = ""


# ---- rewinding -------------------------------------------------------------------------

#: Where a rewind keeps the outputs of the steps it starts over.
REWOUND = "_rewound"


def rewind_problems(folder: Path, st: dict, step_id: str) -> list[str]:
    """Why the execution cannot go back to `step_id` now (empty: it can)."""
    rec = records(st)
    if step_id not in rec:
        return [f"rewind: there is no step {step_id}"]
    if rec[step_id]["status"] == "pending":
        return [f"rewind: step {step_id} has not run yet"]
    affected = [step_id, *dependents(st, step_id)]
    busy = [f"step {s} ({rec[s]['status']})" for s in affected
            if rec.get(s, {}).get("status") in ("running", "waiting", "validating")]
    busy += [f"job {j['id']} of step {j['step']}" for s in affected
             for j in jobs.records(folder, s) if jobs.status(j)["state"] == "running"]
    return [f"rewind: still running: {', '.join(busy)} -- stop the run first"] if busy else []


def rewind(folder: Path, st: dict, step_id: str, *, by: str = "user") -> list[dict]:
    """Start over from `step_id`: it and every step that needs it go back to
    pending with fresh attempts. Their outputs move to
    artifacts/_rewound/<time>-to-step-<id>/, so nothing is lost. Returns the
    state changes; moves the files."""
    affected = [step_id, *dependents(st, step_id)]
    rec = records(st)
    redo = [s for s in affected if rec.get(s, {}).get("status") != "pending"]
    keep = Path(folder) / "artifacts" / REWOUND / f"{datetime.now():%Y%m%d-%H%M%S}-to-step-{step_id}"
    for s in redo:
        out = Path(folder) / "artifacts" / views.folder_name(s)
        if out.exists() and any(out.iterdir()):
            keep.mkdir(parents=True, exist_ok=True)
            shutil.move(str(out), str(keep / out.name))
    ops = []
    for s in redo:
        ops.append(record_op(st, s, status="pending", attempts=0, extra_attempts=0, go=False,
                             verdict="", summary="", problems=[], artifacts=[], jobs=[],
                             sessions=[], started="", finished="", gpu_hours=0.0))
        st = S.apply(st, ops[-1:], by=by)[0]
    ops.append({"op": "add", "section": "decisions", "value": {
        "what": f"Rewind to step {step_id}: steps {', '.join(redo)} start over",
        "why": f"the {by} asked for it" + (f"; earlier outputs kept in {keep}"
                                           if keep.exists() else ""), "by": by}})
    return ops


def step_problems(st: dict, retry, waive, max_attempts: int | None = None) -> list[str]:
    """Another attempt, or a pass by the user, is for a step that failed or
    was stopped (a retry also for a skipped one) -- and, given the limit, a
    retry only for a step out of attempts: one already granted is not
    granted twice."""
    rec = records(st)
    if max_attempts is not None:
        retry = list(retry)
        left = [s for s in retry if rec.get(s, {}).get("status") in ("fail", "stopped")
                and rec[s].get("attempts", 0) < max_attempts + rec[s].get("extra_attempts", 0)]
        if left:
            return [f"retry: step {s} still has an attempt left (the orchestrator will use "
                    f"it); leave `retry` empty" for s in left]
    problems = [f"retry: step {s} has not failed or been skipped (it is "
                f"{rec[s]['status'] if s in rec else 'not a step'})"
                for s in retry if rec.get(s, {}).get("status") not in REOPENABLE]
    steps = [w["step"] if isinstance(w, dict) else w.step for w in waive]
    problems += [f"waive: step {s} has not failed (it is "
                 f"{rec[s]['status'] if s in rec else 'not a step'})"
                 for s in steps if rec.get(s, {}).get("status") not in ("fail", "stopped")]
    return problems


def out_of_attempts(st: dict, max_attempts: int, run_dir=None) -> str:
    """The failed steps no attempt is left for, in words: only the user's
    `retry` gives one -- measured, models took "the retry is authorized" in
    words for an attempt, and waited for one that never came."""
    lines = [f"- step {r['id']} {r['status']}: {r.get('attempts', 0)} of "
             f"{max_attempts + r.get('extra_attempts', 0)} attempts used"
             for r in records(st).values()
             if r["status"] in ("fail", "stopped")
             and r.get("attempts", 0) >= max_attempts + r.get("extra_attempts", 0)]
    if not lines:
        return ""
    return prompt_files.text("notes/out_of_attempts", run_dir) + "\n" + "\n".join(lines)


def user_step_ops(st: dict, retry, waive, by: str = "user") -> list[dict]:
    """The user's call on failed steps, as changes: another attempt beyond
    the limit, or a pass -- kept as the verdict, with their reason."""
    ops = []
    for sid in dict.fromkeys(retry):
        r = records(st).get(sid)
        if not r:
            continue
        reopen = {"status": "stopped"} if r["status"] == "skipped" else {}
        ops.append(record_op(st, sid, extra_attempts=r.get("extra_attempts", 0) + 1, **reopen))
        st = S.apply(st, ops[-1:], by="user")[0]
        if reopen:
            # The steps skipped because this one was wait for it again.
            for later in dependents(st, sid):
                if records(st).get(later, {}).get("status") == "skipped":
                    ops.append(record_op(st, later, status="pending"))
                    st = S.apply(st, ops[-1:], by="user")[0]
        ops.append({"op": "add", "section": "decisions", "value": {
            "what": f"Another attempt at step {sid}", "why": f"the {by} asked for it", "by": by}})
        st = S.apply(st, ops[-1:], by="user")[0]
    for w in waive:
        sid, why = (w["step"], w["why"]) if isinstance(w, dict) else (w.step, w.why)
        r = records(st).get(sid)
        if not r:
            continue
        ops.append(record_op(st, sid, status="pass", finished=_now(),
                             verdict=f"passed by the user: {why} (the validator's verdict was: "
                                     f"{r.get('verdict', '')[:600]})"))
        st = S.apply(st, ops[-1:], by="user")[0]
        ops.append({"op": "add", "section": "decisions", "value": {
            "what": f"Step {sid} passes on the {by}'s decision", "why": why, "by": by}})
        st = S.apply(st, ops[-1:], by="user")[0]
    return ops


def breakpoint_problems(st: dict, breakpoints) -> list[str]:
    steps = {s["id"] for s in (st.get("plan") or {}).get("steps") or []}
    return [f"breakpoints: {b.step} is not a plan step" for b in breakpoints if b.step not in steps]


def breakpoint_ops(st: dict, breakpoints) -> list[dict]:
    """Each breakpoint as a change to its step's record."""
    ops = []
    for b in breakpoints:
        ops.append(record_op(st, b.step, **{f"pause_{b.when}": b.on}))
        st = _apply_one(st, ops[-1])
    return ops


def _apply_one(st: dict, op: dict) -> dict:
    return S.apply(st, [op], by="discussor")[0]


def discussor(run: ExecRun, st: dict, *, seen: dict, user: str = "", note: str = "",
              awaiting: str = "") -> DiscussorTurn:
    open_ids = {q["id"] for q in st.get("open_questions") or [] if q.get("status") == "open"}

    def check(out: schemas.ExecDiscussorOutput) -> list[str]:
        problems = [f"answers: {a.id} is not an open question" for a in out.answers
                    if a.id not in open_ids]
        if out.go and not awaiting:
            problems.append("go: no step is waiting for the user's go")
        if out.hand_to == "orchestrator" and not (out.requests or out.go or out.stop
                                                  or out.retry or out.waive or out.rewind):
            problems.append('hand_to is "orchestrator" but there is no request or go for the '
                            'team to act on: add the request, or hand to "user"')
        if out.rewind:
            problems += rewind_problems(run.folder, st, out.rewind)
        return (problems + breakpoint_problems(st, out.breakpoints)
                + step_problems(st, out.retry, out.waive, run.limits.max_attempts))

    budget = run.brief_budget("discussor")
    brief = views.discussor_turn(st, seen, user=user, note=note, awaiting=awaiting,
                                 budget=budget)

    def takeover() -> str:
        # A new conversation: what was said, and the whole state -- both
        # within the brief's budget on the model now taking it.
        room = run.brief_budget("discussor")
        said = chat.handover(run.folder, room // 3)
        return said + "\n\n" + views.discussor_turn(st, {}, user=user, note=note,
                                                     awaiting=awaiting, budget=room - len(said))
    if run.takes_over(DISCUSSOR_SESSION, "discussor"):
        brief = takeover()
    stuck = out_of_attempts(st, run.limits.max_attempts, run.folder)
    if stuck:
        brief += "\n\n" + stuck
    with run.discussor_lock:
        out, session = run.call("discussor", brief=brief, schema=schemas.ExecDiscussorOutput,
                                session=DISCUSSOR_SESSION, check=check, takeover=takeover)
    ops = breakpoint_ops(st, out.breakpoints)
    ops += [{"op": "add", "section": "decisions", "value": {**d.model_dump(), "by": "user"}}
            for d in out.decisions]
    ops += [{"op": "update", "section": "open_questions", "id": a.id,
             "value": {"status": "answered", "answer": a.answer}} for a in out.answers]
    return DiscussorTurn(ops=ops, summary=out.reply[:200], session=session, data=out,
                         reply=out.reply, requests=list(out.requests), hand_to=out.hand_to,
                         go=out.go and bool(awaiting), stop=out.stop, retry=list(out.retry),
                         waive=[w.model_dump() for w in out.waive], rewind=out.rewind)


def aside(run: ExecRun, st: dict, *, user: str, activity: str) -> schemas.AsideOutput:
    """The user writes while the team works. The answer is the discussor's;
    what it records goes to the inbox, for the graph to apply at its next
    step -- the state here may already be behind."""

    def check(out: schemas.AsideOutput) -> list[str]:
        return (breakpoint_problems(st, out.breakpoints)
                + step_problems(st, out.retry, out.waive, run.limits.max_attempts)
                + (rewind_problems(run.folder, st, out.rewind) if out.rewind else []))

    budget = run.brief_budget("discussor")
    plain = views.aside(st, user=user, activity=activity, run_dir=str(run.folder),
                        budget=budget * 2 // 3)

    def takeover() -> str:
        return chat.handover(run.folder, run.brief_budget("discussor") // 3) + "\n\n" + plain
    brief = takeover() if run.takes_over(DISCUSSOR_SESSION, "discussor") else plain
    with run.discussor_lock:
        out, _ = run.call("discussor", brief=brief,
                          schema=schemas.AsideOutput, session=DISCUSSOR_SESSION, check=check,
                          label="discussor (aside)", takeover=takeover)
    for b in out.breakpoints:
        inbox.post(run.folder, "breakpoint", step=b.step, when=b.when, on=b.on)
    for r in out.requests:
        inbox.post(run.folder, "request", text=r)
    for d in out.decisions:
        inbox.post(run.folder, "decision", what=d.what, why=d.why)
    for sid in out.retry:
        inbox.post(run.folder, "retry", step=sid)
    for w in out.waive:
        inbox.post(run.folder, "waive", step=w.step, why=w.why)
    if out.rewind:
        inbox.post(run.folder, "rewind", step=out.rewind)
    if out.stop:
        inbox.post(run.folder, "stop", why="the user stopped the execution")
    return out


# ---- orchestrator ---------------------------------------------------------------------------


def decision_check(st: dict, limit: int, run_dir=None):
    """What code requires of an execution decision in this state -- also used
    to replay a recorded call (research_agent/refine)."""
    moves = open_moves(st, limit)
    untried = [s for s in moves["run"] if not records(st)[s].get("attempts")]
    asked_once = []

    def check(out: schemas.ExecDecision) -> list[str]:
        if out.action == "ask_user" and out.step in moves["retry"] and not asked_once:
            asked_once.append(out.step)
            return [prompt_files.text("notes/retry_not_ask", run_dir, step=out.step)]
        if out.action == "ask_user" and out.step in untried and not asked_once:
            # Once per decision, so a real question still gets through.
            # Measured: the read-only discussor probed the machine, found no
            # GPUs in its own sandbox, and the orchestrator asked the user
            # for "a writable environment" instead of running the preflight.
            asked_once.append(out.step)
            return [prompt_files.text("notes/run_not_ask", run_dir, step=out.step)]
        if out.action in ("run", "retry", "skip") and out.step not in moves[out.action]:
            return [f"{out.action} {out.step or '(no step)'} is not open now; open moves: "
                    + "; ".join(describe_moves(moves, st, limit))]
        if out.action == "done" and not moves["done"]:
            return ["done: not every step has passed or been skipped"]
        if out.action in ("skip", "ask_user", "stop") and not out.task.strip():
            return [f"{out.action} needs a `task` (" + {
                "skip": "why -- it is the step's terminal record", "ask_user": "what to ask",
                "stop": "why"}[out.action] + ")"]
        return []
    return check


def orchestrator(run: ExecRun, st: dict, design: Design, *, budget: str, requests: list[str],
                 last: str = "") -> Outcome:
    limit = run.limits.max_attempts
    moves = open_moves(st, limit)
    check = decision_check(st, limit, run.folder)
    brief = views.orchestrator(
        st, design_part=design.orchestrator.get("instructions", ""),
        moves=describe_moves(moves, st, limit), budget=budget, requests=requests, last=last)
    out, session = run.call("orchestrator", brief=brief, schema=schemas.ExecDecision,
                            skills=skills_for(run, design.orchestrator, "orchestrator"),
                            check=check)
    ops = [{"op": "merge", "section": "status", "value": {"summary": out.summary,
                                                          "next": f"{out.action} {out.step}".strip()}}]
    ops += [{"op": "add", "section": "open_questions", "value": q.model_dump(by_alias=True)}
            for q in out.open_questions]
    ops += [{"op": "add", "section": "decisions", "value": {**d.model_dump(), "by": "orchestrator"}}
            for d in out.decisions]
    return Outcome(ops=ops, summary=f"{out.action} {out.step}: {out.reason}".strip(),
                   session=session, data=out)


# ---- a step's owner -------------------------------------------------------------------------


def owner(run: ExecRun, st: dict, *, node: dict, step: dict, task: str = "",
          wake: list[str] | None = None) -> Outcome:
    """One turn of a step's owner: its first (the brief), or after its jobs
    finished (`wake`: their ids), in the same session."""
    rec = records(st)[step["id"]]
    folder = run.artifacts(step["id"])
    folder.mkdir(parents=True, exist_ok=True)
    run.work.mkdir(parents=True, exist_ok=True)
    hypotheses = {h["id"] for h in st.get("hypotheses") or []}

    def check(out: schemas.StepReport) -> list[str]:
        problems = []
        if out.state == "launched":
            if not out.jobs:
                problems.append("launched, but `jobs` is empty: give the ids the jobs helper "
                                "printed, or launch the jobs first")
            for job_id in out.jobs:
                job = jobs.get(run.folder, job_id)
                if job is None:
                    problems.append(f"there is no job {job_id} (launch it with the jobs helper)")
                elif job["step"] != step["id"]:
                    problems.append(f"job {job_id} is step {job['step']}'s, not this step's")
        if out.state == "done":
            if not out.artifacts:
                problems.append("done, but `artifacts` is empty: list the files that show "
                                "the step's check holds")
            problems += [f"artifact {a} does not exist (looked in {folder} and {run.folder})"
                         for a in out.artifacts if not resolve(run, folder, a)]
        if out.hypotheses and node["role"] != "writer":
            problems.append("hypotheses are judged by the final analysis (a writer), not here")
        problems += [f"{h.id} is not a hypothesis id" for h in out.hypotheses
                     if h.id not in hypotheses]
        return problems

    if wake:
        brief = views.owner_wake(run.folder, wake)
    else:
        brief = views.owner(st, node=node, step=step, task=task, paths=run.paths(step["id"]),
                            jobs_command=run.jobs_command,
                            previous=rec if rec.get("attempts", 0) > 1 else None)
    out, session = run.call(node["role"], brief=brief, schema=schemas.StepReport,
                            skills=skills_for(run, node), access=access_for(node),
                            workdir=run.work, label=f"step {step['id']} ({node['name']})",
                            session=_session(step["id"], rec.get("attempts", 1) or 1),
                            check=check)
    _commit_work(run.work, f"step {step['id']}, attempt {rec.get('attempts', 1)}: "
                           f"{out.state} -- {out.summary[:200]}")
    ops = [{"op": "add", "section": "evidence", "value": {
        "claim": f.claim, "source": f.source, "bears_on": list(f.bears_on),
        "found_by": node["name"]}} for f in out.findings]
    ops += [{"op": "add", "section": "risks", "value": r.model_dump()} for r in out.risks]
    ops += [{"op": "add", "section": "open_questions", "value": {"text": q, "for": "user"}}
            for q in out.questions_for_user]
    summary = f"step {step['id']} {out.state}: {out.summary}"
    ops.append({"op": "add", "section": "results", "value": {
        "node": node["name"], "job": f"step {step['id']}", "asked": step["title"][:500],
        "summary": summary[:1500], "session": session}})
    return Outcome(ops=ops, summary=summary, session=session, data=out)


def resolve(run: ExecRun, folder: Path, path: str) -> Path | None:
    """An artifact's file: in the step's folder, the run folder or the workspace."""
    p = Path(path).expanduser()
    for candidate in ([p] if p.is_absolute() else [folder / p, run.folder / p, run.work / p]):
        if candidate.exists():
            return candidate
    return None


def _commit_work(work: Path, message: str) -> None:
    """Commit the workspace, so each step's code is kept exactly as it ran.
    Best effort: no git, no commit."""
    try:
        if not (work / ".git").exists():
            subprocess.run(["git", "init", "-q"], cwd=work, check=True, timeout=60)
        subprocess.run(["git", "add", "-A"], cwd=work, check=True, timeout=300)
        subprocess.run(["git", "-c", "user.name=research_agent", "-c",
                        "user.email=research_agent@localhost", "commit", "-q",
                        "--allow-empty", "-m", message], cwd=work, check=True, timeout=300,
                       capture_output=True)
    except (OSError, subprocess.SubprocessError):
        pass


# ---- validator ------------------------------------------------------------------------------


def validator(run: ExecRun, st: dict, design: Design, *, node: dict, step: dict,
              report: dict) -> Outcome:
    rec = records(st)[step["id"]]
    rubric = {r["id"] for r in st.get("rubric") or []}

    def check(out: schemas.Verdict) -> list[str]:
        problems = [f"{c.id} is not a rubric id" for c in out.rubric if c.id not in rubric]
        if out.verdict != "pass" and not (out.problems or out.advice.strip()):
            problems.append(f"{out.verdict}: say what is wrong (`problems`) and what a retry "
                            f"must do (`advice`)")
        return problems

    brief = views.validator(st, design_part=design.validator.get("instructions", ""), node=node,
                            step=step, report=report, paths=run.paths(step["id"]))
    attempt = rec.get("attempts", 1) or 1
    out, session = run.call("validator", brief=brief, schema=schemas.Verdict,
                            skills=skills_for(run, design.validator, "validator"),
                            access="read_only",
                            workdir=run.folder, label=f"validate step {step['id']}",
                            session=run.next_name(f"validate{_session(step['id'], attempt)[4:]}"),
                            check=check)
    passed = out.verdict == "pass"
    unmet = [f"{c.id}: {c.note}" for c in out.rubric if c.met == "no"]
    verdict = f"{out.verdict}: {out.summary}"
    ops = [record_op(st, step["id"], status="pass" if passed else "fail", verdict=verdict[:1500],
                     problems=(list(out.problems) + unmet)[:20]
                     + ([f"advice: {out.advice}"] if out.advice and not passed else []),
                     finished=_now() if passed else "")]
    if passed:
        # What the final analysis concluded stands once it is checked.
        ops += [{"op": "update", "section": "hypotheses", "id": h["id"],
                 "value": {"status": h["status"]}} for h in report.get("hypotheses") or []]
    ops.append({"op": "add", "section": "results", "value": {
        "node": design.validator["name"], "job": f"validate step {step['id']}",
        "asked": step["title"][:500], "summary": verdict[:1500], "session": session}})
    return Outcome(ops=ops, summary=f"step {step['id']} {verdict}", session=session, data=out)


# ---- reviewer -------------------------------------------------------------------------------


def paper(run: ExecRun, st: dict) -> Outcome:
    """The paper of the finished study, in <run>/paper (main.tex, main.pdf),
    written on the writer's model once every step is settled."""
    folder = run.folder / "paper"
    folder.mkdir(parents=True, exist_ok=True)
    latex = config.find_latex(run.limits)

    def check(out: schemas.PaperReport) -> list[str]:
        pdf = Path(out.pdf) if out.pdf else None
        if pdf and not pdf.is_absolute():
            pdf = folder / pdf
        if pdf and not (pdf.is_file() and pdf.stat().st_size):
            return [f"there is no PDF at {out.pdf}: compile main.tex, or leave `pdf` empty "
                    f"and say why in `problems`"]
        if not pdf and latex and not out.problems:
            return [f"`pdf` is empty: compile {folder / 'main.tex'} with {latex}, or say in "
                    f"`problems` what stops it"]
        return []

    out, session = run.call("writer", brief=views.paper(st, run_dir=str(run.folder), latex=latex,
                                                        python=run.limits.python),
                            schema=schemas.PaperReport, access="full", workdir=folder,
                            instructions=run.instructions["paper"],
                            skills=skills_for(run, {"name": "paper",
                                                    "skills": list(run.limits.paper_skills)},
                                              "paper"),
                            check=check, label="paper")
    pdf = str(Path(out.pdf) if Path(out.pdf).is_absolute() else folder / out.pdf) if out.pdf else ""
    ops = [{"op": "merge", "section": "status", "value": {"paper": pdf}},
           {"op": "add", "section": "results", "value": {
               "node": "paper", "job": "paper", "asked": "write the paper",
               "summary": (f"{pdf}: " if pdf else "no PDF: ") + out.summary[:1400]
               + "".join(f"\n- {p}" for p in out.problems[:5]),
               "session": session}}]
    return Outcome(ops=ops, summary=f"paper: {pdf or 'no PDF'}", session=session, data=out)


def reviewer(run: ExecRun, st: dict) -> Outcome:
    """A conference-style review of the finished study, kept in the state
    (reviews) and as <run>/reviews/V<n>.md, for the next run."""
    out, session = run.call("reviewer", brief=views.reviewer(st, run_dir=str(run.folder)),
                            schema=schemas.ReviewOutput, access="read_only", workdir=run.folder,
                            skills=skills_for(run, {"name": "reviewer",
                                                    "skills": list(run.limits.reviewer_skills)},
                                              "reviewer"),
                            label="reviewer")
    review_id = f"V{len(st.get('reviews') or []) + 1}"
    folder = run.folder / "reviews"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{review_id}.md"
    outcome = S.render(st, ["execution"])
    value = {**out.model_dump(), "run": str(run.folder), "outcome": outcome, "file": str(path)}
    shown = S.render({"reviews": [{**value, "id": review_id}]}, ["reviews"]).split("\n", 1)[-1]
    path.write_text(f"# Review {review_id} of {run.folder.name}\n\n{shown}\n\n"
                    f"## What was run\n{outcome}\n")
    ops = [{"op": "add", "section": "reviews", "value": value},
           {"op": "add", "section": "results", "value": {
               "node": "reviewer", "job": "review", "asked": "review the finished study",
               "summary": f"rating {out.rating}/10 (confidence {out.confidence}/5): {out.summary}"[:1500],
               "session": session}}]
    return Outcome(ops=ops, summary=f"review {review_id}: rating {out.rating}/10", session=session,
                   data=out)
