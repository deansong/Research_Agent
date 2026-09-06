# An agent that designs agents

A LangGraph learning project. You describe a job; it plans the steps, designs
an agent shaped for those steps, writes that agent to disk as JSON, and runs it.

The agent is **data, not code** — readable, diffable, hand-editable, reusable.

```
PHASE 1  discuss  →  plan  →  you approve  →  design  →  write  →  validate
PHASE 2  run the agent that was just designed
```

---

## Contents

- [Install](#install) · [Your first run](#your-first-run)
- [The five things you'll actually do](#the-five-things-youll-actually-do) —
  start · resume · reuse an agent · edit an agent · configure backends
- [Command reference](#command-reference) · [CLI reference](#cli-reference)
- [Where everything lives](#where-everything-lives)
- [How it works](#how-it-works) · [Four things that will bite you](#four-things-that-will-bite-you)
- [Troubleshooting](#troubleshooting)

---

## Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Only `codex` is implemented as a real provider. Sign in once:

```bash
python main.py login
```

## Your first run

**Try it with no API calls and no login at all.** The `fake` backend returns
canned answers instantly, including a real (small) plan and a real generated
agent, so you can watch the whole pipeline for free:

```bash
python main.py run /path/to/repo --backend fake
```

You'll be asked what you want, then:

```
[discussor] …asks you one question…
you> answer it
you> /plan                     ← you decide when discussion ends

[planner] working out the steps...
  1. Inspect the repository
       1.1 List the files
  2. Report back to the human
[planner] written to .agent/sessions/<name>/plan.json

you> /approve                  ← or edit plan.json first, or /revise

[designer] proposed agent 'x' with 2 nodes
[validator] the design is valid.

===== agent: x =====           ← phase 2: the agent it just designed
```

For real, swap `--backend fake` for nothing (codex is the default).

---

## The five things you'll actually do

### 1. Start a session

```bash
python main.py run . --task "Add pytest tests for parser.py"
```

**A session is one task.** You don't name it — the name is derived from the
task (`add-pytest-tests-for-a1b2c3`), so the same task resumes and a different
task starts somewhere clean. Without `--task` you're prompted.

```bash
python main.py sessions .          # list them
```

### 2. Resume a session

Everything is checkpointed after every completed step. Ctrl-C, close the
laptop, come back tomorrow:

```bash
python main.py run . --session add-pytest-tests-for-a1b2c3
```

It picks up at the exact question it stopped on, with the conversation, the
plan and the agent intact. Re-running the **same `--task`** resolves to the
same session, so that works too.

- `/exit` **finishes** a session. Ctrl-C just **stops** — the graph stays
  parked and resuming continues from there.
- Resuming an unfinished session skips everything already done. It re-runs
  only the node that didn't finish.

### 3. Reuse an agent you liked

A designed agent lives in `.agent/sessions/<session>/agent/`. Keep it:

```bash
python main.py promote . my-reviewer --session <session>
```

That copies it to `.agent/agents/my-reviewer/`, which is **not** gitignored —
commit it. Then skip the whole design phase on any future task:

```bash
python main.py run . --pre-build-agent my-reviewer --task "review the parser"
```

`--pre-build-agent` accepts a promoted name, a path, or a built-in
(`default` is the original four-role discuss→plan→orchestrate→execute agent).

### 4. Edit an agent by hand

This is the reason it's a folder. Open `.agent/agents/my-reviewer/nodes.json`,
change a prompt, add a command, rewire an edge in `graph.json`. Rerun — it
takes effect, no code involved.

Break it and you get told exactly what and where, before a token is spent:

```
  [error: unreachable] node 'executor': nothing leads to it from 'discussor'.
  [error: end_unreachable] graph.json: no path reaches "__end__".
```

See **`docs/AGENT_FOLDER.md`** for every key in the format.

You can also edit **`plan.json`** during the approval gate — `/approve`
re-reads it from disk, so your edits are what gets built.

### 5. Configure which model does what

Every node names a **role**, and every role is configurable:

```bash
python main.py run . --backend fake                        # everything
python main.py run . --backend-role executor=codex:gpt-5.4 # one role
python main.py run . --explain                             # show, spend nothing
```

Or persistently, in `<repo>/.agent/config.json` (per repo) or
`~/.codex-langgraph-agent/config.json` (global) — see `agent.example.json`:

```json
{
  "default": { "provider": "codex" },
  "roles": {
    "executor": { "provider": "codex", "model": "gpt-5.4",
                  "options": { "timeout": 3600 } }
  }
}
```

Precedence, later wins: built-in defaults → global file → repo file →
`--config` → environment (`AGENT_BACKEND`, `AGENT_MODEL`,
`AGENT_BACKEND_EXECUTOR`, …) → CLI flags.

A **generated** agent invents its own role names, and they work identically:
if the designer creates a `reviewer` node, `--backend-role reviewer=codex`
configures it. Each node also declares the repository access it needs, checked
against the backend **before anything is constructed** — so putting a
write-node on a backend with no filesystem access fails immediately, naming
the role and the fix.

| provider | status |
| --- | --- |
| `codex` | **implemented** |
| `fake` | **implemented** — canned answers, for learning and testing |
| `claude_code`, `api`, `antigravity` | stubs; each fails at startup with the steps to finish it. Full design in the module docstring |

---

## Command reference

`/help` lists what's valid **right now** — the list changes with context, and
inside a generated agent it's built from that agent's own JSON.

| always | |
| --- | --- |
| `/help` | commands available right now |
| `/graph` | draw the agent's shape (mermaid) |
| `/state [full]` | where the graph is and what it holds |
| `/transcript` | the conversation so far |
| `/config` | which backend and model each role uses |
| `/usage` | tokens and cache hit rate |
| `/exit` | end the session |

| while discussing | |
| --- | --- |
| `/plan [guidance]` | stop discussing, work out the steps |

| at the plan gate | |
| --- | --- |
| `/approve` | accept the plan (re-reads `plan.json`, so hand edits win) |
| `/revise <what>` | send it back to the planner |
| `/discuss` | go back to talking it over |

| if designing fails | |
| --- | --- |
| `/retry [notes]` · `/use` · `/discuss` | try again · fall back to the built-in agent · talk |

Anything not starting with `/` is an answer for the agent. Start with `//` if
you need an answer that begins with a slash.

## CLI reference

```
python main.py login                          sign in to Codex
python main.py run <repo> [options]           the main command
python main.py sessions <repo>                list sessions
python main.py promote <repo> <name>          keep this session's agent
```

`run` options:

| | |
| --- | --- |
| `--task "…"` / `--task-file F` | the task, instead of being prompted |
| `--session NAME` | resume a specific session |
| `--pre-build-agent NAME\|PATH` | skip designing; run this agent |
| `--backend NAME` | provider for every role |
| `--model NAME` | model for every role |
| `--backend-role ROLE=PROV[:MODEL]` | override one role (repeatable) |
| `--config PATH` | config file (replaces the default two) |
| `--explain` | print config + agent shape and exit, spending nothing |

---

## Where everything lives

```
<repo>/.agent/
    .gitignore              committed; keeps sessions/ out of git
    config.json             per-repo backend config
    sessions/<session>/     gitignored
        request.txt         what you asked for — identifies the session
        plan.json           the numbered steps — EDIT ME before approving
        brief.md            the designer's rewrite of it, for the agent
        agent/              the agent designed for this session
        artifacts/          where a run puts what it PRODUCES
        checkpoint.sqlite   both phases, two thread ids
    agents/<name>/          promoted, committed, reusable
```

**Project changes** (source, its tests, its docs) go in the repository, as
normal. **Run output** (generated data, caches, reports, plots, logs) goes in
the session's `artifacts/`. The loader appends that rule to every write-access
node itself, so a generated agent can't opt out.

---

## How it works

### Phase 1 — designing (hand-written, fixed)

```
  START → discussor → human ──┬── plain text ──→ discussor
                              ├── /plan ───────→ planner
                              └── /exit ───────→ END
  planner → human ────────────┬── /approve ────→ designer
                              ├── /revise ─────→ planner
                              └── /discuss ────→ discussor
  designer → writer → validator ──┬── valid ────→ END
                                  ├── problems ─→ designer   (repair loop)
                                  └── gave up ──→ human
```

`writer` and `validator` call **no model**. The designer already produced the
folder as data, so `writer` is `json.dump` — which makes "no `exec`, no
`eval`, no generated Python" structural rather than a promise.

**Step-scoped context.** The designer assigns plan step ids to nodes. A node's
prompt receives only *its* steps in full (`{my_steps}`), plus one line per
step for orientation (`{plan_outline}`). Giving every node the whole brief is
how one node ends up trying to do the entire project.

### Phase 2 — the agent that was designed

Whatever shape phase 1 produced. `/graph` draws it. The two phases run
**sequentially, not nested** — phase 1 finishes and is discarded, so the
generated agent is an ordinary top-level graph with its own thread, and
`get_state`, `stream` and time travel all work on it normally.

### The seven concepts, and where to see each

| concept | see it here |
| --- | --- |
| **StateGraph** | `agent/bootstrap/graph.py` (hand-written) · `agent/work/compile.py` (built from JSON) |
| **node** | `agent/bootstrap/nodes/discussor.py` — state in, partial update out |
| **conditional edge** | `agent/bootstrap/graph.py::_route_after_validator` |
| **state schema** | `agent/bootstrap/state.py` and `agent/work/state.py` — built two different ways, deliberately |
| **reducer** | `agent/work/state.py`, and `agent/statelib.py` for the rule |
| **checkpointer** | `agent/cli.py::main` |
| **interrupt / resume** | `agent/work/templates/human_node.py` + `agent/terminal.py::drive` |

Reading order: `bootstrap/graph.py` (the map) → `work/state.py` →
`work/templates/agent_node.py` → `work/compile.py` → `cli.py`.

---

## Four things that will bite you

Measured against this stack (`langgraph 1.2.11`), not assumed.

**1. Reducers only work on top-level state keys.** An
`Annotated[list, operator.add]` nested inside a sub-`TypedDict` is silently
overwritten. The rule (`agent/statelib.py`): *hand-merge when your code knows
the keys; use a reducer when the keys come from data.*

**2. A node re-runs from its first line after every resume.** Anything above
`interrupt()` executes again and must be side-effect free.

**3. Two graphs on one `thread_id` silently corrupt each other.** No error —
their channels merge. The two phases use `<session>:bootstrap` and
`<session>:work`.

**4. `compile()` catches less than you'd hope.** Not unreachable nodes, not an
unreachable `END`, not dead ends, not the node names `""` or `"A-B"`. That gap
is why `agent/agentfolder/validate.py` exists.

---

## Troubleshooting

**"Out of credits"** — the run waits and retries rather than dying (20 × 60s by
default), so you can top up and it continues. Ctrl-C stops; your progress is
saved. Tune with `credit_wait_attempts` / `credit_wait_seconds` in a role's
`options`.

**A node times out** — default 600s per turn. Raise it:
`{"roles": {"<role>": {"options": {"timeout": 3600}}}}`. If it still times
out, the *design* is usually wrong: one node has been given a whole project.
Split it across more nodes — `/graph` shows you the shape you got.

**A run scattered files across my repo** — fixed; run output now goes to the
session's `artifacts/`. Older sessions predate this.

**Codex hangs with no output** — keep `developer_instructions` under ~6KB.
Above that a turn never completes (measured; it's size, not content). Bulky
reference material belongs in the prompt, which has no such limit.

**Where did my old sessions go?** Sessions used to live in
`~/.codex-langgraph-agent/`. They're now per-repo under `.agent/sessions/`.
Old files are left untouched.

---

## Tests

```bash
python tests/test_folder.py      # the format: one test per failure mode
python tests/test_graph.py       # the acceptance test
python tests/test_bootstrap.py   # planning, approval, the repair loop
python tests/test_backends.py    # provider contract and recovery
```

`test_graph.py` is the one that matters: it builds a graph **from
`agent/builtin_agents/default/`** and asserts the same things the original
hand-written agent had to satisfy. If it fails, the format is too weak — not
the test. None of them call a model or need a network.

## Further reading

- **`docs/AGENT_FOLDER.md`** — the format reference: every key, what the
  validator checks, how to work with folders.
- `docs/AUTO_GRAPH_DESIGN.md` — superseded, kept because its measurements
  explain why the two phases run sequentially.
