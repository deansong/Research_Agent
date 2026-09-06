"""
WHAT:  Running an agent that was described by a folder.
WHY:   Separated from `agentfolder` (which only understands the JSON) and from
       `bootstrap` (which produces it). This package is the only one that
       touches LangGraph for a generated agent.
CONCEPT: StateGraph built at runtime from data.

Read in this order:  state.py -> templates/agent_node.py -> compile.py
"""
