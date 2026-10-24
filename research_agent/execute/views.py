"""
WHAT:  The brief each execution node is given.
WHY:   The context management, as in the bootstrap: the discussor keeps the
       conversation; every other call starts fresh (a step owner keeps its
       own conversation only while its jobs run) and sees what its job needs.
"""

from __future__ import annotations

from pathlib import Path

from research_agent import state as S
from research_agent.bootstrap.views import _block, _latest_results, _today
from research_agent.execute import jobs as J


def step_text(step: dict) -> str:
    """One plan step in full."""
    lines = [f"Step {step['id']}: {step['title']}{' [gate]' if step.get('gate') else ''}"]
    for key, label in (("detail", "What"), ("check", "Check"), ("depends_on", "After steps"),
                       ("rubric", "Rubric"), ("cost", "Cost")):
        value = step.get(key)
        if value:
            lines.append(f"{label}: {value if isinstance(value, str) else ', '.join(value)}")
    return "\n".join(lines)


def rubric_text(st: dict, ids) -> str:
    wanted = list(dict.fromkeys(ids))
    items = [r for r in st.get("rubric") or [] if r.get("id") in wanted]
    return S.render({**st, "rubric": items}, ["rubric"]).split("\n", 1)[-1] if items else ""


# ---- discussor ------------------------------------------------------------------------------


def discussor_turn(st: dict, seen: dict, *, user: str = "", note: str = "",
                   awaiting: str = "") -> str:
    changed = [s for s in S.changed_since(st, seen) if s != "results"]
    parts = []
    if user:
        parts.append(_block("THE USER SAYS", user))
    if note:
        parts.append(_block("FROM THE ORCHESTRATOR", note))
    if changed or S.version(st, "results") > int((seen or {}).get("results", 0)):
        parts.append(_block("CHANGED SINCE YOU LAST SPOKE",
                            S.render(st, changed) + _latest_results(st, seen)))
    if awaiting:
        step = _step(st, awaiting)
        parts.append(_block("WAITING FOR THE USER'S GO", step_text(step) if step else awaiting))
    status = st.get("status") or {}
    if status.get("summary"):
        parts.append(_block("WHERE THINGS STAND", status["summary"]))
    return "\n\n".join(parts)


# ---- orchestrator ---------------------------------------------------------------------------


def orchestrator(st: dict, *, design_part: str, moves: list[str], budget: str,
                 requests: list[str], last: str = "") -> str:
    parts = [f"Today is {_today()}.",
             _block("YOUR PART OF THE APPROVED DESIGN", design_part),
             _block("THE STUDY", S.render(st, ["goal", "constraints", "protocol"])),
             _block("HYPOTHESES AND RUBRIC", S.render(
                 {**st, "hypotheses": _live(st)}, ["hypotheses", "rubric"], brief=True)),
             _block("THE PLAN", "\n\n".join(step_text(s) for s in _steps(st))),
             _block("THE STEP TABLE", S.render(st, ["execution"])),
             _block("OPEN MOVES -- only these are allowed", "\n".join(f"- {m}" for m in moves)),
             _block("BUDGET", budget)]
    known = S.render(st, ["decisions", "open_questions"], brief=True)
    if known:
        parts.append(_block("DECISIONS AND OPEN QUESTIONS", known))
    if requests:
        parts.append(_block("REQUESTS FROM THE DISCUSSOR", "\n".join(f"- {r}" for r in requests)))
    if last:
        parts.append(_block("THE LAST PIECE OF WORK", last))
    return "\n\n".join(parts) + "\n\nDecide the next move."


# ---- a step's owner ---------------------------------------------------------------------------


def owner(st: dict, *, node: dict, step: dict, task: str, paths: dict, jobs_command: str,
          previous: dict | None = None) -> str:
    parts = [f"Today is {_today()}.",
             _block("YOUR PART OF THE APPROVED DESIGN",
                    f"You are {node['name']} ({node['role']}).\n{node.get('instructions', '')}"),
             _block("YOUR STEP", step_text(step))]
    if task:
        parts.append(_block("FROM THE ORCHESTRATOR", task))
    if previous and (previous.get("verdict") or previous.get("problems")):
        parts.append(_block("THE LAST ATTEMPT FAILED -- do it differently", "\n".join(
            [previous.get("verdict", "")] + [f"- {p}" for p in previous.get("problems") or []])))
    parts += [
        _block("RUBRIC ITEMS YOUR WORK IS CHECKED AGAINST",
               rubric_text(st, list(step.get("rubric") or []) + list(node.get("validated_by") or []))),
        _block("THE STUDY", S.render(st, ["goal", "constraints", "protocol"])),
        _block("HYPOTHESES", S.render({**st, "hypotheses": _live(st)}, ["hypotheses"])),
        _block("EARLIER STEPS' OUTPUTS", upstream(st, step, paths, every=node["role"] == "writer")),
        _block("WHERE TO WORK", "\n".join([
            f"Workspace (your current folder; code goes here): {paths['work']}",
            f"Your artifacts folder (outputs go here): {paths['artifacts']}",
            f"The run folder (read anything in it): {paths['run']}",
            "Launch anything longer than a few minutes as a job, e.g.:",
            f"  {jobs_command} launch --step {step['id']} --gpus 1 --name seed17 -- "
            f"python train.py --seed 17",
            f"  (prints the job id; --gpus N picks N free GPUs; `{jobs_command} status "
            f"--step {step['id']}` lists them)"])),
    ]
    return "\n\n".join(parts)


def owner_wake(run_dir: Path, job_ids: list[str]) -> str:
    lines = []
    for job_id in job_ids:
        job = J.get(run_dir, job_id)
        if job is None:
            lines.append(f"{job_id}: no such job")
            continue
        lines.append(J.describe(job) + f"\nlog: {job['log']}\n--- last lines ---\n{J.tail(job)}")
    return (_block("YOUR JOBS FINISHED", "\n\n".join(lines))
            + "\n\nCheck their outputs. Launch more jobs if the step needs them (answer "
              "`launched`), or finish the step (`done`, with its artifacts), or report "
              "`failed` / `blocked`.")


def upstream(st: dict, step: dict, paths: dict, *, every: bool = False) -> str:
    """What the steps before this one produced: where, and their reports."""
    wanted = [s["id"] for s in _steps(st)] if every else _ancestors(st, step["id"])
    records = {r["id"]: r for r in (st.get("execution") or {}).get("steps") or []}
    lines = []
    for sid in wanted:
        if sid == step["id"] or sid not in records:
            continue
        r = records[sid]
        lines.append(f"Step {sid} [{r['status']}] in {Path(paths['run']) / 'artifacts' / folder_name(sid)}: "
                     f"{r.get('summary') or '-'}"
                     + (f"\n  files: {', '.join(r['artifacts'][:20])}" if r.get("artifacts") else "")
                     + (f"\n  verdict: {r['verdict']}" if r.get("verdict") else ""))
    return "\n".join(lines) or "(none: this is the first step)"


# ---- validator ---------------------------------------------------------------------------------


def validator(st: dict, *, design_part: str, node: dict, step: dict, report: dict,
              paths: dict) -> str:
    step_jobs = [J.describe(j) + f"  log: {j['log']}" for j in J.records(paths["run"], step["id"])]
    metrics = "\n".join(f"- {m['name']}: {m['value']}" for m in report.get("metrics") or [])
    return "\n\n".join([
        _block("YOUR PART OF THE APPROVED DESIGN", design_part),
        _block("THE STEP TO CHECK", step_text(step) + f"\nDone by: {node['name']} ({node['role']})"),
        _block("RUBRIC ITEMS TO CHECK", rubric_text(
            st, list(step.get("rubric") or []) + list(node.get("validated_by") or []))),
        _block("THE PROTOCOL", S.render(st, ["protocol"])),
        _block("THE OWNER'S REPORT -- check it against the files", "\n".join([
            f"Summary: {report.get('summary', '')}",
            "Artifacts:\n" + "\n".join(f"- {a}" for a in report.get("artifacts") or []),
            "Commands:\n" + "\n".join(f"- {c}" for c in report.get("commands") or []),
            "Metrics:\n" + metrics if metrics else "",
            "Problems it saw:\n" + "\n".join(f"- {p}" for p in report.get("problems") or [])
            if report.get("problems") else ""]).strip()),
        _block("THIS STEP'S JOBS", "\n".join(step_jobs) or "(none)"),
        _block("WHERE THINGS ARE", "\n".join([
            f"The step's artifacts: {paths['artifacts']}",
            f"The code (git repository): {paths['work']}",
            f"Earlier steps' outputs: {Path(paths['run']) / 'artifacts'}"])),
    ])


# ---- helpers ----------------------------------------------------------------------------------


def folder_name(step_id: str) -> str:
    return f"step_{int(step_id):02d}" if str(step_id).isdigit() else f"step_{step_id}"


def _steps(st: dict) -> list[dict]:
    return list((st.get("plan") or {}).get("steps") or [])


def _step(st: dict, step_id: str) -> dict | None:
    return next((s for s in _steps(st) if s["id"] == step_id), None)


def _live(st: dict) -> list[dict]:
    return [h for h in st.get("hypotheses") or [] if h.get("status") != "dropped"]


def _ancestors(st: dict, step_id: str) -> list[str]:
    by_id = {s["id"]: s for s in _steps(st)}
    seen: list[str] = []
    todo = list((by_id.get(step_id) or {}).get("depends_on") or [])
    while todo:
        sid = todo.pop()
        if sid not in seen and sid in by_id:
            seen.append(sid)
            todo += by_id[sid].get("depends_on") or []
    return [s["id"] for s in _steps(st) if s["id"] in seen]
