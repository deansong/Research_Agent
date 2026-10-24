"""
WHAT:  The brief each node is given: which parts of the research state, and
       nothing else.
WHY:   This is the context management. The discussor keeps the conversation;
       every other node starts fresh and sees only what its job needs -- never
       the chat, never another node's raw output. Keeping all of it here makes
       "what does the planner see?" a question with one place to look.
"""

from __future__ import annotations

from research_agent import state as S


def approvals_line(st: dict) -> str:
    """"hypotheses: approved | rubric: v2, not approved | plan: none yet" """
    parts = []
    for section in S.APPROVABLE:
        v = S.version(st, section)
        parts.append(f"{section}: " + ("none yet" if not v else "approved" if S.approved(st, section)
                                       else f"v{v}, not approved"))
    return " | ".join(parts)


def _today() -> str:
    """So "recent" means something to a model whose knowledge has a cut-off."""
    from datetime import date

    return date.today().isoformat()


def _block(title: str, text: str) -> str:
    return f"=== {title} ===\n{text.strip() or '(nothing yet)'}"


def discussor_turn(st: dict, seen: dict, *, user: str = "", note: str = "") -> str:
    """One discussor turn: the user's words (if any), what the team just did,
    and what changed in the state since the discussor last looked."""
    changed = S.changed_since(st, seen)
    parts = []
    if user:
        parts.append(_block("THE USER SAYS", user))
    if note:
        parts.append(_block("FROM THE ORCHESTRATOR", note))
    if not seen:
        parts.append(_block("THE RESEARCH STATE", S.render(st)))
    elif changed:
        parts.append(_block("CHANGED SINCE YOU LAST SPOKE",
                            S.render(st, [s for s in changed if s != "results"])
                            + _latest_results(st, seen)))
    parts.append(_block("APPROVALS", approvals_line(st)))
    status = st.get("status") or {}
    if status.get("summary"):
        parts.append(_block("WHERE THINGS STAND", status["summary"]))
    return "\n\n".join(parts)


def _latest_results(st: dict, seen: dict) -> str:
    """The node calls the discussor has not heard about."""
    count = S.version(st, "results") - int((seen or {}).get("results", 0))
    recent = (st.get("results") or [])[-count:] if count > 0 else []
    if not recent:
        return ""
    return "\n\n## Work done\n" + "\n".join(
        f"- {r['node']}{'/' + r['job'] if r.get('job') else ''}: {r['summary']}" for r in recent)


def orchestrator(st: dict, *, requests: list[dict], last: str = "") -> str:
    parts = [f"Today is {_today()}.",
             _block("THE RESEARCH STATE", S.render(st, brief=True)),
             _block("APPROVALS", approvals_line(st))]
    if requests:
        parts.append(_block("REQUESTS FROM THE DISCUSSOR", "\n".join(
            f"- [{r['kind']}] {r['detail']}" for r in requests)))
    if last:
        parts.append(_block("THE LAST PIECE OF WORK", last))
    return "\n\n".join(parts) + "\n\nDecide the next action."


def researcher(st: dict, *, task: str) -> str:
    """The researcher's questions are open questions in the state, so it
    answers them by id."""
    questions = [q for q in st.get("open_questions") or []
                 if q.get("for") == "research" and q.get("status") == "open"]
    hypotheses = S.render(st, ["hypotheses"], brief=True)
    return "\n\n".join([
        f"Today is {_today()}.",
        _block("YOUR TASK", task),
        _block("THE QUESTIONS -- answer each in `answers` by its id",
               "\n".join(f"- {q['id']} {q['text']}" for q in questions)),
        _block("THE STUDY", S.render(st, ["goal", "constraints"])),
        _block("HYPOTHESES SO FAR", hypotheses or
               "None yet: leave `novelty` empty, and say in your findings what has "
               "already been done."),
        _block("EVIDENCE ALREADY FOUND (do not repeat)", S.render(st, ["evidence"], brief=True)),
    ])


def planner(st: dict, *, job: str, task: str) -> str:
    if job == "hypothesize":
        sections = ["goal", "constraints", "assumptions", "evidence", "decisions",
                    "hypotheses", "rubric", "protocol", "risks"]
        title = "JOB: HYPOTHESIZE -- hypotheses, rubric and protocol"
    else:
        sections = ["goal", "constraints", "decisions", "protocol", "rubric", "risks", "plan"]
        title = "JOB: PLAN -- the plan for the approved hypotheses"
    parts = [_block(title, task), _block("THE RESEARCH STATE", S.render(st, sections))]
    if job == "plan":
        chosen = [h for h in st.get("hypotheses") or [] if h.get("status") == "selected"] \
            or [h for h in st.get("hypotheses") or [] if h.get("status") == "proposed"]
        parts.insert(1, _block("THE HYPOTHESES TO TEST", S.render(
            {**st, "hypotheses": chosen}, ["hypotheses"])))
        parts.append(_block("EVIDENCE", S.render(st, ["evidence"], brief=True)))
    return "\n\n".join(parts)


def designer(st: dict, *, task: str, skills: list[tuple[str, str]]) -> str:
    catalogue = "\n".join(f"- {name}: {description}" for name, description in skills)
    return "\n\n".join([
        _block("YOUR TASK", task),
        _block("THE APPROVED PLAN", S.render(st, ["plan"])),
        _block("HYPOTHESES, RUBRIC AND PROTOCOL", S.render(
            {**st, "hypotheses": [h for h in st.get("hypotheses") or []
                                  if h.get("status") != "dropped"]},
            ["hypotheses", "rubric", "protocol"])),
        _block("CONSTRAINTS", S.render(st, ["goal", "constraints"])),
        _block("THE CURRENT DESIGN", S.render(st, ["design"])),
        _block("SKILLS YOU CAN GIVE A NODE", catalogue),
    ])
