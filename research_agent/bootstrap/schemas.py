"""
WHAT:  What each bootstrap node answers with -- one Pydantic class per node.
WHY:   A node proposes changes to the research state; it never writes the
       state. These classes are the shape of those proposals, and code turns
       them into state operations (see nodes.py).
CONCEPT: Strict structured output: every field is sent as required and no
       object takes extra keys (research_backend makes the schema strict), so
       there are no open dicts here. An empty string or list means "nothing
       to say about this".
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

APPROVABLE = Literal["brief", "design"]


# ---- pieces several nodes use --------------------------------------------------------


class DecisionIn(BaseModel):
    what: str
    why: str = ""


class AssumptionIn(BaseModel):
    text: str


class AnswerIn(BaseModel):
    id: str
    """An open question's id, e.g. Q2."""
    answer: str


class QuestionIn(BaseModel):
    text: str
    for_: Literal["user", "research"] = Field("user", alias="for")

    model_config = {"populate_by_name": True}


class RiskIn(BaseModel):
    text: str
    mitigation: str = ""


# ---- discussor --------------------------------------------------------------------------


class GoalIn(BaseModel):
    """Only the fields the user just settled; "" or [] leaves a field as it is."""

    question: str = ""
    motivation: str = ""
    scope_in: list[str] = Field(default_factory=list)
    scope_out: list[str] = Field(default_factory=list)
    deliverable: str = ""


class ConstraintsIn(BaseModel):
    compute: str = ""
    budget: str = ""
    deadline: str = ""
    data: str = ""
    models: str = ""
    other: list[str] = Field(default_factory=list)


class Request(BaseModel):
    kind: Literal["research", "revise_brief", "revise_design", "proceed", "other"]
    detail: str
    """What the user wants, in your own clean words -- never the chat itself."""


class DiscussorOutput(BaseModel):
    reply: str
    """What you say to the user now."""
    hand_to: Literal["user", "orchestrator"] = "user"
    """Who acts next. "user": you answered or asked something, and the user
    replies. "orchestrator": the team has work -- what is in `requests`, or
    what the user's approval unlocks."""
    reason: str = ""
    """Why, in a few words."""
    goal: GoalIn = Field(default_factory=GoalIn)
    constraints: ConstraintsIn = Field(default_factory=ConstraintsIn)
    decisions: list[DecisionIn] = Field(default_factory=list, max_length=10)
    assumptions: list[AssumptionIn] = Field(default_factory=list, max_length=10)
    answers: list[AnswerIn] = Field(default_factory=list, max_length=10)
    brief: str = ""
    """The design brief, written or revised with the user: what to build or
    test, the scope, the main choices -- in plain words. "" leaves it as is."""
    approve: list[APPROVABLE] = Field(default_factory=list)
    """What the user has just clearly approved as it now stands: "brief", or
    "design" (its hypotheses, rubric, protocol, steps and nodes)."""
    requests: list[Request] = Field(default_factory=list, max_length=6)
    """Work for the orchestrator. Empty: the conversation simply continues."""


class BootstrapAside(BaseModel):
    """An answer while the team works: nothing waits for it."""

    reply: str
    requests: list[Request] = Field(default_factory=list, max_length=6)
    """Work for the orchestrator at its next decision."""
    decisions: list[DecisionIn] = Field(default_factory=list, max_length=6)


# ---- orchestrator ----------------------------------------------------------------------


class OrchestratorOutput(BaseModel):
    action: Literal["research", "brief", "design", "ask_user", "ready"]
    reason: str
    task: str
    """For research/design: what that node must do now, with any feedback it
    must take into account. For ask_user: what to ask."""
    questions: list[str] = Field(default_factory=list, max_length=8)
    """For research: the questions to answer."""
    summary: str
    """Where the study stands now, in a few sentences -- kept as the status."""
    next: str = ""
    open_questions: list[QuestionIn] = Field(default_factory=list, max_length=6)
    decisions: list[DecisionIn] = Field(default_factory=list, max_length=6)
    note_for_discussor: str = ""
    """What the discussor should tell or ask the user."""


# ---- researcher ------------------------------------------------------------------------


class FindingIn(BaseModel):
    claim: str
    source: str
    """An arXiv id (e.g. 2106.09685), a DOI, a PMID, a URL, or a dataset or model id."""
    bears_on: list[str] = Field(default_factory=list)


class NoveltyIn(BaseModel):
    hypothesis: str
    """A hypothesis id, e.g. H2."""
    note: str


class ResearcherOutput(BaseModel):
    summary: str
    findings: list[FindingIn] = Field(default_factory=list, max_length=20)
    novelty: list[NoveltyIn] = Field(default_factory=list, max_length=10)
    answers: list[AnswerIn] = Field(default_factory=list, max_length=10)
    unanswered: list[str] = Field(default_factory=list, max_length=10)


# ---- designer: the whole design -------------------------------------------------------


class HypothesisIn(BaseModel):
    id: str
    """H1, H2, ...: an existing id keeps (and revises) that hypothesis."""
    plain: str
    """One or two plain sentences: what you expect and why it matters."""
    statement: str
    prediction: str
    rationale: str = ""
    evidence: list[str] = Field(default_factory=list)


class CriterionIn(BaseModel):
    id: str
    """R1, R2, ..."""
    criterion: str
    measure: str
    threshold: str
    applies_to: list[str] = Field(default_factory=list)
    assessed_at: str
    """The ONE plan step that completes this criterion, where it is checked
    ("3"); the other steps are judged by their own checks."""


class ProtocolIn(BaseModel):
    baselines: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    statistics: str = ""
    seeds: str = ""
    stop_criteria: str = ""


class StepIn(BaseModel):
    id: str
    """"1", "2", ... in order."""
    title: str
    detail: str
    check: str
    depends_on: list[str] = Field(default_factory=list)
    gate: bool = False
    cost: str = ""
    follows: list[Literal["baselines", "metrics", "statistics", "seeds", "stop_criteria",
                          "compute", "budget", "deadline", "data", "models", "other"]] = \
        Field(default_factory=list)
    """The parts of the protocol and constraints the step's work must follow:
    its owner is given only those."""


class DesignNodeIn(BaseModel):
    name: str
    """lower_snake_case, unique."""
    role: Literal["orchestrator", "worker", "researcher", "validator", "writer"]
    steps: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    access: Literal["none", "read_only", "write"] = "read_only"
    instructions: str


class DesignOutput(BaseModel):
    """The whole design, every time: what is left out goes."""

    summary: str
    hypotheses: list[HypothesisIn] = Field(default_factory=list, max_length=8)
    rubric: list[CriterionIn] = Field(min_length=1, max_length=12)
    protocol: ProtocolIn
    steps: list[StepIn] = Field(min_length=1, max_length=12)
    nodes: list[DesignNodeIn] = Field(min_length=2, max_length=16)
    risks: list[RiskIn] = Field(default_factory=list, max_length=8)
    notes: str = ""
