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

## Reaching it from another machine: the SSH bridge

The server binds `127.0.0.1` by default, and that default is load-bearing:
**there is no authentication.** Anyone who can reach the port can start a
session, and a session runs shell commands with write access to the repository
as you. So the way to use it from a laptop is to forward the port over SSH
rather than to open it up.

On the machine that will run the browser:

```bash
ssh -N -L 8420:localhost:8420 you@the-run-host
```

Leave that running, and open **http://localhost:8420** on the laptop. Nothing
about the server changes; `localhost` in the `-L` argument is resolved *on the
run host*, which is exactly why the loopback bind still works.

- `-N` — do not run a remote command, just forward. Drop it if you want a
  shell in the same window.
- `-L <local port>:localhost:<remote port>` — the two ports need not match.
  If 8420 is taken on your laptop, `-L 9000:localhost:8420` and open
  `http://localhost:9000`.
- Add `-J jump-host` if you reach the run host through a bastion.

Worth putting in `~/.ssh/config` on the laptop, so it is one word:

```
Host agent-ui
    HostName the-run-host
    User you
    LocalForward 8420 localhost:8420
    ServerAliveInterval 30
    ServerAliveCountMax 3
```

then `ssh -N agent-ui`.

**`ServerAliveInterval` matters more here than usual.** The UI holds an SSE
stream open for the life of the session, so a tunnel that dies silently looks
like the agent going quiet — which is the one thing this UI is built to tell
you apart from a long turn. Fifteen minutes into a design turn you cannot tell
a dead tunnel from a model that is thinking. With keepalives on, SSH drops the
connection promptly, `EventSource` reconnects by itself once the tunnel is
back, and `Last-Event-ID` replays the gap from the ring buffer — up to its
last 5,000 events, so a very chatty outage can still lose the oldest of
them.

**Run the server so it outlives the tunnel.** Your SSH session and the server
are separate things, and it is worth keeping them that way — a dropped
connection should not kill a design turn twenty minutes in:

```bash
tmux new -s agentui           # on the run host
python main.py web .          # inside it; C-b d to detach
```

Check it from the run host without a browser:

```bash
curl -s localhost:8420/api/health     # {"ok":true,"open_sessions":[...]}
```

### `--host 0.0.0.0`, and what it costs

`python main.py web . --host 0.0.0.0` binds every interface, and there is no
token, no password and no CSRF protection in front of it. On a shared machine
that hands everyone who can route to the port the ability to run commands as
you, in your repository. Use the tunnel unless you have put something in front
of the server yourself.

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

**Pause and Stop** are in the topbar, and they are two different things
because there are two honest answers:

| | |
| --- | --- |
| **Pause** | Stops after the current node finishes. Loses nothing — the graph checkpoints there anyway — so **Start** carries on from the same question. Can take as long as the node in flight. |
| **Stop** | Interrupts the provider mid-turn. Immediate, at the cost of that one node's work: a node is atomic, so there is no half-finished result to keep. Everything earlier is still checkpointed. |

Both are disabled unless a run is actually in flight. Stop reaches all the way
to the provider's own wait loop, which is the only thing that can end a turn
already running — without that, pressing it during a ten-minute executor step
would look like a broken button.

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

**Configure / Context / Activity.** The selected node's settings, the prompt it
will actually receive, and what the provider *did* — every command with its
exit code and output, every file it touched, and its own reasoning. That last
one exists because the terminal only ever said "313 events, last one 7s ago",
which is true and useless; see `agent/activity.py`.

---

## The "still working" line

A long turn prints one of these every thirty seconds:

```
    ... working: 1016s · 412 command · 98 reasoning · quiet 0s
      last: re-running the failing test with -x to see the first failure
```

**One row, rewritten in place** — not one row per heartbeat. Forty of them
appended is how a twenty-minute turn pushes the conversation off the screen
while still telling you nothing about what it is doing.

A long answer produces **no events at all** — the model is writing one
document, not running commands — so the line shows what it is writing instead:

```
    ... working: 861s · writing 16.6k · quiet 3s
      last: message: {"task_brief": "Compare hotel-employee skill associations
```

That text comes from the `delta` field on the provider's streaming
notifications, accumulated into a bounded tail per stream (message, reasoning,
plan, command output). It was being discarded for a while, which is how a turn
that wrote 16,608 tokens of a finished design could report `5 events,
last: * user message` and then be killed for going quiet.

### Where the complete text is

Three different views, and it is worth knowing which answers which question:

| | shows | how much |
| --- | --- | --- |
| the heartbeat line | what it is writing right now | last ~120 chars |
| `show detail` on that line | the live streams + the turn's events | last 8 KB per stream, newest 120 events |
| **Configure ▸ a node ▸ Activity** | every recorded turn, expandable | **the whole message**, up to 200 KB |

So: complete text, yes — in the node inspector's Activity tab, once the turn
has ended. The heartbeat and its expander are deliberately tails, because
while a turn is running there is no end yet.

Two things used to prevent this and no longer do. A message shared the 4 KB
clip with command output, so a 66 KB answer arrived with its middle replaced
by `...[N characters omitted]...`; messages now have their own 200 KB cap and
command output keeps the small one, because a pytest run really is unbounded
and a model's answer is not. And the *final structured answer* was rendered as
`(final answer)` and nothing else — skipped as a duplicate of the normal
return path, which is true for the designer, whose answer becomes `graph.json`
two tabs away, and false for every other node, whose answer becomes state you
cannot read anywhere. It is now shown, pretty-printed, with its size in the
label.

`show detail` on that row expands it into the turn's real events: each command
with its exit code, each file touched, the model's own reasoning. That is
fetched from `/api/sessions/{id}/activity` when you ask for it, and **not**
streamed — one reported turn held 14,885 events in seventeen minutes, and
pushing those down the event stream would evict the conversation from the
ring buffer to show you something nobody had asked to see.

The detail exists mid-turn because `agent/activity.py` writes a `current.json`
as the turn runs (`write_in_flight`), rewritten whole each time so a reader
never catches half a record. Before that, a turn's record was written only
when it *ended* — so during the twenty minutes you actually wanted to look,
there was nothing on disk to look at.

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

## The task field

**It is the opening line of a conversation, not a specification.** The
discussor's first prompt is:

> The human wants this done: *&lt;your task&gt;*. Begin discovery. Ask exactly one
> high-value question about either the work itself or the shape of agent that
> should do it. The human ends the discussion by typing `/plan`; you only advise.

So one sentence is enough. You then discuss — it asks a question, you answer,
repeat — and **you** decide when to stop by typing `/plan`. The LLM never makes
that call; that is enforced by the graph's topology, not by asking it nicely.

It is required for a second reason: `session_name_for()` derives the session's
folder name from it, so an empty task means there is nowhere to put the session.
The name is slug + hash, so two tasks that start with the same words still get
separate sessions.

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
| `POST` | `/api/sessions/{id}/pause` | stop after the current node; resumable |
| `POST` | `/api/sessions/{id}/stop` | stop now, abandoning the turn in flight |
| `DELETE` | `/api/sessions/{id}` | close the runner; the folder is untouched |
| `GET` | `/api/sessions/{id}/events` | **SSE**; honours `Last-Event-ID` and `?after_seq=` |
| `GET`/`PUT` | `/api/sessions/{id}/plan` | `plan.json` |
| `GET`/`PUT` | `/api/sessions/{id}/agent` | `graph.json` + `nodes.json`, plus a drawable view |
| `POST` | `/api/sessions/{id}/agent/validate` | check an edit without writing it |
| `GET` | `/api/sessions/{id}/nodes/{name}/context` | the prompt that node will really get |
| `GET` | `/api/sessions/{id}/nodes/{name}/activity` | every provider event for that node's turns |
| `GET` | `/api/sessions/{id}/activity` | the turn running *right now*, whichever node it is |
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
