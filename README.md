# Codex + LangGraph coding agent

A refactored version of the four-role coding agent:

1. Discussor: multi-turn requirement discovery with the human.
2. Planner: creates/revises an implementation plan.
3. Orchestrator: routes between execution, replanning, human input, and finish.
4. Executor: persistent Codex coding worker per workstream.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Login

```bash
python main.py login
```

## Run

```bash
python main.py run /path/to/repo --session my-project
```

Optional model override:

```bash
export CODEX_MODEL="gpt-5.4"
```

## Design

- `main.py` is intentionally tiny.
- `agent/codex_backend.py` is the provider boundary. Replace or generalize this file to add Claude Code or Antigravity.
- `agent/schemas.py` uses Pydantic as the single source of truth for structured model outputs.
- Each node lives in its own file.
- LangGraph workflow state is separate from Codex conversation state.
- Codex thread IDs are persisted inside LangGraph state.
- Executors use one persistent Codex thread per workstream for context reuse and caching.
