from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DiscussorOutput(StrictModel):
    ready: bool
    question: str = ""
    requirements: str = ""


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
