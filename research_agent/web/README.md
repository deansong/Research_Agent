# research_agent/web: the web UI

Every run in your runs folder, in one page. The page shows:
- the conversation with the discussor;
- the research state, live;
- the graph, the files and the state's history;
- the model calls and jobs.

It looks and works like v1's UI (FastAPI, Server-Sent Events, plain JS modules,
cytoscape), rebuilt for v2's bootstrap and execution stages.

```
pip install fastapi 'uvicorn[standard]'
python -m research_agent web                      # http://127.0.0.1:8421, runs in ~/research_runs
python -m research_agent web --runs DIR --port 8421
```

The page has **no authentication**. It listens on 127.0.0.1. From another
machine, tunnel to it:

```
ssh -N -L 8421:localhost:8421 <host>
```

Then open http://localhost:8421.

## The page

```
+-----------------------------------------------------------------------------------------+
| research agent  <run>  [stage] [phase]  working: researcher (antigravity)  [Pause] [...] |
+------------------+-------------------------------------------------+--------------------+
| Conversation     | Research state | Graph | Files | History  [Open  | Run | Model calls  |
|                  |                                        in window]| | Jobs               |
| you / discussor  | Where things stand  (summary, next, approvals)  | stage, phase, now, |
| messages, model  | Goal  Constraints  Evidence  Hypotheses  Rubric | models per role    |
| calls and steps  | Protocol  Plan  Design  Execution  Decisions    | live model calls,  |
| as they happen   | Open questions  Risks  Reviews  Work done       | every session's    |
| [Approve design] |                                                 | turns; jobs + logs |
| [ always open  ] |                                                 |                    |
+------------------+-------------------------------------------------+--------------------+
```

### Setting a run up before it starts

**New run** has **Set up first** ticked: the run is created with your idea
(kept in `<run>/idea.txt`), and nothing calls a model until you press **Start
the run** over the composer. Meanwhile choose each role's model and backups
(Run tab), each node's skills and the researcher's literature sources (the
node windows, from the Graph tab), and Autopilot. Untick it to start at once.

A role's model changed while one of its calls waits (out of credits, a full
model) takes over at once: the wait ends and the run carries on with the new
model, by itself.

### The discussor is always there

The composer is never disabled. What a message does depends on the moment:

| when | your message |
| --- | --- |
| a new run | is the idea the run starts from |
| the team waits for you | is your answer, and the work goes on from it |
| the team is working | is an **aside**: the discussor answers on the side, in the same conversation, and the work goes on. What you ask for reaches the team at its next step: a request, a decision, and in execution a breakpoint or a stop |
| the run stopped midway (a pause, a failure) | is an aside too; **Carry on** resumes the run |

Asides and answers are one conversation, the discussor's research_backend
session. The chat history is read from that session, so a run driven from the
terminal shows its whole conversation here too.

### Autopilot: the run decides for you

Tick **Autopilot** in the top bar to let the run go on without you. Wherever
it would wait for your answer, the discussor answers as your stand-in: the
discussor's role and model, in a conversation of its own (`autopilot`),
which starts from the recent chat and sees the whole research state at every
decision.

- **What it decides:** approvals, the go for gated steps, retrying or passing
  a failed step, stopping. Its answer goes the same way yours would, shows in
  the chat as **autopilot, for you**, and is recorded in Decisions as the
  autopilot's, with its reason.
- **The whole study:** once the design is approved, it starts the execution
  itself.
- **Limits first:** ticking it asks for the execution's wall-clock and
  GPU-hour limits (both above 0), saved in `execute.json`. It can never
  raise them; when one is reached, it stops.
- **Hands back:** after N decisions in a row (20 by default; you choose) it
  stops answering and waits for you; your answer starts the count again.
- **Errors:** a run that stops with an error carries on by itself, and each
  restart counts as one of its decisions, so an error that keeps coming back
  ends at the limit, handed back to you.
- **Taking over:** untick it, and the run waits for you at its next stop.
  Asides and Pause work as always. A run already waiting goes on as soon as
  you tick it.

Settings: `<run>/autopilot.json`. API: `GET/PUT /api/runs/{id}/autopilot`
with `{on, max_decisions, wall_hours, gpu_hours}`.

### The research state has its own tab and window

The **Research state** tab draws every section of `state/research.json`, as
the team last wrote it, and redraws on every change:
- the graph's steps report their changes;
- a watcher catches the rest (asides, the CLI, hand edits).

What it shows:
- **Highlights:** sections that changed since you looked are marked in the
  list on the left and flash once.
- **Where things stand:** the orchestrator's summary and next step, and the
  approval of the hypotheses, rubric, protocol, plan and design. In the
  bootstrap, the design (all its parts at once) has an **Approve** button, live while the
  team waits for you.
- **Execution:** the step table, with each step's status, attempts, GPU-hours
  and verdict. Tick **before** or **after** to set a breakpoint, and press
  **Go** when a step waits for you. A step that failed can be moved on:
  **Another attempt** runs it again, past the attempt limit; **Pass anyway**
  passes it on your decision, and your reason is kept as its verdict, next
  to what the validator said. A skipped step has **Reopen**. **Rewind here**,
  on any step that has run, starts over from it: that step and every step
  that needs it go back to pending with fresh attempts, and their outputs
  move to `artifacts/_rewound/<time>-to-step-N/` (refused while one of them
  runs). You can also just tell the discussor ("rewind to step 2").
- **Reviews:** the reviewer's scores, requests (must, should, could), and its
  strengths, weaknesses and questions.

**Open in window** opens the same view at `/state/<run>`, in its own browser
window and live from the same event stream. Keep it next to the chat, or on
another screen.

### Each node's work, live, in its own window

Click a node in the **Graph** tab, or **live ↗** next to a call in **Model
calls**. The node opens in its own window at `/node/<run>/<node>`: every call
it makes, while it makes it. That is what you watch when you are debugging.

```
┌ discussor  codex · read_only   IDLE                         [x] follow  [⚙ Node] ┐
├─────────────────────────────┬──────────────────────────────────────────────────────┤
│ (all) asides  problems      │ ANSWERED discussor · Today 10:35 · 14.9s · 285k in   │
│ TODAY                       │ Summary  Input  Activity  Output  Raw                │
│ ✓ 10:35 14.9s 285k → orch.  │ YOU SAID   yes                                       │
│   you: yes                  │ IT ANSWERED                                          │
│   The team will prepare…    │   Reply     The team will prepare a small batch…     │
│ YESTERDAY                   │   Hands to  ORCHESTRATOR                             │
│ ✓ 22:35  8.8s 285k → user   │   Why       The user approved…                       │
│ ⚠ 21:55  9.9s RETRY 2       │ IT ALSO GAVE  decisions: 1   requests: 1             │
└─────────────────────────────┴──────────────────────────────────────────────────────┘
```

- **The calls**, on the left, newest first and grouped by day. Each is a line
  or three: whether it answered (✓) or its answer could not be used (⚠),
  the time, how long, the tokens it read, where it handed to (`→ user`,
  `→ run 4`, `→ pass`), what went in (your words, or its task) and what came
  out (its reply, or its reason). Asides and retries are marked. Filter to
  **asides** or **problems**; **j** / **k** move through the list.
- **The call under way** is pinned on top, with what it is writing now.
  **follow** (on by default) switches to each new call as it starts.
- **The chosen call**, in tabs:
  - **Summary:** what you said or what it was asked; what it answered, field
    by field (reply, hands to, action, verdict, why, task, ...); and what
    else it gave (decisions, requests, ...), each a link into Output.
  - **Input:** the exact message it was sent, split at its `=== SECTION ===`
    headers, with an index; long sections start folded.
  - **Activity:** each command it ran, with the end of its output; each
    tool, web search and file change; its reasoning.
  - **Output:** its answer as a tree, empty fields left out.
  - **Raw:** the settings it ran with, its answer as JSON, and its standing
    instructions.
  - For a call under way: **Live** (what it does, refreshed every second, and
    the text it is writing), **Input** and **Raw**.
- **⚙ Node** opens a drawer with the node's role, model, access, steps, its
  standing instructions and its part of the design, and its **skills**, each
  with its description, and **Change** to choose them:
  - none;
  - auto: the model sees every skill's name and description, and reads the
    ones it needs;
  - chosen skills, from the folder's list;
  - for an execution node, the design's skills again.

  Bootstrap nodes keep the choice in `config.json`. Execution nodes keep it in `execute.json` under `skills.nodes`, over
  the approved design. A change applies from the node's next call, and a
  conversation already under way, such as the discussor's, gets the new
  skills with its next message.
- **The researcher's literature sources.** In the researcher's window, the
  top bar shows them (`sources: openalex, s2`), and the drawer has a box for
  each: OpenAlex, Semantic Scholar, PubMed, Europe PMC and Google Scholar
  (through SerpAPI, a credit per search). The research skill's `search`
  uses the ticked ones, and a source left unticked is never asked, even by
  name. A source whose key is not set in the server's environment
  (`SEMANTIC_SCHOLAR_API_KEY`, `SERPAPI_API_KEY`) is greyed out; if it was
  chosen, it stays chosen and is used once the key is there. The choice is
  kept in `config.json` (`research.sources`) and in the run folder's
  `.research_sources`, which the tool reads, so it applies from the
  researcher's next search. Defaults for new runs: `[research]` in
  `research_agent/bootstrap/config.toml`.
- **Links:** `?tab=input` opens a tab, `?drawer=1` the drawer.

Finished calls come from `<run>/activity/<session>.jsonl` (every event of each
call; the agent writes it in the terminal too) and the sessions' turns (what
was sent and the whole answer). The live feed comes from research_backend's
`send(on_progress=)`: every provider reports what its model is doing about
once a second.

Nodes and their calls: the bootstrap's discussor (asides too), orchestrator,
researcher and designer; in the execution, each step
owner (all its steps and attempts), the validator and the reviewer. **wait**
opens the Jobs tab, and **you** goes back to the chat.

### Which model each role runs on

The **Run** tab lists every role with a provider, model and effort picker.
The **Bootstrap** group holds the discussor, orchestrator, researcher and
designer. The **Execution** group holds the orchestrator, worker,
writer, validator and reviewer. Every node of the design runs on its
role's model.

- **Where it is saved:** a change goes into the run's `config.json` or
  `execute.json`.
- **When it applies:** from the role's next call. The execution group can be
  set before the execution starts, and writing `execute.json` starts nothing.
- **Unlisted models:** a model the provider does not list is shown in red,
  because its calls would likely fail.
- **Refreshing the list:** the lists are cached for ten minutes; "ask the
  providers again" refreshes them. Codex lists models by client version, so
  research_backend runs the newest Codex there is: `$RESEARCH_BACKEND_CODEX_BIN`,
  else the `codex` on PATH when it is newer than the Python SDK's bundled one.
- **Backups.** Under each role, **backup** lists what it moves to, in order,
  when its model is out of credits or cannot be reached
  (`codex_strata, claude_code:sonnet`). The call moves on at once, and the
  model is benched until its limit resets: the time the provider names, else
  an hour. While a role runs on a backup, its row says **on backup: ...**, and
  the chat notes the switch. The last backup waits for credits as usual. A
  role with no backup waits up to a day for its credits, as before.
- **A call that waits.** When a model is out of credits, over its usage
  limit, at capacity or busy, research_backend waits and retries (for
  credits, up to a day). The page says so as it happens: a line in the chat
  ("designer: Out of credits or over the usage limit. Waiting 30
  min, until 20:15 ..."), the status line, and a **Waiting until ...** box in
  the node's window. **Pause** ends such a wait at once; give the role a
  backup or another model, then **Carry on**.
- **Providers without files.** `strata` (the Strata server's API) has no
  files or commands, so the page refuses it for the roles that need them:
  the researcher, and the execution's worker, writer, validator and
  reviewer. `codex_strata` runs the same model through Codex, with its
  tools, and can be any role. Both read the key from `STRATA_API_KEY`, which
  must be set where the server runs.
- **Moving the discussor to another provider** (Claude Code to Codex) cannot
  continue its conversation, because a provider cannot resume another
  provider's thread. Instead:
  - the old session is kept as `discussor-<provider>-NNN`;
  - a new session starts with the last 30 messages and the whole research
    state;
  - the chat shows both sessions as one conversation.

The defaults for new runs are in `research_agent/bootstrap/config.toml` and
`research_agent/execute/config.toml`. On Codex, step owners already see the
GPUs: they run with `full` access, which is Codex's unsandboxed mode.

### The rest

| where | what |
| --- | --- |
| Graph tab | the stage's graph: the bootstrap's, or the execution's built from the design. Each step owner is coloured by its steps' status, and the node at work is outlined |
| Files tab | the run folder (artifacts, reviews, work, jobs, state), with a viewer. `rb/` and the checkpoint are left out |
| History tab | every change to the research state: who, when, which sections, and the operations |
| Run | stage, phase, what is running, the outcome or last error, the model of each role |
| Model calls | each call as it starts and ends (seconds, tokens, problems), and every session; click one for its turns: what was sent and what came back |
| Jobs | the execution's background jobs, their state, GPUs and hours, and their logs |
| Open run | every run, newest first; **Delete** moves one to `.trash` in the runs root (move it back to restore it) |
| Top bar | **Pause** (after the current step), **Carry on**, **Start execution** (once the design is approved: limits and which steps wait for your go), **Stop** (the execution's jobs and steps) |

Deep links: `/?tab=graph&side=jobs#<run>` opens a run with those tabs showing.

## How it works

| file | what |
| --- | --- |
| `server.py` | the FastAPI app: the API, the event stream, static files |
| `host.py` | one open run. Its graph runs on a worker thread; asides on their own threads, one discussor turn at a time; a watcher on the state file; the chat history read from the discussor's session |
| `events.py` | the event bus: numbered, replayable Server-Sent Events (`status`, `chat`, `call`, `step`, `note`, `state`, `error`) |
| `static/js/state.js` | the research state view, shared by the tab and the window |
| `static/js/chat.js`, `md.js` | the conversation; Markdown that is escaped first |
| `static/js/panels.js`, `graph.js` | files, history, run facts, model calls, jobs; the cytoscape graph |
| `static/js/app.js`, `statewin.js`, `node.js` | the main page; the state window; a node's live window |

- **The run's stage** follows its folder: the bootstrap until `execute.json`
  exists, then the execution.
- **One driver per run.** Do not drive the same run from the terminal and the
  page at once: they would share the conversation and the checkpoint.
- **Reconnecting.** EventSource reconnects by itself and sends the last event
  id, so nothing is lost or repeated.

### API

| method | path | what |
| --- | --- | --- |
| GET | `/api/runs` | the runs in the runs folder |
| POST | `/api/runs` | `{idea}` or `{start_from}` (a reviewed run), optional `name`; `start: false` creates it without starting |
| POST | `/api/runs/{id}/start` | start a run created with `start: false`, from its idea |
| GET | `/api/runs/{id}` | its status and chat history |
| POST | `/api/runs/{id}/message` | `{text}`: an answer, an aside, or the idea (the reply says which) |
| POST | `/api/runs/{id}/approve` | `{sections: ["design"]}` |
| POST | `/api/runs/{id}/trash` | delete the run: its folder moves to `<runs root>/.trash/<id>-<time>` (refused while it works or its jobs run) |
| POST | `/api/runs/{id}/go`, `/stop`, `/pause`, `/resume` | |
| POST | `/api/runs/{id}/break` | `{step, when: before\|after, on}` |
| POST | `/api/runs/{id}/execute` | `{wall_hours, gpu_hours, confirm}` |
| GET | `/api/runs/{id}/state`, `/history`, `/history/{n}` | the research state and its changes |
| GET, PUT | `/api/runs/{id}/models` | each role's provider, model, effort, backups and what it runs on now; `{group, role, provider, model, effort, backup}` changes one (`backup` left out keeps them) |
| GET | `/api/runs/{id}/skills` | the skills folder's skills: name and description |
| GET, PUT | `/api/runs/{id}/autopilot` | `{on, max_decisions, wall_hours, gpu_hours}`: the run decides for you (limits required) |
| GET, PUT | `/api/runs/{id}/research` | the literature sources the research may search, and which have keys; `{sources}` chooses them |
| GET, PUT | `/api/runs/{id}/nodes/{node}`, `/nodes/{node}/skills` | a node's settings; `{key, mode: none\|auto\|chosen\|design, skills}` chooses its skills |
| GET | `/api/providers` | each provider's models and effort levels (cached ten minutes) |
| GET | `/api/runs/{id}/graph`, `/sessions`, `/sessions/{sid}`, `/files`, `/file?path=`, `/jobs`, `/jobs/{job}/log` | |
| GET | `/api/runs/{id}/events?after_seq=` | the event stream |
| GET | `/api/runs/{id}/live?node=` | the calls under way (of one node): their newest events and what is being written |
| GET | `/api/runs/{id}/activity?node=` | a node's finished calls, newest first: every event, a line on what went in and came out (`said`, `outcome`, `to`), and the tokens |
| GET | `/state/{id}` | the research state window |
| GET | `/node/{id}/{node}` | a node's live window |

Errors are `{"detail": {"code", "message"}}`, with status 400, 404 or 409
(409: the run is busy).

## Tests

```
python -m pytest -q tests/test_web.py tests/test_web_static.py
```

The tests run on scripted model answers:
- **A whole run through the API:** answers, an aside while the team works,
  pause, an aside while paused, carry on, approving, starting the execution,
  the go, the review, files and the event stream.
- **The page's files:** every module parses (tree-sitter) and its imports
  exist, every element id a script looks up is on its page, and every CSS
  variable is defined.
