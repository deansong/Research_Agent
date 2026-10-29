"""
WHAT:  The brief each node is given: which parts of the research state, and
       nothing else.
WHY:   This is the context management. The discussor keeps the conversation;
       every other node starts fresh and sees only what its job needs -- never
       the chat, never another node's raw output. Keeping all of it here makes
       "what does the designer see?" a question with one place to look.
"""

from __future__ import annotations

from research_agent import prompt_files
from research_agent import state as S


def approvals_line(st: dict) -> str:
    """"brief: approved | design (hypotheses, rubric, protocol, steps, nodes): v2, not approved" """
    def one(section: str, label: str) -> str:
        v = S.version(st, section)
        return f"{label}: " + ("none yet" if not v else "approved" if S.approved(st, section)
                               else f"v{v}, not approved")
    return (one("brief", "brief") + " | "
            + one("design", "design (hypotheses, rubric, protocol, steps, nodes)"))


def _today() -> str:
    """So "recent" means something to a model whose knowledge has a cut-off."""
    from datetime import date

    return date.today().isoformat()


def _block(title: str, text: str) -> str:
    return f"=== {title} ===\n{text.strip() or '(nothing yet)'}"


def discussor_turn(st: dict, seen: dict, *, user: str = "", note: str = "",
                   budget: int | None = None) -> str:
    """One discussor turn: the user's words (if any), what the team just did,
    and what changed in the state since the discussor last looked -- in at
    most about `budget` characters (the state is what gives way)."""
    changed = S.changed_since(st, seen)
    head, tail = [], []
    if user:
        head.append(_block("THE USER SAYS", user))
    if note:
        head.append(_block("FROM THE ORCHESTRATOR", note))
    tail.append(_block("APPROVALS", approvals_line(st)))
    status = st.get("status") or {}
    if status.get("summary"):
        tail.append(_block("WHERE THINGS STAND", status["summary"]))
    room = state_room(budget, head + tail)
    if not seen:
        head.append(_block("THE RESEARCH STATE", S.render_within(st, budget=room)))
    elif changed:
        results = _latest_results(st, seen, None if room is None else room // 4)
        room = None if room is None else max(room - len(results), MIN_STATE)
        head.append(_block("CHANGED SINCE YOU LAST SPOKE",
                           S.render_within(st, [s for s in changed if s != "results"], room)
                           + results))
    return "\n\n".join(head + tail)


#: Characters the state keeps in a brief however little room is left.
MIN_STATE = 4000


def clip(text: str, budget: int | None) -> str:
    """The end of `text` within `budget` characters: the latest lines count most."""
    if budget is None or len(text) <= budget:
        return text
    return "\n(earlier lines left out)\n" + text[-budget:].split("\n", 1)[-1]


def state_room(budget: int | None, parts: list[str]) -> int | None:
    """What is left of `budget` for the research state after `parts`."""
    if budget is None:
        return None
    return max(budget - sum(len(p) + 2 for p in parts) - 100, MIN_STATE)


def _latest_results(st: dict, seen: dict, budget: int | None = None) -> str:
    """The node calls the discussor has not heard about (the latest, when
    they do not all fit in `budget` characters)."""
    count = S.version(st, "results") - int((seen or {}).get("results", 0))
    recent = (st.get("results") or [])[-count:] if count > 0 else []
    if not recent:
        return ""
    return "\n\n## Work done\n" + clip("\n".join(
        f"- {r['node']}{'/' + r['job'] if r.get('job') else ''}: {r['summary']}"
        for r in recent), budget)


def aside(st: dict, *, user: str, activity: str, budget: int | None = None,
          run_dir=None) -> str:
    """The user writes while the team works."""
    room = state_room(budget, [user, activity])
    parts = [_block("AN ASIDE -- the team is working; nothing waits for your answer",
                    prompt_files.text("notes/bootstrap_aside", run_dir)),
             _block("THE USER SAYS", user),
             _block("WHAT IS HAPPENING NOW", activity or "(the team is working)"),
             _block("THE RESEARCH STATE", S.render_within(st, budget=room, brief=True)),
             _block("APPROVALS", approvals_line(st))]
    return "\n\n".join(parts)


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


def brief_request(st: dict, *, task: str = "", budget: int | None = None,
                  run_dir=None) -> str:
    """For the discussor, before anything is designed: everything the team
    found, to explain to the user and turn into the brief with them."""
    found = []
    for r in st.get("results") or []:
        if r.get("node") == "researcher":
            found.append(f"- researcher, asked: {r.get('asked', '')[:300]}\n  {r['summary']}")
    for q in st.get("open_questions") or []:
        if q.get("for") == "research" and q.get("answer"):
            found.append(f"- {q['id']} {q['text']}\n  {q['answer']}")
    material = clip("\n".join(found), None if budget is None else budget // 2)
    evidence = S.render(st, ["evidence"], brief=True)
    if budget is not None:
        evidence = clip(evidence, max(budget - len(material), 2000))
    ask = prompt_files.text("notes/brief_request", run_dir)
    if (st.get("brief") or {}).get("text"):
        ask += " " + prompt_files.text("notes/brief_revise", run_dir)
    parts = [ask]
    if task:
        parts.append(_block("FROM THE ORCHESTRATOR", task))
    parts += [_block("WHAT THE RESEARCHER REPORTED", material or "(no research yet)"),
              _block("EVIDENCE", evidence)]
    return "\n\n".join(parts)


def designer(st: dict, *, task: str, skills: list[tuple[str, str]]) -> str:
    catalogue = "\n".join(f"- {name}: {description}" for name, description in skills)
    return "\n\n".join([
        f"Today is {_today()}.",
        _block("THE APPROVED BRIEF -- design from this",
               (st.get("brief") or {}).get("text") or "(none)"),
        _block("YOUR TASK", task),
        _block("THE RESEARCH STATE", S.render(
            {**st, "hypotheses": [h for h in st.get("hypotheses") or []
                                  if h.get("status") != "dropped"]},
            ["goal", "constraints", "assumptions", "decisions", "hypotheses", "rubric",
             "protocol", "plan", "design", "risks", "reviews"])),
        _block("EVIDENCE (cite by id)", S.render(st, ["evidence"], brief=True)),
        _block("SKILLS YOU CAN GIVE A NODE", catalogue),
    ])
