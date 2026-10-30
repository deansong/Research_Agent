"""
WHAT:  What a node would be sent on its next call, built now from the run's
       state: its standing instructions, the skills put in front of the
       message, the message, the answer's JSON schema, the earlier turns it
       would see -- in the shape a recorded turn has (Session._wire), so the
       node window shows it as it shows a call that ran.
WHY:   To read a node's input before it runs, not only after.
CONCEPT: The same builders the nodes use (views.*), on the state as it is.
       What is only known when the call is made -- the orchestrator's task
       for a step, the owner's report a validator checks, the user's next
       words -- is a marked placeholder.
"""

from __future__ import annotations

import json
from pathlib import Path

from research_agent import state as S

# How a placeholder looks: plainly not text the model would get.
def _later(what: str) -> str:
    return f"[decided when the call is made: {what}]"


def preview(folder: Path, stage: str, node: str, step: str = "") -> dict:
    """{label, prompt, sent, notes, steps, step} for `node`'s next call.
    Raises ValueError for a node there is nothing to preview for."""
    from research_agent.bootstrap import config as boot_config
    from research_agent.bootstrap import prompts as boot_prompts
    from research_agent.bootstrap import schemas as boot_schemas
    from research_agent.bootstrap import views as boot_views
    from research_agent.bootstrap.nodes import DISCUSSOR_SESSION, Run
    from research_agent.execute import config as exec_config
    from research_agent.execute import nodes as exec_nodes
    from research_agent.execute import prompts as exec_prompts
    from research_agent.execute import schemas as exec_schemas
    from research_agent.execute import views as exec_views

    folder = Path(folder)
    st = S.Store(folder).read()
    boot = boot_config.load(folder)
    notes: list[str] = []
    out = {"node": node, "steps": [], "step": ""}

    if node == "discussor":
        execution = stage == "execution"
        prompt = (exec_views if execution else boot_views).discussor_turn(
            st, dict(st.get("versions") or {}), user=_later("your next message"))
        instructions = (exec_prompts if execution else boot_prompts).instructions(folder)["discussor"]
        return _shape(out, label="discussor", prompt=prompt, instructions=instructions,
                      role=boot.roles["discussor"],
                      schema=exec_schemas.ExecDiscussorOutput if execution
                      else boot_schemas.DiscussorOutput,
                      skills=boot.skills.get("discussor"), skills_dir=boot.skills_dir,
                      workspace=folder, earlier=_turns(folder, DISCUSSOR_SESSION),
                      notes=notes + ["Shown with nothing new since its last turn: what the "
                                     "team did since is added then."])

    if stage == "bootstrap" and node in boot_config.ROLES:
        run = Run(folder=folder, roles=boot.roles, skills_dir=boot.skills_dir, quiet=True)
        task = _later("the orchestrator's task for it")
        instructions = boot_prompts.instructions(folder)[node]
        label = node
        if node == "planner":
            # Two calls: the study, then the steps.
            job = step if step in ("study", "steps") else "study"
            out.update(steps=["study", "steps"], step=job)
            label = f"planner ({job})"
            if job == "study":
                instructions = boot_prompts.study(folder)
            else:
                notes.append("Shown with the study as it is now: the call is made right after "
                             "the planner's study, with that study.")
        prompt = {"orchestrator": lambda: boot_views.orchestrator(st, requests=[]),
                  "researcher": lambda: boot_views.researcher(st, task=task),
                  "planner": lambda: (boot_views.study if out["step"] == "study"
                                      else boot_views.planner)(st, task=task, run_dir=folder),
                  "designer": lambda: boot_views.designer(st, task=task, run_dir=folder,
                                                          skills=run.catalogue())}[node]()
        schema = {"orchestrator": boot_schemas.OrchestratorOutput,
                  "researcher": boot_schemas.ResearcherOutput,
                  "planner": boot_schemas.StudyOutput if out["step"] == "study"
                  else boot_schemas.PlanOutput,
                  "designer": boot_schemas.TeamOutput}[node]
        return _shape(out, label=label, prompt=prompt, instructions=instructions,
                      role=boot.roles[node], schema=schema, skills=boot.skills.get(node),
                      skills_dir=boot.skills_dir,
                      workspace=folder / "research" if node == "researcher" else folder,
                      earlier=0, notes=notes)

    # The execution's nodes -- also before it starts, from the approved design.
    exe = exec_config.load(folder)
    run = exec_nodes.ExecRun.from_config(folder, exe, boot.roles["discussor"])
    run.quiet = True
    design = exec_nodes.Design.read(st, skills=run.known_skills())
    instructions = exec_prompts.instructions(folder)
    if not (st.get("execution") or {}).get("steps"):
        st = {**st, "execution": {"steps": [{"id": s["id"], "title": s["title"],
                                             "status": "pending"}
                                            for s in (st.get("plan") or {}).get("steps") or []]}}
        notes.append("The execution has not started: shown as it would be at its start.")
    records = exec_nodes.records(st)
    limit = exe.max_attempts

    if node == "orchestrator":
        moves = exec_nodes.open_moves(st, limit)
        prompt = exec_views.orchestrator(
            st, design_part=design.orchestrator.get("instructions", ""),
            moves=exec_nodes.describe_moves(moves, st, limit), budget=_later("the budget used"),
            requests=[])
        return _shape(out, label="orchestrator", prompt=prompt,
                      instructions=instructions["orchestrator"], role=exe.roles["orchestrator"],
                      schema=exec_schemas.ExecDecision,
                      skills=exec_nodes.skills_for(run, design.orchestrator, "orchestrator"),
                      skills_dir=exe.skills_dir, workspace=folder, earlier=0, notes=notes)
    if node == "paper":
        prompt = exec_views.paper(st, run_dir=str(folder),
                                  latex=exec_config.find_latex(exe), python=exe.python)
        return _shape(out, label="paper", prompt=prompt, instructions=instructions["paper"],
                      role=exe.roles["writer"], schema=exec_schemas.PaperReport,
                      skills=exec_nodes.skills_for(run, {"name": "paper", "role": "researcher",
                                                         "skills": list(exe.paper_skills)},
                                                   "paper"),
                      skills_dir=exe.skills_dir, workspace=folder / "paper", earlier=0,
                      notes=notes)
    if node == "reviewer":
        focus = exec_nodes.PANEL[0][1]
        prompt = exec_views.reviewer(st, run_dir=str(folder), focus=focus)
        return _shape(out, label="reviewer R1", prompt=prompt,
                      instructions=instructions["reviewer"], role=exe.roles["reviewer"],
                      schema=exec_schemas.ReviewOutput,
                      skills=exec_nodes.skills_for(run, {"name": "reviewer",
                                                         "skills": list(exe.reviewer_skills)},
                                                   "reviewer"),
                      skills_dir=exe.skills_dir, workspace=folder, earlier=0,
                      notes=notes + ["The first of the three reviewers; the others differ "
                                     "only in their focus, and the meta-review follows."])

    # A step's owner, or the validator checking one of its steps.
    if node == "validator":
        owned = [s for s in design.owner_of]
    else:
        name = next((g for g, n in design.owners.items()
                     if g == node or n["name"] == node), None)
        if name is None:
            raise ValueError(f"Nothing to preview for {node!r}.")
        owned = list(design.owners[name].get("steps") or [])
    open_ = [s for s in owned if records.get(s, {}).get("status") not in exec_nodes.SETTLED]
    sid = step if step in owned else (open_ or owned or [""])[0]
    if not sid:
        raise ValueError(f"{node!r} owns no steps.")
    plan_step = exec_nodes.plan_step(st, sid)
    owner = design.owners[design.owner_of[sid]]
    rec = records.get(sid, {})
    out.update(steps=owned, step=sid)
    if node == "validator":
        prompt = exec_views.validator(
            st, design_part=design.validator.get("instructions", ""), node=owner, step=plan_step,
            report={"summary": _later("the owner's report: its summary, artifacts, commands, "
                                      "metrics and problems")},
            paths=run.paths(sid))
        return _shape(out, label=f"validate step {sid}", prompt=prompt,
                      instructions=instructions["validator"], role=exe.roles["validator"],
                      schema=exec_schemas.Verdict,
                      skills=exec_nodes.skills_for(run, design.validator, "validator"),
                      skills_dir=exe.skills_dir, workspace=folder, earlier=0, notes=notes)
    role = owner["role"]
    prompt = exec_views.owner(
        st, node=owner, step=plan_step,
        task="", paths=run.paths(sid), jobs_command=run.jobs_command,
        previous=rec if rec.get("attempts", 0) >= 1 and rec.get("status") != "pending" else None,
        dispatch={"context": _later(
            "the dispatch's context: what the step needs besides its detail -- the names it "
            "uses, the protocol details it follows"
            + (f" ({', '.join(plan_step.get('follows'))})" if plan_step.get("follows") else "")),
            "inputs": [{"path": _later("the exact files the dispatch checks exist"),
                        "holds": "what each holds"}]})
    if rec.get("attempts"):
        notes.append(f"Step {sid} has had {rec['attempts']} attempt(s): shown as its next one.")
    return _shape(out, label=f"step {sid} ({owner['name']})", prompt=prompt,
                  instructions=instructions[role], role=exe.roles[role],
                  schema=exec_schemas.StepReport, skills=exec_nodes.skills_for(run, owner),
                  skills_dir=exe.skills_dir, workspace=run.work, earlier=0, notes=notes)


def _shape(out: dict, *, label, prompt, instructions, role, schema, skills, skills_dir,
           workspace, earlier: int, notes: list[str]) -> dict:
    """In the shape of a recorded turn's `sent` (research_backend Session._wire)."""
    from research_backend import skills as rb_skills
    from research_backend.backends import load
    from research_backend.backends._schema import strict_json_schema

    try:
        backend = load(role.provider)
        route = getattr(backend, "INSTRUCTIONS_ROUTE", "with every message")
        adds = getattr(backend, "PROVIDER_ADDS", "not known")
    except Exception:  # noqa: BLE001 -- an unknown provider still previews
        route, adds = "with every message", "not known"
    before = rb_skills.block(skills, skills_dir, workspace=str(workspace)) if skills else ""
    if before:
        before = f"{before}\n\n{'=' * 70}\n\n"
        if earlier:
            notes = notes + ["The skills go in front of a conversation's first message only: "
                             "this one has had them already, unless they changed."]
    return {**out, "label": label, "prompt": prompt, "runs_on": str(role), "notes": notes,
            "sent": {"route": route, "provider_adds": adds, "earlier_turns": earlier,
                     "instructions": instructions, "before_message": before,
                     "schema": strict_json_schema(schema)}}


def _turns(folder: Path, session: str) -> int:
    try:
        return len(json.loads((folder / "rb" / "sessions" / session / "session.json")
                              .read_text()).get("turns") or [])
    except (OSError, ValueError):
        return 0
