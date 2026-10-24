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

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import research_backend as RB

from research_agent import state as S
from research_agent.bootstrap import prompts, schemas, verify, views
from research_agent.bootstrap import config
from research_agent.bootstrap.config import RoleConfig

#: Answer attempts per call: the first, and two with the problems fed back.
ATTEMPTS = 3
DISCUSSOR_SESSION = "discussor"


class NodeFailed(RB.BackendError):
    """A node could not give a usable answer within its attempts."""


@dataclass
class Run:
    """One bootstrap run: its folder, which provider each role uses, and
    which skills each node gets (bootstrap/config.toml unless given)."""

    folder: Path
    roles: dict[str, RoleConfig]
    skills_dir: Path | None = None
    skills: dict | None = None
    """Per node (config.NODES): None, "auto" or a list of skill names."""
    progress: bool = False
    """Print what each model does while it works (its commands, files)."""
    trace: bool = False
    """Print every model call's full input and output."""
    quiet: bool = False
    """Print nothing about calls -- not even that one has started."""

    def __post_init__(self):
        self._traced_roles: set[str] = set()
        if self.skills is None or self.skills_dir is None:
            base = config.defaults()
            self.skills = base.skills if self.skills is None else self.skills
            self.skills_dir = base.skills_dir if self.skills_dir is None else self.skills_dir

    @classmethod
    def from_config(cls, folder: Path, cfg: "config.BootstrapConfig", *, progress=False,
                    trace=False) -> "Run":
        return cls(folder, cfg.roles, skills_dir=cfg.skills_dir, skills=cfg.skills,
                   progress=progress, trace=trace)

    @property
    def rb_home(self) -> Path:
        return self.folder / "rb"

    def call(self, role: str, *, brief: str, schema, skills_key: str | None = None,
             access: str | None = None, workdir: Path | None = None,
             session: str | None = None,
             check: Callable[[object], list[str]] | None = None):
        """One node call: (answer, session id). Retries with the problems."""
        cfg = self.roles[role]
        chosen = (self.skills or {}).get(skills_key or role)
        skills_dir = self.skills_dir if self.skills_dir and self.skills_dir.is_dir() else None
        rb = RB(cfg.provider, cfg.model, effort=cfg.effort,
                instructions=prompts.INSTRUCTIONS[role], access=access,
                workdir=str(workdir or self.folder), home=self.rb_home,
                skills=chosen if skills_dir else None, skills_dir=skills_dir, **cfg.options)
        rb.new_session(session or self._next_name(role), exist_ok=bool(session))
        label = (skills_key or role).replace("_", " ")
        prompt = brief
        for attempt in range(1, ATTEMPTS + 1):
            self._announce(label, cfg, rb, attempt, prompt, chosen if skills_dir else None)
            started = time.monotonic()
            try:
                # Tracing shows the work as it happens too, not only its ends.
                reply = rb.send(prompt, schema=schema, progress=self.progress or self.trace)
                problems = check(reply.data) if check else []
            except RB.BackendOutputError as exc:
                problems = [str(exc)[:2000]]
                reply = None
            self._report(label, reply, problems, time.monotonic() - started)
            if not problems:
                return reply.data, rb.session_id
            if attempt == ATTEMPTS:
                raise NodeFailed(f"{role} gave no usable answer in {ATTEMPTS} attempts. "
                                 f"Last problems:\n" + "\n".join(f"- {p}" for p in problems))
            prompt = ("Your answer could not be used:\n" + "\n".join(f"- {p}" for p in problems)
                      + "\n\nAnswer the same task again, fixing these.")

    # ---- what the terminal shows -----------------------------------------------------

    def _announce(self, label, cfg, rb, attempt, prompt, skills) -> None:
        if self.quiet:
            return
        again = f", attempt {attempt}" if attempt > 1 else ""
        print(f"  > {label}: asking {cfg} (session {rb.session_id}{again})...", flush=True)
        if not self.trace:
            return
        role = label.split()[0]
        if role not in self._traced_roles:
            self._traced_roles.add(role)
            print(_box(f"{role}: standing instructions", rb.session.instructions))
        sent = prompt
        if skills is not None and attempt == 1 and not rb.session.turns:
            sent = f"[+ skills, sent in front of the first message: {_skills_word(skills)}]\n\n" + sent
        print(_box(f"{label}: INPUT", sent))

    def _report(self, label, reply, problems, seconds) -> None:
        if self.quiet:
            return
        if self.trace and reply is not None:
            tokens = reply.tokens
            used = (f", {tokens.input_tokens:,} in / {tokens.output_tokens:,} out tokens"
                    if tokens else "")
            print(_box(f"{label}: OUTPUT ({seconds:.0f}s{used})", reply.text))
        if problems:
            print(f"  ! {label}: answer not usable -- " + "; ".join(p[:200] for p in problems[:4]),
                  flush=True)
        elif not self.trace:
            print(f"  < {label}: answered in {seconds:.0f}s", flush=True)

    def _next_name(self, role: str) -> str:
        folder = self.rb_home / "sessions"
        taken = len(list(folder.glob(f"{role}-*"))) if folder.is_dir() else 0
        return f"{role}-{taken + 1:03d}"

    def catalogue(self) -> list[tuple[str, str]]:
        """Every skill in the folder: (name, description)."""
        if not (self.skills_dir and self.skills_dir.is_dir()):
            return []
        return [(s.name, s.description) for s in RB.list_skills(self.skills_dir)]


@dataclass
class Outcome:
    """What a node produced: state operations, a one-line summary, and its
    session."""

    ops: list[dict] = field(default_factory=list)
    summary: str = ""
    session: str = ""
    data: object = None


def _box(title: str, text: str) -> str:
    """A titled block of text for the terminal."""
    rule = "-" * max(10, 78 - len(title))
    return f"\n---- {title} {rule}\n{(text or '').rstrip()}\n{'-' * 84}\n"


def _skills_word(skills) -> str:
    return skills if isinstance(skills, str) else ", ".join(skills)


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

    out, session = run.call("discussor", brief=views.discussor_turn(st, seen, user=user, note=note),
                            schema=schemas.DiscussorOutput, session=DISCUSSOR_SESSION, check=check)
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


# ---- orchestrator ---------------------------------------------------------------------------


def orchestrator(run: Run, st: dict, *, requests: list[dict], last: str = "") -> Outcome:
    def check(out: schemas.OrchestratorOutput) -> list[str]:
        if out.action == "research" and not (out.questions or out.task.strip()):
            return ["research needs `questions` (or a task saying what to find)"]
        if out.action in ("hypothesize", "plan", "design", "ask_user") and not out.task.strip():
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


# ---- planner ---------------------------------------------------------------------------------


def hypothesize(run: Run, st: dict, *, task: str) -> Outcome:
    hypotheses, rubric, evidence = _ids(st, "hypotheses"), _ids(st, "rubric"), _ids(st, "evidence")

    def check(out: schemas.HypothesizeOutput) -> list[str]:
        # An id that does not exist yet is a new item -- models number their
        # drafts (H1, H2, ...), and a retry costs a whole turn. Code assigns
        # the real ids.
        problems = [f"{h.id or h.statement[:40]!r} cites {e}, which is not an evidence id"
                     for h in out.hypotheses for e in h.evidence if e not in evidence]
        problems += [f"hypothesis {h.statement[:40]!r} has no prediction"
                     for h in out.hypotheses if not h.prediction.strip()]
        return problems

    out, session = run.call("planner", brief=views.planner(st, job="hypothesize", task=task),
                            schema=schemas.HypothesizeOutput, skills_key="planner_hypothesize",
                            check=check)
    ops = []
    for h in out.hypotheses:
        value = h.model_dump(exclude={"id"})
        ops.append({"op": "update", "section": "hypotheses", "id": h.id, "value": value}
                   if h.id in hypotheses else {"op": "add", "section": "hypotheses", "value": value})
    for r in out.rubric:
        value = r.model_dump(exclude={"id"})
        ops.append({"op": "update", "section": "rubric", "id": r.id, "value": value}
                   if r.id in rubric else {"op": "add", "section": "rubric", "value": value})
    ops.append({"op": "set", "section": "protocol", "value": out.protocol.model_dump()})
    ops += [{"op": "add", "section": "risks", "value": r.model_dump()} for r in out.risks]
    ops += [{"op": "add", "section": "assumptions", "value": a.model_dump()}
            for a in out.assumptions]
    summary = (f"{len(out.hypotheses)} hypotheses, {len(out.rubric)} rubric criteria, "
               f"protocol set. {out.summary}")
    ops.append(_result_op("planner", "hypothesize", task, summary, session))
    return Outcome(ops=ops, summary=summary, session=session, data=out)


def plan(run: Run, st: dict, *, task: str) -> Outcome:
    rubric = _ids(st, "rubric")

    def check(out: schemas.PlanOutput) -> list[str]:
        ids = [s.id for s in out.steps]
        problems = [] if ids == [str(n) for n in range(1, len(ids) + 1)] else [
            f"step ids must be \"1\", \"2\", ... in order, not {ids}"]
        for s in out.steps:
            problems += [f"step {s.id} depends on {d}, which is not an earlier step"
                         for d in s.depends_on if d not in ids[:ids.index(s.id)]]
            problems += [f"step {s.id} serves {r}, which is not a rubric id"
                         for r in s.rubric if r not in rubric]
            if not s.check.strip():
                problems.append(f"step {s.id} has no check")
        return problems

    out, session = run.call("planner", brief=views.planner(st, job="plan", task=task),
                            schema=schemas.PlanOutput, skills_key="planner_plan", check=check)
    ops = [{"op": "set", "section": "plan", "value": {
        "summary": out.summary, "steps": [s.model_dump() for s in out.steps]}}]
    ops += [{"op": "add", "section": "risks", "value": r.model_dump()} for r in out.risks]
    summary = f"a plan of {len(out.steps)} steps. {out.summary}"
    ops.append(_result_op("planner", "plan", task, summary, session))
    return Outcome(ops=ops, summary=summary, session=session, data=out)


# ---- designer ---------------------------------------------------------------------------------


def design(run: Run, st: dict, *, task: str) -> Outcome:
    steps = [s["id"] for s in (st.get("plan") or {}).get("steps") or []]
    rubric = _ids(st, "rubric")
    catalogue = run.catalogue()
    known_skills = {name for name, _ in catalogue}

    def check(out: schemas.DesignOutput) -> list[str]:
        names = [n.name for n in out.nodes]
        problems = [f"node names must be unique: {names}"] if len(set(names)) != len(names) else []
        problems += [f"{n} is not lower_snake_case" for n in names
                     if not n.replace("_", "").isalnum() or n != n.lower() or not n[:1].isalpha()]
        for role in ("orchestrator", "validator"):
            count = sum(n.role == role for n in out.nodes)
            if count != 1:
                problems.append(f"there must be exactly one {role} node, not {count}")
        problems += [f"the validator {n.name} must be read_only" for n in out.nodes
                     if n.role == "validator" and n.access != "read_only"]
        owners: dict[str, list[str]] = {}
        for n in out.nodes:
            for s in n.steps:
                owners.setdefault(s, []).append(n.name)
        problems += [f"plan step {s} has no node" for s in steps if s not in owners]
        problems += [f"plan step {s} has several nodes: {', '.join(o)}"
                     for s, o in owners.items() if len(o) > 1]
        problems += [f"{s} is not a plan step" for s in owners if s not in steps]
        problems += [f"{n.name}: no skill called {k}" for n in out.nodes for k in n.skills
                     if known_skills and k not in known_skills]
        problems += [f"{n.name}: {r} is not a rubric id" for n in out.nodes
                     for r in n.validated_by if r not in rubric]
        return problems

    out, session = run.call("designer", brief=views.designer(st, task=task, skills=catalogue),
                            schema=schemas.DesignOutput, check=check)
    ops = [{"op": "set", "section": "design", "value": out.model_dump()}]
    summary = f"a design of {len(out.nodes)} nodes. {out.summary}"
    ops.append(_result_op("designer", "", task, summary, session))
    return Outcome(ops=ops, summary=summary, session=session, data=out)
