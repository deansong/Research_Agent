"""
WHAT:  The brief each execution node is given.
WHY:   The context management, as in the bootstrap: the discussor keeps the
       conversation; every other call starts fresh (a step owner keeps its
       own conversation only while its jobs run) and sees what its job needs.
"""

from __future__ import annotations

from pathlib import Path

from research_agent import prompt_files
from research_agent import state as S
from research_agent.bootstrap.views import _block, _latest_results, _today, state_room
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


def user_decisions(st: dict) -> str:
    """What the user decided -- standing over the plan where they conflict."""
    mine = [d for d in st.get("decisions") or [] if d.get("by") == "user"]
    return "\n".join(f"- {d['id']} {d['what']}" + (f" -- {d['why']}" if d.get("why") else "")
                     for d in mine)


def rubric_text(st: dict, ids) -> str:
    wanted = list(dict.fromkeys(ids))
    items = [r for r in st.get("rubric") or [] if r.get("id") in wanted]
    return S.render({**st, "rubric": items}, ["rubric"]).split("\n", 1)[-1] if items else ""


# ---- discussor ------------------------------------------------------------------------------


def discussor_turn(st: dict, seen: dict, *, user: str = "", note: str = "",
                   awaiting: str = "", budget: int | None = None) -> str:
    """As the bootstrap's: the state is what gives way to `budget`."""
    changed = [s for s in S.changed_since(st, seen) if s != "results"]
    parts, tail = [], []
    if user:
        parts.append(_block("THE USER SAYS", user))
    if note:
        parts.append(_block("FROM THE ORCHESTRATOR", note))
    if awaiting.startswith("after:"):
        sid = awaiting.split(":", 1)[1]
        parts.append(_block(f"PAUSED AFTER STEP {sid} -- a breakpoint", "Show the user its results; "
                            "the work goes on when they say go."))
    elif awaiting:
        step = _step(st, awaiting)
        record = (st.get("execution") or {}).get("steps") or []
        why = ("a breakpoint" if any(r["id"] == awaiting and r.get("pause_before") for r in record)
               else "the plan gates it")
        parts.append(_block(f"WAITING FOR THE USER'S GO ({why})",
                            step_text(step) if step else awaiting))
    status = st.get("status") or {}
    if status.get("summary"):
        tail.append(_block("WHERE THINGS STAND", status["summary"]))
    if changed or S.version(st, "results") > int((seen or {}).get("results", 0)):
        room = state_room(budget, parts + tail)
        results = _latest_results(st, seen, None if room is None else room // 4)
        room = None if room is None else max(room - len(results), 0)
        parts.append(_block("CHANGED SINCE YOU LAST SPOKE",
                            S.render_within(st, changed, room) + results))
    return "\n\n".join(parts + tail)


def aside(st: dict, *, user: str, activity: str, run_dir: str,
          budget: int | None = None) -> str:
    """The user writes while the team works: what is happening now, from code."""
    parts = [_block("AN ASIDE -- the team is working; nothing waits for your answer",
                    prompt_files.text("notes/execute_aside", run_dir)),
             _block("THE USER SAYS", user),
             _block("WHAT IS HAPPENING NOW", activity),
             _block("THE STEP TABLE", S.render_within(st, ["execution"],
                                                     state_room(budget, [user, activity])))]
    status = st.get("status") or {}
    if status.get("summary"):
        parts.append(_block("WHERE THINGS STAND", status["summary"]))
    parts.append(_block("FILES", f"Everything is in {run_dir}: artifacts/step_NN/ (outputs), "
                                 f"work/ (code), jobs/ (logs), state/research.md"))
    return "\n\n".join(parts)


# ---- reviewer -----------------------------------------------------------------------------


def paper(st: dict, *, run_dir: str, latex: str, python: str = "") -> str:
    root = Path(run_dir)
    folder = root / "paper"
    compile_ = (f"cd {folder} && {latex} main.tex" if latex.endswith("tectonic") else
                f"cd {folder} && {latex} -pdf main.tex" if latex.endswith("latexmk") else
                f"cd {folder} && {latex} main.tex; bibtex main; {latex} main.tex; {latex} main.tex"
                if latex else "")
    parts = [f"Today is {_today()}.",
             _block("THE STUDY", S.render(st, ["goal", "constraints", "protocol"])),
             _block("HYPOTHESES AND RUBRIC", S.render({**st, "hypotheses": _live(st)},
                                                      ["hypotheses", "rubric"])),
             _block("WHAT WAS RUN -- the step table", S.render(st, ["execution"])),
             _block("DECISIONS AND RISKS", S.render(st, ["decisions", "risks"], brief=True)),
             _block("EVIDENCE YOU MAY CITE", S.render(st, ["evidence"], brief=True)),
             _block("THE FILES -- read them", "\n".join(_listing(root) + [
                 f"Code (a git repository): {root / 'work'}", f"Job logs: {root / 'jobs'}"])),
             _block("THE PAPER", "\n".join([
                 f"Write it in {folder} (main.tex, figures/, tables/, references.bib); "
                 f"{folder / 'main.pdf'} is what the user reads.",
                 f"Compile: {compile_}" if compile_ else
                 "No LaTeX compiler is installed: leave the folder ready to compile and say so.",
                 f"Python for the figure and table scripts: {python}" if python else ""]))]
    if st.get("reviews"):
        parts.append(_block("EARLIER REVIEWS -- answer them where the files allow",
                            S.render(st, ["reviews"], brief=True)))
    return "\n\n".join(parts)


def _listing(root: Path) -> list[str]:
    listing = []
    for folder in sorted((root / "artifacts").glob("step_*")):
        files = sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())
        listing.append(f"{folder}: {', '.join(files[:30]) or '(empty)'}"
                       + (f" ... and {len(files) - 30} more" if len(files) > 30 else ""))
    return listing


def reviewer(st: dict, *, run_dir: str, focus: str = "") -> str:
    root = Path(run_dir)
    listing = _listing(root)
    parts = [f"Today is {_today()}.",
             _block("THE STUDY", S.render(st, ["goal", "constraints", "protocol"])),
             _block("HYPOTHESES AND RUBRIC", S.render({**st, "hypotheses": _live(st)},
                                                      ["hypotheses", "rubric"])),
             _block("THE PLAN", "\n\n".join(step_text(s) for s in _steps(st))),
             _block("WHAT WAS RUN -- the step table", S.render(st, ["execution"])),
             _block("DECISIONS AND RISKS", S.render(st, ["decisions", "risks"], brief=True)),
             _block("EVIDENCE CITED", S.render(st, ["evidence"], brief=True)),
             _block("THE FILES -- read them", "\n".join(listing + [
                 f"Code (a git repository; `git log` shows each step): {root / 'work'}",
                 f"Job records and logs: {root / 'jobs'}"]))]
    parts.append(_paper_block(root))
    if focus:
        parts.append(_block("YOUR FOCUS -- fill the whole form, go deepest here", focus))
    if st.get("reviews"):
        parts.append(_block("EARLIER REVIEWS -- say whether this run answers them",
                            S.render(st, ["reviews"], brief=True)))
    parts.append("The authors -- the user who owns this run -- ask for this review of their "
                 "own work.")
    return "\n\n".join(parts)


def _paper_block(root: Path) -> str:
    pdf = root / "paper" / "main.pdf"
    if pdf.is_file():
        return _block("THE PAPER -- the submission you review",
                      f"{pdf} (its LaTeX source beside it). Check its claims and numbers "
                      f"against the files above.")
    return _block("THE PAPER", "There is no compiled paper: review the study's own write-up "
                               "in the step artifacts as the submission, and say so.")


def meta_reviewer(st: dict, *, run_dir: str, reviews: list[tuple[str, str, dict]]) -> str:
    """`reviews`: (id, focus, the review) for each panel member."""
    root = Path(run_dir)
    panel = []
    for rid, focus, r in reviews:
        scores = ", ".join(f"{k} {r[k]['score']}/4" for k in ASSESSED)
        panel.append(_block(f"REVIEWER {rid} -- focus: {focus}", "\n".join([
            f"rating {r['rating']}/10, confidence {r['confidence']}/5; {scores}",
            f"summary: {r['summary']}", f"why this rating: {r['justification']}",
            *[f"{k}: {r[k]['why']}" for k in ASSESSED],
            *[f"strength: {x}" for x in r["strengths"]],
            *[f"weakness: {x}" for x in r["weaknesses"]],
            *[f"question: {x}" for x in r.get("questions") or []],
            *[f"missing prior work: {x}" for x in r.get("missing_work") or []],
            *([f"ethics: {r['ethics']}"] if r.get("ethics") else [])])))
    parts = [f"Today is {_today()}.",
             _block("THE STUDY", S.render(st, ["goal", "protocol"])),
             _block("WHAT WAS RUN -- the step table", S.render(st, ["execution"])),
             _block("THE FILES", "\n".join(_listing(root) + [f"Job logs: {root / 'jobs'}"])),
             _paper_block(root), *panel]
    if st.get("reviews"):
        parts.append(_block("EARLIER DECISIONS -- does this run answer them",
                            S.render(st, ["reviews"], brief=True)))
    return "\n\n".join(parts)


#: The criteria each reviewer scores, in the form's order.
ASSESSED = ("originality", "significance", "soundness", "clarity", "reproducibility")


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
    decided = user_decisions(st)
    if decided:
        parts.append(_block("THE USER'S DECISIONS -- they stand over the plan where they "
                            "conflict; apply them and say so in your report", decided))
    parts += [
        _block("RUBRIC ITEMS YOUR WORK IS CHECKED AGAINST",
               rubric_text(st, list(step.get("rubric") or []))),
        _block("THE STUDY", S.render(st, ["goal", "constraints", "protocol"])),
        _block("HYPOTHESES", S.render({**st, "hypotheses": _live(st)}, ["hypotheses"])),
        _block("EARLIER STEPS' OUTPUTS", upstream(st, step, paths, every=node["role"] == "writer")),
        _block("WHERE TO WORK", "\n".join([
            f"Workspace (your current folder; code goes here): {paths['work']}",
            f"Your artifacts folder (outputs go here): {paths['artifacts']}",
            f"The run folder (read anything in it): {paths['run']}",
            *[f"An earlier run, reviewed in {r['id']} (its files stand; read them): {r['run']}"
              for r in st.get("reviews") or [] if r.get("run") and r["run"] != paths["run"]],
            *([f"The study's Python (torch, CUDA, its libraries) -- use it for the study's "
               f"code: {paths['python']}"] if paths.get("python") else []),
            "Launch anything longer than a few minutes as a job, e.g.:",
            f"  {jobs_command} launch --step {step['id']} --gpus 1 --name seed17 -- "
            f"{paths.get('python') or 'python'} train.py --seed 17",
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
            + "\n\n" + prompt_files.text("notes/owner_wake", run_dir))


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
            st, list(step.get("rubric") or []))),
        _block("THE PROTOCOL", S.render(st, ["protocol"])),
        _block("THE USER'S DECISIONS -- they stand over the plan where they conflict: "
               "work that follows one is not a failure", user_decisions(st) or "(none)"),
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
