"""
WHAT:  The seam that lets one terminal loop drive two different graphs.
WHY:   There are now two graphs with two state schemas -- the bootstrap graph
       that designs an agent, and the generated agent itself. The REPL should
       not know or care which one it is talking to.
CONCEPT: Not LangGraph. A small adapter, so terminal.py keeps all the I/O and
       none of the schema knowledge.

terminal.py used to reach into state directly (values["human"]["question"],
values["task_cycle"], ...). Each of those became one callable here, and each
phase supplies its own. See agent/work/session.py for a worked example.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from agent.commands import SlashCommand


@dataclass(frozen=True)
class GraphSession:
    """Everything terminal.drive() needs to run one graph to completion."""

    title: str
    """Shown when the run starts, e.g. "designing an agent" or "agent: default"."""

    graph: Any
    """A compiled LangGraph."""

    config: dict[str, Any]
    """LangGraph run config: configurable.thread_id and recursion_limit.

    NOTE the two phases MUST use different thread_ids. Measured: running two
    graphs with different schemas on the SAME thread does not raise -- their
    channels silently merge, and one graph's keys turn up in the other's state.
    """

    registry: tuple[SlashCommand, ...]
    """Which slash commands exist in this phase. The bootstrap graph and each
    generated agent have their own."""

    initial_state: dict[str, Any]
    """Input for a fresh run. Ignored when resuming an interrupted one."""

    resume_payload: Callable[[dict], dict | None]
    """values -> the interrupt payload to re-show when picking up a session
    that was interrupted in a previous process, or None to just continue."""

    state_report: Callable[[dict, bool], str]
    """(values, full) -> what /state prints."""

    transcript: Callable[[dict], list[dict]]
    """values -> [{role, text}] for /transcript."""

    usage: Callable[[dict], dict]
    """values -> {key: usage dict} for /usage."""
