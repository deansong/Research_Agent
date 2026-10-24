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
    """An arXiv id, a URL, or a dataset or model id."""
    verified: bool | None = None
    """True or False once code has checked the source; None until then."""
    found_by: str = ""
    bears_on: list[str] = Field(default_factory=list)
    """Hypothesis or question ids."""


class Hypothesis(BaseModel):
    id: str = ""
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


class ResearchState(BaseModel):
    goal: Goal = Field(default_factory=Goal)
    constraints: Constraints = Field(default_factory=Constraints)
    assumptions: list[Assumption] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    rubric: list[Criterion] = Field(default_factory=list)
    protocol: Protocol = Field(default_factory=Protocol)
    plan: Plan = Field(default_factory=Plan)
    design: Design = Field(default_factory=Design)
    decisions: list[Decision] = Field(default_factory=list)
    open_questions: list[Question] = Field(default_factory=list)
    risks: list[Risk] = Field(default_factory=list)
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
    "results": ("N", Result),
}
OBJECTS: dict[str, type[BaseModel]] = {
    "goal": Goal, "constraints": Constraints, "protocol": Protocol, "plan": Plan,
    "design": Design, "status": Status,
}
SECTIONS = ("goal", "constraints", "assumptions", "evidence", "hypotheses", "rubric",
            "protocol", "plan", "design", "decisions", "open_questions", "risks",
            "results", "status")
#: What the user approves, in the order the work needs them.
APPROVABLE = ("hypotheses", "rubric", "protocol", "plan", "design")
#: Never deleted: a dropped hypothesis is marked dropped, a decision stands.
KEPT = {"hypotheses", "decisions", "results"}


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
    """Approved, and not changed since."""
    at = (state.get("approvals") or {}).get(section)
    return at is not None and at == version(state, section) and version(state, section) > 0


def item(state: dict, section: str, item_id: str) -> dict | None:
    return next((x for x in state.get(section) or [] if x.get("id") == item_id), None)


# ---- changing it -----------------------------------------------------------------------


def apply(state: dict, ops: list[dict], *, by: str) -> tuple[dict, list[str]]:
    """Apply `ops` to a copy of `state`; return (new state, sections changed).

    An op is one of:
        {"op": "add",    "section": "hypotheses", "value": {...}}      new id given
        {"op": "update", "section": "hypotheses", "id": "H2", "value": {...}}
        {"op": "drop",   "section": "evidence",   "id": "E4"}
        {"op": "set",    "section": "plan",       "value": {...}}      a whole object
        {"op": "merge",  "section": "goal",       "value": {...}}      some fields
        {"op": "approve", "section": "plan"}                           this version

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
            if section not in APPROVABLE:
                raise StateError(f"{section} is not something to approve.")
            if version(out, section) == 0:
                raise StateError(f"There is no {section} to approve yet.")
            out.setdefault("approvals", {})[section] = version(out, section)
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
        value["id"] = _next_id(items, prefix)
        if section == "decisions":
            value.setdefault("by", by)
            value.setdefault("at", _now())
        if section == "evidence":
            value.setdefault("found_by", by)
        if section == "results":
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
            if section in APPROVABLE and version(state, section):
                mark = (" (approved)" if approved(state, section) else
                        f" (v{version(state, section)}, not approved)")
            parts.append(f"## {section.replace('_', ' ').title()}{mark}\n{text}")
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
    return "\n".join(_line(section, x, brief) for x in value or [])


def _line(section: str, x: dict, brief: bool) -> str:
    i = x.get("id", "")
    if section == "assumptions":
        return f"- {i} [{x['status']}] {x['text']}"
    if section == "evidence":
        check = {True: "verified", False: "NOT FOUND", None: "unchecked"}[x.get("verified")]
        bears = f" (bears on {', '.join(x['bears_on'])})" if x.get("bears_on") else ""
        return f"- {i} [{check}] {x['claim']} -- {x['source']}{bears}"
    if section == "hypotheses":
        line = f"- {i} [{x['status']}] {x['statement']}"
        if not brief:
            line += "".join(f"\n  {label}: {x[key] if isinstance(x[key], str) else ', '.join(x[key])}"
                            for key, label in (("prediction", "prediction"),
                                               ("rationale", "why"), ("evidence", "evidence"),
                                               ("novelty", "novelty")) if x.get(key))
        return line
    if section == "rubric":
        tail = "" if brief else "".join(
            f"; {label} {x[key]}" for key, label in (("measure", "measured by"),
                                                      ("threshold", "passes at")) if x.get(key))
        return f"- {i} {x['criterion']}{tail} (weight {x.get('weight', 1)})"
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
              changed: list[str] | None = None) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        _atomic(self.path, json.dumps(state, indent=2, ensure_ascii=False))
        _atomic(self.folder / "research.md", "# Research state\n\n" + render(state) + "\n")
        if ops:
            self.history.mkdir(exist_ok=True)
            n = len(list(self.history.glob("*.json"))) + 1
            _atomic(self.history / f"{n:04d}.json", json.dumps(
                {"at": _now(), "by": by, "changed": changed or [], "ops": ops,
                 "versions": state.get("versions", {})}, indent=2, ensure_ascii=False))


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
