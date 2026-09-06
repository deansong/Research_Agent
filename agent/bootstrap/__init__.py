"""
WHAT:  The bootstrap graph -- the agent that designs an agent.
WHY:   Phase 1 of a run. It discusses the job with you, designs a graph for it,
       writes that graph to disk, and validates it. Then it is DISCARDED and
       the generated agent runs as an ordinary top-level graph.
CONCEPT: Two graphs in sequence, not nested.

Why sequential matters: nesting one graph inside another node means interrupts
get swallowed unless you avoid passing `configurable`, checkpoint namespaces
get confusing, get_state(subgraphs=True) cannot see the child, and the parent
node re-runs from line 1 on every resume. Running them one after the other
avoids every one of those. The generated agent gets its own thread, and
get_state / stream / get_state_history all work on it normally.

    START -> discussor -> human
    human:  text -> discussor    /plan -> designer    /exit -> END (aborted)
    designer -> writer -> validator
    validator:  ok -> END (ready)   retry -> designer   give_up -> human
    human at design_failed:  /retry  /discuss  /use  /exit

Read graph.py first; it is the map.
"""
