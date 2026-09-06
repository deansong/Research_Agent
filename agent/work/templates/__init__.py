"""
WHAT:  The registry of node kinds a folder may use.
WHY:   `kind` in graph.json selects one of these. Adding a kind means writing
       a template here and adding one line -- and nothing else in the system
       needs to know.
CONCEPT: This is what makes "the planner never writes Python" true. The
       planner picks a kind and configures it; the code that runs is always
       ours.
"""

from agent.work.templates.agent_node import make_agent_node
from agent.work.templates.human_node import make_human_node

KINDS = ("agent", "human")

__all__ = ["KINDS", "make_agent_node", "make_human_node"]
