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

from research_agent.bootstrap.schemas import PlanStepIn

from research_agent.bootstrap.schemas import AnswerIn, DecisionIn, FindingIn, QuestionIn, RiskIn


# ---- orchestrator ---------------------------------------------------------------------------


class ExecDecision(BaseModel):
    action: Literal["run", "retry", "skip", "ask_user", "stop", "done"]
    step: str = ""
    """For run, retry and skip: the plan step id."""
    reason: str
    task: str = ""
    """run / retry: a note for the dispatch, which checks the step's inputs and
    writes its owner's exact files and context next (for a retry, what to do
    differently). skip: why -- kept as the step's terminal record. ask_user:
    what to ask. stop: why."""
    summary: str
    """Where the execution stands, in a few sentences -- kept as the status."""
    decisions: list[DecisionIn] = Field(default_factory=list, max_length=6)
    open_questions: list[QuestionIn] = Field(default_factory=list, max_length=6)
    note_for_discussor: str = ""


# ---- the dispatch: a step's inputs checked before it runs ------------------------------


class InputIn(BaseModel):
    path: str
    """A file or folder the step reads: absolute, or relative to the run folder."""
    holds: str
    """What it holds, and what the step takes from it."""


class MissingIn(BaseModel):
    what: str
    from_step: str
    """The id of the earlier step that should have produced it."""


class DispatchOutput(BaseModel):
    ready: bool
    """True: everything the step needs is there, and named below. False: it
    must not start (say what is missing)."""
    inputs: list[InputIn] = Field(default_factory=list, max_length=40)
    """The exact files the step reads, from earlier steps and outside."""
    context: str = ""
    """What its owner needs that its step does not say: the names the step
    uses, the values and protocol details it must keep, the user's decisions
    that bear on it, for a retry what to do differently. "" if nothing."""
    obtain: list[str] = Field(default_factory=list, max_length=12)
    """Outside resources it needs that are not here yet -- a dataset, model
    weights, a tokenizer, a package: what, from which official source, and
    where to put it. Its owner gets them (it has the network): not missing."""
    missing: list[MissingIn] = Field(default_factory=list, max_length=12)
    """What an earlier step of this plan should have produced and did not
    (not ready)."""


# ---- a step's owner (worker, writer) ----------------------------------------------


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
    state: Literal["done", "launched", "failed", "dead_end", "blocked"]
    """done: the step's outputs are written, ready to be checked.
    launched: jobs are running (their ids in `jobs`); you will be woken when
    they finish. failed: something went wrong that another attempt could
    fix -- a bug, a crash, the environment (say what). dead_end: the work
    cannot go on as specified however it is retried -- access refused, the
    method diverges at every setting, it cannot fit the machine or the
    budget (say why, in `problems`): another route is taken. blocked: it
    needs a decision from the user (in `questions_for_user`)."""
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
    findings: list[FindingIn] = Field(default_factory=list, max_length=20)
    """Papers and datasets the step found: a researcher's are checked."""
    risks: list[RiskIn] = Field(default_factory=list, max_length=6)
    hypotheses: list[HypothesisResultIn] = Field(default_factory=list, max_length=10)
    """Only for the final analysis: which hypotheses the results support or
    refute. Kept once the validator passes the step."""


# ---- validator ------------------------------------------------------------------------------


class FindingIn(BaseModel):
    """One measurement the step made, as its files have it."""

    measure: str
    """The measurement plan's id when it is one (R3), else a short name."""
    value: str
    """The value as the files have it, with the file it is in."""
    expectation: str = ""
    """What the measurement plan expected, if anything."""
    against: Literal["as expected", "not as expected", "no expectation", "unclear"]
    note: str = ""


class Verdict(BaseModel):
    """Whether the owner did the work as it was told, and its results are
    real -- not whether they came out as hoped: that is a finding."""

    verdict: Literal["done", "deviated", "broken", "dead_end"]
    """done: the work was done as told -- the models, data, settings and
    procedure its step and context name, as the code, configs and logs that
    ran show -- and its outputs are complete, with numbers that can be
    recomputed from the saved files. deviated: it ran, but not as told
    (another model, a subset, a skipped seed, a changed setting, a silent
    fallback): `problems` says where. broken: outputs are missing or
    incomplete, a run crashed, or the numbers do not follow from the files.
    dead_end: the work was done as told and the files show the route itself
    cannot work -- not a bug another attempt could fix: `problems` gives the
    evidence. Another route is then taken."""
    summary: str
    findings: list[FindingIn] = Field(default_factory=list, max_length=30)
    """Every measurement the step made, each against its expectation."""
    problems: list[str] = Field(default_factory=list, max_length=20)
    advice: str = ""
    """For a retry: what the owner must do differently."""


# ---- reviewer -------------------------------------------------------------------------------


class ReviewRequestIn(BaseModel):
    what: str
    """What the next run should do -- an experiment, a baseline, an analysis,
    a fix -- specific enough to plan."""
    why: str
    """The weakness or question it answers."""
    priority: Literal["must", "should", "could"]


class PaperReport(BaseModel):
    """The paper of the finished study."""

    summary: str
    """What the paper says, in a few sentences, with its headline numbers."""
    pdf: str = ""
    """The compiled PDF's path; "" when there is none."""
    problems: list[str] = Field(default_factory=list, max_length=20)
    """What is missing or could not be done (no PDF: why)."""


class Assessment(BaseModel):
    score: int = Field(ge=1, le=4)
    """1 poor, 2 fair, 3 good, 4 excellent."""
    why: str
    """What in the paper (section, table, figure) the score rests on."""


class ReviewOutput(BaseModel):
    """One program-committee review, on the form AAAI asks for."""

    summary: str
    """What the paper claims and how it supports it, in your own words."""
    originality: Assessment
    """What is new against prior work."""
    significance: Assessment
    """Whether it matters: to whom, and how much."""
    soundness: Assessment
    """Technical quality: do the method, experiments and statistics support
    each claim."""
    clarity: Assessment
    reproducibility: Assessment
    """Could a reader redo it from the paper and its appendices."""
    strengths: list[str] = Field(max_length=10)
    weaknesses: list[str] = Field(max_length=12)
    """Each says where (section, table, figure) and what would fix it."""
    questions: list[str] = Field(default_factory=list, max_length=10)
    """For the authors: answers that could change your score."""
    missing_work: list[str] = Field(default_factory=list, max_length=10)
    """Prior work the paper should engage with -- only work you are sure exists."""
    ethics: str = ""
    rating: int = Field(ge=1, le=10)
    """1 strong reject, 3 reject, 5 borderline reject, 6 borderline accept,
    7 accept, 9 strong accept, 10 award quality."""
    justification: str
    """Why this rating: what weighs most."""
    confidence: int = Field(ge=1, le=5)


class MetaReviewOutput(BaseModel):
    """The senior program-committee member's meta-review of the panel."""

    summary: str
    """What the paper claims, and the panel's view of it."""
    consensus: list[str] = Field(max_length=10)
    """What the reviewers agree on."""
    disagreements: list[str] = Field(default_factory=list, max_length=10)
    """Where they differ, and which side the paper's evidence supports."""
    decision: Literal["accept", "weak accept", "borderline", "weak reject", "reject"]
    rating: int = Field(ge=1, le=10)
    confidence: int = Field(ge=1, le=5)
    justification: str
    requests: list[ReviewRequestIn] = Field(min_length=1, max_length=12)
    """What the next run must do for this paper to be accepted."""


# ---- discussor ------------------------------------------------------------------------------


class BreakpointIn(BaseModel):
    step: str
    """A plan step id."""
    when: Literal["before", "after"]
    """Stop before the step starts, or once it is checked."""
    on: bool = True
    """False removes the breakpoint."""


class AmendIn(BaseModel):
    """A change to the approved design during the execution."""

    target: Literal["step", "rubric", "protocol"]
    id: str
    """A step id ("4"), a rubric id ("R3"), or a protocol part (metrics,
    baselines, statistics, seeds, stop_criteria)."""
    field: Literal["detail", "check", "title", "criterion", "measure", "threshold", "value"]
    """step: detail, check or title; rubric: criterion, measure or threshold;
    protocol: value."""
    value: str
    """The new text, whole (for a list part of the protocol: one item a line)."""
    kind: Literal["workflow", "research"]
    """workflow: the step asks for something wrong or impossible that is not
    the research -- a check no step can meet, a record only the system keeps,
    a wrong path or name. research: anything that changes what is tested or
    how it is judged (always so for the rubric and the protocol)."""
    why: str


class WaiveIn(BaseModel):
    step: str
    why: str
    """The user's reason, in their words -- kept as the step's verdict."""


class ReplanStepIn(PlanStepIn):
    owner: str
    """The existing team node that does it: one of the kind the step is."""


class ReplanOutput(BaseModel):
    """The rest of the study on a new route: the steps that replace every
    step not yet done."""

    summary: str
    steps: list[ReplanStepIn] = Field(min_length=1, max_length=12)


class StepResults(BaseModel):
    """The discussor's message to the user once a step is checked."""

    reply: str
    """The step's results for the user, in the chat: nothing waits for them."""


class AsideOutput(BaseModel):
    """An answer while the team works: nothing waits for it."""

    reply: str
    breakpoints: list[BreakpointIn] = Field(default_factory=list, max_length=10)
    go: list[str] = Field(default_factory=list, max_length=10)
    """Steps the user gives the go for now, before they wait for it."""
    stop: bool = False
    """The user just said to stop the execution."""
    retry: list[str] = Field(default_factory=list, max_length=10)
    """Steps the user gives another attempt (beyond the attempt limit)."""
    waive: list[WaiveIn] = Field(default_factory=list, max_length=10)
    """Failed steps the user passes anyway, with why."""
    skip: list[WaiveIn] = Field(default_factory=list, max_length=10)
    """Steps the user says to skip, with why -- the only way a step that later
    steps need is skipped."""
    rewind: str = ""
    """A step id when the user asks to go back to it: that step and every
    step after it that needs it start over (outputs kept). "" otherwise."""
    amend: list[AmendIn] = Field(default_factory=list, max_length=30)
    """Changes to the approved design: a workflow fix on your own (with
    `retry` for a failed step it unblocks), a research change only when the
    user (or the autopilot) asks for it."""
    requests: list[str] = Field(default_factory=list, max_length=6)
    """Work for the orchestrator at its next decision, in clean words."""
    decisions: list[DecisionIn] = Field(default_factory=list, max_length=6)


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
    breakpoints: list[BreakpointIn] = Field(default_factory=list, max_length=10)
    """Where the user wants the work to stop for them."""
    retry: list[str] = Field(default_factory=list, max_length=10)
    """Steps the user gives another attempt (beyond the attempt limit)."""
    waive: list[WaiveIn] = Field(default_factory=list, max_length=10)
    """Failed steps the user passes anyway, with why."""
    skip: list[WaiveIn] = Field(default_factory=list, max_length=10)
    """Steps the user says to skip, with why -- the only way a step that later
    steps need is skipped."""
    rewind: str = ""
    """A step id when the user asks to go back to it: that step and every
    step after it that needs it start over (outputs kept). "" otherwise."""
    amend: list[AmendIn] = Field(default_factory=list, max_length=30)
    """Changes to the approved design: a workflow fix on your own (with
    `retry` for a failed step it unblocks), a research change only when the
    user (or the autopilot) asks for it."""
    replan: str = ""
    """A new route for the rest of the study, when the user agreed to it:
    what it must do instead, and why. Every step not yet done is replaced."""
    decisions: list[DecisionIn] = Field(default_factory=list, max_length=6)
    answers: list[AnswerIn] = Field(default_factory=list, max_length=6)
