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

**The login routes refuse to run on a non-loopback bind.** Reading the status
is still allowed from anywhere — it says whether a login exists, never what it
is — but starting one is not. Signing in writes credentials to this machine,
so without that rule anyone who could reach the port could sign it into *their*
account, and every turn afterwards would run as them, on their bill, with
nothing on the page saying so. That is a worse thing than the command
execution above, because it leaves no trace.

---

## Provider logins

The **Logins** button in the topbar, and the dot beside it. The dot goes amber
when a provider a role is actually pointed at cannot run; a provider nothing
uses does not colour it, because a dot that is always amber is a dot nobody
looks at.

Each provider shows one of three states — signed in, signed out, or *unknown*.
Three and not two on purpose: a probe that failed is not the same as a login
that is missing, and showing it as "signed out" sends you off to re-run a
login that works.

What the button can do depends on the CLI, and the server decides which:

| provider | signing in from the browser |
| --- | --- |
| `codex` | yes — a device code. Open the link, type the code, the page waits |
| `claude_code` | yes — open the link, paste the code back into the page |
| `antigravity` | **no.** `agy` signs in through a full-screen terminal interface; the panel shows the command to run instead |

Driving `agy` would mean writing a terminal emulator, and a half-working one
would be worse than the sentence telling you to run it yourself.

### Signing in again is safe, and it did not used to be

Measured: **`codex login --device-auth` deletes `~/.codex/auth.json` the moment
it starts**, not when it succeeds. So pressing "Sign in again" on a working
login and then changing your mind — or letting the fifteen-minute device code
expire — signed the machine out. It cost a working login once while this was
being tested, which is how it was found.

The flow now copies the credential files before it spawns anything and puts
them back on any outcome that is not a confirmed login, and the page says so
rather than doing it quietly. Restoring *only* when the provider did not
confirm is also what makes it safe if you sign in from a terminal while the
page is open: the re-probe sees that login, calls the flow done, and nothing
is written.

A login counts as finished when the provider says so — the flow re-runs the
status check after the process exits. Exit code 0 has been caught lying twice
in this project already.

---

## What to read, in what order

Seven Python files and seven JavaScript ones. If you read them in this order each
one only depends on what came before.

| | |
| --- | --- |
| **1. `runner.py`** | The interesting one. Read `agent/terminal.py::drive()` first, then this — they are the same loop. |
| **2. `events.py`** | What a session emits, and how a dropped connection catches up. |
| **3. `capture.py`** | How a `print()` inside a graph node reaches the browser. Honest about being a hack. |
| **4. `server.py`** | Routes. Deliberately the boring half. |
| **5. `editing.py`** | Reading and writing `plan.json` and the agent folder, and the three traps involved. |
| **6. `models.py`** | The request and response shapes. |
| **7. `auth.py`** | Driving a login on a pseudo-terminal. Independent of everything above it, and the only file here that spawns a process of its own. |
| **8. `static/js/api.js`** | Every call to the server, in one place. |
| **9. `static/js/app.js`** | Wires the panels together. The only file that knows they all exist. |
| **10. `static/js/chat.js`** | The left column — the only panel that can drive the agent. |
| **11. `static/js/graph.js`** | Cytoscape. |
| **12. `static/js/plan.js`** | The plan tree, and the step ↔ node link. |
| **13. `static/js/auth.js`** | The Logins panel. Polls rather than riding the event stream, because a login happens before there is a session. |
| **14. `static/js/inspector.js`** / **`topology.js`** | The right column, and editing the graph's shape. |

---

## The screen

```
┌──────────────────────────────────────────────────────────────────────┐
│  agent designer   <session> [phase] agent: name  [Logins] [New] [Open] │
├───────────────┬──────────────────────────────┬───────────────────────┤
│ Conversation  │ Design agent │ Graph │ Plan │… │ Run │ Configure │ ... │
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

**Design agent** is the agent that designs yours — the fixed graph in
`agent/bootstrap/graph.py` — drawn, with a card per node. For each of the three
model nodes (discussor, planner, designer) it shows the provider, the model,
the backend's timeouts and limits (read from the backend's own constructor),
and every prompt it sends with where it is sent and its size; the per-turn
prompts are built from state, so they are described rather than editable. The
New-session dialog's **Review the design agent before it starts** box (on by
default) creates the session without starting it and opens this tab, whose
**Start conversation** button begins the run.

Edits are saved to `<session>/bootstrap.json` as a diff against the defaults,
checked when you save (agy is refused here, because it cannot run read_only),
and locked while the session runs: the design graph is built at Start, so
pause, edit, and Start again.

**Pause and Stop** are in the topbar, and they are two different things
because there are two honest answers:

| | |
| --- | --- |
| **Pause** | Stops after the current node finishes. Loses nothing — the graph checkpoints there anyway — so **Start** carries on from the next node. Can take as long as the node in flight, except while it is waiting out a usage limit or a busy provider: then it stops at once, and Start runs that node again. |
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

**Configure / Context / Conversation.** The selected node's settings, then two
halves that are easy to confuse and are deliberately kept apart:

- **Context** is what the node will be sent *next* — its templates rendered
  against the session's state **as it is now**.
- **Conversation** is what actually happened, turn by turn: the prompt that
  turn was really sent, the commands it ran with their exit codes and output,
  the files it touched, its reasoning, and the structured answer it returned.

They are different documents, and that is the point. A prompt is a template
rendered at a moment; turn 3 of a retry loop carries the verifier's complaint
and turn 1 did not. Re-rendering the template afterwards shows you today's
state, not what the model read — so "why did it do that" used to have no
answer anywhere, and each turn now records its own.

Each turn also names which template it used (`prompts.first` or
`prompts.next`). A loop that never converges often turns out to be re-sending
`first` every time, because the provider thread was never continued, and
nothing else on the record shows that.

The whole tab exists because the terminal only ever said "313 events, last one
7s ago", which is true and useless; see `agent/activity.py`.

**Any node that has a record is selectable there**, from a row of chips — not
just the one picked in the graph. That matters because the design phase does
its own turns and is *not* in the generated agent's graph: `discussor`,
`planner` and `designer` could never be selected, so the conversation that
produced the whole agent sat on disk with no way to reach it. Turns read
oldest-first, because a conversation does and a tail does not.

**The chat panel replays its history when you open a session.** It used to
live only in an in-memory event buffer, so restarting the server emptied the
browser while the session still held every word — read now from the
checkpoint (`storage.read_transcript`), which needs no graph, no backends and
no provider. A dashed rule marks where the replay ends and the live feed
begins; without it a session reopened halfway through reads as one continuous
chat with no way to tell which part is happening now.

---

## Changing which model a node runs on

A designed node names a **role** (`coder`, `checker`, ...), and the role decides
the provider and model. To change it, click the node, then in **Configure ▸
Runs on** pick the provider and a model from that provider's own list, and
press **Use for this role**. Every node with that role moves with it (the hint
says which); to move one node alone, give it a new role name in **Backend
(role)** first and save the node.

The choice is saved with the session (`options.json`, as the New dialog's
Models section saves its choices) and refused at once if the provider cannot do
the role's job — antigravity cannot run a read-only role. It is used when the
run starts, so a change at the design review applies to the run you are about
to approve; for a run already going, press Pause, then Start.

### Delegating when a provider runs out of credits

Off unless you turn it on, per role. Under **Runs on**, tick **If its provider
runs out of credits, delegate to** and pick the delegate's provider and model —
say antigravity with `claude-opus-5-5-high` — then **Use for this role**.

When that role's provider reports it is out of credits, the turn is not waited
on: it runs again on the delegate as a **new conversation with the node's
first prompt** (the delegate has seen none of the old one), and the role stays
on the delegate for the rest of that run. The conversation says so, and the
turn's record is marked `delegated`. Unticked, the role waits for credits as
before (up to a day). The delegate must be able to do the role's job:
antigravity cannot stand in for a read-only checker — use claude_code there.

### Giving a role the GPUs

Codex runs every command a node executes inside its own sandbox, and that
sandbox has **no GPU devices**: `nvidia-smi` and `torch.cuda` fail inside it on
a machine full of GPUs. So a Codex node that has to use the GPU — a training
run, a hardware preflight — needs the **GPU** box under **Runs on** (shown when
the provider is codex). Its write turns then run without Codex's sandbox and
see the GPUs; its read-only turns keep the sandbox. The price: for that role,
staying inside the session's workspace rests on the instructions, as it already
does on antigravity (which has no sandbox, and sees the GPUs).

Every turn still inside the sandbox is told so, so it does not report "no GPU"
as a blocker. **Which** GPUs a run gets is still `CUDA_VISIBLE_DEVICES` in the
environment the server was started with: `CUDA_VISIBLE_DEVICES=0,1 python
main.py web ...` to give runs those two.

## Watching a turn live

Every backend reads its provider's stream as it arrives — Codex's
notifications, and the `stream-json` lines `claude` and `agy` print — and keeps
the tail of what the model is writing and reasoning, and the commands and file
edits it makes, in the turn's in-flight record (`activity/<node>/current.json`,
rewritten every second). While a session runs, the page polls that record every
1.5 seconds, so a **working row** appears as soon as a turn starts and shows it
as it happens:

```
s1_code (codex/gpt-5.6-sol) — working 34s · 3 command · 1 message
  ● I'm inspecting the existing 1.1–1.7 interfaces first, then I'll
    implement only substep 1.8.
  ⏺ $ git status --short && sed -n '1,260p' hotel_skill_study/config.py
    │ … 117 more lines
    │     raise RecordError("durable record verification failed")
  ⏺ $ python -m pytest -q tests/hotel_skill_study
    │ FAILED test_cli.py::test_resume - KeyError: 'cell_id'
    │ exit 1
  ⏺ edited hotel_skill_study/cli.py
  writing ▸ Compare LoRA with a linear probe on IMDB ...
```

It reads like a coding assistant's terminal (`static/js/transcript.js`):

| marker | what |
| --- | --- |
| `●` | what the model said — its prose; a JSON message shows its verdict and first prose field (`[retry] pytest fails on the loader`), the JSON itself under **as sent** |
| `✻` | its reasoning summary |
| `⏺ $` | a command — as typed, not wrapped in `/bin/bash -lc "..."` — with the last four lines of its output underneath, in red with `exit N` when it failed; **whole output** for the rest |
| `⏺` | a tool call (agy, Claude Code), a file edit (path relative to the workspace), a web search |
| `✗` | an error |

A command's start and end are **one** row: `running…` (its marker blinking)
until it ends. Codex's bookkeeping — token-usage, moderation, diff and
approval-review notices, and the prompt echoed back — is not drawn. Counted
across a real session it was more than half of all events (535 token-usage
and 535 moderation notices against 663 commands), each drawn as its own line,
so the stream was mostly "thread token usage updated" and you could not tell
which step went wrong. The terminal and chat log lines drop the same kinds.

The working row keeps the newest twelve steps plus the live tails;
**show detail** shows up to 120 with whole outputs. When the turn ends the row
folds to its one-line summary, and the whole turn is in the node inspector's
Activity tab, drawn the same way.

It lags the provider by a second or two, by design: polling a small file is
cheap, and pushing every token down the event stream would evict the
conversation from its ring buffer (one turn held 14,885 events in seventeen
minutes).

## The "still working" line

A long turn also prints one of these every thirty seconds, to the terminal and
the log:

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
| **Configure ▸ a node ▸ Conversation** | every recorded turn, expandable | **the whole message**, up to 200 KB |

So: complete text, yes — in the node inspector's Conversation tab, once the turn
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
cannot read anywhere. It is now shown: its prose as the row, the whole
document under **as sent**.

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
| where `.agent/` goes | `config.json` and promoted agents live under `<repo>/.agent/`, and sessions too unless you choose another sessions folder |
| what the session's workspace is a copy of | in a git repository each new session works in its own worktree of this project (`<session>/workspace`, branch `agent/<id>`); `{repo_path}` and every "inspect the repository" instruction mean that copy |
| what a write node may change | its workspace — never this checkout; everything it *produces* goes to the session's `artifacts/` |

The field is prefilled with the absolute path the server was started for, so
what is on screen is the real answer. A **relative** path is taken from that
same repo, not from the server's working directory — `python main.py web
../projectA` names the project, and the field must not quietly disagree. An
absolute path or `~/...` is honoured as typed, so one server can serve several
projects.

**It does not have to be a git repository**, despite the name. A plain folder
works — but then there is no worktree to give each session, so sessions work
in the folder itself and can see each other's changes.
It also does not have to be *this* project — pointing it at the codebase you
actually want worked on is the normal case.

---

## The sessions folder

Every **New** session gets a new folder with a unique id — a few words of the
task, the time and a random suffix, e.g. `survey-hotel-jobs-20261018-1405-a3f9`
— so nothing from an earlier session is ever reopened. The **Sessions folder**
decides where those folders are made. By default it is
`<repo>/.agent/sessions/`.

**To choose it from the browser:** open **New**, and type the path into
**Sessions folder** (it is prefilled with what the server resolved). `~` works;
a relative path is taken from the repository. The browser remembers your
choice for the next New, and **Open** lists the sessions in that same folder —
change the field and press Open to look in another. Clear it to go back to the
default.

**To set it for the server, so every browser gets it:**

```bash
python main.py web . --sessions-root ~/agent-sessions
# or
AGENT_SESSIONS_ROOT=~/agent-sessions python main.py web .
```

**To set it permanently for a repository**, in `<repo>/.agent/config.json`:

```json
{ "sessions_root": "~/agent-sessions" }
```

Precedence, later wins: config file → `AGENT_SESSIONS_ROOT` → `--sessions-root`
→ the dialog field.

**Changing it does not stop older sessions.** A session that was running when
the server last stopped is resumed wherever its folder is, so after switching
to a new sessions folder the old running ones come back too. **Open** lists
them alongside the new folder's sessions, and clicking one reattaches it.
**Pause** one you do not want, and it will not be resumed again — or start the
server once with `--no-resume`, which skips them and forgets them. (With the
server stopped, deleting `<repo>/.agent/running/<session id>.json` does the
same for one session; its folder is untouched.)

**Outside the repository is the better place.** Then a model working in the
project cannot come across another session's folder at all. (Every model is
also told which folder is its own and to leave the others alone.)

**Exact session folder** is the other field: use it for one session whose
folder you have prepared — say, with a hand-written `brief.txt` in it — and
that folder is used as it is, wherever it is.

**Deleting a session:** in **Open**, press **Delete…** under it, then
**Delete for good**. A running session is stopped first; then its folder (the
conversation, the agent, every result under `artifacts/`), its worktree and
its resume mark are removed. Its branch `agent/<id>` keeps the code it wrote
unless you tick *Also delete its git branch*. Anything that does not look like
a session folder, or that contains the project, is refused.

A session remembers the options it was started with (`options.json`: models,
supervisor mode, pre-built agent), so opening it again or resuming it after a
server restart runs it the same way.

---

## Leaving a run alone

A run lives in the server, not the browser: close the tab and it carries on;
open the page again and **Open** the session to watch. Keep the server itself
alive with `screen`, `tmux` or `nohup`. If it stops anyway — a crash, a restart
to pick up new code — every session that was running carries on when
`python main.py web` starts again, from the last finished node (start the
server with `--no-resume` to skip that). A session that was waiting for your
answer asks the same question again.

Out of credits or over a usage limit, a run waits instead of failing: until
the time the provider names, or 1, 2, 4, 8, 16 minutes and then every 30
minutes, for up to 24 hours (`credit_wait_hours` in a role's `options`).

---

## The Files tab

Shows this session's folder and nothing else:

| group | what |
| --- | --- |
| **Code** | the files the session changed in its workspace since it was made |
| **This session** | its plan, brief, designed agent, the supervisor's reports |
| **Results** | everything under `artifacts/` (collapsed: often thousands) |

Not listed and not readable: the rest of the repository (this tool's own
`agent/` and `webui/` included), other sessions, and the unchanged part of the
workspace, which is just a copy of the project. A path that climbs out —
`session/../../...`, or a symlink pointing outside — is refused with a 403.
That matters more than it looks: with `--host 0.0.0.0`, whatever this tab can
read, anyone who can reach the port can read.

---

## The research agent

The New session dialog's **Let the discussor ask the research agent first**
box (ticked by default) lets the discussor send the **researcher** a few
questions before it asks you: the standard datasets, baselines and their
numbers, the usual settings, what a run costs. You see it in the conversation —
the discussor's "Looking these up first", then the researcher's findings with
their sources — and then the discussor's questions, with suggestions that cite
them. Each round is also saved to `research/round-N.md` in the Files tab, and
the researcher is a card on the **Design agent** tab, where its model, its
instructions and its skills (the tools it may run) can be changed.

It runs on antigravity by default and takes a few minutes a round. If it
cannot start or fails, the discussor carries on without it and says so.

---

## The task field

**It is the opening line of a conversation, not a specification.** The
discussor's first prompt is:

> The human wants this done: *&lt;your task&gt;*. Begin discovery. Inspect the
> repository, then ask EVERY question you still need, all in this one turn, each
> with your suggested answer. … The human ends the discussion by typing `/plan`;
> you only advise.

So one sentence is enough. The discussor reads the repository, then asks
everything it still needs **in one message**, numbered, each with the answer it
will assume and why it matters. Answer only the ones you disagree with ("2: 5
seeds, not 3") — every other suggestion stands, and goes to the planner marked
*(assumed)*. It asks follow-ups only when an answer opens something new, and
**you** decide when to stop by typing `/plan`. The LLM never makes
that call; that is enforced by the graph's topology, not by asking it nicely.

Its first few words also go into the new session's id (`new_session_id()`:
words, time, random suffix). Every New session is a new folder, even for a task
you typed before, so nothing from an earlier session is reopened. The *Sessions
folder* field says where those folders go; Open lists the sessions in it.

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
| `GET` | `/api/sessions` | list what is on disk, newest first; `?root=` another sessions folder |
| `POST` | `/api/sessions` | resolve or create one, and start it |
| `GET` | `/api/sessions/{id}` | phase, pending question, problems |
| `POST` | `/api/sessions/{id}/start` | run a session that is idle |
| `POST` | `/api/sessions/{id}/answer` | `{"text": "/plan"}` |
| `POST` | `/api/sessions/{id}/pause` | stop after the current node; resumable |
| `POST` | `/api/sessions/{id}/stop` | stop now, abandoning the turn in flight |
| `DELETE` | `/api/sessions/{id}` | close the runner; the folder is untouched |
| `POST` | `/api/sessions/{id}/delete` | delete it for good: `{"repo", "sessions_root", "drop_branch"}` |
| `GET` | `/api/sessions/{id}/events` | **SSE**; honours `Last-Event-ID` and `?after_seq=` |
| `GET`/`PUT`/`DELETE` | `/api/sessions/{id}/bootstrap` | the design agent: its models and prompts; `DELETE` resets |
| `GET`/`PUT` | `/api/sessions/{id}/plan` | `plan.json` |
| `GET`/`PUT` | `/api/sessions/{id}/agent` | `graph.json` + `nodes.json`, plus a drawable view |
| `POST` | `/api/sessions/{id}/agent/validate` | check an edit without writing it |
| `PUT` | `/api/sessions/{id}/roles/{role}` | `{"provider", "model", "gpu", "delegate": {"provider", "model"} or null}` for a role, this session |
| `GET` | `/api/sessions/{id}/nodes/{name}/context` | the prompt that node will really get |
| `GET` | `/api/sessions/{id}/nodes/{name}/activity` | every provider event for that node's turns |
| `GET` | `/api/sessions/{id}/activity` | the turn running *right now*, whichever node it is |
| `GET` | `/api/sessions/{id}/timeline` | every node visit, in order |
| `GET` | `/api/sessions/{id}/iterations` | the supervisor's reviews, one per run |
| `GET` | `/api/sessions/{id}/files` | the session folder's files: code, session, results |
| `GET` | `/api/sessions/{id}/files/content?path=` | one of them; nothing outside the folder |
| `GET` | `/api/models` | each provider's models; `?refresh=true` to ask again |
| `GET` | `/api/schema` | node kinds, field types, access levels, providers |
| `GET` | `/api/config` | the resolved backend for each role |
| `GET` | `/api/defaults` | the repo this server was started for, its sessions folder, and its flags |

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
