"""
WHAT:  Re-exports the five node factories so graph.py has one import.
WHY:   Each node lives in its own file; this keeps graph.py readable.
CONCEPT: Nodes. Four of these are FACTORIES -- make_x(backend) returns the
       actual node function -- because LangGraph calls a node with only the
       state, so a backend has to be captured in a closure.
       human_input is a plain function: it takes no backend, because a human
       checkpoint must never call a model.
"""

from .discussor import make_discussor
from .executor import make_executor
from .human import human_input
from .orchestrator import make_orchestrator
from .planner import make_planner

__all__ = [
    "make_discussor",
    "make_executor",
    "human_input",
    "make_orchestrator",
    "make_planner",
]
