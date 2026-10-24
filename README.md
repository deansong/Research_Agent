# Research Agent (v2)

A research agent rebuilt on `research_backend` (RB): one way to talk to
Codex, Claude Code and Antigravity -- models, usage limits, sessions you can
continue, effort, JSON schemas and skills.

| folder | what |
| --- | --- |
| `research_agent/` | the agent; so far the bootstrap stage -- see [its README](research_agent/README.md) |
| `research_backend/` | the backend package; see [its README](research_backend/README.md) |
| `skills/` | shared skills (`<name>/SKILL.md`) any model call can be given |
| `tests/` | `python -m pytest -q -p no:cacheprovider tests` |

The agent is being rebuilt on top, starting with the bootstrap stage:
turning an idea into an approved research design.

```
pip install langgraph langgraph-checkpoint-sqlite jsonschema openai-codex
python -m research_agent bootstrap "your research idea"
```
