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
                       ("rubric", "Rubric"), ("follows", "Follows"), ("cost", "Cost")):
        value = step.get(key)
        if value:
            lines.append(f"{label}: {value if isinstance(value, str) else ', '.join(value)}")
    return "\n".join(lines)


def user_decisions(st: dict, *, since_execution: bool = False) -> str:
    """What the user decided -- standing over the plan where they conflict.
    `since_execution`: only what they decided once the execution started; the
    earlier decisions are in the approved design already."""
    started = (st.get("execution") or {}).get("started", "")
    if since_execution and not started:
        return ""                       # not started: every decision is in the design
    mine = [d for d in st.get("decisions") or [] if d.get("by") == "user"
            and (not since_execution or (d.get("at") or "") >= started)]
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
                            (step_text(step) if step else awaiting)
                            + "\nIt starts only when the user says go now (you set `go`): ask "
                              "for it; what they said before does not start it."))
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
    running = running_jobs(Path(run_dir))
    parts = [_block("AN ASIDE -- the team is working; nothing waits for your answer",
                    prompt_files.text("notes/execute_aside", run_dir)),
             _block("THE USER SAYS", user),
             _block("WHAT IS HAPPENING NOW", activity),
             *([_block("RUNNING JOBS -- measured by code just now", running)] if running else []),
             _block("THE STEP TABLE", S.render_within(st, ["execution"],
                                                     state_room(budget, [user, activity])))]
    status = st.get("status") or {}
    if status.get("summary"):
        parts.append(_block("WHERE THINGS STAND", status["summary"]))
    parts.append(_block("FILES", f"Everything is in {run_dir}: artifacts/step_NN/ (outputs), "
                                 f"work/ (code), jobs/ (logs), state/research.md"))
    return "\n\n".join(parts)


def running_jobs(run_dir: Path, now: float | None = None) -> str:
    """What the running jobs have done so far, measured: how long each has
    run, its last log line, and its step's files -- lines, size, and how
    recently each changed -- so how far along it is, and how long it has
    left, can be worked out. Measured (run 1119): asked when a job would
    finish, the discussor had only "Working now: step 2", said it could not
    tell, and asked the team to report its progress."""
    import time

    now = now or time.time()
    out = []
    for job in J.records(run_dir):
        state = J.status(job)
        if state["state"] != "running":
            continue
        out.append(f"{job['id']} ({job.get('name') or job['step']}), step {job['step']}: "
                   f"running {(now - job['started']) / 60:.1f} min since {job.get('started_at', '')}"
                   + (f" on GPUs {', '.join(map(str, job['gpus']))}" if job.get("gpus") else "")
                   + (f"\n  last log line: {tail[-300:]}" if (tail := _last_line(J.tail(job, 5)))
                      else ""))
        folder = Path(run_dir) / "artifacts" / folder_name(job["step"])
        files = sorted((p for p in folder.rglob("*") if p.is_file()),
                       key=lambda p: -p.stat().st_mtime)    # what is being written first
        for p in files[:FILES]:
            age = now - p.stat().st_mtime
            out.append(f"  {p.relative_to(run_dir).as_posix()}: {_lines(p)}{_size(p)}, "
                       f"changed {_ago(age)}"
                       + (" -- being written" if age < 120 and p.stat().st_mtime > job["started"]
                          else ""))
    return "\n".join(out)


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").replace("\r", "\n").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def _lines(p: Path) -> str:
    """Its line count, for a text file small enough to count."""
    if p.suffix not in (".jsonl", ".txt", ".csv", ".tsv", ".log") or p.stat().st_size > 200e6:
        return ""
    with open(p, "rb") as handle:
        return f"{sum(1 for _ in handle)} lines, "


def _ago(seconds: float) -> str:
    return f"{seconds:.0f} s ago" if seconds < 120 else f"{seconds / 60:.0f} min ago"


def step_results(st: dict, *, step: dict, record: dict, report: dict, run_dir: str) -> str:
    """For the discussor, once a step is checked: its results, to tell the
    user in the chat while the work goes on."""
    metrics = "\n".join(f"- {m['name']}: {m['value']}" for m in report.get("metrics") or [])
    folder = Path(run_dir) / "artifacts" / folder_name(step["id"])
    return "\n\n".join([
        _block("A STEP IS DONE -- tell the user its results; the work goes on, nothing waits "
               "for your answer", prompt_files.text("notes/step_results", run_dir)),
        _block("THE STEP", f"Step {step['id']}: {step['title']}\n{step.get('detail', '')[:1500]}"),
        _block("WHAT ITS OWNER REPORTED", "\n".join(filter(None, [
            report.get("summary") or record.get("summary", ""),
            f"Metrics:\n{metrics}" if metrics else "",
            f"Files: {folder}"]))),
        _block("THE VALIDATOR'S VERDICT", record.get("verdict") or "-"),
        _block("THE STEP TABLE", S.render(st, ["execution"], brief=True)),
    ])


# ---- reviewer -----------------------------------------------------------------------------


def paper(st: dict, *, run_dir: str, latex: str, python: str = "") -> str:
    """The paper writer's brief: what the user wanted, what was planned and
    done, the files, the literature found so far -- and, in a run that
    continues another, that run's paper to revise. Measured (run 1455): with
    only the run's own records as "evidence", the paper cited 3 works."""
    root = Path(run_dir)
    folder = root / "paper"
    compile_ = (f"cd {folder} && {latex} main.tex" if latex.endswith("tectonic") else
                f"cd {folder} && {latex} -pdf main.tex" if latex.endswith("latexmk") else
                f"cd {folder} && {latex} main.tex; bibtex main; {latex} main.tex; {latex} main.tex"
                if latex else "")
    records = {r["id"]: r for r in (st.get("execution") or {}).get("steps") or []}
    plan = []
    for s in _steps(st):
        r = records.get(s["id"], {})
        plan.append("\n".join(filter(None, [
            f"{s['title']} [{r.get('status', 'pending')}] -- files in "
            f"{root / 'artifacts' / folder_name(s['id'])}",
            f"  planned: {s.get('detail', '')}", f"  done when: {s.get('check', '')}",
            f"  what it did: {r.get('summary', '')}" if r.get("summary") else "",
            f"  checked: {r['verdict']}" if r.get("verdict") else ""])))
    parts = [f"Today is {_today()}.",
             _block("WHAT THE USER WANTED -- the brief they approved",
                    (st.get("brief") or {}).get("text") or S.render(st, ["goal"])),
             _block("THE STUDY AS DESIGNED", S.render({**st, "hypotheses": _live(st)},
                                                      ["hypotheses", "rubric", "protocol"])),
             _block("WHAT WAS PLANNED AND DONE", "\n\n".join(plan) or "(no steps)"),
             _block("THE FILES -- read them; every number comes from them", "\n".join(
                 _listing(root) + [f"Code (a git repository): {root / 'work'}",
                                   f"Job logs: {root / 'jobs'}"])),
             _block("THE LITERATURE FOUND SO FAR -- start related work here, then search for "
                    "more with the research skill's tools", literature(st))]
    decided = user_decisions(st)
    if decided:
        parts.append(_block("THE USER'S DECISIONS", decided))
    changed = amendments_text(st)
    if changed:
        parts.append(_block("DEVIATIONS FROM THE APPROVED DESIGN -- report every one, with why; "
                            "research changes in the method section", changed))
    previous = (st.get("status") or {}).get("previous")
    if previous and (Path(previous) / "paper" / "main.tex").is_file():
        parts.append(_block("THE LAST RUN'S PAPER -- revise it, do not start over",
                            f"{Path(previous) / 'paper'} (main.tex, references.bib, figures, "
                            f"tables). Copy what still holds into {folder}; change what this "
                            f"run changed and what the review below asked for."))
    if st.get("reviews"):
        parts.append(_block("THE REVIEWS -- answer them where the files allow",
                            S.render({"reviews": st["reviews"][-1:]}, ["reviews"]).split("\n", 1)[-1]))
    parts.append(_block("THE PAPER", "\n".join(filter(None, [
        f"Write it in {folder} (main.tex, figures/, tables/, references.bib); "
        f"{folder / 'main.pdf'} is what the user reads.",
        "Every references.bib entry needs an eprint (arXiv id), doi or pmid: each is looked "
        "up, and one that is not found is sent back to you.",
        f"Compile: {compile_}" if compile_ else
        "No LaTeX compiler is installed: leave the folder ready to compile and say so.",
        f"Python for the figure and table scripts: {python}" if python else ""]))))
    return "\n\n".join(parts)


def amendments_text(st: dict) -> str:
    """The design's amendments during the execution, in words."""
    return "\n".join(
        f"- {a['id']} ({a['kind']}, by {a['by']}): {a['target']} {a['of']} {a['field']} "
        f"changed from {str(a['from'])[:300]!r} to {str(a['to'])[:300]!r} -- {a['why']}"
        for a in (st.get("execution") or {}).get("amendments") or [])


def literature(st: dict) -> str:
    """What the researchers found in the literature: checked findings and the
    answers to research questions -- not the run's own records."""
    found = [e for e in st.get("evidence") or [] if e.get("verified") is not None
             or e.get("found_by") == "researcher"]
    lines = [f"- {e['id']} [{ {True: 'verified', False: 'NOT FOUND -- do not cite'}.get(e.get('verified'), 'unchecked') }] "
             f"{e.get('claim', '')} -- {e.get('source', '')}" for e in found]
    answers = [q for q in st.get("open_questions") or []
               if q.get("for") == "research" and q.get("answer")]
    lines += [f"- {q['id']} {q['text']}\n  {q['answer'][:3000]}" for q in answers]
    novelty = [f"- {h['id']}: {h['novelty']}" for h in _live(st) if h.get("novelty")]
    if novelty:
        lines += ["Novelty, as the researcher judged it:", *novelty]
    return "\n".join(lines) or "(none: search for it)"


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
          previous: dict | None = None, dispatch: dict | None = None) -> str:
    """A step's work order: everything its owner needs to do the step alone,
    and nothing about the rest of the study. Measured (run 1134): owners got
    the study's goal and scope, the hypotheses and "After steps: 2 / Rubric:
    R3 / Cost: ...", and still could not tell what done meant."""
    parts = [f"Today is {_today()}.",
             _block("YOUR JOB", "\n".join(filter(None, [
                 f"You are {node['name']}.",
                 f"The work: {step.get('detail') or step['title']}",
                 f"Done when: {step['check']}" if step.get("check") else ""])))]
    criteria = criteria_text(st, list(step.get("rubric") or []))
    parts.append(_block("HOW YOUR WORK IS CHECKED",
                        "A validator reads the files you list and checks \"Done when\""
                        + (" and these criteria:\n" + criteria if criteria else ".")))
    # What the dispatch found it needs (execute/nodes.dispatch), and the
    # orchestrator's own note (a retry's "what to do differently") as it
    # wrote it, whatever the dispatch made of it.
    context = ((dispatch or {}).get("context") or "").strip()
    if task.strip() and task.strip() not in context:
        context = (f"{context}\n\nFrom the orchestrator: {task.strip()}" if context
                   else task.strip())
    if context:
        parts.append(_block("WHAT THE STUDY NEEDS FROM THIS STEP -- the names it uses and the "
                            "rules it follows", context))
    if previous and (previous.get("verdict") or previous.get("problems")):
        parts.append(_block("THE LAST ATTEMPT FAILED -- do it differently", "\n".join(
            [previous.get("verdict", "")] + [f"- {p}" for p in previous.get("problems") or []])))
    earlier = upstream(st, step, paths, every=node["role"] == "writer")
    obtain = (dispatch or {}).get("obtain") or []
    if obtain:
        parts.append(_block("TO OBTAIN -- not on this machine yet: get each from its official "
                            "source, and say in your report where it came from",
                            "\n".join(f"- {o}" for o in obtain)))
    exact = dispatched({"dispatch": dispatch or {}})
    if exact:
        parts.append(_block("YOUR INPUTS -- the files you read, checked to exist before you "
                            "started", exact + "\n\nThe earlier work they come from:\n" + earlier))
    else:
        parts.append(_block("YOUR INPUTS -- files earlier work produced", earlier))
    decided = user_decisions(st, since_execution=True)
    if decided:
        parts.append(_block("THE USER'S DECISIONS -- they stand over the plan where they "
                            "conflict; apply them and say so in your report", decided))
    if node["role"] == "writer" and _live(st):
        parts.append(_block("HYPOTHESES -- say in your report which the results support or refute",
                            S.render({**st, "hypotheses": _live(st)}, ["hypotheses"])))
    parts.append(_block("WHERE TO WORK", "\n".join([
        *_execution_lines(st, paths),
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
        f"--step {step['id']}` lists them)"])))
    return "\n\n".join(parts)


RULE_LABELS = {"stop_criteria": "Stop criteria", "other": "Other constraints"}


def rule_names(step: dict) -> list[str]:
    """The parts of the protocol and constraints a step follows, by name."""
    return [RULE_LABELS.get(k, k.capitalize()) for k in step.get("follows") or []]


def criteria_text(st: dict, ids) -> str:
    """Rubric items as plain criteria: what, measured how, passing when --
    no ids, weights or step numbers, which mean nothing to an owner."""
    wanted = list(dict.fromkeys(ids))
    lines = []
    for r in st.get("rubric") or []:
        if r.get("id") in wanted:
            line = f"- {r['criterion']}"
            if r.get("measure"):
                line += f", measured by {r['measure']}"
            if r.get("threshold"):
                line += f"; passes at {r['threshold']}"
            lines.append(line)
    return "\n".join(lines)


def dispatch(st: dict, *, node: dict, step: dict, note: str, paths: dict,
             previous: dict | None = None) -> str:
    """For the dispatch before a step runs: the step, the files the work it
    depends on really left (listed by code, not as reported), the protocol
    parts it follows, the user's decisions -- to check that everything it
    needs is there and name exactly what its owner reads."""
    run = Path(paths["run"])
    earlier = []
    records = {r["id"]: r for r in (st.get("execution") or {}).get("steps") or []}
    for sid in _ancestors(st, step["id"]):
        plan = _step(st, sid) or {}
        r = records.get(sid) or {}
        folder = run / "artifacts" / folder_name(sid)
        found = sorted(p for p in folder.rglob("*") if p.is_file()) if folder.is_dir() else []
        files = [f"    {p.relative_to(run).as_posix()} ({_size(p)})" for p in found[:FILES]]
        if len(found) > FILES:
            files.append(f"    ... and {len(found) - FILES} more")
        earlier.append("\n".join([
            f"{plan.get('title') or 'Earlier work'} [{r.get('status', 'pending')}]: "
            f"{folder.relative_to(run).as_posix()}/",
            f"  it was to deliver: {plan.get('check') or '-'}",
            f"  its report: {r.get('summary') or '-'}",
            *([f"  checked: {r['verdict']}"] if r.get("verdict") else []),
            "  files there now:", *(files or ["    (none)"])]))
    rules = []
    for key in step.get("follows") or []:
        value = ((st.get("protocol") or {}).get(key)
                 if key in ("baselines", "metrics", "statistics", "seeds", "stop_criteria")
                 else (st.get("constraints") or {}).get(key))
        if value:
            rules.append(f"{RULE_LABELS.get(key, key.capitalize())}: "
                         f"{value if isinstance(value, str) else '; '.join(map(str, value))}")
    parts = [f"Today is {_today()}.",
             _block("THE STEP ABOUT TO RUN", step_text(step)
                    + f"\nDone by: {node['name']} ({node['role']})"),
             _block("HOW ITS WORK IS CHECKED", criteria_text(st, list(step.get("rubric") or []))
                    or "(its check only)"),
             _block("WHAT IT DEPENDS ON -- the files there now, listed by code",
                    "\n\n".join(earlier) or "(nothing: it depends on no earlier step)"),
             _block("THE PROTOCOL AND CONSTRAINTS IT FOLLOWS", "\n".join(rules) or "(none named)")]
    decided = user_decisions(st, since_execution=True)
    if decided:
        parts.append(_block("THE USER'S DECISIONS -- they stand over the plan", decided))
    if previous and (previous.get("verdict") or previous.get("problems")):
        parts.append(_block("ITS LAST ATTEMPT FAILED", "\n".join(
            [previous.get("verdict", "")] + [f"- {p}" for p in previous.get("problems") or []])))
    if note.strip():
        parts.append(_block("THE ORCHESTRATOR'S NOTE", note.strip()))
    parts.append(_block("WHERE IT WILL WORK -- its owner, not you", "\n".join([
        f"Its owner runs with full access: it writes files, runs commands, uses the "
        f"GPUs and the network -- it downloads datasets, models and packages itself. "
        f"You are read-only, with no network; that is only you.",
        f"The run folder: {paths['run']} (paths may be relative to it)",
        f"Its outputs folder: {paths['artifacts']}",
        f"The code folder: {paths['work']}",
        *_execution_lines(st, paths)])))
    return "\n\n".join(parts)


def _execution_lines(st: dict, paths: dict) -> list[str]:
    """The execution's start, as code keeps it (execute/nodes.start_record)."""
    started = (st.get("execution") or {}).get("started")
    if not started:
        return []
    record = Path(paths["run"]) / "execution" / "execution_start.json"
    return [f"The execution started at {started} (UTC); code keeps that record, with the "
            f"user's go that started it, in {record}"]


#: Files listed per earlier step in the dispatch's brief.
FILES = 60


def _size(p: Path) -> str:
    n = p.stat().st_size
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def dispatched(record: dict) -> str:
    """The dispatch's inputs, for its owner."""
    d = record.get("dispatch") or {}
    return "\n".join(f"- {i['path']}: {i['holds']}" for i in d.get("inputs") or [])


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
        plan = _step(st, sid) or {}
        verdict = r.get("verdict", "")
        state = (" [partial: some of it is missing]" if verdict.startswith("partial") else
                 " [passed by the user's decision, not by its check]"
                 if verdict.startswith("passed by") else
                 "" if r["status"] == "pass" else f" [{r['status']}]")
        lines.append(f"{plan.get('title') or 'Earlier work'}{state}: "
                     f"{Path(paths['run']) / 'artifacts' / folder_name(sid)}\n"
                     f"  what it did: {r.get('summary') or '-'}"
                     + (f"\n  files: {', '.join(r['artifacts'][:20])}" if r.get("artifacts") else "")
                     + (f"\n  checked: {r['verdict']}" if r.get("verdict") else ""))
    return "\n".join(lines) or "(none: you start from the rules below)"


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
        _block("NOT THIS EXECUTION'S WORK", (
            "artifacts/_before_redesign-*/, artifacts/_rewound/ and jobs/_earlier/ hold what "
            "an earlier execution or a rewound attempt left, set aside by code and readable "
            "only by the run's owner. They are not this step's outputs or jobs: do not judge "
            "the step by them, nor its report against them.")),
        *([_block("THE USER'S DECISIONS -- they stand over the plan where they conflict: "
                  "work that follows one is not a failure", decided)]
          if (decided := user_decisions(st, since_execution=True)) else []),
        _block("THE OWNER'S REPORT -- check it against the files", "\n".join([
            f"Summary: {report.get('summary', '')}",
            "Artifacts:\n" + "\n".join(f"- {a}" for a in report.get("artifacts") or []),
            "Commands:\n" + "\n".join(f"- {c}" for c in report.get("commands") or []),
            "Metrics:\n" + metrics if metrics else "",
            "Problems it saw:\n" + "\n".join(f"- {p}" for p in report.get("problems") or [])
            if report.get("problems") else ""]).strip()),
        *([_block("A LITERATURE STEP", (
            "Its owner is a researcher with the research skill's tools, which reach OpenAlex, "
            "Semantic Scholar (no key needed) and arXiv. A source recorded as a coverage gap "
            "counts only when those tools failed on it, with their error recorded; a gap "
            "for want of an API key, or code that never asked, is not one.")
            + ("\nPapers the research tool could not find: " + "; ".join(report["unverified"])
               if report.get("unverified") else ""))]
          if node.get("role") == "researcher" else []),
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
