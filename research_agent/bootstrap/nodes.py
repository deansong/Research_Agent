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

from dataclasses import dataclass, field
from pathlib import Path

from research_agent import chat, inbox
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
        self.instructions = {**prompts.INSTRUCTIONS, **self.instructions}
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
    ops += [{"op": "add", "section": "decisions", "value": {**d.model_dump(), "by": "user"}}
            for d in out.decisions]
    ops += [{"op": "add", "section": "assumptions", "value": a.model_dump()}
            for a in out.assumptions]
    ops += [{"op": "update", "section": "open_questions", "id": a.id,
             "value": {"status": "answered", "answer": a.answer}} for a in out.answers]
    ops += [{"op": "approve", "section": s} for s in dict.fromkeys(out.approve)]
    return DiscussorTurn(ops=ops, summary=out.reply[:200], session=session, data=out,
                         reply=out.reply, requests=[r.model_dump() for r in out.requests],
                         hand_to=out.hand_to)


def aside(run: Run, st: dict, *, user: str, activity: str = "") -> schemas.BootstrapAside:
    """The user writes while the team works: the discussor answers in its own
    conversation; requests and decisions go to the inbox for the graph."""
    budget = run.brief_budget("discussor")
    plain = views.aside(st, user=user, activity=activity, budget=budget * 2 // 3)

    def takeover() -> str:
        return chat.handover(run.folder, run.brief_budget("discussor") // 3) + "\n\n" + plain
    brief = takeover() if run.takes_over(DISCUSSOR_SESSION, "discussor") else plain
    with run.discussor_lock:
        out, _ = run.call("discussor", brief=brief,
                          schema=schemas.BootstrapAside, session=DISCUSSOR_SESSION,
                          label="discussor (aside)", takeover=takeover)
    for r in out.requests:
        inbox.post(run.folder, "request", text=r.detail, request_kind=r.kind)
    for d in out.decisions:
        inbox.post(run.folder, "decision", what=d.what, why=d.why)
    return out


# ---- orchestrator ---------------------------------------------------------------------------


def orchestrator(run: Run, st: dict, *, requests: list[dict], last: str = "") -> Outcome:
    def check(out: schemas.OrchestratorOutput) -> list[str]:
        if out.action == "research" and not (out.questions or out.task.strip()):
            return ["research needs `questions` (or a task saying what to find)"]
        if out.action in ("design", "ask_user") and not out.task.strip():
            return [f"{out.action} needs a `task`"]
        return []

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


def design(run: Run, st: dict, *, task: str) -> Outcome:
    """The whole design in one call -- hypotheses, rubric, protocol, steps and
    the nodes that do them -- so the study and the agent are thought out
    together. It replaces the last one: what it leaves out goes."""
    evidence = _ids(st, "evidence")
    catalogue = run.catalogue()
    known_skills = {name for name, _ in catalogue}

    def check(out: schemas.DesignOutput) -> list[str]:
        problems = _unique("hypothesis", [h.id for h in out.hypotheses], "H")
        problems += _unique("rubric", [r.id for r in out.rubric], "R")
        problems += [f"{h.id} cites {e}, which is not an evidence id"
                     for h in out.hypotheses for e in h.evidence if e not in evidence]
        ids = [s.id for s in out.steps]
        if ids != [str(n) for n in range(1, len(ids) + 1)]:
            problems.append(f'step ids must be "1", "2", ... in order, not {ids}')
        rubric = {r.id for r in out.rubric}
        for s in out.steps:
            problems += [f"step {s.id} depends on {d}, which is not an earlier step"
                         for d in s.depends_on if d not in ids[:ids.index(s.id)]]
            problems += [f"step {s.id} serves {r}, which is not a rubric id"
                         for r in s.rubric if r not in rubric]
        problems += design_problems({"nodes": [n.model_dump() for n in out.nodes]}, steps=ids,
                                    rubric=rubric, skills=known_skills)
        return problems

    out, session = run.call("designer", brief=views.designer(st, task=task, skills=catalogue),
                            schema=schemas.DesignOutput, check=check)
    ops = _replace(st, "hypotheses", [h.model_dump() for h in out.hypotheses])
    ops += _replace(st, "rubric", [r.model_dump() for r in out.rubric])
    ops += [{"op": "set", "section": "protocol", "value": out.protocol.model_dump()},
            {"op": "set", "section": "plan", "value": {
                "summary": out.summary, "steps": [s.model_dump() for s in out.steps]}},
            {"op": "set", "section": "design", "value": {
                "summary": out.summary, "nodes": [n.model_dump() for n in out.nodes],
                "notes": out.notes}}]
    # Its risks are part of the design: they replace the last design's, or
    # every revision piles its list on (measured: 141 risks after 23).
    ops += [{"op": "drop", "section": "risks", "id": r["id"]} for r in st.get("risks") or []]
    ops += [{"op": "add", "section": "risks", "value": r.model_dump()} for r in out.risks]
    summary = (f"a design of {len(out.steps)} steps and {len(out.nodes)} nodes, "
               f"{len(out.hypotheses)} hypotheses, {len(out.rubric)} criteria. {out.summary}")
    ops.append(_result_op("designer", "", task, summary, session))
    return Outcome(ops=ops, summary=summary, session=session, data=out)


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
