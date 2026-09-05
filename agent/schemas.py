"""
WHAT:  Pydantic models describing exactly what we want each role to return.
WHY:   Single source of truth for structured output.  The JSON Schema handed
       to the provider and the parsing on the way back both come from the
       same class, so they cannot disagree.
CONCEPT: Not LangGraph -- this is the model-output contract.  Nodes turn these
       objects into state updates.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    """Base class that forbids unexpected fields.

    extra="forbid" becomes "additionalProperties": false in the JSON Schema,
    which is what makes providers' strict structured-output modes work, and it
    turns a hallucinated field into a loud error instead of silent data loss.
    """

    model_config = ConfigDict(extra="forbid")


class DiscussorOutput(StrictModel):
    """What the discussor says after each turn of requirement discovery.

    NOTE what is NOT here: there is no `ready` flag that moves the graph on.
    The discussor used to set one and the graph routed on it, which meant the
    MODEL decided when discussion was over.  Now the human decides by typing
    /plan, and the discussor may only offer an opinion via `advice`.
    """

    reply: str
    """What to say to the human this turn.  Recorded in the transcript, so the
    planner later sees the discussor's reasoning and not just its questions."""

    question: str = ""
    """At most one question.  Empty if the discussor has nothing to ask."""

    requirements: str = ""
    """The running requirements brief, rewritten in full each turn."""

    advice: Literal["keep_discussing", "ready_to_plan"] = "keep_discussing"
    """Advisory only.  Printed as a hint; never routed on."""

    advice_reason: str = ""


class PlannerOutput(StrictModel):
    plan: str
    workstream: str
    next_task: str


class OrchestratorOutput(StrictModel):
    action: Literal["execute", "replan", "ask_human", "finish"]
    task: str = ""
    workstream: str = ""
    feedback: str = ""
    question: str = ""
    final_summary: str = ""


class ExecutorOutput(StrictModel):
    status: Literal["done", "blocked"]
    summary: str
    files_changed: list[str] = Field(default_factory=list)
    tests_run: list[str] = Field(default_factory=list)
    test_result: str = ""
    blockers: list[str] = Field(default_factory=list)
    suggested_next_step: str = ""
