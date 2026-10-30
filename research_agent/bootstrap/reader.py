"""
WHAT:  The reader check: each step of a new design is given, alone, to a fast
       model as the context code will give the step's subagent; the questions
       it would have to ask before it could do the work go back to the
       designer.
WHY:   A step is its subagent's whole context. Code can catch a letter or a
       step number; only a reader catches "the specification", "the
       prescribed evaluations", or a step asked to copy a design its subagent
       never sees (run 1119, designer-006).
CONCEPT: The context is built by the execution's own builder (execute/views.
       owner) from the design as it would be approved, without what the
       orchestrator adds at run time: a step must stand without it. Its inputs
       are the earlier steps' folders, each with what that step is to
       deliver. One call per step, in parallel, with no tools (it must not go
       looking: the files are not there yet), on the [design] reader model of
       bootstrap/config.toml; a reader that fails is skipped and never blocks
       the design.
"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor

import research_backend as RB

from research_agent import prompt_files
from research_agent.bootstrap import schemas

#: The model that reads: config.toml [design] reader, or the run's config.json.
ROLE = "reader"
#: Readers at a time.
AT_ONCE = 6
#: Questions kept per step.
KEEP = 5


def contexts(run, st: dict, out: schemas.DesignOutput) -> dict[str, str]:
    """Each step's subagent context, as code would build it from this design."""
    from research_agent.execute import config as exec_config
    from research_agent.execute import jobs, views as exec_views

    steps = [{**s.model_dump(), "rubric": [r.id for r in out.rubric if r.assessed_at == s.id]}
             for s in out.steps]
    seen = {**st, "rubric": [r.model_dump() for r in out.rubric],
            "hypotheses": [{"status": "proposed", **h.model_dump()} for h in out.hypotheses],
            "plan": {"summary": out.summary, "steps": steps},
            # Not run yet: each earlier step shows what it is to deliver, where
            # its report and files will be.
            "execution": {"steps": [{"id": s["id"], "status": "pass",
                                     "summary": f"(not run yet) it is to deliver: {s['check']}"}
                                    for s in steps]}}
    try:
        python = exec_config.load(run.folder).python
    except Exception:  # noqa: BLE001 -- a context without it still reads
        python = None
    owners = {sid: n.model_dump() for n in out.nodes for sid in n.steps}
    found = {}
    for step in steps:
        node = owners.get(step["id"]) or {"name": "the worker", "role": "worker"}
        paths = {"run": str(run.folder), "work": str(run.folder / "work"),
                 "artifacts": str(run.folder / "artifacts" / exec_views.folder_name(step["id"])),
                 "python": python}
        found[step["id"]] = exec_views.owner(
            seen, node=node, step=step, task="", paths=paths,
            jobs_command=f"{sys.executable} {jobs.__file__} --run {run.folder}")
    return found


def read(run, st: dict, out: schemas.DesignOutput) -> dict[str, list[str]]:
    """{step id: the questions its subagent would have to ask}."""
    from research_agent.bootstrap import config

    found = contexts(run, st, out)
    if ROLE not in run.roles:
        spec = config.load(run.folder).reader or run.roles["designer"]
        run.roles = {**run.roles, ROLE: spec}
    instructions = prompt_files.instructions("roles/bootstrap/reader", run.folder)
    first = int(run.next_name("reader").rsplit("-", 1)[1])

    def one(n: int, sid: str) -> tuple[str, list[str]]:
        try:
            data, _ = run.call(ROLE, brief=found[sid], schema=schemas.ReaderOutput, skills=None,
                               instructions=instructions, access="none",
                               session=f"reader-{first + n:03d}",
                               label=f"designer (reader, step {sid})")
        except RB.BackendCancelled:
            raise
        except RB.BackendError as exc:
            run.say(f"  ! the reader of step {sid} could not read it ({str(exc)[:200]}): skipped")
            return sid, []
        return sid, [q.strip() for q in data.questions if q.strip()][:KEEP]

    with ThreadPoolExecutor(AT_ONCE) as pool:
        return dict(pool.map(lambda job: one(*job), enumerate(found)))


def problems(questions: dict[str, list[str]]) -> list[str]:
    """The questions, as problems the designer is sent back with."""
    out = [f"step {sid}: a reader given only its subagent's context would ask: "
           + " | ".join(qs) for sid, qs in questions.items() if qs]
    if out:
        out.append("Each question is missing from that step's own context: answer it in "
                   "the step's `detail` or `check` -- not in the protocol, another step or "
                   "the hypotheses, which its subagent never sees.")
    return out
