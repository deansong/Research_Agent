"""
WHAT:  What the bootstrap graph's two model nodes must return.
WHY:   The designer's output is the interesting one: it IS an agent folder,
       expressed in a shape a strict structured-output mode will accept.
CONCEPT: Not LangGraph. Same extra="forbid" lesson as everywhere else.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agent.agentfolder.schema import (
    BranchSpec,
    EdgeSpec,
    FieldSpec,
    NodeRef,
    PromptPair,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class DiscussorQuestion(StrictModel):
    """One thing the discussor needs to know, with the answer it would assume."""

    topic: str
    """Which part of the checklist: "Claim", "Code", "Experiment design", ..."""
    question: str
    suggestion: str
    """The answer it will go ahead with if the human does not say otherwise."""
    why: str
    """What the answer changes -- the work, or the shape of the agent."""


class DiscussorOutput(StrictModel):
    """It advises, it never routes. Every open question comes in ONE turn, each
    with a suggested answer: one question per turn made a discussion of a dozen
    questions a dozen round trips, most of them answered "yes, that"."""

    reply: str
    questions: list[DiscussorQuestion] = Field(default_factory=list, max_length=15)
    research: list[str] = Field(default_factory=list, max_length=4)
    """Questions for the research agent, asked BEFORE the human is: the
    literature, standard datasets and baselines, typical results and costs.
    Ignored when research is off for the session."""
    requirements: str = ""
    advice: Literal["keep_discussing", "ready_to_plan"] = "keep_discussing"
    advice_reason: str = ""


class ResearchFinding(StrictModel):
    finding: str
    source: str
    """An arXiv id, a dataset or model id, or a URL -- never empty."""
    bears_on: str
    """Which part of the experiment it decides: dataset, baseline, metric..."""


class ResearchReport(StrictModel):
    """What the research agent found, for the discussor to ground its
    suggestions in -- and for the human to read in the session's research/."""

    summary: str
    findings: list[ResearchFinding] = Field(default_factory=list, max_length=20)
    suggestions: str = ""
    """What the discussor should suggest, in light of the findings."""
    unanswered: str = ""
    """What it could not find, said plainly."""


class SetSpec(StrictModel):
    """One value a command writes into `vars`, as a name/value PAIR.

    On disk this is a plain object -- {"planner_feedback": "{argument}"} --
    which is what you want to hand-edit. But an open-ended dict[str, X] cannot
    be expressed in a strict structured-output schema, so the designer emits
    pairs and the writer converts. Measured against a live Codex call:

        invalid_json_schema: ... Extra required key 'sets' supplied.

    Same reason `nodes` below is a list rather than an object.
    """

    name: str
    value: str


class CommandProposal(StrictModel):
    """A human-node command, as the designer emits it.

    Mirrors agentfolder.schema.CommandSpec except for `sets` (pairs, above) and
    `purposes` (an empty list rather than None, to avoid a nullable union).
    """

    name: str
    to: str
    purposes: list[str] = Field(default_factory=list, max_length=8)
    """Empty means "valid everywhere"."""
    aliases: list[str] = Field(default_factory=list, max_length=4)
    argument: str = ""
    summary: str = ""
    record: str = ""
    sets: list[SetSpec] = Field(default_factory=list, max_length=8)
    outcome: str = ""


class SubStep(StrictModel):
    """A piece of one step. Deliberately NOT recursive.

    A self-referencing model produces a recursive JSON Schema, which strict
    structured-output modes reject. Two levels -- step and substep -- is also
    as deep as a plan stays readable.
    """

    id: str = Field(pattern=r"^[0-9]+\.[0-9]+$")
    """Dotted, e.g. "2.1", so a substep's parent is obvious at a glance."""
    title: str
    detail: str = ""

    check: str = ""
    """Same meaning as PlanStep.check, and here for a structural reason.

    A plan's top level is the four STAGES of the work, so the thing a single
    node owns -- and therefore the thing a verifier has to apply -- is a
    substep. Without this field the check lands one level above the node that
    has to satisfy it, and every verifier in a designed graph is left applying
    its whole stage's check to one third of the work."""

    gate: bool = False
    """Same meaning as PlanStep.gate. A person approves a substep -- "start the
    long training run" -- not a whole stage."""


class PlanStep(StrictModel):
    """One numbered step of the work."""

    id: str = Field(pattern=r"^[0-9]+$")
    title: str
    detail: str = ""
    substeps: list[SubStep] = Field(default_factory=list, max_length=8)

    check: str = ""
    """How to tell this step actually worked -- concretely.

    Here rather than buried in `detail` because it is the one thing the
    designer needs in order to build a verifier, and because writing it forces
    the question at planning time, when it is cheap. "Run pytest
    tests/test_loader.py" is a check; "make sure it works" is not.

    A step with no check is a step nobody will notice failing."""

    gate: bool = False
    """True when a PERSON must approve before the run continues past this step.

    Separate from `check` because they are different questions -- a check is
    something a machine can decide, a gate is something it must not. The
    designer turns this into a human node, and it is expensive: a gated step
    stops the run until somebody comes back to it. Reserve it for what is
    costly or irreversible -- launching a long training run, publishing a
    result, changing shared data."""


class PlannerOutput(StrictModel):
    """The plan, produced BEFORE any graph is designed.

    Splitting planning from designing is the point: the designer used to jump
    straight from a conversation to a topology, which meant nobody -- human or
    model -- ever wrote down what the work actually consists of. Now the steps
    come first, you approve them, and the graph is designed to carry them out.
    """

    summary: str
    """Two or three sentences on the approach, for the human reading it."""
    steps: list[PlanStep] = Field(min_length=1, max_length=6)
    """At most six: each top-level step becomes one node of the designed
    agent (its substeps are turns of that node), so this caps the agent at
    about a dozen nodes. Real plans of 12-13 flat steps made 30-node agents."""


class GraphProposal(StrictModel):
    """graph.json, as the designer emits it."""

    name: str
    description: str = ""
    entry: str
    nodes: list[NodeRef] = Field(min_length=1, max_length=48)
    edges: list[EdgeSpec] = Field(default_factory=list)
    branches: list[BranchSpec] = Field(default_factory=list, max_length=16)
    """Must match agentfolder.schema.GraphFile, which has the reasoning.
    A cap here that is tighter than the one on disk is a design the designer
    cannot express but a human could hand-write."""


class SkillProposal(StrictModel):
    mode: Literal["none", "auto", "selected"] = "none"
    skills: list[str] = Field(default_factory=list, max_length=20)


class NodeProposal(StrictModel):
    """One entry of nodes.json, plus the name it belongs to.

    A LIST of these rather than an object keyed by name, for one concrete
    reason: OpenAI's strict structured-output mode rejects open-ended
    dict[str, X]. The writer converts the list into the on-disk object, where
    an object is what you actually want to hand-edit.

    Both the agent-node fields and the human-node `commands` live here, all
    optional, because a discriminated union is another thing strict mode
    handles badly. The writer picks the right subset using `kind` from the
    graph, and the validator catches any mismatch.
    """

    name: str

    # ---- agent nodes ----
    backend: str = ""
    access: Literal["none", "read_only", "write"] = "read_only"

    steps: list[str] = Field(default_factory=list, max_length=20)
    """Which plan steps this node is responsible for, by id (e.g. ["2", "3.1"]).

    This is what keeps a node's context small. Its prompt receives ONLY these
    steps via {my_steps}, plus a one-line outline of the rest via
    {plan_outline} so it knows where it sits. Handing every node the entire
    plan is how you get one node trying to do the whole project."""

    instructions: str = ""
    output: list[FieldSpec] = Field(default_factory=list, max_length=12)
    prompts: PromptPair = Field(default_factory=lambda: PromptPair(first=""))
    thread_key: str = ""
    refresh_on: str = ""
    bump: list[str] = Field(default_factory=list, max_length=4)
    capture: list[Literal["git_status", "git_diff", "turn_changes"]] = Field(default_factory=list)
    record: str = ""
    announce: str = ""

    # ---- human nodes ----
    commands: list[CommandProposal] = Field(default_factory=list, max_length=12)

    # ---- an orchestrator (hub agents): the nodes it is wired to ----
    validator: str = ""
    paper: str = ""
    review: str = ""

    skills: SkillProposal | None = None
    """Which shared skills the node gets (agent/skills)."""


class DesignerOutput(StrictModel):
    """A complete agent folder, plus the brief that agent will start from."""

    task_brief: str
    """Self-contained. The generated agent never sees this conversation, so
    anything it needs to know must be written out in full here."""

    rationale: str = ""
    graph: GraphProposal
    nodes: list[NodeProposal] = Field(min_length=1, max_length=48)
    """One per name in `graph.nodes`, and the same cap, because a shorter
    list here is exactly the failure this cap once caused: 20 names and one
    entry."""



class ExtraField(StrictModel):
    """One more string a node must return, beyond its role's standard ones."""

    name: str
    description: str = ""


class NodeSpec(StrictModel):
    """One node, as the designer describes it -- a few lines, not a page.

    The prompts, the output fields and the branch field with its exact choices
    are written by code from this and from the graph (bootstrap/spec.py). That
    split is MEASURED: asked for ~30 complete node entries, the model sent one
    or none while saying it had sent them all; asked for ~30 of these, it
    sent all of them, correctly, first time.
    """

    name: str
    role: Literal["worker", "runner", "checker", "router", "human",
                  "orchestrator", "validator", "paper_writer"]
    """A hub agent's orchestrator, validator and paper_writer are fixed by
    code, one each; the designer gives them instructions only."""
    steps: list[str] = Field(default_factory=list, max_length=20)
    """A work node's own leaf step id -- fixed by the plan. Others own none."""
    tier: Literal["light", "standard"] = "standard"
    """Which model a work node runs on: "light" maps to the `coder_light` role,
    a cheaper model, for work that needs no judgement."""
    reads: list[str] = Field(default_factory=list, max_length=20)
    """Nodes whose outputs this node is shown -- its inputs and only those.
    Empty means the nodes that lead straight into it (through a checker or
    router to the work it judged)."""
    instructions: str = ""
    outputs: list[ExtraField] = Field(default_factory=list, max_length=6)
    commands: list[CommandProposal] = Field(default_factory=list, max_length=12)
    """Human nodes only: the slash commands it accepts and where each goes."""
    skill_mode: Literal["none", "auto", "selected"] = "none"
    """Which shared skills the node gets: none; auto (it reads what it needs
    from the list); selected (`skills`, in full). See agent/skills."""
    skills: list[str] = Field(default_factory=list, max_length=8)
    """For skill_mode selected: the skill names (folders in skills/)."""


class DesignerSpec(StrictModel):
    """What the designer returns: the whole graph, and every node as a spec."""

    task_brief: str
    rationale: str = ""
    plan_issues: list[str] = Field(default_factory=list, max_length=20)
    """Problems with the PLAN the designer cannot fix by design, because the
    work nodes are fixed to its steps: a step too big for one turn, a check
    nobody could apply. Shown at the review stop; the fix is /revise."""
    graph: GraphProposal
    nodes: list[NodeSpec] = Field(min_length=1, max_length=48)
