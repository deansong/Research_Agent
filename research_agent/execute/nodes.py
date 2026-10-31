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

import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from research_agent import run as run_base
from research_agent import state as S
from research_agent.bootstrap.config import RoleConfig
from research_agent.bootstrap import verify
from research_agent.bootstrap.nodes import DISCUSSOR_SESSION, Outcome, design_problems
from research_agent import autopilot, chat, inbox, prompt_files
from research_agent.execute import config, jobs, prompts, schemas, views

NodeFailed = run_base.NodeFailed

#: Graph node names the design's own names must not take.
RESERVED = {"discussor", "orchestrator", "dispatch", "validator", "wait", "human", "paper",
            "reviewer", "results", "reroute", "replan"}
#: Where a step's status says it is finished, one way or another.
SETTLED = {"pass", "skipped", "replaced"}
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
    if node.get("role") == "researcher" and isinstance(choice, list) and RESEARCH not in choice:
        choice = [RESEARCH, *choice]      # its tools are what make it a researcher
    if choice is None or choice == "auto":
        return choice if run.known_skills() else None
    known = run.known_skills()
    return [s for s in choice if s in known] or None


#: The skill whose tools a researcher searches with.
RESEARCH = "research"


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


def ancestors(st: dict, step_id: str) -> list[str]:
    """The steps `step_id` needs, directly or through others."""
    steps = {s["id"]: s for s in (st.get("plan") or {}).get("steps") or []}
    found, todo = [], list((steps.get(step_id) or {}).get("depends_on") or [])
    while todo:
        d = todo.pop()
        if d not in found and d in steps:
            found.append(d)
            todo += steps[d].get("depends_on") or []
    return found


def short(rec: dict) -> bool:
    """A settled step that did not deliver everything: skipped, partial, or
    passed by the user over its verdict."""
    return rec.get("status") == "skipped" or rec.get("status") == "pass" and (
        rec.get("verdict", "").startswith(("partial", "passed by")))


def needed_by(st: dict, step_id: str) -> list[str]:
    """The steps not settled yet that need `step_id`: what skipping it costs."""
    rec = records(st)
    return [d for d in dependents(st, step_id) if rec.get(d, {}).get("status") not in SETTLED]


def user_skip_ops(run_dir, st: dict, skips, by: str = "user") -> list[dict]:
    """The user's skip of steps, for their reason: recorded as the step's
    terminal record, and as their decision."""
    ops = []
    for w in skips:
        sid, why = (w["step"], w["why"]) if isinstance(w, dict) else (w.step, w.why)
        r = records(st).get(sid)
        if not r or r["status"] not in ("pending", "fail", "stopped"):
            continue
        folder = Path(run_dir) / "artifacts" / views.folder_name(sid)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SKIPPED.md").write_text(f"# Step {sid} skipped\n\nBy the {by}, {_now()}: {why}\n")
        ops.append(record_op(st, sid, status="skipped", finished=_now(),
                             summary=f"skipped by the {by}: {why}"[:1500],
                             artifacts=["SKIPPED.md"]))
        st = S.apply(st, ops[-1:], by=by)[0]
        ops.append({"op": "add", "section": "decisions", "value": {
            "what": f"Skip step {sid}", "why": why, "by": by}})
        st = S.apply(st, ops[-1:], by=by)[0]
    return ops


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
    skip: list[dict] = field(default_factory=list)
    rewind: str = ""
    amend: list = field(default_factory=list)
    """Its amendments (AmendIn), checked; applied by the graph."""
    replan: str = ""
    """A new route for the rest of the study, on the user's word."""


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
        jobs.set_aside(folder, keep / "jobs", step=s)
    if keep.exists():
        close(keep.parent)
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


# ---- amendments: the approved design changed during the execution --------------------------
#
# Measured (run 1119): an approved plan asked its steps for a start record,
# proof that later workers could not read the test files, and delivery
# receipts -- things no step can produce -- and with the design frozen, each
# such step failed every attempt until the user passed it by hand. Now the
# discussor amends: a workflow fix on its own, a research change only on the
# user's (or the autopilot's) word -- and never once what it changes has
# been judged.

STEP_FIELDS = ("detail", "check", "title")
#: The design approved again as amended (after amend_ops, in its own commit).
REAPPROVE = [{"op": "approve", "section": "design"}]
RUBRIC_FIELDS = ("criterion", "measure", "threshold")
PROTOCOL_PARTS = ("baselines", "metrics", "statistics", "seeds", "stop_criteria")
#: Where a step may be amended: not yet judged, or failed or stopped.
AMENDABLE = ("pending", "fail", "stopped")


def _unjudged(st: dict, step_id: str) -> bool:
    """No verdict on this step yet: what judges it may still change."""
    rec = records(st).get(step_id) or {}
    return rec.get("status") in AMENDABLE and not rec.get("verdict")


def amend_problems(st: dict, amends, *, user: bool) -> list[str]:
    """What is wrong with these amendments, in words. `user`: the user (or the
    autopilot) asked for them in this turn."""
    rec = records(st)
    rubric = {r["id"]: r for r in st.get("rubric") or []}
    problems = []
    for a in amends:
        if a.target == "step":
            if a.field not in STEP_FIELDS:
                problems.append(f"amend step {a.id}: a step's field is {', '.join(STEP_FIELDS)}")
            elif a.id not in rec:
                problems.append(f"amend: there is no step {a.id}")
            elif rec[a.id].get("status") not in AMENDABLE:
                problems.append(f"amend step {a.id}: it is {rec[a.id]['status']}; only a pending, "
                                f"failed or stopped step is amended")
        elif a.target == "rubric":
            if a.field not in RUBRIC_FIELDS:
                problems.append(f"amend {a.id}: a criterion's field is {', '.join(RUBRIC_FIELDS)}")
            elif a.id not in rubric:
                problems.append(f"amend: there is no criterion {a.id}")
            elif rubric[a.id].get("assessed_at") and not _unjudged(st, rubric[a.id]["assessed_at"]):
                problems.append(f"amend {a.id}: step {rubric[a.id]['assessed_at']}, which judges "
                                f"it, has a verdict already -- changing the bar after the result "
                                f"would fit it to the result")
        else:
            if a.field != "value" or a.id not in PROTOCOL_PARTS:
                problems.append(f"amend protocol: the part is one of {', '.join(PROTOCOL_PARTS)}, "
                                f"its field `value`")
            else:
                judged = [s["id"] for s in (st.get("plan") or {}).get("steps") or []
                          if a.id in (s.get("follows") or []) and not _unjudged(st, s["id"])]
                if judged:
                    problems.append(f"amend protocol {a.id}: steps {', '.join(judged)} follow it "
                                    f"and have verdicts already")
        if (a.kind == "research" or a.target != "step") and not user:
            problems.append(f"amend {a.target} {a.id}: a research change (the rubric, the "
                            f"protocol, or what a step tests) is the user's -- propose it to them, "
                            f"or make it when they (or the autopilot) say so")
    return problems


def amend_ops(st: dict, amends, *, by: str) -> list[dict]:
    """The amendments as changes: the plan, rubric or protocol changed, each
    logged in execution.amendments (from what, to what, why, by whom). Commit
    REAPPROVE after them, on its own: the execution runs on an approved design,
    and an approval stamps the versions as they are when it is applied."""
    plan = json.loads(json.dumps(st.get("plan") or {}))
    log = list((st.get("execution") or {}).get("amendments") or [])
    ops = []
    for a in amends:
        if a.target == "step":
            step = next(s for s in plan.get("steps") or [] if s["id"] == a.id)
            before, step[a.field] = step.get(a.field, ""), a.value
        elif a.target == "rubric":
            before = next(r for r in st.get("rubric") or [] if r["id"] == a.id).get(a.field, "")
            ops.append({"op": "update", "section": "rubric", "id": a.id,
                        "value": {a.field: a.value}})
        else:
            before = (st.get("protocol") or {}).get(a.id, "")
            value = ([x.strip() for x in a.value.splitlines() if x.strip()]
                     if a.id in ("baselines", "metrics") else a.value)
            ops.append({"op": "merge", "section": "protocol", "value": {a.id: value}})
        log.append({"id": f"A{len(log) + 1}", "target": a.target, "of": a.id, "field": a.field,
                    "from": before, "to": a.value, "kind": "research" if a.target != "step"
                    else a.kind, "why": a.why, "by": by, "at": _now()})
    if any(a.target == "step" for a in amends):
        ops.insert(0, {"op": "set", "section": "plan", "value": plan})
    ops += [{"op": "merge", "section": "execution", "value": {"amendments": log}},
            {"op": "add", "section": "decisions", "value": {
                "what": "Amend the approved design: " + "; ".join(
                    f"{x['id']} {x['target']} {x['of']} {x['field']}" for x in log[-len(amends):]),
                "why": "; ".join(a.why for a in amends)[:600], "by": by}}]
    return ops


# ---- routes: a dead end, and the way round it -----------------------------------------------
#
# V3. A step that cannot go on as specified -- not a bug a retry would fix --
# is a dead end. Its next planned alternative (in the design the user
# approved) is taken by code, with fresh attempts, and the user is told; with
# none left, the discussor proposes a new route for the rest of the study and,
# once the user agrees, a replanner replaces every step not yet done. Every
# change of route is kept in execution.routes, for the paper.

#: What a replan may not happen during: a step at work.
AT_WORK = ("running", "waiting", "validating")


def _route_entry(st: dict, **fields) -> dict:
    routes = (st.get("execution") or {}).get("routes") or []
    return {"id": f"route{len(routes) + 1}", "at": _now(), **fields}


def take_alternative(st: dict, step_id: str, why: str) -> tuple[list[dict], dict] | None:
    """The step's next planned route, as changes (and its log entry); None
    when it has none left."""
    step, rec = plan_step(st, step_id) or {}, records(st)[step_id]
    alternatives, n = step.get("alternatives") or [], rec.get("route", 0)
    if n >= len(alternatives):
        return None
    alt = alternatives[n]
    plan = json.loads(json.dumps(st.get("plan") or {}))
    planned = next(x for x in plan["steps"] if x["id"] == step_id)
    entry = _route_entry(st, kind="planned alternative", step=step_id, why=why,
                         when=alt.get("when", ""), replaced=planned.get("detail", "")[:600],
                         by=alt.get("detail", "")[:600])
    planned["detail"], planned["check"] = alt["detail"], alt["check"]
    routes = list((st.get("execution") or {}).get("routes") or []) + [entry]
    ops = [{"op": "set", "section": "plan", "value": plan},
           record_op(st, step_id, status="pending", route=n + 1, attempts=0, extra_attempts=0,
                     verdict="", summary="", findings=[], dispatch={}, jobs=[], sessions=[],
                     dead_end="",
                     problems=[f"dead end on its route {n}: {why}"[:1500]]),
           {"op": "merge", "section": "execution", "value": {"routes": routes}},
           {"op": "add", "section": "decisions", "value": {
               "what": f"Step {step_id} takes its planned alternative {n + 1} ({entry['id']})",
               "why": f"a dead end: {why}"[:600], "by": "orchestrator"}}]
    return ops, entry


def team_of(st: dict) -> dict[str, str]:
    """The design's step owners: node name -> its kind (worker, researcher, writer)."""
    return {n["name"]: role_of(n) for n in (st.get("design") or {}).get("nodes") or []
            if role_of(n) in config.OWNERS}


def replan_problems(st: dict, *, user: str) -> list[str]:
    """Whether a new route may be planned now."""
    if not user or user.startswith(autopilot.MARK):
        return ["replan: a new route is the user's to agree: propose it, and set `replan` once "
                "they say yes"]
    rec = records(st)
    busy = [s for s, r in rec.items() if r["status"] in AT_WORK]
    if busy:
        return [f"replan: steps {', '.join(busy)} are at work; a new route waits until they "
                f"finish"]
    if all(r["status"] in SETTLED for r in rec.values()):
        return ["replan: every step is settled; nothing is left to replace"]
    return []


def replan_check(st: dict):
    """What code requires of a new route for the rest of the study."""
    rec = records(st)
    settled = [s for s, r in rec.items() if r["status"] in ("pass", "skipped")]
    replaced = [s for s, r in rec.items() if r["status"] not in SETTLED]
    first = max((int(s) for s in rec if s.isdigit()), default=0) + 1
    team = team_of(st)
    rubric = {r["id"]: r for r in st.get("rubric") or []}
    due = [r for r, x in rubric.items() if not x.get("assessed_at")
           or x["assessed_at"] in replaced]

    def check(out: schemas.ReplanOutput) -> list[str]:
        busy = [s for s, r in rec.items() if r["status"] in AT_WORK]
        if busy:
            return [f"steps {', '.join(busy)} are at work: a new route waits until they finish"]
        ids = [x.id for x in out.steps]
        want = [str(first + i) for i in range(len(ids))]
        problems = [] if ids == want else [f"the new steps are {', '.join(want)}, not {ids}"]
        for i, x in enumerate(out.steps):
            problems += [f"step {x.id} depends on {d}: only steps done ({', '.join(settled)}) "
                         f"or new steps before it" for d in x.depends_on
                         if d not in settled and d not in ids[:i]]
            if team.get(x.owner) != x.kind:
                problems.append(f"step {x.id}: its owner is a team node of its kind ({x.kind}): "
                                + ", ".join(f"{n} ({k})" for n, k in team.items()))
        done_at = {r: [x.id for x in out.steps if r in x.completes] for r in rubric}
        problems += [f"{r} is measured by steps already done: not again" for r, at in done_at.items()
                     if at and r not in due]
        problems += [f"{r} is measured by no step: give it the one new step that measures it"
                     for r in due if not done_at[r]]
        problems += [f"{r} is measured by steps {', '.join(at)}: give it ONE" for r, at in
                     done_at.items() if len(at) > 1]
        return problems
    return check, replaced


def replan(run: ExecRun, st: dict, *, ask: str) -> tuple[list[dict], dict, object]:
    """The rest of the study on a new route: (changes, log entry, answer)."""
    check, replaced = replan_check(st)
    out, session = run.call("orchestrator", brief=views.replan(st, ask=ask, team=team_of(st),
                                                               replaced=replaced,
                                                               run_dir=run.folder),
                            schema=schemas.ReplanOutput, instructions=run.instructions["replanner"],
                            access="read_only", workdir=run.folder, skills=None,
                            session=run.next_name("replan"), label="orchestrator (replan)",
                            check=check)
    plan = json.loads(json.dumps(st.get("plan") or {}))
    new = []
    for x in out.steps:
        step = x.model_dump()
        step["rubric"] = step.pop("completes")
        step.pop("owner")
        new.append(step)
    plan["steps"] = list(plan.get("steps") or []) + new
    owners = {x.id: x.owner for x in out.steps}
    design = json.loads(json.dumps(st.get("design") or {}))
    for node in design.get("nodes") or []:
        node["steps"] = list(node.get("steps") or []) + [s for s, o in owners.items()
                                                          if o == node["name"]]
    records_ = [({**r, "status": "replaced"} if r["id"] in replaced else r)
                for r in (st.get("execution") or {}).get("steps") or []]
    records_ += [{"id": x.id, "title": x.title, "owner": x.owner, "status": "pending"}
                 for x in out.steps]
    entry = _route_entry(st, kind="replan", step=", ".join(replaced), why=ask,
                         replaced=", ".join(replaced),
                         by=", ".join(f"{x.id} {x.title}" for x in out.steps),
                         summary=out.summary)
    measured = {r: x.id for x in out.steps for r in x.completes}
    ops = [{"op": "set", "section": "plan", "value": plan},
           {"op": "set", "section": "design", "value": design},
           *[{"op": "update", "section": "rubric", "id": r, "value": {"assessed_at": sid}}
             for r, sid in measured.items()],
           {"op": "merge", "section": "execution", "value": {
               "steps": records_,
               "routes": list((st.get("execution") or {}).get("routes") or []) + [entry]}},
           {"op": "add", "section": "decisions", "value": {
               "what": f"A new route ({entry['id']}): steps {', '.join(replaced)} replaced by "
                       f"{', '.join(owners)}", "why": ask[:600], "by": "user"}},
           {"op": "add", "section": "results", "value": {
               "node": "orchestrator", "job": "replan", "asked": ask[:500],
               "summary": out.summary[:1500], "session": session}}]
    return ops, entry, out


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
        problems += [f"skip: step {w.step} is {records(st).get(w.step, {}).get('status', 'not a step')}"
                     f"; only a pending, failed or stopped step is skipped" for w in out.skip
                     if records(st).get(w.step, {}).get("status") not in ("pending", "fail",
                                                                          "stopped")]
        if out.skip and user.startswith(autopilot.MARK):
            problems.append("skip: only the user skips a step; the autopilot cannot")
        problems += amend_problems(st, out.amend, user=bool(user))
        # Its own workflow fix may give the failed step it fixes another
        # attempt, with no word from the user; any other retry is theirs.
        fixed = {a.id for a in out.amend if a.target == "step" and a.kind == "workflow"}
        if not user:
            problems += [f"retry {s}: without the user, only a step you amend (a workflow fix) "
                         f"gets another attempt" for s in out.retry if s not in fixed]
        if out.hand_to == "orchestrator" and not (out.requests or out.go or out.stop
                                                  or out.retry or out.waive or out.rewind
                                                  or out.skip or out.amend
                                                  or out.replan.strip()):
            problems.append('hand_to is "orchestrator" but there is no request or go for the '
                            'team to act on: add the request, or hand to "user"')
        if out.rewind:
            problems += rewind_problems(run.folder, st, out.rewind)
        if out.replan.strip():
            problems += replan_problems(st, user=user)
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
    # The autopilot's answer is recorded once, as its own (autopilot.decision_op):
    # what the discussor makes of it is not the user's decision.
    if not user.startswith(autopilot.MARK):
        ops += [{"op": "add", "section": "decisions", "value": {**d.model_dump(), "by": "user"}}
                for d in out.decisions]
    ops += [{"op": "update", "section": "open_questions", "id": a.id,
             "value": {"status": "answered", "answer": a.answer}} for a in out.answers]
    return DiscussorTurn(ops=ops, summary=out.reply[:200], session=session, data=out,
                         reply=out.reply, requests=list(out.requests), hand_to=out.hand_to,
                         go=out.go and bool(awaiting), stop=out.stop, retry=list(out.retry),
                         waive=[w.model_dump() for w in out.waive],
                         skip=[w.model_dump() for w in out.skip], rewind=out.rewind,
                         amend=list(out.amend), replan=out.replan.strip())


def step_results(run: ExecRun, st: dict, *, step: dict, report: dict) -> str:
    """The discussor tells the user a checked step's results, in its own
    conversation, while the work goes on: by default, without being asked
    and without a gate (the user, run 1119: "report results = print the
    results in the chat via the discussor")."""
    brief = views.step_results(st, step=step, record=records(st)[step["id"]], report=report,
                               run_dir=str(run.folder))
    with run.discussor_lock:
        out, _ = run.call("discussor", brief=brief, schema=schemas.StepResults,
                          session=DISCUSSOR_SESSION, label="discussor (results)")
    return out.reply


def route_told(run: ExecRun, st: dict, entry: dict) -> str:
    """The discussor tells the user the route changed; the work goes on."""
    with run.discussor_lock:
        out, _ = run.call("discussor", brief=views.route_told(st, entry),
                          schema=schemas.StepResults, session=DISCUSSOR_SESSION,
                          label="discussor (route)")
    return out.reply


def aside(run: ExecRun, st: dict, *, user: str, activity: str) -> schemas.AsideOutput:
    """The user writes while the team works. The answer is the discussor's;
    what it records goes to the inbox, for the graph to apply at its next
    step -- the state here may already be behind."""

    def check(out: schemas.AsideOutput) -> list[str]:
        return (breakpoint_problems(st, out.breakpoints)
                + [f"go: {s} is not a pending step" for s in out.go
                   if records(st).get(s, {}).get("status") != "pending"]
                + step_problems(st, out.retry, out.waive, run.limits.max_attempts)
                + (rewind_problems(run.folder, st, out.rewind) if out.rewind else [])
                + amend_problems(st, out.amend, user=True))

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
    for a in out.amend:
        inbox.post(run.folder, "amend", amendment=a.model_dump())
    for d in out.decisions:
        inbox.post(run.folder, "decision", what=d.what, why=d.why)
    for sid in out.retry:
        inbox.post(run.folder, "retry", step=sid)
    for w in out.waive:
        inbox.post(run.folder, "waive", step=w.step, why=w.why)
    for w in out.skip:
        inbox.post(run.folder, "skip", step=w.step, why=w.why)
    for sid in out.go:
        inbox.post(run.folder, "go", step=sid)
    if out.rewind:
        inbox.post(run.folder, "rewind", step=out.rewind)
    if out.stop:
        inbox.post(run.folder, "stop", why="the user stopped the execution")
    return out


# ---- orchestrator ---------------------------------------------------------------------------


def decision_check(st: dict, limit: int, run_dir=None):
    """What code requires of an execution decision in this state -- also used
    so it can be tested alone."""
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
        if out.action == "stop" and untried and not asked_once:
            asked_once.append("stop")
            return [prompt_files.text("notes/run_not_stop", run_dir, step=untried[0])]
        if out.action == "skip" and out.step in moves["skip"] and needed_by(st, out.step):
            # Measured (run 1455-next): a runtime gate failed by 57 s, the
            # step it guarded was skipped, and the study's main analysis --
            # the step after -- could no longer be done. Whether to raise the
            # limit or give that up is the user's.
            asked_once.append(out.step)      # so the question it is told to ask gets through
            return [prompt_files.text("notes/skip_needed", run_dir, step=out.step,
                                      needed=", ".join(needed_by(st, out.step)))]
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


# ---- the dispatch: a step's inputs checked before it runs -----------------------------------
#
# What Claude Code's parent does when it starts a subagent: it writes the
# subagent's prompt then, with the earlier work done and in view. The
# orchestrator picks the step; the dispatch, on the orchestrator's model,
# looks at what the step depends on as it is on disk, names the exact files
# its owner reads (code checks they exist) and what else it must be told --
# or says what is missing, and the step does not start.


def close(folder: Path) -> None:
    """Outputs kept aside -- an earlier execution's, a rewound step's -- are
    for looking back, not for the work: only the run's owner may read them.
    Measured (run 1119): the validator failed a step because an archived copy
    of the test labels was readable by the selection workers' group."""
    try:
        folder.chmod(0o700)
    except OSError:
        pass


def set_aside_earlier(run: ExecRun, st: dict) -> None:
    """What an execution set aside before this one left behind, for a run set
    back before code did it: job records started before this execution, and
    archived outputs open to others. Measured (run 1119): a step's validator
    counted the earlier execution's jobs as the step's own."""
    started = (st.get("execution") or {}).get("started")
    if started:
        jobs.set_aside(run.folder, run.folder / "jobs" / "_earlier", before=started)
    for kept in (run.folder / "artifacts").glob("_*"):
        close(kept)


def dispatch(run: ExecRun, st: dict, *, node: dict, step: dict, note: str = "") -> Outcome:
    """The check before `step` runs: Outcome.data is a DispatchOutput."""
    rec = records(st)[step["id"]]
    follows = step.get("follows") or []
    earlier = views._ancestors(st, step["id"])

    def check(out: schemas.DispatchOutput) -> list[str]:
        problems = [f"input {i.path} does not exist (paths are absolute, or relative to "
                    f"{run.folder}): if it is an outside resource its owner gets, put it "
                    f"in `obtain`" for i in out.inputs if not _exists(run, i.path)]
        # Only an earlier step's work can be missing: measured (run 1119), a
        # dispatch with no network held step 1 for GoEmotions' official
        # splits and a Qwen tokenizer -- downloads that are the step's own
        # work -- and handed them to an "execution setup" that does not exist.
        problems += [f"missing {m.what!r}: {m.from_step or '(no step)'} is not an earlier step "
                     f"it depends on ({', '.join(earlier) or 'it depends on none'}). An outside "
                     f"resource -- data, weights, a tokenizer, a package -- is its owner's to "
                     f"get: put it in `obtain`" for m in out.missing if m.from_step not in earlier]
        if out.ready and out.missing:
            problems.append("ready, but `missing` lists what is not there: either it is not "
                            "ready, or nothing is missing")
        if not out.ready and not out.missing:
            problems.append("not ready, but `missing` is empty: say what is missing and which "
                            "earlier work should have made it")
        if out.ready and step.get("depends_on") and not out.inputs:
            problems.append("it depends on earlier work: name the files it reads from it in "
                            "`inputs`")
        if out.ready and follows and len(out.context.strip()) < 40:
            problems.append(f"`context` is what its owner is told besides its step, and the "
                            f"step follows {', '.join(follows)}: write in the values and "
                            f"protocol details it must keep, and what each name means")
        return problems

    brief = views.dispatch(st, node=node, step=step, note=note, paths=run.paths(step["id"]),
                           previous=rec if rec.get("attempts") else None)
    out, session = run.call("orchestrator", brief=brief, schema=schemas.DispatchOutput,
                            instructions=run.instructions["dispatch"], skills=None,
                            access="read_only", workdir=run.folder,
                            session=run.next_name("dispatch"),
                            label=f"orchestrator (dispatch, step {step['id']})", check=check)
    summary = (f"step {step['id']} dispatched: {len(out.inputs)} inputs, {len(out.obtain)} to "
               f"obtain" if out.ready else f"step {step['id']} not dispatched -- missing: "
               + "; ".join(f"{m.what} (step {m.from_step})" for m in out.missing))
    ops = [{"op": "add", "section": "results", "value": {
        "node": "orchestrator", "job": f"dispatch step {step['id']}",
        "asked": step["title"][:500], "summary": summary[:1500], "session": session}}]
    return Outcome(ops=ops, summary=summary, session=session, data=out)


def _exists(run: ExecRun, path: str) -> bool:
    p = Path(path).expanduser()
    return (p if p.is_absolute() else run.folder / p).exists()


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
        if out.state == "dead_end" and not out.problems:
            problems.append("dead_end: say why the route cannot work (`problems`), with what "
                            "you tried")
        if out.state == "done" and node["role"] == "researcher" and not out.findings:
            problems.append("done, but `findings` is empty: a researcher reports the key papers "
                            "it found, each with its arXiv id, DOI or PMID (they are checked)")
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
                            previous=rec if rec.get("attempts", 0) > 1 else None,
                            dispatch=rec.get("dispatch") or None)
    out, session = run.call(node["role"], brief=brief, schema=schemas.StepReport,
                            skills=skills_for(run, node), access=access_for(node),
                            workdir=run.work, label=f"step {step['id']} ({node['name']})",
                            session=_session(step["id"], rec.get("attempts", 1) or 1),
                            check=check)
    _commit_work(run.work, f"step {step['id']}, attempt {rec.get('attempts', 1)}: "
                           f"{out.state} -- {out.summary[:200]}")
    # A researcher's sources are checked as the bootstrap researcher's are;
    # the validator is told which were not found.
    checked = {f.source: verify.source(f.source, run.skills_dir) if node["role"] == "researcher"
               else None for f in out.findings}
    unverified = [s for s, ok in checked.items() if ok is False]
    ops = [{"op": "add", "section": "evidence", "value": {
        "claim": f.claim, "source": f.source, "bears_on": list(f.bears_on),
        "found_by": node["name"], **({"verified": checked[f.source]}
                                     if node["role"] == "researcher" else {})}}
           for f in out.findings]
    ops += [{"op": "add", "section": "risks", "value": r.model_dump()} for r in out.risks]
    ops += [{"op": "add", "section": "open_questions", "value": {"text": q, "for": "user"}}
            for q in out.questions_for_user]
    summary = f"step {step['id']} {out.state}: {out.summary}"
    ops.append({"op": "add", "section": "results", "value": {
        "node": node["name"], "job": f"step {step['id']}", "asked": step["title"][:500],
        "summary": summary[:1500], "session": session}})
    if unverified:
        out = out.model_copy(update={"problems": [*out.problems, *(
            f"the research tool found no paper for {s}" for s in unverified)]})
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
    measured_here = [r for r in step.get("rubric") or [] if r in rubric]

    def check(out: schemas.Verdict) -> list[str]:
        named = {f.measure for f in out.findings}
        problems = [f"finding {f.measure}: not a measurement-plan id" for f in out.findings
                    if re.fullmatch(r"R\d+", f.measure) and f.measure not in rubric]
        if out.verdict == "done":
            problems += [f"{r} is measured at this step: record it as a finding, with the "
                         f"value from the files" for r in measured_here if r not in named]
        elif out.verdict == "dead_end" and not out.problems:
            problems.append("dead_end: give the evidence from the files (`problems`) that the "
                            "route itself cannot work")
        elif not (out.problems or out.advice.strip()):
            problems.append(f"{out.verdict}: say what differs or is wrong (`problems`) and what "
                            f"a retry must do (`advice`)")
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
    # Done means done as told, whatever the numbers say: a result against
    # its expectation is a finding, not a failure (V3).
    done = out.verdict == "done"
    verdict = f"{out.verdict}: {out.summary}"
    ops = [record_op(st, step["id"], status="pass" if done else "fail", verdict=verdict[:1500],
                     findings=[f.model_dump() for f in out.findings],
                     problems=list(out.problems)[:20]
                     + ([f"advice: {out.advice}"] if out.advice and not done else []),
                     finished=_now() if done else "",
                     dead_end="; ".join([out.summary, *out.problems])[:1500]
                     if out.verdict == "dead_end" else "")]
    if done:
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
        problems = reference_problems(folder / "references.bib", run.skills_dir)
        if problems:
            return problems
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
                            skills=skills_for(run, {"name": "paper", "role": "researcher",
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


_BIB_ENTRY = re.compile(r"@(\w+)\s*\{\s*([^,\s]+)\s*,(.*?)(?=\n@|\Z)", re.S)


def reference_problems(bib: Path, skills_dir) -> list[str]:
    """What is wrong with a paper's references: an entry the research tool
    cannot find, or a paper with no id to look it up by. Software, data and
    web pages (@misc with a url and no id, @software, @online) may stand."""
    if not bib.is_file():
        return []
    problems = []
    for kind, key, body in _BIB_ENTRY.findall(bib.read_text(errors="replace")):
        if kind.lower() in ("comment", "string", "preamble"):
            continue
        ident = verify.paper_id(body) or verify.paper_id(
            " ".join(re.findall(r"(?:eprint|arxiv)\s*=\s*[{\"]([^}\"]+)", body, re.I)))
        if ident is None:
            if kind.lower() in ("article", "inproceedings", "incollection", "proceedings") \
                    and not re.search(r"\burl\s*=", body, re.I):
                problems.append(f"references.bib: {key} is a paper with no eprint, doi or pmid "
                                f"to check it by: add one from the research tool, or drop it")
            continue
        if verify.source(ident, skills_dir) is False:
            problems.append(f"references.bib: {key} ({ident}) was not found by the research "
                            f"tool: fix its id, or drop it and what cites it")
    return problems


#: The panel: each reviewer fills the whole form, deepest on its focus.
PANEL = (("R1", "method and soundness: is the method right, and do the experiments, "
                "baselines and statistics support each claim"),
         ("R2", "novelty and significance: what is new against prior work, whether it "
                "matters, and what related work the paper misses"),
         ("R3", "clarity and reproducibility: could a reader follow the paper, and redo "
                "the study from it and its appendices"))


def reviewer(run: ExecRun, st: dict) -> Outcome:
    """The paper reviewed as AAAI would: a panel of three reviewers, each
    with a focus, then a meta-review that weighs them and decides. Kept in the
    state (reviews) and as <run>/reviews/V<n>.md, for the next run."""
    skills = skills_for(run, {"name": "reviewer", "skills": list(run.limits.reviewer_skills)},
                        "reviewer")
    panel = []
    for rid, focus in PANEL:
        out, _ = run.call("reviewer", brief=views.reviewer(st, run_dir=str(run.folder),
                                                           focus=focus),
                          schema=schemas.ReviewOutput, access="read_only", workdir=run.folder,
                          skills=skills, label=f"reviewer {rid}")
        panel.append((rid, focus, out.model_dump()))
    meta, session = run.call("reviewer", brief=views.meta_reviewer(st, run_dir=str(run.folder),
                                                                   reviews=panel),
                             schema=schemas.MetaReviewOutput, access="read_only",
                             workdir=run.folder, skills=skills,
                             instructions=run.instructions["meta_reviewer"], label="meta-review")
    review_id = f"V{len(st.get('reviews') or []) + 1}"
    folder = run.folder / "reviews"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{review_id}.md"
    value = {**meta.model_dump(), "run": str(run.folder), "outcome": S.render(st, ["execution"]),
             "file": str(path),
             "panel": [{"id": rid, "focus": focus, **r} for rid, focus, r in panel]}
    path.write_text(review_markdown(review_id, run.folder.name, value))
    ops = [{"op": "add", "section": "reviews", "value": value},
           {"op": "add", "section": "results", "value": {
               "node": "reviewer", "job": "review", "asked": "review the paper (panel of 3)",
               "summary": f"{meta.decision}, rating {meta.rating}/10 (confidence "
                          f"{meta.confidence}/5): {meta.summary}"[:1500],
               "session": session}}]
    return Outcome(ops=ops, summary=f"review {review_id}: {meta.decision}, {meta.rating}/10",
                   session=session, data=meta)


def review_markdown(review_id: str, run_name: str, v: dict) -> str:
    """The review as a conference sends it: the meta-review, then each review."""
    lines = [f"# Review {review_id} of {run_name}", "",
             f"## Meta-review -- **{v['decision']}** (rating {v['rating']}/10, "
             f"confidence {v['confidence']}/5)", "", v["summary"], "",
             f"**Justification.** {v['justification']}", "",
             "**The reviewers agree:**", *[f"- {x}" for x in v["consensus"]], ""]
    if v.get("disagreements"):
        lines += ["**They differ:**", *[f"- {x}" for x in v["disagreements"]], ""]
    lines += ["**For the next run:**", *[f"- [{r['priority']}] {r['what']} -- {r['why']}"
                                         for r in v["requests"]], ""]
    for r in v["panel"]:
        lines += [f"## Reviewer {r['id']} -- rating {r['rating']}/10, confidence "
                  f"{r['confidence']}/5", f"*Focus: {r['focus']}*", "",
                  f"**Summary.** {r['summary']}", "", "| Criterion | Score | Why |",
                  "|---|---|---|"]
        lines += [f"| {k} | {r[k]['score']}/4 | " + " ".join(r[k]["why"].replace("|", "/").split())
                  + " |"
                  for k in views.ASSESSED]
        for key, title in (("strengths", "Strengths"), ("weaknesses", "Weaknesses"),
                           ("questions", "Questions for the authors"),
                           ("missing_work", "Missing prior work")):
            if r.get(key):
                lines += ["", f"**{title}**", *[f"- {x}" for x in r[key]]]
        if r.get("ethics"):
            lines += ["", f"**Ethics.** {r['ethics']}"]
        lines += ["", f"**Why this rating.** {r['justification']}", ""]
    lines += ["## What was run", v["outcome"], ""]
    return "\n".join(lines) + "\n"
