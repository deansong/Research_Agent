> **SUPERSEDED — kept for its measurements, not its design.**
>
> This document proposed running the generated graph as a *child* invoked
> inside a parent node. That is not what was built. The shipped design runs the
> two graphs **sequentially** — the bootstrap graph designs an agent, finishes,
> and is discarded; the generated agent then runs as an ordinary top-level
> graph. That deletes almost everything sections 5, 6 and 8 below wrestle with.
>
> **Read `README.md` and `docs/AGENT_FOLDER.md` for what actually exists.**
>
> What is still worth reading here: the measured LangGraph behaviours in
> sections 5 and 11. They are accurate, and they are precisely *why* the
> sequential design is better — every one of them is a hazard the shipped
> design does not have to think about. One correction: section 4 says
> `compile()` does not catch duplicate node names. On langgraph 1.2.11 it does
> raise; the real gaps are the node names `""` and `"A-B"`, which it accepts.

---

# Design: letting the agent build its own graph

**Status: design only. No code for this exists yet — that is deliberate, so you can
read and change the design first.**

Everything described here sits *behind* a new `/plan graph` command. The graph you have
today (`discussor → human → planner → orchestrator → executor`) is untouched and stays
the default.

---

## 1. What we are trying to build

Today the workflow is fixed: one planner, one orchestrator, one executor, looping. That
shape is right for "implement this feature", but it is wrong for plenty of other jobs —
"audit these 12 modules for the same bug" wants a fan-out, "migrate this API" wants a
per-endpoint pipeline, "review this PR" wants several independent reviewers and then a
merge step.

The idea: during planning, the agent produces **a plan for a graph**, and a new node
**builds and runs that graph**.

```
  TODAY                              WITH AUTO GRAPH BUILDING

  human                              human
    |                                  |
  discussor                          discussor
    |                                  |
  planner   ("what to build")        planner  -- mode="direct" --> orchestrator/executor
    |                                     \
  orchestrator <-> executor                `-- mode="graph" ---> build
    (one fixed loop)                                               |
                                                            compiles + runs
                                                            a graph the planner
                                                            just designed
```

---

## 2. The constraint that shapes everything

**A compiled LangGraph is immutable.** `add_node` lives on `StateGraph` (the builder);
`compile()` returns a `CompiledStateGraph` that has no way to gain a node. Verified
against `langgraph 1.2.11`.

So "the agent builds its own graph" can never mean *mutating the graph that is currently
running*. It has to mean:

> the running graph contains a node that **compiles a second graph and runs it**.

Two levels, then:

| | what it is | when it is built | who wrote it |
|---|---|---|---|
| **Meta-graph** | `discussor → human → planner → build` | at startup, once | you, by hand |
| **Child graph** | whatever the job needs | at runtime, per task | the planner, as *data* |

The child graph is **data, not code**. That is the second big decision, and section 3 is
about why.

---

## 3. The graph spec: a declarative IR, never generated Python

The tempting version of this feature is "ask the model to write a Python file that
builds a StateGraph, then `exec` it". Don't. It means arbitrary code execution driven by
model output, it cannot be checkpointed, and every failure is a syntax error at midnight.

Instead the planner fills in a **schema**. Its `kind` field selects one of a small,
fixed set of node templates *we* implement.

`agent/dyngraph/spec.py`:

```python
NODE_NAME = r"^[a-z][a-z0-9_]{0,31}$"   # narrow on purpose: ':' is illegal in
                                        # LangGraph node names, and reserved
                                        # names like __start__ must not appear

class SpecModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

class NodeSpec(SpecModel):
    name: str = Field(pattern=NODE_NAME)
    kind: Literal["llm_step", "human_step", "summarize"]
    title: str = ""             # printed while the node runs
    instructions: str = ""      # developer instructions (llm_step)
    prompt: str = ""            # template, fixed placeholder vocabulary (llm_step)
    question: str = ""          # human_step
    access: Literal["read_only", "write"] = "read_only"
    role: str = "child_worker"  # which configured backend to use

class CaseSpec(SpecModel):
    when: str
    target: str

class BranchSpec(SpecModel):
    source: str
    key: Literal["last_status"]         # the only switchable field in v1
    cases: list[CaseSpec] = Field(max_length=8)
    default: str

class EdgeSpec(SpecModel):
    source: str
    target: str                          # a node name, or "__end__"

class GraphSpec(SpecModel):
    version: Literal[1] = 1
    title: str
    goal: str
    entry: str
    nodes: list[NodeSpec] = Field(min_length=1, max_length=20)
    edges: list[EdgeSpec] = Field(default_factory=list)
    branches: list[BranchSpec] = Field(default_factory=list, max_length=8)
```

Four decisions worth defending:

**No codegen, no `exec`, no `eval`.** `kind` picks a hand-written template. Branch
conditions come from `Literal["last_status"]` plus string equality, evaluated by our
code. The only free-form strings the model produces are prompts and questions — text
that was always going to be free-form anyway.

**`cases` is a list of pairs, not a dict.** An open-ended `dict[str, str]` is rejected by
OpenAI's strict JSON-schema mode. A list of `{when, target}` objects works on every
provider.

**Prompt placeholders are a fixed four-token vocabulary** — `{goal}`, `{repo_path}`,
`{previous}`, `{human_answers}` — rendered by an explicit `_render()` doing four
`str.replace` calls. **Do not use `str.format`.** Model-written prompts routinely contain
a stray `{` or an embedded JSON example, and `str.format` raises `KeyError` on those. Any
unknown `{token}` is caught by the validator instead.

**The spec is stored in state, typed.** `BuildState.spec` must be annotated
`GraphSpec | None`, *not* `dict[str, Any]`. Under `LANGGRAPH_STRICT_MSGPACK=true` (which
`main.py` sets), `compile()` walks the state type annotations to build a serde allowlist.
A `GraphSpec` reachable from the annotations round-trips through `SqliteSaver` and comes
back as the real class — verified. Typed as `dict`, it would serialize fine and then be
**blocked on load**.

> **Consequence: `agent/dyngraph/spec.py` is a permanent module path.** The module path is
> baked into the checkpoint payload. Moving or renaming `GraphSpec` later strands every
> saved session containing one. This belongs in the file's docstring.

---

## 4. Validation, and the repair loop

LangGraph's own `compile()` catches less than you would hope. Measured:

| problem | does `compile()` catch it? |
|---|---|
| edge to an unknown node | yes — `ValueError` |
| conditional branch to an unknown target | yes |
| no entrypoint from `START` | yes |
| `__start__` / `__end__` / `:` in a node name | yes |
| **unreachable node** | **no** — compiles happily |
| **`END` can never be reached** | **no** — compiles, then `GraphRecursionError` at runtime |
| **duplicate node names** | **no** — last one silently wins |
| **node with no outgoing edge** | **no** — silently behaves as `END` |

The bottom four are exactly the mistakes a model makes. So we validate ourselves.

`agent/dyngraph/validate.py`:

```python
@dataclass(frozen=True)
class SpecProblem:
    code: str       # stable, machine-readable
    where: str      # which node / edge / branch
    message: str    # a sentence the planner can act on

def validate_spec(spec, *, allow_human, write_mode, backends) -> list[SpecProblem]
def format_problems(problems) -> str
```

Checks: `duplicate_name`, `reserved_name`, `entry_missing`, `unknown_source`,
`unknown_target`, `conflicting_outgoing` (a node has an edge *or* a branch, never both),
`dead_end`, `unreachable` (BFS from entry), `end_unreachable` (BFS must reach `__end__`),
`missing_field`, `bad_placeholder`, `human_not_allowed`, `write_not_allowed`,
`access_unsupported` (the role's configured backend cannot provide the requested access —
reuses `roles.REQUIRED_ACCESS` and `backend.max_access` from phase 3).

**All checks run and all problems are reported together**, so one repair round can fix
everything rather than playing whack-a-mole.

### The repair loop

The `build` node **never calls a model**. The planner owns all generation; `build` only
validates, compiles and runs. That keeps the expensive thing in one place.

```
planner (mode="graph")
    emits build.spec, build.attempt += 1
        |
        v
     build
        |
        +-- spec valid? ------------------> compile + run   (section 5)
        |
        +-- attempt >= max_repair_attempts?
        |       control.action = "ask_human"
        |       human.purpose  = "graph_failed"
        |       "I could not produce a valid graph after N tries.
        |        /replan, /plan direct, or /new?"
        |       (the validation problems go in human.context)
        |
        +-- otherwise
                planning.feedback = format_problems(problems)
                control.action = "repair"   ---> routes back to "planner"
```

A second failure feeds the same counter: if the planner's backend raises
`BackendOutputError` (the model emitted JSON that is not a valid `GraphSpec`), the
**planner** catches it, bumps `build.attempt`, puts the pydantic errors in
`planning.feedback`, and returns with no spec. `build` sees `spec is None` and takes the
same branch. One counter, two failure sources, one message format.

`build.attempt` resets on `/plan` and `/new`.

> LangGraph note: `planner → build → planner` is a cycle inside a single `invoke()` with
> no interrupt between passes, so it costs `2 × max_attempts` super-steps against
> `recursion_limit`. At the default 1000 this is nothing — but leave a comment so nobody
> later "tidies" the limit down to 10.

---

## 5. Compiling and running the child

Two ways to attach a child graph. I measured both.

### Option A — register the child as a real LangGraph subgraph

`builder.add_node("generated", compiled_child)`. This is the textbook approach and it
buys real things: `get_state(subgraphs=True)` introspection, `stream(subgraphs=True)`
per-node events, `get_graph(xray=1)` drawing, time travel into the child.

**It cannot work for a spec discovered at runtime** (section 2: compiled graphs are
immutable). The only version that works is a two-phase lifecycle: at process start, read
the checkpoint, and *if* a spec is already saved there, build the parent **with** the
subgraph node before compiling. Which means a spec produced during this run cannot be
attached until you restart. In a project whose whole purpose is being easy to follow,
that is a bad trade.

### Option B — the `build` node compiles and `.invoke()`s the child inline ✅ recommended

```python
child = compile_spec(spec, backends=backends, repo_path=state["repo_path"])
result = child.invoke(child_input, config={"recursion_limit": cfg.child_recursion_limit})
```

Everything we need was measured working:

- the child **inherits the parent's checkpointer** (compile it with `checkpointer=None`);
- `interrupt()` inside a child node **propagates all the way out** to the top-level
  `graph.invoke()` return value;
- `graph.get_state(cfg).next` correctly reports the parent's `build` node as pending;
- a top-level `Command(resume=...)` resumes the child **exactly where it stopped**;
- child nodes that already completed are **not re-run**;
- re-entering `build` in a later step produces a fresh child run, no stale-resume hazard.

The one real cost: `get_state(subgraphs=True)` does **not** list an inline child as a
task, so there is no built-in introspection or time travel into it. Mitigated by a
`/graph` command that ASCII-renders the spec — which is more useful anyway, because you
want to see the plan *before* it runs.

### The rule that will cost you an afternoon if you miss it

```python
# child.invoke() is called with NO "configurable" key in its config, on purpose.
#
# Measured: passing one makes the child a TOP-LEVEL run. It then swallows
# interrupt() and returns {"__interrupt__": [...]} as ordinary data while this
# node completes normally -- so human steps silently stop working.
#
# LangGraph decides nesting by looking for a parent task id in config[CONF].
# An explicit "configurable" severs that link. Passing only
# {"recursion_limit": N} preserves nesting AND gives the child its own budget.
```

Measured, all four combinations:

| child checkpointer | config passed to `child.invoke()` | interrupt reaches you? |
|---|---|---|
| `None` (inherit) | none | ✅ yes |
| `None` (inherit) | `{"configurable": {...}}` | ❌ swallowed |
| own saver | none | ✅ yes |
| own saver | `{"configurable": {...}}` | ❌ swallowed |

And one more, equally important:

```python
# Do NOT wrap child.invoke() in a bare `except Exception`. GraphInterrupt is
# an exception, and it MUST propagate for human steps to work. If you add a
# broad handler, re-raise GraphBubbleUp first. (LangGraph's own tool_node.py
# carries the same warning.)
```

---

## 6. The child's state — where reducers finally earn their keep

`agent/dyngraph/childstate.py`:

```python
class ChildNote(TypedDict):
    node: str
    kind: str
    text: str

class ChildState(TypedDict, total=False):
    # ---- inputs: written once by build.py, never updated ------------------
    goal: str
    repo_path: str

    # ---- reducers ---------------------------------------------------------
    # Annotated[T, fn] means: when a node returns this key, do not overwrite --
    # call fn(old, new). Nodes therefore return only their NEW items.
    notes:         Annotated[list[ChildNote], operator.add]
    human_answers: Annotated[list[str],       operator.add]
    threads:       Annotated[dict[str, str],  operator.or_]   # dict merge

    # ---- no reducer: last write wins --------------------------------------
    # If two nodes wrote this in the SAME super-step LangGraph raises
    # InvalidUpdateError. That is why parallel nodes append to `notes` instead.
    last_status: str
    summary: str
    write_approval: str          # "ask" | "always"  (section 7)
```

Two things this teaches that the parent graph cannot:

`operator.or_` on `threads` is a **non-list reducer**, and it solves provider-thread
continuity for child nodes: a template reads `state["threads"].get(node.name)` and returns
`{"threads": {node.name: run.thread_id}}`. Because it lives in the child's own checkpoint
it survives interrupts *within* a child run, which is all that matters — a generated graph
is one-shot.

And the child is where a **fan-out** becomes possible: several nodes running in the same
super-step can all append to `notes` safely, whereas they could not all write `summary`.

---

## 7. Write access and the permission mode

You asked for generated graphs to be able to edit files, with a Claude-Code-style choice
between confirming and running free. Design:

```python
# agent/config.py
generated_graph_writes: Literal["off", "ask", "auto"] = "ask"
```

| value | meaning |
|---|---|
| `off` | the validator rejects any node with `access: "write"` |
| `ask` | write nodes are allowed, and each one **pauses for your confirmation first** |
| `auto` | write nodes run without stopping |

**How `ask` works.** It is not a separate node kind — that would let the planner design a
graph that "forgets" to ask. It is enforced by the *template*, which the planner cannot
see or modify:

```python
def make_llm_step(node: NodeSpec, ...):
    def step(state: ChildState):
        # Enforced by us, not by the spec. A generated graph cannot opt out.
        if node.access == "write" and write_mode == "ask" \
                and state.get("write_approval") != "always":
            answer = interrupt({
                "type": "human_input",
                "purpose": "generated_write",
                "question": f"Step '{node.name}' wants to edit files. Allow?",
                "context": f"{node.title}\n\nIt will be asked to:\n{rendered_prompt}",
            })
            # /yes -> once, /always -> rest of this run, /no -> skip the step
            ...
```

Answers map onto the existing command registry — `/yes`, `/no`, `/always`, `/exit` — with
`contexts=("generated_write",)`, so they only appear in `/help` at that prompt. **No
change to `terminal.py` is needed**: it already renders any interrupt payload with a
`question` and `context`, and already routes commands by `purpose`.

`/always` writes `write_approval="always"` into child state, so it lasts for the rest of
*that* generated run and no longer — a new `/plan graph` starts asking again.

`/exit` inside a generated graph raises `AbortGeneratedGraph`, caught in `build.py`, which
sets `control.terminate`. An exception rather than a state flag, because when the child is
interrupted it never returns a value for the parent to inspect.

**Ordinary human steps** (`kind: "human_step"`, the graph asking you a question because
the *job* needs an answer) are separate from write confirmations, gated by
`allow_generated_human_steps` (default on), and capped by the validator at 5 per graph so
the model cannot design an interrogation.

---

## 8. Checkpointing the child

- Compile the child with **`checkpointer=None`** and invoke with **no `configurable`**: it
  inherits the parent's `SqliteSaver`. One database, one `thread_id`, no new plumbing.
- The child's checkpoints are written under `checkpoint_ns = "build:<task-uuid>"`
  (measured shape: `'p:e5193e17-…'` for a parent node named `p`).
- Re-entering `build` later gets a new task id, hence a new namespace, hence a clean run —
  so `/plan graph` twice in one session needs no cleanup code.
- `/state deep` can enumerate child checkpoints directly, since they share the file:
  `checkpointer.list({"configurable": {"thread_id": session}})` filtered on
  `checkpoint_ns.startswith("build:")`.
- After a successful run, `build` copies `result["notes"]` and `result["summary"]` into
  the parent's `build.notes` / `build.result_summary`, so `/state` and the transcript can
  show what happened without anyone reaching into child checkpoints.

---

## 9. How this coexists with the graph you have

- `planning.mode: Literal["direct", "graph"]`, **default `"direct"` — today's behaviour,
  unchanged.**
- `/plan` uses the configured default; `/plan graph` and `/plan direct` override it for
  this task cycle. The human node writes `planning.mode`.
- **One planner node, not two.** It has two prompts and two output models
  (`PlannerOutput` vs `GraphPlannerOutput{spec, rationale}`), and a single new router
  `_route_after_planner` keyed on `planning.mode`. Keeping the fork in one place is the
  whole point.
- The two paths never interleave within a task cycle.
- After `build` finishes you land on `purpose="graph_done"` and can `/new`, `/discuss`, or
  **`/plan direct`** — which re-plans the same requirements down the classic
  orchestrator/executor path, with the whole transcript still intact. That fallback is the
  best argument for building this behind a mode flag rather than as a replacement.

New topology:

```python
builder.add_node("build", make_build(backends, cfg))

builder.add_conditional_edges("planner", _route_after_planner,
    {"orchestrator": "orchestrator", "build": "build"})     # was a plain edge
builder.add_conditional_edges("build", _route_after_build,
    {"planner": "planner", "human": "human", "end": END})   # the repair loop
builder.add_conditional_edges("human", _route_after_human,
    {..., "build": "build"})                                # resume after a pause
```

---

## 10. What actually changes in the existing code

| file | change |
|---|---|
| `agent/dyngraph/` | **new package**: `spec.py`, `validate.py`, `templates.py`, `compile.py`, `childstate.py`, `render.py` |
| `agent/nodes/build.py` | **new node**: validate → repair-or-compile → inline invoke |
| `agent/state.py` | add `build: BuildState` (with `spec: GraphSpec | None`) and `planning.mode` |
| `agent/graph.py` | add the `build` node and two conditional edges |
| `agent/nodes/planner.py` | branch on `planning.mode`; emit a `GraphSpec` in graph mode |
| `agent/schemas.py` | add `GraphPlannerOutput` |
| `agent/prompts.py` | add `GRAPH_PLANNER_INSTRUCTIONS` |
| `agent/commands.py` | `/plan` gains a `[direct\|graph]` argument; add `/graph`, `/yes`, `/no`, `/always` |
| `agent/config.py` | add `planner_mode`, `generated_graph_writes`, `allow_generated_human_steps`, `max_graph_repair_attempts`, `child_recursion_limit` |
| `agent/roles.py` | add `GRAPH_PLANNER` and `CHILD_WORKER` roles |
| `agent/terminal.py` | **nothing** — it already renders arbitrary interrupt payloads |

Adding `build` and `planning.mode` is purely **additive** to the state schema — new
channels absent from an old checkpoint just start empty — so this does **not** need
another database version bump. Phase 2's `-v2.sqlite` remains the only break.

---

## 11. The three things that will bite you

Repeated here because they are measured, not guessed, and each one costs an afternoon:

1. **Reducers only work on top-level state keys.** `Annotated[list, operator.add]` nested
   inside a sub-`TypedDict` is silently overwritten. (This is why `agent/state.py` has
   `merge_section` at all, and why `transcript` is top-level.)
2. **A node body re-runs from line 1 on every interrupt resume.** Anything before
   `interrupt()` — or before `child.invoke()` in `build` — must be side-effect free. In
   particular: print your banner from the child's entry node, not from `build`, or you
   will see it again after every human step.
3. **Never pass `configurable` to a nested `.invoke()`.** Section 5.

---

## 12. Suggested build order

Each step is independently testable, and steps 1–3 need **zero API calls** — write a
hand-made `GraphSpec` in a test and drive it with `--backend fake`.

1. `spec.py` + `validate.py` + unit tests for every problem code. No graph involvement.
2. `childstate.py` + `templates.py` + `compile.py`: compile a hand-written spec and run it
   standalone. Prove the reducers accumulate.
3. `nodes/build.py` with the repair loop, still fed hand-written specs. Prove interrupts
   propagate and resume works.
4. Planner graph mode + `GraphPlannerOutput` + `/plan graph`. Only now does a real model
   get involved.
5. `render.py` + `/graph`, and the `ask` permission flow.

---

## 13. Deliberately out of scope for v1

- **`shell_step`.** The one node kind that would let a model choose an executed command.
  A v2 addition, gated behind a command allowlist *and* a mandatory preceding
  confirmation.
- **Nested generated graphs.** A generated graph that generates another one. No reason to
  allow it, and the namespacing gets genuinely confusing.
- **Parallel fan-out in generated graphs.** `ChildState` is already built for it (that is
  what the `notes` reducer is for), but the validator should reject multiple outgoing
  edges from one node in v1 until sequential graphs are known to work.
- **Migrating v1 checkpoints.** Still not worth it.
