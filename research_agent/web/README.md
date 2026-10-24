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
| [Approve plan]   |                                                 | turns; jobs + logs |
| [ always open  ] |                                                 |                    |
+------------------+-------------------------------------------------+--------------------+
```

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
  bootstrap, each pending section has an **Approve** button, live while the
  team waits for you.
- **Execution:** the step table, with each step's status, attempts, GPU-hours
  and verdict. Tick **before** or **after** to set a breakpoint, and press
  **Go** when a step waits for you.
- **Reviews:** the reviewer's scores, requests (must, should, could), and its
  strengths, weaknesses and questions.

**Open in window** opens the same view at `/state/<run>`, in its own browser
window and live from the same event stream. Keep it next to the chat, or on
another screen.

### The rest

| where | what |
| --- | --- |
| Graph tab | the stage's graph: the bootstrap's, or the execution's built from the design. Each step owner is coloured by its steps' status, and the node at work is outlined |
| Files tab | the run folder (artifacts, reviews, work, jobs, state), with a viewer. `rb/` and the checkpoint are left out |
| History tab | every change to the research state: who, when, which sections, and the operations |
| Run | stage, phase, what is running, the outcome or last error, the model of each role |
| Model calls | each call as it starts and ends (seconds, tokens, problems), and every session; click one for its turns: what was sent and what came back |
| Jobs | the execution's background jobs, their state, GPUs and hours, and their logs |
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
| `static/js/app.js`, `statewin.js` | the main page; the state window |

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
| POST | `/api/runs` | `{idea}` or `{start_from}` (a reviewed run), optional `name`: start a run |
| GET | `/api/runs/{id}` | its status and chat history |
| POST | `/api/runs/{id}/message` | `{text}`: an answer, an aside, or the idea (the reply says which) |
| POST | `/api/runs/{id}/approve` | `{sections}` |
| POST | `/api/runs/{id}/go`, `/stop`, `/pause`, `/resume` | |
| POST | `/api/runs/{id}/break` | `{step, when: before\|after, on}` |
| POST | `/api/runs/{id}/execute` | `{wall_hours, gpu_hours, confirm}` |
| GET | `/api/runs/{id}/state`, `/history`, `/history/{n}` | the research state and its changes |
| GET | `/api/runs/{id}/graph`, `/sessions`, `/sessions/{sid}`, `/files`, `/file?path=`, `/jobs`, `/jobs/{job}/log` | |
| GET | `/api/runs/{id}/events?after_seq=` | the event stream |
| GET | `/state/{id}` | the research state window |

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
