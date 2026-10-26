"""
WHAT:  Autopilot: the run decides for the user. Where the graph would stop for
       the user's answer (the `human` node of either stage), the discussor --
       in a conversation of its own, `autopilot` -- answers as the user's
       stand-in, and the run goes on.
WHY:   A study can run unattended: the user states the goal and the limits,
       ticks Autopilot, and comes back to a result.
CONCEPT: The answer goes through the same path a person's does (approvals, a
       go for a gated step, a stop), marked "[autopilot]" in the chat and
       recorded as the autopilot's decision. Code holds the limits: it never
       raises a budget (those are execute.json's, set beforehand), it answers
       only while the run is unfinished, and it hands back to the user after
       `max_decisions` answers in a row. Settings: <run>/autopilot.json, read
       at each stop, so ticking or unticking applies at the next one.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from research_agent import chat
from research_agent import state as S

FILE = "autopilot.json"
SESSION = "autopilot"
#: Prefix of the user's message when the autopilot wrote it.
MARK = "[autopilot]"
DEFAULT_MAX = 20


@dataclass
class Settings:
    on: bool = False
    max_decisions: int = DEFAULT_MAX
    """Answers in a row before it hands back to the user."""
    streak: int = 0
    handed_back: str = ""
    """Why it stopped answering, until the user answers."""
    decisions: int = 0
    """Answers it has given in this run, all told."""


def load(folder: Path) -> Settings:
    path = Path(folder) / FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Settings()
    return Settings(**{k: v for k, v in data.items() if k in Settings.__dataclass_fields__})


def save(folder: Path, s: Settings) -> None:
    path = Path(folder) / FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(s), indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)


def should_answer(folder: Path) -> tuple[bool, str]:
    """(answer now?, a note when it has just handed back)."""
    s = load(folder)
    if not s.on:
        return False, ""
    if s.streak >= s.max_decisions:
        if s.handed_back:
            return False, ""
        s.handed_back = (f"Autopilot handed back to you after {s.streak} decisions in a row "
                         f"(the limit). Answer, and it carries on.")
        save(folder, s)
        return False, s.handed_back
    return True, ""


def counted(folder: Path) -> None:
    s = load(folder)
    s.streak += 1
    s.decisions += 1
    save(folder, s)


def user_answered(folder: Path) -> None:
    """A person answered: the count of answers in a row starts again."""
    s = load(folder)
    if s.streak or s.handed_back:
        s.streak, s.handed_back = 0, ""
        save(folder, s)


# ---- the decision -----------------------------------------------------------------------


class AutopilotOutput(BaseModel):
    answer: str
    """What the user says now, in their voice: short, decisive."""
    why: str
    """Why this is what the user would want, from what they said and the state."""
    approve: list[Literal["hypotheses", "rubric", "protocol", "plan", "design"]] = \
        Field(default_factory=list)
    """Bootstrap: sections to approve as they now stand."""
    go: bool = False
    """Execution: the step waiting for the user's go may start."""
    stop: bool = False
    """Execution: end the run (a budget reached, nothing sensible left)."""


INSTRUCTIONS = """\
You are the AUTOPILOT of a research study: the user is away and asked you to
decide for them, so the work goes on unattended. You are the same discussor
that talks with them; now you answer the discussor's questions AS THE USER.

Decide as the user would, from what they said (the conversation, their goal,
constraints and decisions in the research state):
- Approve a section when it is complete, consistent with the goal and the
  constraints, and the team's checks raised nothing open. Otherwise ask for
  the specific change in `answer`, in one or two sentences.
- In the execution, give the go for a step that is next in the plan; retry a
  failed step once when the verdict says what to fix; pass or stop when it
  cannot succeed within the limits.
- Prefer the smallest decision that moves the study on. Never widen the
  scope, add data or models the user did not name, or raise a budget: the
  limits are the user's. When a budget is reached, stop.
- Where the user said nothing that settles it, choose the conservative option
  within their constraints and say in `why` that it was your choice.
- Never claim the user said something they did not.
Write `answer` as the user's reply: short and decisive."""


def decide(run, st: dict, *, stage: str, reply: str, awaiting: str = "") -> AutopilotOutput:
    """The user's answer, by the autopilot: one call of the discussor's role,
    in the `autopilot` conversation."""
    from research_backend import conversation

    def check(out: AutopilotOutput) -> list[str]:
        problems = [] if out.answer.strip() else ["`answer` is empty: write the user's reply"]
        if stage == "execution" and out.approve:
            problems.append("`approve` is for the bootstrap; in the execution use go or stop")
        if stage == "bootstrap" and (out.go or out.stop):
            problems.append("`go` and `stop` are for the execution; in the bootstrap approve "
                            "or answer")
        for s in out.approve:
            if not S.version(st, s):
                problems.append(f"approve: there is no {s} yet")
        if out.go and not awaiting:
            problems.append("`go`: no step is waiting for a go")
        return problems

    parts = []
    if not conversation.exists(SESSION, home=run.rb_home):
        said = chat.handover(run.folder)
        if said:
            parts.append(said.replace("YOU TAKE OVER THIS CONVERSATION",
                                      "THE CONVERSATION WITH THE USER SO FAR"))
    parts += [_block("THE DISCUSSOR ASKS THE USER", reply or "(nothing: decide how to go on)")]
    if awaiting:
        step = awaiting.split(":", 1)[1] if awaiting.startswith("after:") else awaiting
        parts.append(_block("WAITING FOR A GO", f"step {step}"
                            + (" (after it ran: to go on)" if awaiting.startswith("after:")
                               else " (before it runs)")))
    sections = (["goal", "constraints", "decisions", "hypotheses", "rubric", "protocol",
                 "plan", "design", "open_questions", "risks", "status"] if stage == "bootstrap"
                else ["goal", "constraints", "decisions", "plan", "execution", "status"])
    parts.append(_block("THE RESEARCH STATE", S.render(st, sections)))
    parts.append(f"Today is {datetime.now(timezone.utc):%Y-%m-%d}. Answer as the user.")
    out, _ = run.call("discussor", brief="\n\n".join(parts), schema=AutopilotOutput,
                      skills=None, instructions=INSTRUCTIONS, session=SESSION,
                      label="discussor (autopilot)", check=check)
    return out


def _block(title: str, text: str) -> str:
    return f"=== {title} ===\n{text.strip()}"


def answer_for(out: AutopilotOutput) -> dict:
    """The resume value the human node takes, as if the user had answered."""
    return {"text": f"{MARK} {out.answer.strip()}", "approve": list(out.approve),
            "go": out.go, "stop": out.stop}


def decision_op(out: AutopilotOutput) -> dict:
    return {"op": "add", "section": "decisions", "value": {
        "what": out.answer.strip()[:500], "why": out.why.strip()[:500], "by": "autopilot"}}
