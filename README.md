# An agent that designs agents — built to be read

A LangGraph learning project. It runs in two phases:

1. **Design.** Talk to it about what you want. When you type `/plan`, it designs
   an agent for that job and writes it to disk as JSON.
2. **Run.** That agent — a graph of nodes it just invented — runs and does the work.

The agent is **data, not code**. You can read it, diff it, hand-edit it, commit
it, and run it again on another repository.

---

## Five minutes

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# The whole thing, with no API calls and no login:
python main.py run /path/to/repo --backend fake

# For real:
python main.py login
python main.py run /path/to/repo --session my-project
```

`--backend fake` returns canned answers instantly, including a real (small)
generated agent — so you can watch design → write → validate → run end to end
for free. `--explain` prints the resolved config and the agent's shape and then
exits, spending nothing.

Skip the design phase and run the original four-role agent:

```bash
python main.py run /path/to/repo --pre-build-agent default --task "add tests"
```

---

## The two graphs

They run **one after the other**, not nested. Phase 1 finishes and is
discarded; the generated agent is then an ordinary top-level graph with its own
thread, so `get_state`, `stream` and time travel all work on it normally.

### Phase 1 — designing (hand-written, fixed)

```
  START → discussor → human ──┬── plain text ──→ discussor
                              ├── /plan ───────→ designer
                              └── /exit ───────→ END
       designer → writer → validator ──┬── valid ────→ END
                                       ├── problems ─→ designer   (repair loop)
                                       └── gave up ──→ human
```

`writer` and `validator` call **no model**. The designer already produced the
folder as data, so `writer` is `json.dump`. That is what makes "no `exec`, no
`eval`, no generated Python" a structural property rather than a promise.

### Phase 2 — the agent that was designed

Whatever shape phase 1 came up with. `/graph` draws it.

The shipped `default` agent is the original four-role loop:

```
  START → discussor → human ──┬── plain text → discussor
                              ├── /plan ─────→ planner → orchestrator
                              └── /exit ─────→ END
  orchestrator ──┬── execute → executor → orchestrator
                 ├── replan  → planner
                 └── finish  → human
```

**Notice what is missing: there is no arrow from `discussor` to `planner`.** The
model cannot decide that discussion is over. Only you can, by typing `/plan`. It
may *suggest* it, and that suggestion is printed and then ignored by the routing.

---

## The seven concepts, and where to see each

| concept | see it here |
| --- | --- |
| **StateGraph** | `agent/bootstrap/graph.py` (hand-written) and `agent/work/compile.py` (built from JSON) |
| **node** | `agent/bootstrap/nodes/discussor.py` — a plain function, state in, partial update out |
| **conditional edge** | `agent/bootstrap/graph.py::_route_after_validator` |
| **state schema** | `agent/bootstrap/state.py` and `agent/work/state.py` — deliberately built two different ways |
| **reducer** | `agent/work/state.py`, and `agent/statelib.py` for the rule |
| **checkpointer** | `agent/cli.py::main` |
| **interrupt / resume** | `agent/work/templates/human_node.py` + `agent/terminal.py::drive` |

Reading order: `bootstrap/graph.py` (the map) → `work/state.py` (what a
generated agent remembers) → `work/templates/agent_node.py` (how a node runs) →
`work/compile.py` (how JSON becomes a graph) → `cli.py` (how it is all wired).

A reader who only wants to understand *running* an agent needs five files:
`agentfolder/schema.py`, `work/state.py`, `work/compile.py`, and the two
templates.

---

## Four things that will bite you

Measured against this exact stack (`langgraph 1.2.11`), not assumed. Each one
costs an afternoon if you learn it the hard way.

### 1. Reducers only work on **top-level** state keys

```python
class Inner(TypedDict):
    items: Annotated[list, operator.add]     # ← silently does nothing
class State(TypedDict):
    inner: Inner                             # ← THIS is the channel
    top:   Annotated[list, operator.add]     # ← this one works
```

Only `State`'s own keys become channels. `inner` is one last-value-wins channel,
so returning `{"inner": {...}}` replaces the whole dict, annotation and all.

**The rule** (`agent/statelib.py`): *hand-merge when your code knows the keys;
use a reducer when the keys come from data.* `bootstrap/state.py` knows its keys
and uses `merge_section`. `work/state.py` cannot — node names come from JSON —
so every dict channel there gets `operator.or_`. Both are right.

### 2. A node re-runs from its first line after every resume

When you resume, LangGraph re-executes the interrupted node from the top; this
time its `interrupt()` call *returns* your answer instead of stopping. Anything
**above** `interrupt()` runs again on every resume and must be side-effect free.

### 3. Two graphs on one `thread_id` silently corrupt each other

Running two graphs with different schemas on the same thread does **not** raise.
Their channels merge, and one graph's keys turn up in the other's state. The two
phases use `<session>:bootstrap` and `<session>:work` for exactly this reason.

### 4. `compile()` catches less than you would hope

It does **not** catch unreachable nodes, a graph that can never reach `END`
(compiles fine, then `GraphRecursionError`), dead-end nodes, or the node names
`""` and `"A-B"`. That gap is the entire reason `agent/agentfolder/validate.py`
exists. It *does* catch duplicate node names, unknown edge targets, and reserved
names.

---

## Commands

`/help` lists what is valid **right now** — the list changes with context, and
inside a generated agent it is built from that agent's own JSON, so it can never
drift from what the agent accepts.

| | |
| --- | --- |
| `/help` | commands available right now |
| `/graph` | draw the agent's shape |
| `/state [full]` | where the graph is and what it holds |
| `/transcript` | the conversation so far |
| `/config` | which backend and model each role uses |
| `/usage` | tokens and cache hit rate |
| `/plan [guidance]` | **stop discussing, design the agent** |
| `/exit` | end the session |

Generated agents add their own. Anything not starting with `/` is an answer for
the agent; start with `//` if you need an answer that begins with a slash.

**`/exit` finishes a session. ctrl-D just stops** — the graph stays parked and
rerunning with the same `--session` picks up at the same question.

---

## Choosing a backend per node

Every node names a **role**, and every role is configurable:

```bash
python main.py run . --backend fake                        # everything
python main.py run . --backend-role executor=codex:gpt-5.4 # one role
python main.py run . --explain                             # show, spend nothing
```

Or in `~/.codex-langgraph-agent/config.json` (global) or `<repo>/.agent/config.json`
(per repo) — see `agent.example.json`. Precedence, later wins: defaults → global
file → repo file → `--config` → environment → CLI flags.

A **generated** agent invents its own role names, and they work the same way: if
the designer creates a `reviewer` node, `--backend-role reviewer=codex` configures
it. Each node also declares the repository access it needs, which is checked
against the backend **before anything is constructed** — so putting the executor
on a backend with no filesystem access fails immediately, naming the role and
the fix.

| provider | status |
| --- | --- |
| `codex` | **implemented** |
| `fake` | **implemented** — canned answers, for learning and testing |
| `claude_code`, `api`, `antigravity` | stubs. Each fails at startup with the exact steps to finish it; the full design is in the module docstring |

To add one, read `agent/backends/antigravity.py` — 60 lines, and it lists the
three methods you need. `agent/backends/codex.py` is the same file filled in.

---

## Where things live

```
<repo>/.agent/
    config.json             per-repo backend config
    sessions/<session>/     gitignored scratch
        checkpoint.sqlite   both phases, two thread ids
        brief.md            the task brief
        agent/              the agent designed for this session
    agents/<name>/          promoted, committed, reusable
```

```bash
python main.py promote . my-reviewer     # keep this session's agent
```

Then **edit the JSON by hand**. Change a prompt, add a command, rewire an edge —
no code involved. Break it and the validator tells you exactly what and where
before a token is spent. That editability is the main reason the agent is a
folder rather than something held in memory.

`.agent/` is in the executor's write sandbox, so the loader automatically
appends "never touch `.agent/`" to every write-access node's instructions —
enforced by our code, not trusted to the folder.

---

## Tests

```bash
python tests/test_folder.py      # the format: 13 tests, one per failure mode
python tests/test_graph.py       # the acceptance test (see below)
python tests/test_bootstrap.py   # designing, and the repair loop
```

`tests/test_graph.py` is the one that matters. It builds a graph **from
`builtin_agents/default/`** and asserts the same things the original
hand-written agent had to satisfy. If it ever fails, the format is too weak and
needs extending — not the test relaxing.

None of them call a model or need a network.

---

## Further reading

- `docs/AGENT_FOLDER.md` — the format reference: every key, what the validator
  checks, and how to work with folders.
- `docs/AUTO_GRAPH_DESIGN.md` — superseded. Kept because its measurements
  explain *why* the two graphs run sequentially instead of nested.
