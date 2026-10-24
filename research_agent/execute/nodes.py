"""
WHAT:  The execution stage's nodes as plain functions -- the discussor, the
       orchestrator, a step's owner, the validator -- and the approved design
       read as the graph's nodes (`Design`).
WHY:   The design is data: one orchestrator, one read-only validator, and
       owners (worker, runner, writer) of the plan's steps. Code interprets
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

import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from research_agent import run as run_base
from research_agent.bootstrap.config import RoleConfig
from research_agent.bootstrap.nodes import DISCUSSOR_SESSION, Outcome, design_problems
from research_agent.execute import config, jobs, prompts, schemas, views

NodeFailed = run_base.NodeFailed

#: Graph node names the design's own names must not take.
RESERVED = {"discussor", "orchestrator", "validator", "wait", "human"}
#: Where a step's status says it is finished, one way or another.
SETTLED = {"pass", "skipped"}


@dataclass
class ExecRun(run_base.Run):
    """One run's execution: the bootstrap's run folder, with the execution
    roles, the limits, and the bootstrap's discussor."""

    limits: config.ExecConfig | None = None

    def __post_init__(self):
        super().__post_init__()
        self.instructions = {**prompts.INSTRUCTIONS, **self.instructions}
        if self.limits is None:
            self.limits = config.defaults()

    @classmethod
    def from_config(cls, folder: Path, cfg: config.ExecConfig, discussor: RoleConfig, *,
                    progress=False, trace=False) -> "ExecRun":
        return cls(folder, {**cfg.roles, "discussor": discussor}, skills_dir=cfg.skills_dir,
                   skills={}, progress=progress, trace=trace, limits=cfg)

    @property
    def work(self) -> Path:
        return self.folder / "work"

    def artifacts(self, step_id: str) -> Path:
        return self.folder / "artifacts" / views.folder_name(step_id)

    def paths(self, step_id: str) -> dict:
        return {"run": str(self.folder), "work": str(self.work),
                "artifacts": str(self.artifacts(step_id))}

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
        steps = [s["id"] for s in (st.get("plan") or {}).get("steps") or []]
        problems = design_problems(design, steps=steps, skills=set(skills),
                                   rubric={r["id"] for r in st.get("rubric") or []})
        problems += [f"{n['name']} is a {n['role']}: an execution node is orchestrator, "
                     f"validator, worker, runner or writer"
                     for n in design.get("nodes") or []
                     if n["role"] not in ("orchestrator", "validator", *config.OWNERS)]
        problems += [f"{n['name']} owns steps but is the {n['role']}"
                     for n in design.get("nodes") or []
                     if n["role"] in ("orchestrator", "validator") and n.get("steps")]
        if problems:
            raise ValueError("The design cannot be run:\n" + "\n".join(f"- {p}" for p in problems))
        nodes = design["nodes"]
        owners = {graph_name(n["name"]): n for n in nodes if n["role"] in config.OWNERS}
        return cls(orchestrator=next(n for n in nodes if n["role"] == "orchestrator"),
                   validator=next(n for n in nodes if n["role"] == "validator"),
                   owners=owners,
                   owner_of={s: name for name, n in owners.items() for s in n.get("steps") or []})


def graph_name(name: str) -> str:
    return f"node_{name}" if name in RESERVED else name


def skills_for(run: ExecRun, node: dict) -> list[str] | None:
    """The design's skills for a node that are in the folder (None: none)."""
    known = run.known_skills()
    chosen = [s for s in node.get("skills") or [] if s in known]
    return chosen or None


def access_for(node: dict) -> str:
    """A design's `write` owner runs commands -- on the GPUs, writing outside
    its workspace (artifacts, jobs) -- which research_backend calls `full`:
    Claude Code's `write` cannot run a command without asking."""
    return {"write": "full", "read_only": "read_only", "none": "none"}[node.get("access", "write")]


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
        if r["status"] in ("fail", "stopped") and ready and r.get("attempts", 0) < max_attempts:
            moves["retry"].append(s["id"])
        if r["status"] in ("pending", "fail", "stopped"):
            moves["skip"].append(s["id"])
    return moves


def describe_moves(moves: dict, st: dict, max_attempts: int) -> list[str]:
    rec = records(st)
    lines = [f"run {s}" for s in moves["run"]]
    lines += [f"retry {s} (attempt {rec[s].get('attempts', 0) + 1} of {max_attempts})"
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


def discussor(run: ExecRun, st: dict, *, seen: dict, user: str = "", note: str = "",
              awaiting: str = "") -> DiscussorTurn:
    open_ids = {q["id"] for q in st.get("open_questions") or [] if q.get("status") == "open"}

    def check(out: schemas.ExecDiscussorOutput) -> list[str]:
        problems = [f"answers: {a.id} is not an open question" for a in out.answers
                    if a.id not in open_ids]
        if out.go and not awaiting:
            problems.append("go: no step is waiting for the user's go")
        if out.hand_to == "orchestrator" and not (out.requests or out.go or out.stop):
            problems.append('hand_to is "orchestrator" but there is no request or go for the '
                            'team to act on: add the request, or hand to "user"')
        return problems

    out, session = run.call("discussor", brief=views.discussor_turn(
        st, seen, user=user, note=note, awaiting=awaiting), schema=schemas.ExecDiscussorOutput,
        session=DISCUSSOR_SESSION, check=check)
    ops = [{"op": "add", "section": "decisions", "value": {**d.model_dump(), "by": "user"}}
           for d in out.decisions]
    ops += [{"op": "update", "section": "open_questions", "id": a.id,
             "value": {"status": "answered", "answer": a.answer}} for a in out.answers]
    return DiscussorTurn(ops=ops, summary=out.reply[:200], session=session, data=out,
                         reply=out.reply, requests=list(out.requests), hand_to=out.hand_to,
                         go=out.go and bool(awaiting), stop=out.stop)


# ---- orchestrator ---------------------------------------------------------------------------


def orchestrator(run: ExecRun, st: dict, design: Design, *, budget: str, requests: list[str],
                 last: str = "") -> Outcome:
    limit = run.limits.max_attempts
    moves = open_moves(st, limit)

    def check(out: schemas.ExecDecision) -> list[str]:
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

    brief = views.orchestrator(
        st, design_part=design.orchestrator.get("instructions", ""),
        moves=describe_moves(moves, st, limit), budget=budget, requests=requests, last=last)
    out, session = run.call("orchestrator", brief=brief, schema=schemas.ExecDecision,
                            skills=skills_for(run, design.orchestrator), check=check)
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
                            skills=skills_for(run, design.validator), access="read_only",
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

