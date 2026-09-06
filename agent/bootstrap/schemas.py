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
    CommandSpec,
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
    instructions: str = ""
    output: list[FieldSpec] = Field(default_factory=list, max_length=12)
    prompts: PromptPair | None = None
    thread_key: str = ""
    refresh_on: str = ""
    bump: list[str] = Field(default_factory=list, max_length=4)
    capture: list[Literal["git_status", "git_diff"]] = Field(default_factory=list)
    record: str = ""
    announce: str = ""

    # ---- human nodes ----
    commands: list[CommandSpec] = Field(default_factory=list, max_length=12)


class DesignerOutput(StrictModel):
    """A complete agent folder, plus the brief that agent will start from."""

    task_brief: str
    """Self-contained. The generated agent never sees this conversation, so
    anything it needs to know must be written out in full here."""

    rationale: str = ""
    graph: GraphProposal
    nodes: list[NodeProposal] = Field(min_length=1, max_length=20)
