"""
WHAT:  The agent-folder format -- reading, validating and rendering the JSON
       that describes an agent.
WHY:   An agent is data on disk, not code. This package is the only thing that
       understands that data. It knows nothing about LangGraph.
CONCEPT: Not LangGraph at all. Deliberately: you can unit-test every rule in
       here without building a graph, which is why step 1 of the build order
       needed no model and no StateGraph.

Read in this order:
    schema.py   what a folder may contain (Pydantic, extra="forbid")
    load.py     read it off disk
    outputs.py  turn a node's declared output fields into a Pydantic model
    render.py   fill {placeholders} in prompts
    validate.py everything Pydantic cannot check (graph shape, cross-refs)
    commands.py turn a human node's commands into slash-command rows
"""
