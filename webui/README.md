# The web UI

A browser front end for the agent designer, alongside the terminal REPL. Both
drive the same sessions — the same folders, the same `checkpoint.sqlite`, the
same `graph.json` — so you can start in one and resume in the other.

```bash
pip install fastapi 'uvicorn[standard]'
python main.py web .                    # then open http://localhost:8420
python main.py web . --backend fake     # the whole flow, no API calls, no login
```

Start with `--backend fake`. It returns canned answers instantly, including a
real plan and a real generated agent, so you can watch every panel work for
free before spending anything.

---

## What to read, in what order

Six Python files and six JavaScript ones. If you read them in this order each
one only depends on what came before.

| | |
| --- | --- |
| **1. `runner.py`** | The interesting one. Read `agent/terminal.py::drive()` first, then this — they are the same loop. |
| **2. `events.py`** | What a session emits, and how a dropped connection catches up. |
| **3. `capture.py`** | How a `print()` inside a graph node reaches the browser. Honest about being a hack. |
| **4. `server.py`** | Routes. Deliberately the boring half. |
| **5. `editing.py`** | Reading and writing `plan.json` and the agent folder, and the three traps involved. |
| **6. `models.py`** | The request and response shapes. |
| **7. `static/js/api.js`** | Every call to the server, in one place. |
| **8. `static/js/app.js`** | Wires the panels together. The only file that knows they all exist. |
| **9. `static/js/chat.js`** | The left column — the only panel that can drive the agent. |
| **10. `static/js/graph.js`** | Cytoscape. |
| **11. `static/js/plan.js`** | The plan tree, and the step ↔ node link. |
| **12. `static/js/inspector.js`** / **`topology.js`** | The right column, and editing the graph's shape. |

---

## The screen

```
┌──────────────────────────────────────────────────────────────────────┐
│  agent designer      <session>  [phase]  agent: name    [New] [Open] │
├───────────────┬──────────────────────────────┬───────────────────────┤
│ Conversation  │ Graph │ Plan │ Wiring │ JSON │ Run │ Configure │ ... │
│               │                              │                       │
│  transcript   │        the agent, drawn      │   the selected node   │
│  node output  │        click to select       │   its parameters      │
│  ─────────    │                              │   and its real prompt │
│  the question │                              │                       │
│  /command     │                              │                       │
│  [ compose  ] │                              │                       │
└───────────────┴──────────────────────────────┴───────────────────────┘
```

Drag either divider to resize. The app switches tabs for you: clicking a node
jumps to Configure, and a save that broke something jumps to Problems.

**Conversation.** The transcript, plus everything the nodes print — which
command the executor ran, which file it touched, what a turn cost. When the
graph stops to ask you something the composer unlocks; the chips above it are
the slash commands legal *at that question*, computed on the server from the
folder's own registry.

**Graph.** The topology, drawn from the folder rather than from a compiled
graph — so it works before backends exist and before anything compiles, which
is exactly when you most want to look at an agent that will not run. Three edge
kinds, and the distinction matters:

| | |
| --- | --- |
| solid | an unconditional edge from `graph.json` |
| dashed | one arm of a branch, labelled with the value that selects it |
| dotted | a slash command on a human node — these live in `nodes.json`, so a picture drawn from `graph.json` alone shows every human node as a dead end |

A thicker edge is one that stops and asks you something.

**Plan.** `plan.json`, editable. Each step shows which nodes own it; clicking a
step lights those nodes up in the graph. That link is the whole point of the
`steps` field — see below.

**Wiring.** Add and delete nodes, rewire edges, retarget branch cases, change
the entry node.

**JSON.** `graph.json` and `nodes.json` as text, parsed as you type. The escape
hatch for anything the forms do not cover.

**Configure / Context.** The selected node's settings, and the prompt it will
actually receive.

---

## The Repository field

It is **the project the agent works on**, and it decides three things:

| | |
| --- | --- |
| where `.agent/` goes | sessions, `config.json`, and promoted agents all live under `<repo>/.agent/` |
| what the nodes are pointed at | it becomes `repo_path`, so `{repo_path}` and every "inspect the repository" instruction mean this directory |
| what a write node may change | a write-access node edits files here — everything it *produces* goes to the session's `artifacts/` instead |

The field is prefilled with the absolute path the server was started for, so
what is on screen is the real answer. A **relative** path is taken from that
same repo, not from the server's working directory — `python main.py web
../projectA` names the project, and the field must not quietly disagree. An
absolute path or `~/...` is honoured as typed, so one server can serve several
projects.

**It does not have to be a git repository**, despite the name. It is just the
directory the agent is allowed to look at and change; a plain folder works.
It also does not have to be *this* project — pointing it at the codebase you
actually want worked on is the normal case.

---

## Three things worth understanding

### 1. The Context tab is the point

What you write in `prompts.first` is a **template**. What the model reads is
that template with placeholders resolved from live state. Those are very
different strings, and the gap between them is where the surprises live —
because an unresolved placeholder renders as an **empty string** rather than
raising.

`{out.planner.workstram}` (note the typo) validates clean, saves clean, runs
clean, and quietly sends the model a prompt with a hole in it. The Context tab
is the only place that is visible.

It also shows the working rules the compiler appends to a write-access node.
Those are not in `nodes.json` at all — a generated agent is not allowed to opt
out of them, so they are added at compile time. Reading only the file would
misrepresent what runs.

### 2. Saving is allowed to break the agent; running is not

A folder saves even when validation fails, and the response lists the problems.
That is deliberate: you cannot rewire a graph without passing through states
where a node is briefly unreachable, and an editor that refuses those is an
editor you cannot use.

Running is what is gated. `agent/cli.py` already refuses a folder with blocking
problems and prints why, so a half-wired agent on disk is a clear message rather
than a crash.

The plan is gated the other way round — an invalid plan is refused. A plan is a
small tree with no cross-references, so there is no half-finished state to pass
through; anything that fails there is a mistake rather than a step on the way
somewhere.

### 3. `steps` is how each node's context is controlled

A node's `steps` list names the plan steps it is given **in full**
(`{my_steps}`). Everything else in the plan it sees as one line each
(`{plan_outline}`). That is the mechanism for not drowning every node in the
whole plan, and it is invisible in both JSON files — hence the highlighting
between the Plan panel and the graph.

Nothing in the agent checks that a node's step ids exist: `steps_for()` silently
drops one it does not recognise. The UI flags it (an amber node border) because
the UI is the only place it can be caught.

---

## How it works

### The loop

`agent/terminal.py::drive()` is:

```python
result = invoke(initial_state)
while there is an interrupt:
    answer = input()                      # blocks the whole program
    result = invoke(Command(resume=answer))
```

`runner.py::_pump()` is the same, one substitution different:

```python
result = invoke(initial_state)
while there is an interrupt:
    emit a "question" event
    answer = self._answers.get()          # blocks only this thread
    result = invoke(Command(resume=answer))
```

One worker thread per session; the browser holds an SSE stream open and POSTs
answers. That works because of a decision made long before any of this existed:
`agent/commands.py` insists the value passed to `Command(resume=...)` is a bare
string that the node re-parses, explicitly so the graph stays safe "when driven
by something that is not our terminal (a test, a future web UI)". The browser
POSTs the same text you would type. `/plan` and `/approve` need no special
handling at all.

**One run in flight per session.** A session owns one SQLite checkpoint and one
LangGraph thread id; two runs against those at once is corruption, and two
browser tabs on one URL is the normal case. Answering when nothing was asked is
a 409, not a queued reply that gets consumed at the *next* question.

### The event stream

SSE, not WebSockets: the traffic is one-way except answers, and an answer is an
ordinary POST. Every event carries a monotonic `seq` in the SSE `id:` field, so
a browser that drops its connection reconnects with `Last-Event-ID` and loses
nothing and repeats nothing. Seven event types, a closed list, in `events.py`.

### Getting node output into the browser

About twenty `print()` calls live inside graph nodes, plus the provider's
progress lines. None pass through the `GraphSession` seam, and they are the most
useful output there is.

`capture.py` replaces `sys.stdout` with a proxy that routes **by calling
thread**, so two sessions never see each other's output and an unregistered
thread falls through to the real stdout. Its docstring is honest about this
being the pragmatic choice over threading an `emit` callable through every node
signature, and about when to do it properly instead.

---

## The API

| method | path | |
| --- | --- | --- |
| `GET` | `/api/sessions` | list what is on disk |
| `POST` | `/api/sessions` | resolve or create one, and start it |
| `GET` | `/api/sessions/{id}` | phase, pending question, problems |
| `POST` | `/api/sessions/{id}/start` | run a session that is idle |
| `POST` | `/api/sessions/{id}/answer` | `{"text": "/plan"}` |
| `DELETE` | `/api/sessions/{id}` | close the runner; the folder is untouched |
| `GET` | `/api/sessions/{id}/events` | **SSE**; honours `Last-Event-ID` and `?after_seq=` |
| `GET`/`PUT` | `/api/sessions/{id}/plan` | `plan.json` |
| `GET`/`PUT` | `/api/sessions/{id}/agent` | `graph.json` + `nodes.json`, plus a drawable view |
| `POST` | `/api/sessions/{id}/agent/validate` | check an edit without writing it |
| `GET` | `/api/sessions/{id}/nodes/{name}/context` | the prompt that node will really get |
| `GET` | `/api/schema` | node kinds, field types, access levels, providers |
| `GET` | `/api/config` | the resolved backend for each role |
| `GET` | `/api/defaults` | the repo this server was started for, and its flags |

Errors use one shape, the same one validation uses:

```json
{"detail": {"code": "not_waiting", "where": "my-session", "message": "...", "warning": false}}
```

`/api/schema` exists so the browser never carries its own copy of the field
lists. Hand-maintaining two schemas is how the project this design borrows from
ended up with a TypeScript model and a Pydantic model a whole version apart.

---

## Things that will bite you

**Editing while a run is in flight** is refused with a 409. Stop the run first.

**A shipped or promoted agent is not editable** through the UI —
`--pre-build-agent default` is read-only, because it is not this session's own
folder. Design one, or copy the folder into the session.

**`from` is a reserved word in Python**, so `EdgeSpec` calls the field `from_`
with `alias="from"`. Anything that dumps a graph without `by_alias=True` writes
`from_` into `graph.json`, which the loader then rejects — the agent you just
saved will not load. `test_editing.py` round-trips the shipped agent byte for
byte to catch exactly this.

**Every folder model is `extra="forbid"`.** A stray key the browser
round-trips is a hard error, not an ignored field.

**Duplicate keys in `nodes.json` collapse silently** during parsing, so two
`worker` entries lose one without a word. That is why saving writes to
`agent.tmp/`, loads it *back from disk*, validates that, and only then renames.

**No node/npm.** There is no build step by design: plain ES modules, normal
multi-line CSS, libraries vendored into `static/vendor/`. See that folder's
README before updating one.

---

## Tests

```bash
python -m pytest webui/tests -q
```

| | |
| --- | --- |
| `test_api.py` | a whole session over HTTP — discuss, `/plan`, `/approve`, design, run, `/exit` |
| `test_stream.py` | SSE over a real socket: headers, framing, and reconnect-with-replay |
| `test_editing.py` | round-trips, refusals, and building a three-node agent through the states in between |
| `test_static.py` | parses the JS with tree-sitter, and checks every element id, import, CSS variable and vendored global actually exists |

`test_static.py` is there because there is no build step and no browser here: a
stray bracket in `app.js` would otherwise produce a blank page and an error only
visible in a console nobody has open. Every check in it was verified to fail
when the fault is injected.

None of them call a model or need a network.
