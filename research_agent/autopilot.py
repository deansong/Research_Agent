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


def hand_back(folder: Path, why: str) -> str:
    """The autopilot stops answering until the user does; the note for them."""
    s = load(folder)
    s.handed_back = f"Autopilot handed back to you: {why}. Answer, and it carries on."
    save(folder, s)
    return s.handed_back


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
    approve: list[Literal["brief", "design"]] = Field(default_factory=list)
    """Bootstrap: "brief" or "design", to approve it as it now stands."""
    go: bool = False
    """Execution: the step waiting for the user's go may start."""
    retry: list[str] = Field(default_factory=list)
    """Execution: steps that failed, were stopped or skipped, to give one more
    attempt -- the only way a step out of attempts gets another."""
    waive: list[str] = Field(default_factory=list)
    """Execution: failed or stopped steps to pass as they are, for `why` --
    the only way a step that did its own part passes over a failed verdict."""
    stop: bool = False
    """Execution: end the run (a budget reached, nothing sensible left)."""
    hand_back: str = ""
    """Why only the user can decide this ("" when you decide): raising a
    limit against giving up planned work, or dropping part of the study.
    Nothing else in your answer is done; the user is asked."""


def instructions(run_dir=None) -> str:
    """The autopilot's standing instructions: prompts/roles/autopilot.md."""
    from research_agent import prompt_files

    return prompt_files.instructions("roles/autopilot", run_dir)


INSTRUCTIONS = instructions()


def decide(run, st: dict, *, stage: str, reply: str, awaiting: str = "") -> AutopilotOutput:
    """The user's answer, by the autopilot: one call of the discussor's role,
    in the `autopilot` conversation."""
    from research_backend import conversation

    stopped_once = []

    def check(out: AutopilotOutput) -> list[str]:
        problems = [] if out.answer.strip() else ["`answer` is empty: write the user's reply"]
        if stage == "execution" and out.stop and not stopped_once:
            # Measured (run 1455-next): a step failed only for want of what a
            # gate had cancelled, and the autopilot stopped the whole run --
            # a final stop -- instead of passing the step.
            from research_agent.execute import nodes as exec_nodes
            failed = [r["id"] for r in exec_nodes.records(st).values()
                      if r["status"] in ("fail", "stopped")]
            if failed:
                stopped_once.append(True)
                problems.append(
                    f"`stop` ends the run for good, and step(s) {', '.join(failed)} can still be "
                    f"decided: `waive` a step that did its own part (what it lacks is an earlier "
                    f"or later step's), `retry` one the verdict's advice can fix. Stop only when "
                    f"neither can, or a budget is reached -- then answer with stop again.")
        if stage == "execution" and out.approve:
            problems.append("`approve` is for the bootstrap; in the execution use go or stop")
        if stage == "bootstrap" and (out.go or out.stop or out.retry or out.waive):
            problems.append("`go`, `stop` and `retry` are for the execution; in the bootstrap "
                            "approve or answer")
        if stage == "execution" and out.retry:
            from research_agent.execute import nodes as exec_nodes
            problems += exec_nodes.step_problems(st, out.retry, [])
        if stage == "execution" and out.waive:
            from research_agent.execute import nodes as exec_nodes
            problems += exec_nodes.step_problems(st, [], [{"step": s} for s in out.waive])
        for s in out.approve:
            if not S.version(st, s):
                problems.append(f"approve: there is no {s} yet")
        if out.go and not awaiting:
            problems.append("`go`: no step is waiting for a go")
        return problems

    budget = run.brief_budget("discussor")
    parts = []
    if not conversation.exists(SESSION, home=run.rb_home):
        said = chat.handover(run.folder, budget // 3)
        if said:
            parts.append(said.replace("YOU TAKE OVER THIS CONVERSATION",
                                      "THE CONVERSATION WITH THE USER SO FAR"))
    parts += [_block("THE DISCUSSOR ASKS THE USER", reply or "(nothing: decide how to go on)")]
    if awaiting:
        step = awaiting.split(":", 1)[1] if awaiting.startswith("after:") else awaiting
        parts.append(_block("WAITING FOR A GO", f"step {step}"
                            + (" (after it ran: to go on)" if awaiting.startswith("after:")
                               else " (before it runs)")))
    sections = (["goal", "constraints", "decisions", "brief", "hypotheses", "rubric", "protocol",
                 "plan", "design", "open_questions", "risks", "status"] if stage == "bootstrap"
                else ["goal", "constraints", "decisions", "plan", "execution", "status"])
    parts.append(_block("THE RESEARCH STATE", S.render_within(
        st, sections, max(budget - sum(len(p) for p in parts), 4000))))
    limits = getattr(run, "limits", None)
    if stage == "execution" and limits is not None:
        from research_agent.execute import nodes as exec_nodes
        stuck = exec_nodes.out_of_attempts(st, limits.max_attempts, run.folder)
        if stuck:
            parts.append(stuck)
    parts.append(f"Today is {datetime.now(timezone.utc):%Y-%m-%d}. Answer as the user.")
    out, _ = run.call("discussor", brief="\n\n".join(parts), schema=AutopilotOutput,
                      skills=None, instructions=instructions(run.folder), session=SESSION,
                      label="discussor (autopilot)", check=check)
    return out


def _block(title: str, text: str) -> str:
    return f"=== {title} ===\n{text.strip()}"


def answer_for(out: AutopilotOutput) -> dict:
    """The resume value the human node takes, as if the user had answered."""
    return {"text": f"{MARK} {out.answer.strip()}", "approve": list(out.approve),
            "go": out.go, "stop": out.stop, "retry": list(out.retry),
            "waive": [{"step": s, "why": f"autopilot: {out.why.strip()[:400]}"}
                      for s in out.waive]}


def decision_op(out: AutopilotOutput) -> dict:
    return {"op": "add", "section": "decisions", "value": {
        "what": out.answer.strip()[:500], "why": out.why.strip()[:500], "by": "autopilot"}}
