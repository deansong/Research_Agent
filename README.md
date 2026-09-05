# A LangGraph coding agent, built to be read

A small four-role coding agent — and a worked example of the LangGraph concepts you
need to build your own. Every concept below points at an exact file and function, so
the explanation and the code cannot drift apart.

```
discussor  →  works out WHAT you want, one question at a time
planner    →  turns the discussion into a technical plan
orchestrator → decides what happens next after every step
executor   →  actually edits your repository
```

---

## Run it in five minutes

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Try the whole graph with NO API calls and no login at all:
python main.py run /path/to/repo --backend fake

# For real:
python main.py login
python main.py run /path/to/repo --session my-project
```

`--backend fake` returns canned answers instantly. It is the best way to learn the
control flow — every command, the interrupts, resuming a session — without spending
anything. Also try `--explain`, which prints the resolved configuration and the graph
topology and then exits.

---

## The graph

```
  START
    |
    v
discussor -----------> human <--------------------------+
(asks one question,      |                              |
 keeps a running         |  you type...                 |
 requirements brief)     |                              |
                         |                              |
      +--------+---------+---------+---------+          |
      |        |         |         |         |          |
  plain text  /plan   /replan  /discuss    /exit        |
      |        |         |         |         |          |
      v        v         v         +---------+          |
  discussor  planner  planner                          END
                |                                       ^
                v                                       |
          orchestrator <--------------------+           |
                |                           |           |
      +---------+---------+---------+       |           |
      |         |         |         |       |           |
   execute   replan   ask_human  finish     |           |
      |         |         |         |       |           |
      v         v         v         v       |           |
  executor   planner    human     human ----+-----------+
      |
      +--> orchestrator
```

**Notice what is missing: there is no arrow from `discussor` to `planner`.** The
discussor cannot decide that discussion is over. Only you can, by typing `/plan`. The
discussor may *suggest* it ("I think we have enough to plan — type /plan when you are
ready"), and that suggestion is printed and then ignored by the routing.

---

## The seven concepts, and where to see each one

| concept | what it is | see it here |
|---|---|---|
| **StateGraph** | the builder you add nodes and edges to | `agent/graph.py` → `build_graph` |
| **node** | a plain function: state in, partial update out | `agent/nodes/discussor.py` → `make_discussor` |
| **conditional edge** | a function that returns *which* node runs next | `agent/graph.py` → `_route_after_human` |
| **state schema** | a `TypedDict` whose top-level keys are "channels" | `agent/state.py` → `AgentState` |
| **reducer** | how a channel merges updates instead of replacing | `agent/state.py` → `transcript` |
| **checkpointer** | saves state after every step, so you can resume | `agent/cli.py` → `main` |
| **interrupt / resume** | pause for a human, continue with `Command(resume=…)` | `agent/nodes/human.py` + `agent/terminal.py` → `drive_graph` |

A useful reading order: `graph.py` (the map) → `state.py` (what is remembered) →
`nodes/human.py` (how a pause works) → `nodes/discussor.py` (how a node calls a model)
→ `cli.py` (how it is all wired up).

---

## Three things that will bite you

These are not opinions. Each was measured against this exact stack (`langgraph 1.2.11`),
and each costs an afternoon if you learn it the hard way.

### 1. Reducers only work on **top-level** keys

```python
class Inner(TypedDict):
    items: Annotated[list, operator.add]     # ← silently does nothing

class State(TypedDict):
    inner: Inner                             # ← THIS is the channel
    top:   Annotated[list, operator.add]     # ← this one works
```

Only `State`'s own keys become channels. `inner` is one last-value-wins channel, so a
node returning `{"inner": {...}}` replaces the whole dict, reducer annotation and all.

This is the entire reason `agent/state.py` has a `merge_section()` helper, and the reason
`transcript` is a top-level key rather than living inside `discussion`.

### 2. A node re-runs from its first line after every resume

When you resume an interrupted graph, LangGraph re-executes the interrupted node from the
top; this time its `interrupt()` call *returns* your answer instead of stopping.

So everything **above** `interrupt()` runs again on every resume, and must be free of side
effects — no model calls, no writes, no counters. Everything **below** it runs once. See
the comment block in `agent/nodes/human.py`.

### 3. Never pass `configurable` to a nested `.invoke()`

If you ever call one compiled graph from inside another graph's node, pass
`config={"recursion_limit": N}` and **no `configurable` key**. LangGraph detects nesting
by looking for a parent task id in the config; an explicit `configurable` severs that
link, and the child's `interrupt()` is silently swallowed instead of reaching you.
Explained in full in `docs/AUTO_GRAPH_DESIGN.md`.

---

## Commands

Everything you type either starts with `/` (a command) or is an answer for the agent.
`/help` lists what is valid *right now* — the list changes with context, so `/replan`
only appears once there is a plan to replace.

| | |
|---|---|
| `/help` | commands available right now |
| `/state [full]` | where the graph is and what it is holding |
| `/transcript [all]` | the discussion so far |
| `/config` | which backend and model each role uses |
| `/usage` | tokens and cache hit rate per role |
| `/plan [guidance]` | **stop discussing, start planning** |
| `/replan [reason]` | throw out the plan and make a new one |
| `/discuss` | go back to talking it through |
| `/new <request>` | abandon this task, start another in the same repo |
| `/exit` | end the session (it is saved; `--session` picks it up) |

`/help`, `/state`, `/transcript`, `/config` and `/usage` are answered by the terminal and
never wake the graph. The rest are forwarded into it and interpreted by
`agent/nodes/human.py`. Both sides call the same parser, `agent/commands.py::parse` —
adding a command means one row in `REGISTRY` plus one handler.

Need an answer that genuinely starts with a slash? Type `//` — `//etc/hosts` reaches the
agent as `/etc/hosts`.

---

## Choosing a backend per role

Each of the four roles can use a different provider and model.

```bash
python main.py run ./repo --backend fake                        # everything, no API calls
python main.py run ./repo --backend-role executor=codex:gpt-5.4 # just the executor
python main.py run ./repo --explain                             # show config, spend nothing
```

Or in a file — `~/.codex-langgraph-agent/config.json` (global) or `<repo>/.agent/config.json`
(per repository). See `agent.example.json`.

```json
{
  "default": { "provider": "codex" },
  "roles": { "executor": { "provider": "codex", "model": "gpt-5.4" } }
}
```

Full precedence, later wins: built-in defaults → global file → repo file → `--config` →
environment (`AGENT_BACKEND`, `AGENT_MODEL`, `AGENT_BACKEND_EXECUTOR`, …) → CLI flags.

### Which providers exist

| provider | status |
|---|---|
| `codex` | **implemented** |
| `fake` | **implemented** — canned answers, for learning and testing |
| `claude_code` | stub. The full design is at the top of `agent/backends/claude_code.py` |
| `api` | stub. The full design is at the top of `agent/backends/api.py` |
| `antigravity` | stub. `agent/backends/antigravity.py` |

The stubs are real classes and legal config values: they fail at **startup** with the
exact steps needed to finish them, rather than crashing three nodes into a task.

### Adding a provider

Read `agent/backends/antigravity.py`. It is 60 lines and it is the whole job:

1. start or resume a conversation and get back an id;
2. send a prompt and read the response back, matching a JSON Schema;
3. report token usage.

Fill in `run_structured()`, set the three class attributes, add one line to `PROVIDERS`
in `agent/backends/__init__.py`. `agent/backends/codex.py` is the same file with the
bodies filled in — read them side by side.

Roles declare how much repository access they need in `agent/roles.py`, and that is
checked against `backend.max_access` **before anything is constructed**. So configuring
the executor onto a backend with no filesystem access fails immediately, with a message
naming the role and the fix.

---

## Where state lives, and what is *not* in it

LangGraph remembers the **workflow**; the provider remembers the **conversation**. All we
keep is the id that lets us pick a conversation back up (`ProviderState` in
`agent/state.py`). That is why checkpoints stay small, and why one long-lived thread per
role gives you a high cache hit rate — watch the `cache=` figure in `/usage`.

Checkpoints live in `~/.codex-langgraph-agent/<hash-of-repo-path>-v2.sqlite`: one file per
repository, one thread per `--session` name. The `-v2` is the state schema version; when
the schema changes incompatibly the filename changes and your old sessions are left alone
rather than half-loaded.

---

## What's next

`docs/AUTO_GRAPH_DESIGN.md` — a design (not yet built) for letting the planner *design a
graph* for the job at hand, which a new `build` node then compiles and runs. It covers why
a compiled LangGraph cannot be modified, why the generated graph must be data rather than
generated Python, and the measured LangGraph behaviours that make running a child graph
inside a node actually work.
