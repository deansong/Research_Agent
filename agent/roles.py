"""
WHAT:  The role names the HAND-WRITTEN graphs use.
WHY:   config.py and the bootstrap nodes both have to spell "designer" the
       same way.  One place to spell it.
CONCEPT: Not LangGraph -- just shared constants.  Kept in its own module so
       config.py and bootstrap/ can both import it without a cycle.

A note on scope, because this file used to do more.  Role names are now
OPEN-ENDED: a generated agent invents its own (`nodes.json` says
`"backend": "reviewer"`, and that string is the role).  So nothing here is a
whitelist, and there is no table of required access -- a node's access comes
from its own `access` field in `nodes.json`, collected by
`work.compile.backends_needed()` and handed to `build_backends()`.

What is left is exactly the roles that appear in Python source we wrote.
""" 

from __future__ import annotations

# The design phase's own roles. These always exist, whatever agent gets built:
# they belong to the hand-written bootstrap graph in agent/bootstrap/.
DISCUSSOR = "discussor"
PLANNER = "planner"
DESIGNER = "designer"

# Roles of the shipped `default` agent (agent/builtin_agents/default/nodes.json).
# Named here only so --explain can show them and so a typo gets a suggestion;
# the folder itself is the source of truth.
ORCHESTRATOR = "orchestrator"
EXECUTOR = "executor"

#: Every role that some file in this repository mentions by name.  Used for
#: typo suggestions and for the --explain listing -- NEVER as a whitelist.
#: The roles the RESEARCH SKELETON names, which config.py now gives defaults
#: to. Not built into any graph here -- a designed agent declares them -- but
#: named in Python, which is the line ALL_ROLES draws.
CODER = "coder"
RUNNER = "runner"
CHECKER = "checker"

ALL_ROLES: tuple[str, ...] = (DISCUSSOR, PLANNER, DESIGNER, ORCHESTRATOR,
                              EXECUTOR, CODER, RUNNER, CHECKER)
