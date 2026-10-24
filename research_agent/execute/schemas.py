"""
WHAT:  What each execution node answers with.
WHY:   As in the bootstrap: nodes propose, code decides. A step owner reports;
       only the validator's verdict (and code's own checks) marks a step done.
CONCEPT: Strict structured output (research_backend makes every field
       required and allows no extra keys), so no open dicts.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from research_agent.bootstrap.schemas import AnswerIn, DecisionIn, FindingIn, QuestionIn, RiskIn


# ---- orchestrator ---------------------------------------------------------------------------


class ExecDecision(BaseModel):
    action: Literal["run", "retry", "skip", "ask_user", "stop", "done"]
    step: str = ""
    """For run, retry and skip: the plan step id."""
    reason: str
    task: str = ""
    """run / retry: anything the step's owner needs beyond its brief (for a
    retry, what to do differently). skip: why -- kept as the step's terminal
    record. ask_user: what to ask. stop: why."""
    summary: str
    """Where the execution stands, in a few sentences -- kept as the status."""
    decisions: list[DecisionIn] = Field(default_factory=list, max_length=6)
    open_questions: list[QuestionIn] = Field(default_factory=list, max_length=6)
    note_for_discussor: str = ""


# ---- a step's owner (worker, runner, writer) ----------------------------------------------


class MetricIn(BaseModel):
    name: str
    value: str


class HypothesisResultIn(BaseModel):
    id: str
    """A hypothesis id, e.g. H2."""
    status: Literal["supported", "refuted"]
    why: str
    """The result that decides it, with the artifact it comes from."""


class StepReport(BaseModel):
    state: Literal["done", "launched", "failed", "blocked"]
    """done: the step's outputs are written, ready to be checked.
    launched: jobs are running (their ids in `jobs`); you will be woken when
    they finish. failed: it cannot be done as specified (say why).
    blocked: it needs a decision from the user (in `questions_for_user`)."""
    summary: str
    artifacts: list[str] = Field(default_factory=list, max_length=60)
    """Files the validator should check: paths in your artifacts folder
    (relative to it) or absolute paths in the run folder."""
    commands: list[str] = Field(default_factory=list, max_length=30)
    metrics: list[MetricIn] = Field(default_factory=list, max_length=40)
    jobs: list[str] = Field(default_factory=list, max_length=30)
    """For launched: the job ids the jobs helper printed."""
    problems: list[str] = Field(default_factory=list, max_length=20)
    questions_for_user: list[str] = Field(default_factory=list, max_length=6)
    findings: list[FindingIn] = Field(default_factory=list, max_length=10)
    risks: list[RiskIn] = Field(default_factory=list, max_length=6)
    hypotheses: list[HypothesisResultIn] = Field(default_factory=list, max_length=10)
    """Only for the final analysis: which hypotheses the results support or
    refute. Kept once the validator passes the step."""


# ---- validator ------------------------------------------------------------------------------


class RubricCheck(BaseModel):
    id: str
    """A rubric id, e.g. R3."""
    met: Literal["yes", "no", "not_applicable"]
    note: str


class Verdict(BaseModel):
    verdict: Literal["pass", "fail", "incomplete"]
    """pass: the step's check holds and its rubric items are met.
    fail: something is wrong. incomplete: outputs are missing."""
    summary: str
    rubric: list[RubricCheck] = Field(default_factory=list, max_length=20)
    problems: list[str] = Field(default_factory=list, max_length=20)
    advice: str = ""
    """For a retry: what the owner must do differently."""


# ---- discussor ------------------------------------------------------------------------------


class ExecDiscussorOutput(BaseModel):
    reply: str
    """What you say to the user now."""
    hand_to: Literal["user", "orchestrator"] = "user"
    reason: str = ""
    go: bool = False
    """The user just said the step waiting for them may start."""
    stop: bool = False
    """The user just said to stop the execution."""
    requests: list[str] = Field(default_factory=list, max_length=6)
    """Work for the orchestrator, in clean words (never the chat)."""
    decisions: list[DecisionIn] = Field(default_factory=list, max_length=6)
    answers: list[AnswerIn] = Field(default_factory=list, max_length=6)
