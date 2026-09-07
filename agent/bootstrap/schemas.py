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


class DiscussorOutput(StrictModel):
    """Unchanged in spirit from the original agent: it advises, it never routes."""

    reply: str
    question: str = ""
    requirements: str = ""
    advice: Literal["keep_discussing", "ready_to_plan"] = "keep_discussing"
    advice_reason: str = ""


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
    steps: list[PlanStep] = Field(min_length=1, max_length=20)


class GraphProposal(StrictModel):
    """graph.json, as the designer emits it."""

    name: str
    description: str = ""
    entry: str
    nodes: list[NodeRef] = Field(min_length=1, max_length=20)
    edges: list[EdgeSpec] = Field(default_factory=list)
    branches: list[BranchSpec] = Field(default_factory=list, max_length=8)


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
    capture: list[Literal["git_status", "git_diff"]] = Field(default_factory=list)
    record: str = ""
    announce: str = ""

    # ---- human nodes ----
    commands: list[CommandProposal] = Field(default_factory=list, max_length=12)


class DesignerOutput(StrictModel):
    """A complete agent folder, plus the brief that agent will start from."""

    task_brief: str
    """Self-contained. The generated agent never sees this conversation, so
    anything it needs to know must be written out in full here."""

    rationale: str = ""
    graph: GraphProposal
    nodes: list[NodeProposal] = Field(min_length=1, max_length=20)
