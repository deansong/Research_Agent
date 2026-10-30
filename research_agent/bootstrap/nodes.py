"""
WHAT:  The five bootstrap nodes as plain functions: each makes one
       research_backend call with its brief and turns the answer into state
       operations.
WHY:   Nodes propose; code decides. An answer naming an id that does not
       exist, or a plan step nobody owns, goes back to the model with what is
       wrong (up to twice) instead of into the state.
CONCEPT: Every call but the discussor's is a NEW research_backend session,
       named <role>-NNN in <run>/rb -- so it starts clean and can be read
       afterwards (`RB.list_sessions(home=<run>/rb)`). The discussor has one
       session for the whole run: the conversation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from research_agent import autopilot, chat, inbox
from research_agent import run as run_base
from research_agent import state as S
from research_agent.bootstrap import prompts, schemas, verify, views
from research_agent.bootstrap import config

DISCUSSOR_SESSION = "discussor"
NodeFailed = run_base.NodeFailed


@dataclass
class Run(run_base.Run):
    """One bootstrap run: its folder, which provider each role uses, and
    which skills each node gets (bootstrap/config.toml unless given)."""

    def __post_init__(self):
        super().__post_init__()
        self.instructions = {**prompts.instructions(self.folder), **self.instructions}
        if self.skills is None or self.skills_dir is None:
            base = config.defaults()
            self.skills = base.skills if self.skills is None else self.skills
            self.skills_dir = base.skills_dir if self.skills_dir is None else self.skills_dir

    @classmethod
    def from_config(cls, folder: Path, cfg: "config.BootstrapConfig", *, progress=False,
                    trace=False) -> "Run":
        return cls(folder, cfg.roles, skills_dir=cfg.skills_dir, skills=cfg.skills,
                   progress=progress, trace=trace)


@dataclass
class Outcome:
    """What a node produced: state operations, a one-line summary, and its
    session."""

    ops: list[dict] = field(default_factory=list)
    summary: str = ""
    session: str = ""
    data: object = None


def _result_op(node: str, job: str, asked: str, summary: str, session: str) -> dict:
    return {"op": "add", "section": "results", "value": {
        "node": node, "job": job, "asked": asked[:500], "summary": summary, "session": session}}


def _ids(st: dict, section: str) -> set[str]:
    return {x.get("id") for x in st.get(section) or []}


# ---- discussor -----------------------------------------------------------------------------


@dataclass
class DiscussorTurn(Outcome):
    reply: str = ""
    requests: list[dict] = field(default_factory=list)
    hand_to: str = "user"
    """Who the discussor says acts next: "user" or "orchestrator"."""


def discussor(run: Run, st: dict, *, seen: dict, user: str = "", note: str = "") -> DiscussorTurn:
    open_ids = {q["id"] for q in st.get("open_questions") or [] if q.get("status") == "open"}

    def check(out: schemas.DiscussorOutput) -> list[str]:
        problems = [f"answers: {a.id} is not an open question" for a in out.answers
                    if a.id not in open_ids]
        problems += [f"approve: there is no {s} yet" for s in out.approve
                     if not S.version(st, s)]
        if out.hand_to == "orchestrator" and not (out.requests or out.approve):
            problems.append('hand_to is "orchestrator" but there is no request or approval '
                            'for the team to act on: add the request, or hand to "user"')
        return problems

    budget = run.brief_budget("discussor")
    brief = views.discussor_turn(st, seen, user=user, note=note, budget=budget)

    def takeover() -> str:
        # A new conversation: what was said, and the whole state -- both
        # within the brief's budget on the model now taking it.
        room = run.brief_budget("discussor")
        said = chat.handover(run.folder, room // 3)
        return said + "\n\n" + views.discussor_turn(st, {}, user=user, note=note,
                                                     budget=room - len(said))
    if run.takes_over(DISCUSSOR_SESSION, "discussor"):
        brief = takeover()
    with run.discussor_lock:
        out, session = run.call("discussor", brief=brief,
                                schema=schemas.DiscussorOutput, session=DISCUSSOR_SESSION,
                                check=check, takeover=takeover)
    ops: list[dict] = []
    goal = {k: v for k, v in out.goal.model_dump().items() if v}
    if goal:
        ops.append({"op": "merge", "section": "goal", "value": goal})
    constraints = {k: v for k, v in out.constraints.model_dump().items() if v}
    if constraints:
        ops.append({"op": "merge", "section": "constraints", "value": constraints})
    # The autopilot's answer is recorded once, as its own (autopilot.decision_op).
    if not user.startswith(autopilot.MARK):
        ops += [{"op": "add", "section": "decisions", "value": {**d.model_dump(), "by": "user"}}
                for d in out.decisions]
    ops += [{"op": "add", "section": "assumptions", "value": a.model_dump()}
            for a in out.assumptions]
    ops += [{"op": "update", "section": "open_questions", "id": a.id,
             "value": {"status": "answered", "answer": a.answer}} for a in out.answers]
    if out.brief.strip():
        ops.append({"op": "set", "section": "brief", "value": {"text": out.brief.strip()}})
    ops += [{"op": "approve", "section": s} for s in dict.fromkeys(out.approve)]
    return DiscussorTurn(ops=ops, summary=out.reply[:200], session=session, data=out,
                         reply=out.reply, requests=[r.model_dump() for r in out.requests],
                         hand_to=out.hand_to)


def aside(run: Run, st: dict, *, user: str, activity: str = "") -> schemas.BootstrapAside:
    """The user writes while the team works: the discussor answers in its own
    conversation; requests and decisions go to the inbox for the graph."""
    budget = run.brief_budget("discussor")
    plain = views.aside(st, user=user, activity=activity, budget=budget * 2 // 3,
                        run_dir=run.folder)

    def takeover() -> str:
        return chat.handover(run.folder, run.brief_budget("discussor") // 3) + "\n\n" + plain
    brief = takeover() if run.takes_over(DISCUSSOR_SESSION, "discussor") else plain
    with run.discussor_lock:
        out, _ = run.call("discussor", brief=brief,
                          schema=schemas.BootstrapAside, session=DISCUSSOR_SESSION,
                          label="discussor (aside)", takeover=takeover)
    for r in out.requests:
        inbox.post(run.folder, "request", text=r.detail, request_kind=r.kind, redo=r.redo)
    for d in out.decisions:
        inbox.post(run.folder, "decision", what=d.what, why=d.why)
    return out


# ---- orchestrator ---------------------------------------------------------------------------


def orchestrator_check(out: schemas.OrchestratorOutput) -> list[str]:
    if out.action == "research" and not (out.questions or out.task.strip()):
        return ["research needs `questions` (or a task saying what to find)"]
    if out.action in ("design", "ask_user") and not out.task.strip():
        return [f"{out.action} needs a `task`"]
    return []


def orchestrator(run: Run, st: dict, *, requests: list[dict], last: str = "") -> Outcome:
    check = orchestrator_check
    out, session = run.call("orchestrator", brief=views.orchestrator(st, requests=requests,
                                                                     last=last),
                            schema=schemas.OrchestratorOutput, check=check)
    ops = [{"op": "merge", "section": "status", "value": {"summary": out.summary,
                                                          "next": out.next or out.action}}]
    ops += [{"op": "add", "section": "open_questions", "value": q.model_dump(by_alias=True)}
            for q in out.open_questions]
    ops += [{"op": "add", "section": "decisions", "value": {**d.model_dump(), "by": "orchestrator"}}
            for d in out.decisions]
    return Outcome(ops=ops, summary=f"{out.action}: {out.reason}", session=session, data=out)


# ---- researcher ----------------------------------------------------------------------------


def researcher(run: Run, st: dict, *, task: str, questions: list[str]) -> Outcome:
    # The questions go into the state first, so they have ids the researcher
    # answers by -- a plain list made it number them itself (Q1..Q6) and miss.
    known = {q["text"].strip().lower() for q in st.get("open_questions") or []}
    ops = [{"op": "add", "section": "open_questions", "value": {"text": q, "for": "research"}}
           for q in dict.fromkeys(q.strip() for q in questions if q.strip())
           if q.lower() not in known]
    if ops:
        st = S.apply(st, ops, by="orchestrator")[0]
    hypotheses = _ids(st, "hypotheses")
    open_ids = {q["id"] for q in st.get("open_questions") or [] if q.get("status") == "open"}

    def check(out: schemas.ResearcherOutput) -> list[str]:
        # Only what cannot be kept goes back. A research round costs minutes
        # and millions of tokens, so a wrong reference is re-filed, not retried.
        return [f"finding {i + 1} has no source" for i, f in enumerate(out.findings)
                if not f.source.strip()]

    folder = run.folder / "research"
    folder.mkdir(parents=True, exist_ok=True)
    out, session = run.call("researcher", brief=views.researcher(st, task=task),
                            schema=schemas.ResearcherOutput, access="write", workdir=folder,
                            check=check)
    checked = {"verified": 0, "not found": 0}
    for f in out.findings:
        ok = verify.source(f.source, run.skills_dir)
        if ok is True:
            checked["verified"] += 1
        elif ok is False:
            checked["not found"] += 1
        ops.append({"op": "add", "section": "evidence", "value": {
            "claim": f.claim, "source": f.source, "verified": ok, "found_by": "researcher",
            "bears_on": list(f.bears_on)}})
    refiled = []
    for n in out.novelty:
        if n.hypothesis in hypotheses:
            ops.append({"op": "update", "section": "hypotheses", "id": n.hypothesis,
                        "value": {"novelty": n.note}})
        else:
            refiled.append(n.hypothesis)
            ops.append({"op": "add", "section": "evidence", "value": {
                "claim": f"Novelty: {n.note}", "source": "the researcher's novelty assessment",
                "found_by": "researcher"}})
    for a in out.answers:
        if a.id in open_ids:
            ops.append({"op": "update", "section": "open_questions", "id": a.id,
                        "value": {"status": "answered", "answer": a.answer}})
        else:
            refiled.append(a.id)
            ops.append({"op": "add", "section": "open_questions", "value": {
                "text": f"(answered by the researcher under {a.id}, which is not a question here)",
                "for": "research", "status": "answered", "answer": a.answer}})
    ops += [{"op": "add", "section": "open_questions", "value": {"text": u, "for": "research"}}
            for u in out.unanswered]
    summary = (f"{len(out.findings)} findings ({checked['verified']} sources verified, "
               f"{checked['not found']} not found). {out.summary}")
    if refiled:
        summary += f" (Kept, though filed under unknown ids: {', '.join(refiled)}.)"
    ops.append(_result_op("researcher", "", task, summary, session))
    return Outcome(ops=ops, summary=summary, session=session, data=out)


# ---- designer ---------------------------------------------------------------------------------


def design_problems(design: dict, *, steps: list[str], rubric: set[str],
                    skills: set[str]) -> list[str]:
    """What is wrong with a design, in words: names, one orchestrator, one
    read-only validator, every plan step owned by exactly one node, skills
    and rubric ids that exist. The execution stage checks it again."""
    nodes = design.get("nodes") or []
    names = [n["name"] for n in nodes]
    problems = [f"node names must be unique: {names}"] if len(set(names)) != len(names) else []
    problems += [f"{n} is not lower_snake_case" for n in names
                 if not n.replace("_", "").isalnum() or n != n.lower() or not n[:1].isalpha()]
    for role in ("orchestrator", "validator"):
        count = sum(n["role"] == role for n in nodes)
        if count != 1:
            problems.append(f"there must be exactly one {role} node, not {count}")
    problems += [f"the validator {n['name']} must be read_only" for n in nodes
                 if n["role"] == "validator" and n.get("access") != "read_only"]
    problems += [f"the validator {n['name']} owns steps {', '.join(n['steps'])}, but it only "
                 f"checks the other nodes' work, read-only, after each step: give those steps "
                 f"to a worker (one that writes and runs the checks) and leave the "
                 f"validator's `steps` empty" for n in nodes
                 if n["role"] == "validator" and n.get("steps")]
    owners: dict[str, list[str]] = {}
    for n in nodes:
        for s in n.get("steps") or []:
            owners.setdefault(s, []).append(n["name"])
    problems += [f"plan step {s} has no node" for s in steps if s not in owners]
    problems += [f"plan step {s} has several nodes: {', '.join(o)}"
                 for s, o in owners.items() if len(o) > 1]
    problems += [f"{s} is not a plan step" for s in owners if s not in steps]
    problems += [f"{n['name']}: no skill called {k}" for n in nodes for k in n.get("skills") or []
                 if skills and k not in skills]
    problems += [f"{n['name']}: `validated_by` lists rubric ids ({', '.join(sorted(rubric)) or 'none'}), "
                 f"not {r}" for n in nodes for r in n.get("validated_by") or [] if r not in rubric]
    return problems


#: Things named by a bare letter: "A–E", "B-D", "condition C", "from A", "A's".
LETTER_LABEL = re.compile(
    r"\b[A-Z]\s*[–—-]\s*[A-Z]\b"                       # a range: A–E
    r"|(?<![\w/*+^=<>])(?!I\b)[B-Z](?![\w/(*+^=<>])"    # a lone capital: C, D (not I, nor
                                                       # a formula's: 2*S*U/(S+U))
    r"|\bA(?=[’']s\b|\s*[,;:.)]|\s*$)"                 # A's, "from A,", "A."
    # after a word like these, an article would be "a": a capital A is a name
    r"|\b(?:from|than|of|to|and|with|on|over|versus|against|after|before|by|for|the)\s+A\b")
#: "step 2", "steps 1-7", "step-2 config", "Steps 3–5": a step named by number.
STEP_NUMBER = re.compile(r"\bsteps?[\s-]*\d+(?:\s*(?:[-–—,]|and|to)\s*\d+)*", re.I)


def standalone_problems(steps) -> list[str]:
    """Where a step's detail or check names what its owner cannot see: another
    step by number, or a thing by a bare letter."""
    problems = []
    for s in steps:
        # A letter the step itself defines ("H = 2SU/(S+U)", "A: the original
        # checkpoint") is its own name, and fine.
        defined = set(re.findall(r"(?<![\w/])([A-Z])\s*[=:]", f"{s.detail}\n{s.check}"))
        for field, text in (("detail", s.detail), ("check", s.check)):
            number = STEP_NUMBER.search(text or "")
            if number:
                problems.append(f"step {s.id}: its {field} refers to {number.group(0)!r}, but "
                                f"its owner never sees step numbers: name that work by what "
                                f"it produced")
            letters = sorted({m.group(0).split()[-1] for m in LETTER_LABEL.finditer(text or "")}
                             - defined)
            if letters:
                problems.append(f"step {s.id}: its {field} names things by letter "
                                f"({', '.join(letters[:6])}); its owner never sees what they "
                                f"stand for: name each in full (the model, the training, the "
                                f"data), as in \"the supervised LoRA model\", not \"B\"")
    return problems


# ---- planner (the study, then the steps) and designer (the team) ---------------------------
#
# Three calls, each with one job and its own prompt, run back to back; the
# user approves the whole design once. A revision starts at the part it
# changes, and the parts after it are redone: the study -> the steps -> the
# team (REDO).

#: Where a design is redone from, and what that redoes.
REDO = ("study", "steps", "team")


def study_check(st: dict):
    """What code requires of a study: ids, and evidence that exists."""
    evidence = _ids(st, "evidence")

    def check(out: schemas.StudyOutput) -> list[str]:
        problems = _unique("hypothesis", [h.id for h in out.hypotheses], "H")
        problems += _unique("rubric", [r.id for r in out.rubric], "R")
        problems += [f"{h.id} cites {e}, which is not an evidence id"
                     for h in out.hypotheses for e in h.evidence if e not in evidence]
        return problems
    return check


def study(run: Run, st: dict, *, task: str) -> Outcome:
    """The planner's first job: hypotheses, rubric and protocol -- what the
    study tests and how it is judged, before any step is planned."""
    out, session = run.call("planner", brief=views.study(st, task=task, run_dir=run.folder),
                            schema=schemas.StudyOutput, check=study_check(st),
                            instructions=prompts.study(run.folder), label="planner (study)")
    ops = _replace(st, "hypotheses", [h.model_dump() for h in out.hypotheses])
    # Where each criterion is checked is the steps' to say: until then, nowhere.
    ops += _replace(st, "rubric", [{**r.model_dump(), "assessed_at": ""} for r in out.rubric])
    ops.append({"op": "set", "section": "protocol", "value": out.protocol.model_dump()})
    # Its risks replace the last study's, or every revision piles its list on
    # (measured: 141 risks after 23).
    ops += [{"op": "drop", "section": "risks", "id": r["id"]} for r in st.get("risks") or []]
    ops += [{"op": "add", "section": "risks", "value": r.model_dump()} for r in out.risks]
    summary = (f"the study: {len(out.hypotheses)} hypotheses, {len(out.rubric)} criteria. "
               f"{out.summary}")
    ops.append(_result_op("planner", "study", task, summary, session))
    return Outcome(ops=ops, summary=summary, session=session, data=out)


def plan_check(st: dict, reader=None):
    """What code requires of the steps, given the study they carry out.
    `reader(steps)`: each step's subagent context read alone ({step:
    questions}, bootstrap/reader). `check.unresolved`: what the last answer
    checked still leaves its subagents to guess."""
    rubric = [r["id"] for r in st.get("rubric") or []]
    sent_back: list[bool] = []
    unresolved: list[str] = []

    def check(out: schemas.PlanOutput) -> list[str]:
        ids = [s.id for s in out.steps]
        problems = []
        if ids != [str(n) for n in range(1, len(ids) + 1)]:
            problems.append(f'step ids must be "1", "2", ... in order, not {ids}')
        for s in out.steps:
            problems += [f"step {s.id} depends on {d}, which is not an earlier step"
                         for d in s.depends_on if d not in ids[:ids.index(s.id)]]
            problems += [f"step {s.id} completes {r}, which is not a rubric criterion"
                         for r in s.completes if r not in rubric]
        done_at = {r: [s.id for s in out.steps if r in s.completes] for r in rubric}
        problems += [f"{r} is completed by no step: give it the one step where it is checked"
                     for r, at in done_at.items() if not at]
        problems += [f"{r} is completed by steps {', '.join(at)}: give it the ONE step where "
                     f"it is checked" for r, at in done_at.items() if len(at) > 1]
        # A step is its subagent's whole context: what it names must make
        # sense there. Code catches letters and step numbers; steps that pass
        # everything else are then read, one by one, as their subagents would
        # (one call a step). Sent back on every attempt but the last, so a
        # plan that keeps them still reaches the user -- told which steps.
        alone = standalone_problems(out.steps)
        if not alone and not problems and reader is not None:
            from research_agent.bootstrap import reader as R

            alone = R.problems(reader(out.steps))
        unresolved[:] = alone
        if alone and len(sent_back) < run_base.ATTEMPTS - 1:
            sent_back.append(True)
            problems += alone
        return problems
    check.unresolved = unresolved
    return check


def plan(run: Run, st: dict, *, task: str) -> Outcome:
    """The planner's second job: the steps that carry the study out, each
    written as its subagent's whole context, and the criterion each step
    completes."""
    from research_agent.bootstrap import reader

    def read(steps):
        return reader.read(run, st, steps=[_plan_step(s) for s in steps],
                           rubric=_rubric_at(st, steps), owners={})

    check = plan_check(st, reader=read)
    out, session = run.call("planner", brief=views.planner(st, task=task, run_dir=run.folder),
                            schema=schemas.PlanOutput, check=check, label="planner (steps)")
    ops = [{"op": "set", "section": "plan", "value": {
        "summary": out.summary, "steps": [_plan_step(s) for s in out.steps]}}]
    ops += [{"op": "update", "section": "rubric", "id": r["id"],
             "value": {"assessed_at": r["assessed_at"]}} for r in _rubric_at(st, out.steps)]
    summary = f"the plan: {len(out.steps)} steps. {out.summary}"
    if check.unresolved:
        summary += ("\nNOT SELF-CONTAINED after every attempt -- tell the user, and fix "
                    "before approving: " + "; ".join(check.unresolved[:8]))
    ops.append(_result_op("planner", "steps", task, summary, session))
    return Outcome(ops=ops, summary=summary, session=session, data=out)


def _plan_step(s) -> dict:
    """A planned step as the state keeps it: the criteria it completes as `rubric`."""
    step = s.model_dump()
    step["rubric"] = step.pop("completes")
    return step


def _rubric_at(st: dict, steps) -> list[dict]:
    """The rubric with each criterion's `assessed_at`: the step completing it."""
    at = {r: s.id for s in steps for r in s.completes}
    return [{**r, "assessed_at": at.get(r["id"], "")} for r in st.get("rubric") or []]


def team_check(st: dict, known_skills: set[str]):
    """What code requires of the team, given the steps it carries out."""
    steps = {s["id"]: s for s in (st.get("plan") or {}).get("steps") or []}
    rubric = _ids(st, "rubric")

    def check(out: schemas.TeamOutput) -> list[str]:
        nodes = [n.model_dump() for n in out.nodes]
        problems = design_problems({"nodes": nodes}, steps=list(steps), rubric=rubric,
                                   skills=known_skills)
        # New designs only: execution still accepts earlier ones.
        problems += [f"the orchestrator {n.name} owns steps {', '.join(n.steps)}, but it only "
                     f"decides what runs next: give those steps to a worker or writer and "
                     f"leave the orchestrator's `steps` empty"
                     for n in out.nodes if n.role == "orchestrator" and n.steps]
        problems += [f"step {sid} is planned for a {steps[sid]['kind']}, but {n.name} is a "
                     f"{n.role}: give it to a {steps[sid]['kind']} node"
                     for n in out.nodes if n.role in ("worker", "researcher", "writer")
                     for sid in n.steps if steps.get(sid, {}).get("kind") not in (None, "", n.role)]
        return problems
    return check


def team(run: Run, st: dict, *, task: str) -> Outcome:
    """The designer's job: the team of nodes that carries the planned steps
    out -- who owns each step, its role, access and skills."""
    catalogue = run.catalogue()
    out, session = run.call("designer", brief=views.designer(st, task=task, skills=catalogue,
                                                            run_dir=run.folder),
                            schema=schemas.TeamOutput,
                            check=team_check(st, {name for name, _ in catalogue}),
                            label="designer (team)")
    ops = [{"op": "set", "section": "design", "value": {
        "summary": out.summary, "nodes": [n.model_dump() for n in out.nodes],
        "notes": out.notes}}]
    summary = f"the team: {len(out.nodes)} nodes. {out.summary}"
    ops.append(_result_op("designer", "team", task, summary, session))
    return Outcome(ops=ops, summary=summary, session=session, data=out)


#: Each part's call.
PART_CALLS = {"study": study, "steps": plan, "team": team}


def redesign(run: Run, st: dict, *, task: str, start: str = "study",
             commit=None) -> tuple[dict, list[Outcome]]:
    """The design redone from `start` on, each part committed (`commit(st,
    outcome, part)` -> st) before the next reads it. Returns the state and
    the outcomes."""
    done = []
    for part in REDO[REDO.index(start):]:
        out = PART_CALLS[part](run, st, task=task)
        st = commit(st, out, part) if commit else _apply_ops(st, out)
        done.append(out)
    return st, done


def _apply_ops(st: dict, out: Outcome) -> dict:
    return S.apply(st, out.ops, by="designer")[0]


def _unique(what: str, ids: list[str], prefix: str) -> list[str]:
    bad = [i for i in ids if not (i.startswith(prefix) and i[len(prefix):].isdigit())]
    problems = [f"{what} ids are {prefix}1, {prefix}2, ...: not {', '.join(bad)}"] if bad else []
    twice = sorted({i for i in ids if ids.count(i) > 1})
    return problems + ([f"{what} ids used twice: {', '.join(twice)}"] if twice else [])


def _replace(st: dict, section: str, items: list[dict]) -> list[dict]:
    """Ops making `section` these items, by id: kept ids are revised, new ones
    added, the rest dropped (a hypothesis is kept, marked dropped)."""
    have = {x["id"]: x for x in st.get(section) or []}
    ops = []
    for x in items:
        value = {k: v for k, v in x.items() if k != "id"}
        if x["id"] in have:
            if have[x["id"]].get("status") == "dropped":
                value["status"] = "proposed"
            ops.append({"op": "update", "section": section, "id": x["id"], "value": value})
        else:
            ops.append({"op": "add", "section": section, "id": x["id"], "value": value})
    wanted = {x["id"] for x in items}
    ops += [{"op": "drop", "section": section, "id": i} for i, x in have.items()
            if i not in wanted and x.get("status") != "dropped"]
    return ops
