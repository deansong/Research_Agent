"""
WHAT:  The names of the agent's roles, and how much repo access each needs.
WHY:   Config, the backend factory and the nodes all have to agree on the
       string "executor".  One place to spell it.
CONCEPT: Not LangGraph -- just shared constants.  Kept in its own module so
       config.py and backends/ can both import it without a cycle.
"""

from __future__ import annotations

from agent.backends.base import Access

DISCUSSOR = "discussor"
PLANNER = "planner"
ORCHESTRATOR = "orchestrator"
EXECUTOR = "executor"

ALL_ROLES: tuple[str, ...] = (DISCUSSOR, PLANNER, ORCHESTRATOR, EXECUTOR)

REQUIRED_ACCESS: dict[str, Access] = {
    # The three thinking roles only ever look at the repository.
    DISCUSSOR: Access.READ_ONLY,
    PLANNER: Access.READ_ONLY,
    ORCHESTRATOR: Access.READ_ONLY,
    # The executor is the only role that changes your files.
    EXECUTOR: Access.WRITE,
}
