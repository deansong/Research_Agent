# Research Agent (v3)

A research agent rebuilt on `research_backend` (RB): one way to talk to
Codex, Claude Code and Antigravity -- models, usage limits, sessions you can
continue, effort, JSON schemas and skills.

| folder | what |
| --- | --- |
| `research_agent/` | the agent: the bootstrap and execution stages -- see [its README](research_agent/README.md) |
| `research_backend/` | the backend package; see [its README](research_backend/README.md) |
| `skills/` | shared skills (`<name>/SKILL.md`) any model call can be given |
| `tests/` | `python -m pytest -q -p no:cacheprovider tests` |

**What V3 changes.** Scientific discovery is not passing a rubric. A step is
judged on whether its worker did the work *as it was told* and its results
are *real*; what it measured is a *finding*, reported against an
*expectation* (the study's prediction), never a bar it must clear. When a
route reaches a dead end, the study takes another: a step's planned
alternative at once, or -- with none left -- a new route for the rest of the
study, on your word. V2's workarounds for getting steps past impossible
checks (the start record, receipts, the label regexes) are gone.

The agent has two stages so far. The bootstrap turns an idea into an approved
research design. The execution stage carries that design out.

```
pip install langgraph langgraph-checkpoint-sqlite jsonschema openai-codex
python -m research_agent bootstrap "your research idea"
python -m research_agent execute --run ~/research_runs/<run>
python -m research_agent web          # the web UI: http://127.0.0.1:8421 (pip install fastapi 'uvicorn[standard]')
```
