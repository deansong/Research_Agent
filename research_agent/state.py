"""
WHAT:  The research state: everything the agent knows about one study -- the
       goal, constraints, evidence, hypotheses, rubric, protocol, plan,
       design, decisions, open questions, risks, results and status -- and the
       only way it changes: a list of small operations, applied by code.
WHY:   Context management. No node carries the conversation; each is handed
       the part of this state it needs. So the state has to be complete,
       compact and trustworthy: nodes never rewrite it, they propose changes
       by id, and every change is versioned, attributed and kept.
CONCEPT: Sections are either one object (goal, plan, ...) or a list of items
       with ids (H1, E3, D2, ...). A change bumps its section's version; an
       approval records the version approved, so a later change shows as
       "changed since you approved it" without anyone clearing anything.

On disk, in a run folder:
    state/research.json      the current state
    state/research.md        the same, readable
    state/history/0001.json  one file per change: who, when, what
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


# ---- the sections -------------------------------------------------------------------


class Goal(BaseModel):
    question: str = ""
    """The research question, in one or two sentences."""
    motivation: str = ""
    scope_in: list[str] = Field(default_factory=list)
    scope_out: list[str] = Field(default_factory=list)
    deliverable: str = ""


class Constraints(BaseModel):
    compute: str = ""
    budget: str = ""
    deadline: str = ""
    data: str = ""
    models: str = ""
    other: list[str] = Field(default_factory=list)


class Assumption(BaseModel):
    id: str = ""
    text: str
    status: Literal["unverified", "checked", "false"] = "unverified"


class Evidence(BaseModel):
    id: str = ""
    claim: str
    source: str
    """An arXiv id, a DOI, a PMID, a URL, or a dataset or model id."""
    verified: bool | None = None
    """True or False once code has checked the source; None until then."""
    found_by: str = ""
    bears_on: list[str] = Field(default_factory=list)
    """Hypothesis or question ids."""


class Hypothesis(BaseModel):
    id: str = ""
    plain: str = ""
    """The hypothesis in one or two plain sentences, for any researcher."""
    statement: str
    prediction: str = ""
    """What would be observed if it is true -- falsifiable."""
    rationale: str = ""
    evidence: list[str] = Field(default_factory=list)
    novelty: str = ""
    status: Literal["proposed", "selected", "dropped", "supported", "refuted"] = "proposed"


class Criterion(BaseModel):
    id: str = ""
    criterion: str
    measure: str = ""
    threshold: str = ""
    weight: float = 1.0
    applies_to: list[str] = Field(default_factory=list)
    assessed_at: str = ""
    """The plan step that completes it, where the validator checks it."""


class Brief(BaseModel):
    """What the design is to be, as the user agreed it with the discussor
    after hearing what the team found: what to build or test, the scope, the
    choices made. The designer designs from it."""

    text: str = ""


class Protocol(BaseModel):
    baselines: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    statistics: str = ""
    seeds: str = ""
    stop_criteria: str = ""


class Step(BaseModel):
    id: str
    title: str
    detail: str = ""
    check: str = ""
    """How to tell the step is done and right."""
    depends_on: list[str] = Field(default_factory=list)
    gate: bool = False
    """Stop for the user's approval before this step runs."""
    rubric: list[str] = Field(default_factory=list)
    cost: str = ""
    follows: list[str] = Field(default_factory=list)
    """The parts of the protocol and constraints this step's work must
    follow (RULES): its owner is given those, not the rest."""
    kind: str = ""
    """Who does it: a worker, researcher or writer ("" in plans made before
    the planner said)."""


#: What a step can follow: the protocol's parts, then the constraints'.
RULES = ("baselines", "metrics", "statistics", "seeds", "stop_criteria",
         "compute", "budget", "deadline", "data", "models", "other")


class Plan(BaseModel):
    summary: str = ""
    steps: list[Step] = Field(default_factory=list)


class DesignNode(BaseModel):
    name: str
    role: str
    steps: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    access: Literal["none", "read_only", "write"] = "read_only"
    instructions: str = ""
    validated_by: list[str] = Field(default_factory=list)
    """Rubric ids the validator checks this node's work against."""


class Design(BaseModel):
    summary: str = ""
    nodes: list[DesignNode] = Field(default_factory=list)
    notes: str = ""


class StepRecord(BaseModel):
    """How one plan step went in the execution stage."""

    id: str
    """The plan step's id."""
    title: str = ""
    owner: str = ""
    """The design node that carries it out."""
    status: Literal["pending", "running", "waiting", "validating", "pass", "fail", "skipped",
                    "stopped"] = "pending"
    go: bool = False
    """The user let this gated step start."""
    pause_before: bool = False
    """A breakpoint: stop for the user before this step starts."""
    pause_after: bool = False
    """A breakpoint: stop for the user once this step is checked."""
    attempts: int = 0
    extra_attempts: int = 0
    """Attempts the user granted beyond the limit."""
    sessions: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    jobs: list[str] = Field(default_factory=list)
    summary: str = ""
    """The owner's last report, short."""
    verdict: str = ""
    """The validator's last verdict, short."""
    problems: list[str] = Field(default_factory=list)
    gpu_hours: float = 0.0
    started: str = ""
    finished: str = ""


class Execution(BaseModel):
    """The execution stage: one record per plan step, and the budget."""

    started: str = ""
    wall_hours: float = 0.0
    """The limit on hours since execution started; 0 = none."""
    gpu_hours: float = 0.0
    """The limit on GPU-hours used by jobs; 0 = none."""
    steps: list[StepRecord] = Field(default_factory=list)


class ReviewRequest(BaseModel):
    what: str
    why: str = ""
    priority: Literal["must", "should", "could"] = "should"


class Review(BaseModel):
    """A conference review of a run's paper: the meta-review of a panel of
    three reviewers (AAAI's form; `panel` holds their reviews). Reviews of
    earlier versions are one reviewer's, scored NeurIPS's way. It is carried
    into the next run, whose designer addresses it."""

    id: str = ""
    run: str = ""
    """The run folder it reviews -- its artifacts are there."""
    summary: str = ""
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    limitations: str = ""
    decision: str = ""
    """accept, weak accept, borderline, weak reject, reject."""
    justification: str = ""
    consensus: list[str] = Field(default_factory=list)
    disagreements: list[str] = Field(default_factory=list)
    panel: list[dict] = Field(default_factory=list)
    """Each reviewer's review: id, focus, and the form."""
    soundness: int = 0
    presentation: int = 0
    contribution: int = 0
    """1-4 each, as NeurIPS asks (earlier versions' reviews)."""
    rating: int = 0
    """1-10, as NeurIPS asks (6: weak accept)."""
    confidence: int = 0
    """1-5."""
    requests: list[ReviewRequest] = Field(default_factory=list)
    """What the next run should do."""
    outcome: str = ""
    """The run's step table when it was reviewed."""
    file: str = ""
    at: str = ""


class Decision(BaseModel):
    id: str = ""
    what: str
    why: str = ""
    by: str = ""
    supersedes: str = ""
    at: str = ""


class Question(BaseModel):
    id: str = ""
    text: str
    for_: Literal["user", "research"] = Field("user", alias="for")
    status: Literal["open", "answered"] = "open"
    answer: str = ""

    model_config = {"populate_by_name": True}


class Risk(BaseModel):
    id: str = ""
    text: str
    mitigation: str = ""


class Result(BaseModel):
    id: str = ""
    node: str
    job: str = ""
    asked: str = ""
    summary: str = ""
    session: str = ""
    """The node call's RB session id -- open it to see the whole call."""
    at: str = ""


class Status(BaseModel):
    phase: str = "discussing"
    summary: str = ""
    next: str = ""
    paper: str = ""
    """The paper's PDF, once written."""
    previous: str = ""
    """The run folder this one continues: its paper and review are redesigned from."""


class ResearchState(BaseModel):
    goal: Goal = Field(default_factory=Goal)
    constraints: Constraints = Field(default_factory=Constraints)
    assumptions: list[Assumption] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    brief: Brief = Field(default_factory=Brief)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    rubric: list[Criterion] = Field(default_factory=list)
    protocol: Protocol = Field(default_factory=Protocol)
    plan: Plan = Field(default_factory=Plan)
    design: Design = Field(default_factory=Design)
    execution: Execution = Field(default_factory=Execution)
    decisions: list[Decision] = Field(default_factory=list)
    open_questions: list[Question] = Field(default_factory=list)
    risks: list[Risk] = Field(default_factory=list)
    reviews: list[Review] = Field(default_factory=list)
    results: list[Result] = Field(default_factory=list)
    status: Status = Field(default_factory=Status)
    versions: dict[str, int] = Field(default_factory=dict)
    approvals: dict[str, int] = Field(default_factory=dict)
    """Section -> the version the user approved."""


#: List sections and the letter their ids start with.
LISTS: dict[str, tuple[str, type[BaseModel]]] = {
    "assumptions": ("A", Assumption),
    "evidence": ("E", Evidence),
    "hypotheses": ("H", Hypothesis),
    "rubric": ("R", Criterion),
    "decisions": ("D", Decision),
    "open_questions": ("Q", Question),
    "risks": ("K", Risk),
    "reviews": ("V", Review),
    "results": ("N", Result),
}
OBJECTS: dict[str, type[BaseModel]] = {
    "goal": Goal, "constraints": Constraints, "brief": Brief, "protocol": Protocol, "plan": Plan,
    "design": Design, "execution": Execution, "status": Status,
}
SECTIONS = ("goal", "constraints", "assumptions", "evidence", "brief", "hypotheses", "rubric",
            "protocol", "plan", "design", "execution", "decisions", "open_questions", "risks",
            "reviews", "results", "status")
#: The design, as the designer writes it in one go and the user approves it:
#: its hypotheses, rubric, protocol, plan (the steps) and nodes.
PARTS = ("hypotheses", "rubric", "protocol", "plan", "design")
#: What the user approves: the brief the designer works from, then the
#: design, all its parts at once.
APPROVABLE = ("brief", "design")
#: Never deleted: a dropped hypothesis is marked dropped, a decision stands.
KEPT = {"hypotheses", "decisions", "results", "reviews"}


class StateError(ValueError):
    """An operation that cannot be applied, said in words."""


# ---- reading it -------------------------------------------------------------------------


def new() -> dict:
    return ResearchState().model_dump(by_alias=True)


def load(data: dict) -> ResearchState:
    return ResearchState.model_validate(data or {})


def version(state: dict, section: str) -> int:
    return int((state.get("versions") or {}).get(section, 0))


def approved(state: dict, section: str) -> bool:
    """Approved, and not changed since. The design counts as approved only
    with every part of it that exists."""
    if section == "design":
        return _stamped(state, "design") and all(
            _stamped(state, p) for p in PARTS if version(state, p))
    return _stamped(state, section)


def _stamped(state: dict, section: str) -> bool:
    at = (state.get("approvals") or {}).get(section)
    return at is not None and at == version(state, section) and version(state, section) > 0


def item(state: dict, section: str, item_id: str) -> dict | None:
    return next((x for x in state.get(section) or [] if x.get("id") == item_id), None)


# ---- changing it -----------------------------------------------------------------------


def apply(state: dict, ops: list[dict], *, by: str) -> tuple[dict, list[str]]:
    """Apply `ops` to a copy of `state`; return (new state, sections changed).

    An op is one of:
        {"op": "add",    "section": "hypotheses", "value": {...}}      new id given
        {"op": "add",    "section": "rubric", "id": "R3", "value": {...}}  that id, if free
        {"op": "update", "section": "hypotheses", "id": "H2", "value": {...}}
        {"op": "drop",   "section": "evidence",   "id": "E4"}
        {"op": "set",    "section": "plan",       "value": {...}}      a whole object
        {"op": "merge",  "section": "goal",       "value": {...}}      some fields
        {"op": "approve", "section": "design"}                         this version
                                                         (the design: with all its parts)

    All or nothing: a bad op raises StateError and nothing changes.
    """
    out = json.loads(json.dumps(state))
    changed: list[str] = []
    for op in ops:
        section = op.get("section", "")
        kind = op.get("op", "")
        if section not in SECTIONS:
            raise StateError(f"There is no section {section!r}.")
        if kind == "approve":
            if section not in (*PARTS, "brief"):
                raise StateError(f"{section} is not something to approve.")
            if version(out, section) == 0:
                raise StateError(f"There is no {section} to approve yet.")
            for part in (PARTS if section == "design" else (section,)):
                if version(out, part):
                    out.setdefault("approvals", {})[part] = version(out, part)
            continue
        if section in LISTS:
            _list_op(out, section, kind, op, by)
        else:
            _object_op(out, section, kind, op)
        if section not in changed:
            changed.append(section)
    for section in changed:
        out.setdefault("versions", {})[section] = version(out, section) + 1
    load(out)  # still a valid state -- or raise before anything is kept
    return out, changed


def _list_op(state: dict, section: str, kind: str, op: dict, by: str) -> None:
    prefix, model = LISTS[section]
    items = state.setdefault(section, [])
    value = dict(op.get("value") or {})
    if kind == "add":
        wanted = str(op.get("id") or "")
        if wanted and not (wanted.startswith(prefix) and wanted[len(prefix):].isdigit()):
            raise StateError(f"{wanted!r} is not a {section} id ({prefix}1, {prefix}2, ...).")
        if wanted and item(state, section, wanted):
            raise StateError(f"{section} already has {wanted}.")
        value["id"] = wanted or _next_id(items, prefix)
        if section == "decisions":
            value.setdefault("by", by)
            value.setdefault("at", _now())
        if section == "evidence":
            value.setdefault("found_by", by)
        if section in ("results", "reviews"):
            value.setdefault("at", _now())
        items.append(model.model_validate(value).model_dump(by_alias=True))
        return
    if kind not in ("update", "drop"):
        raise StateError(f"{kind!r} does not apply to {section}; use add, update or drop.")
    target = item(state, section, str(op.get("id", "")))
    if target is None:
        known = ", ".join(x.get("id", "") for x in items) or "none"
        raise StateError(f"No {section} item {op.get('id')!r} (there are: {known}).")
    if kind == "update":
        value.pop("id", None)
        merged = {**target, **value}
        target.clear()
        target.update(model.model_validate(merged).model_dump(by_alias=True))
    elif kind == "drop":
        if section == "hypotheses":
            target["status"] = "dropped"
        elif section in KEPT:
            raise StateError(f"{section} are kept; add a new one that supersedes it.")
        else:
            items.remove(target)


def _object_op(state: dict, section: str, kind: str, op: dict) -> None:
    model = OBJECTS[section]
    value = dict(op.get("value") or {})
    if kind == "set":
        state[section] = model.model_validate(value).model_dump(by_alias=True)
    elif kind == "merge":
        state[section] = model.model_validate({**(state.get(section) or {}), **value}) \
            .model_dump(by_alias=True)
    else:
        raise StateError(f"{kind!r} does not apply to {section}; use set or merge.")


def _next_id(items: list[dict], prefix: str) -> str:
    numbers = [int(x["id"][len(prefix):]) for x in items
               if str(x.get("id", "")).startswith(prefix) and x["id"][len(prefix):].isdigit()]
    return f"{prefix}{max(numbers, default=0) + 1}"


# ---- showing it ------------------------------------------------------------------------


def render(state: dict, sections=SECTIONS, *, brief: bool = False) -> str:
    """Markdown for `sections`, empty ones left out. `brief` keeps lists to one
    line per item -- what most briefs need."""
    parts = []
    for section in sections:
        text = _render(state, section, brief)
        if text:
            mark = ""
            if section in (*PARTS, "brief") and version(state, section):
                mark = (" (approved)" if approved(state, section) else
                        f" (v{version(state, section)}, not approved)")
            parts.append(f"## {section.replace('_', ' ').title()}{mark}\n{text}")
    return "\n\n".join(parts)


def render_within(state: dict, sections=SECTIONS, budget: int | None = None, *,
                  brief: bool = False) -> str:
    """`render`, in at most `budget` characters: as asked when it fits, else
    one line per item, else section by section until the budget, naming the
    rest -- the reader finds them in state/research.md."""
    full = render(state, sections, brief=brief)
    if budget is None or len(full) <= budget:
        return full
    short = render(state, sections, brief=True)
    if len(short) <= budget:
        return short
    parts, left, rest = [], budget, []
    for section in sections:
        text = render(state, [section], brief=True)
        if not text:
            continue
        if len(text) <= left and not rest:
            parts.append(text)
            left -= len(text) + 2
        else:
            rest.append(section)
    parts.append("(Too long to show here: " + ", ".join(rest) + " -- read them in "
                 "state/research.md in the run folder.)")
    return "\n\n".join(parts)


def _render(state: dict, section: str, brief: bool) -> str:
    value = state.get(section)
    if section == "goal":
        g = value or {}
        lines = [f"Question: {g.get('question')}" if g.get("question") else "",
                 f"Motivation: {g.get('motivation')}" if g.get("motivation") else "",
                 f"In scope: {'; '.join(g['scope_in'])}" if g.get("scope_in") else "",
                 f"Out of scope: {'; '.join(g['scope_out'])}" if g.get("scope_out") else "",
                 f"Deliverable: {g.get('deliverable')}" if g.get("deliverable") else ""]
        return "\n".join(x for x in lines if x)
    if section == "brief":
        return (value or {}).get("text", "")
    if section in ("constraints", "protocol", "status"):
        return "\n".join(f"{k.replace('_', ' ').capitalize()}: "
                         f"{'; '.join(v) if isinstance(v, list) else v}"
                         for k, v in (value or {}).items() if v)
    if section == "plan":
        p = value or {}
        lines = [p.get("summary", "")] if p.get("summary") else []
        for s in p.get("steps") or []:
            extra = "" if brief else "".join(
                f"\n   {label}: {s[key] if isinstance(s[key], str) else ', '.join(s[key])}"
                for key, label in (("detail", "detail"), ("check", "check"),
                                   ("depends_on", "after"), ("rubric", "rubric"),
                                   ("cost", "cost")) if s.get(key))
            lines.append(f"{s['id']}. {s['title']}{' [gate]' if s.get('gate') else ''}{extra}")
        return "\n".join(lines)
    if section == "design":
        d = value or {}
        lines = [d.get("summary", "")] if d.get("summary") else []
        for n in d.get("nodes") or []:
            lines.append(f"- {n['name']} ({n['role']}, {n['access']}): steps "
                         f"{', '.join(n.get('steps') or []) or '-'}; skills "
                         f"{', '.join(n.get('skills') or []) or '-'}")
        if d.get("notes") and not brief:
            lines.append(d["notes"])
        return "\n".join(lines)
    if section == "execution":
        e = value or {}
        if not e.get("steps"):
            return ""
        limits = [f"{e[k]:g} {label}" for k, label in (("wall_hours", "wall hours"),
                                                        ("gpu_hours", "GPU-hours")) if e.get(k)]
        lines = [f"Started {e.get('started') or '-'}; limits: {', '.join(limits) or 'none'}"]
        for r in e["steps"]:
            tail = f" -- {r['verdict'] or r['summary']}" if (r.get("verdict") or r.get("summary")) \
                else ""
            if brief and len(tail) > 160:
                tail = tail[:157] + "..."
            go = ", go given" if r.get("go") else ""
            go += "".join(f", pause {w}" for w in ("before", "after") if r.get(f"pause_{w}"))
            lines.append(f"{r['id']}. [{r['status']}] {r.get('title', '')} ({r.get('owner', '')}{go}, "
                         f"{r.get('attempts', 0)} attempts, {r.get('gpu_hours', 0):.1f} GPU-h){tail}")
        return "\n".join(lines)
    items = list(value or [])
    head = []
    if brief and section == "decisions":
        # The user's (and the autopilot's, for the user) bind the team: all
        # of them. The team's own: the newest. Measured: 172 decisions and
        # 107 questions were 117k of a 166k-character orchestrator brief.
        theirs = [x for x in items if x.get("by") not in ("user", "autopilot")]
        dropped = theirs[:-BRIEF_TEAM_DECISIONS] if len(theirs) > BRIEF_TEAM_DECISIONS else []
        if dropped:
            gone = {id(x) for x in dropped}
            items = [x for x in items if id(x) not in gone]
            head = [f"- ({len(dropped)} earlier team decisions, {dropped[0].get('id', '')} to "
                    f"{dropped[-1].get('id', '')}, are in state/research.md)"]
    if brief and section == "open_questions":
        answered = [x for x in items if x.get("status") == "answered"]
        dropped = answered[:-BRIEF_ANSWERED] if len(answered) > BRIEF_ANSWERED else []
        if dropped:
            gone = {id(x) for x in dropped}
            items = [x for x in items if id(x) not in gone]
            head = [f"- ({len(dropped)} earlier questions, answered, are in state/research.md)"]
    lines = [_line(section, x, brief) for x in items]
    if brief and section in ("decisions", "open_questions"):
        lines = [x if len(x) <= BRIEF_LINE else x[:BRIEF_LINE - 3] + "..." for x in lines]
    return "\n".join(head + lines)


#: In a brief (render(..., brief=True)): the team's decisions kept, answered
#: questions kept, and the characters of one such line.
BRIEF_TEAM_DECISIONS = 20
BRIEF_ANSWERED = 10
BRIEF_LINE = 260


def _line(section: str, x: dict, brief: bool) -> str:
    i = x.get("id", "")
    if section == "assumptions":
        return f"- {i} [{x['status']}] {x['text']}"
    if section == "evidence":
        check = {True: "verified", False: "NOT FOUND", None: "unchecked"}[x.get("verified")]
        bears = f" (bears on {', '.join(x['bears_on'])})" if x.get("bears_on") else ""
        return f"- {i} [{check}] {x['claim']} -- {x['source']}{bears}"
    if section == "hypotheses":
        line = f"- {i} [{x['status']}] {x.get('plain') or x['statement']}"
        if not brief:
            if x.get("plain"):
                line += f"\n  claim: {x['statement']}"
            line += "".join(f"\n  {label}: {x[key] if isinstance(x[key], str) else ', '.join(x[key])}"
                            for key, label in (("prediction", "prediction"),
                                               ("rationale", "why"), ("evidence", "evidence"),
                                               ("novelty", "novelty")) if x.get(key))
        return line
    if section == "rubric":
        tail = "" if brief else "".join(
            f"; {label} {x[key]}" for key, label in (("measure", "measured by"),
                                                      ("threshold", "passes at")) if x.get(key))
        at = f", assessed at step {x['assessed_at']}" if x.get("assessed_at") else ""
        return f"- {i} {x['criterion']}{tail} (weight {x.get('weight', 1)}{at})"
    if section == "decisions":
        why = f" -- because {x['why']}" if x.get("why") and not brief else ""
        by = f" [{x['by']}]" if x.get("by") else ""
        return f"- {i}{by} {x['what']}{why}"
    if section == "open_questions":
        state = "answered: " + x["answer"] if x.get("status") == "answered" else "open"
        return f"- {i} (for {x.get('for', 'user')}, {state}) {x['text']}"
    if section == "risks":
        return f"- {i} {x['text']}" + (f" -- mitigation: {x['mitigation']}"
                                       if x.get("mitigation") and not brief else "")
    if section == "reviews":
        scores = (f"panel {', '.join(str(r.get('rating')) for r in x['panel'])}"
                  if x.get("panel") else
                  f"soundness {x.get('soundness')}, presentation {x.get('presentation')}, "
                  f"contribution {x.get('contribution')}")
        head = (f"- {i} {x['decision'] + ', ' if x.get('decision') else ''}rating "
                f"{x.get('rating')}/10 (confidence {x.get('confidence')}/5; {scores}) of "
                f"{x.get('run') or 'this run'}: {x.get('summary', '')}")
        requests = "".join(f"\n  [{r['priority']}] {r['what']}" + (f" -- {r['why']}" if r.get("why")
                                                                     and not brief else "")
                           for r in x.get("requests") or [])
        if brief:
            return head + requests
        return head + "".join(
            f"\n  {label}:" + "".join(f"\n    - {v}" for v in x.get(key) or [])
            for key, label in (("consensus", "the reviewers agree"),
                               ("disagreements", "they differ"), ("strengths", "strengths"),
                               ("weaknesses", "weaknesses"), ("questions", "questions"))
            if x.get(key)) + (
            f"\n  justification: {x['justification']}" if x.get("justification") else "") + (
            "".join(f"\n  reviewer {r.get('id')} ({r.get('rating')}/10): "
                    + "; ".join(r.get("weaknesses") or [])[:600] for r in x.get("panel") or [])) + (
            f"\n  limitations: {x['limitations']}" if x.get("limitations") else "") + (
            "\n  requests:" + requests if requests else "")
    if section == "results":
        return f"- {i} {x['node']}{'/' + x['job'] if x.get('job') else ''}: {x['summary']}"
    return f"- {json.dumps(x)}"


def changed_since(state: dict, seen: dict[str, int]) -> list[str]:
    """Sections whose version moved past `seen` -- what someone who last
    looked at `seen` has not seen yet."""
    return [s for s in SECTIONS if version(state, s) > int((seen or {}).get(s, 0))]


# ---- keeping it ------------------------------------------------------------------------


class Store:
    """A run folder's state/ directory: the current state, its readable
    rendering, and one history file per change."""

    def __init__(self, run_dir: str | os.PathLike):
        self.folder = Path(run_dir) / "state"
        self.path = self.folder / "research.json"
        self.history = self.folder / "history"

    def read(self) -> dict:
        if not self.path.exists():
            return new()
        return json.loads(self.path.read_text(encoding="utf-8"))

    def write(self, state: dict, *, by: str = "", ops: list[dict] | None = None,
              changed: list[str] | None = None, session: str = "") -> None:
        """`session`: the model call that made these changes, when one did."""
        self.folder.mkdir(parents=True, exist_ok=True)
        _atomic(self.path, json.dumps(state, indent=2, ensure_ascii=False))
        _atomic(self.folder / "research.md", "# Research state\n\n" + render(state) + "\n")
        if ops:
            self.history.mkdir(exist_ok=True)
            n = len(list(self.history.glob("*.json"))) + 1
            _atomic(self.history / f"{n:04d}.json", json.dumps(
                {"at": _now(), "by": by, "changed": changed or [], "ops": ops,
                 "versions": state.get("versions", {}),
                 **({"session": session} if session else {})}, indent=2, ensure_ascii=False))


def _atomic(path: Path, text: str) -> None:
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(text if text.endswith("\n") else text + "\n")
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dump(value: Any) -> Any:
    """A Pydantic object (or a list of them) as plain data."""
    if isinstance(value, BaseModel):
        return value.model_dump(by_alias=True)
    if isinstance(value, list):
        return [dump(v) for v in value]
    return value
