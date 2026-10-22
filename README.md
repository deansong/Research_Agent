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

- [Install](#install) · [**The browser UI**](#the-browser-ui) · [Your first run](#your-first-run) · [**Setup**](#setup) — session folder · [a folder of your own](#putting-the-session-folder-somewhere-else) · [`brief.txt`](#starting-from-a-written-brief-brieftxt) · config file · env vars
- [The five things you'll actually do](#the-five-things-youll-actually-do) —
  start · resume · reuse an agent · edit an agent · configure backends
- [Command reference](#command-reference) · [CLI reference](#cli-reference)
- [Where everything lives](#where-everything-lives)
- [How it works](#how-it-works) · [**Skills**](#skills) · [Four things that will bite you](#four-things-that-will-bite-you)
- [Troubleshooting](#troubleshooting)

---

## Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Three providers are real, and each signs in through its own CLI. To see where
you stand:

```bash
python main.py login
```

```
  ok  antigravity  Logged in; 14 models available.
  --  claude_code  Not logged in.
  ok  codex        Logged in using ChatGPT  (ChatGPT)

To fix, in a terminal on this machine:
    claude auth login   # claude_code
```

It reports rather than signing in, because the answer is usually "they are
fine" — and for `codex`, finding out should not mean starting a flow that
clears the stored credentials before it does anything else. The browser UI has
a **Logins** button that can drive the `codex` and `claude` flows end to end;
`agy` signs in through a full-screen terminal interface, so that one is always
a terminal job.

A signed-out provider is named again by `--explain`, and once more before a
run spends anything — the failure it replaces was a turn dying five minutes
in with a message about the model.

## The browser UI

There is a web front end as well as the terminal, and it is the easier way to
see what an agent actually is:

```bash
pip install fastapi 'uvicorn[standard]'
python main.py web . --backend fake     # then open http://localhost:8420
```

Chat on the left, the graph and the plan in the middle, the selected node on
the right. You can edit the plan before approving it, rewire the graph, change
any node's backend, and — the useful one — see the prompt a node will *really*
be sent, with its placeholders resolved.

Both front ends drive the same sessions, so you can start one in the browser
and resume it with `run --session <name>`. See [`webui/README.md`](webui/README.md).

**Where its sessions go.** Every New session gets a new folder with a unique
id, inside the sessions folder: `~/research_agent_sessions/` unless you
choose another -- never inside the repository. Choose it once, any of these ways:

```bash
python main.py web . --sessions-root ~/agent-sessions     # this server
AGENT_SESSIONS_ROOT=~/agent-sessions python main.py web . # this shell
```

or `"sessions_root": "~/agent-sessions"` in `<repo>/.agent/config.json`
(permanent), or type it in the **Sessions folder** field of the New session
dialog (this browser remembers it; **Open** lists the sessions in that same
folder). The field is prefilled with what the server resolved. To use one exact
folder for a single session — one holding a hand-written `brief.txt`, say —
fill **Exact session folder** instead. Details in
[`webui/README.md`](webui/README.md#the-sessions-folder).

**Leave it running.** A run lives in the server, not the browser: close the tab
and it carries on. Keep the server alive (`screen`, `tmux`, `nohup`); if it
stops anyway, every session that was running resumes when `python main.py
web` starts again (`--no-resume` skips and forgets them). **Pause** stops after the node in
progress — at once during a credit wait — and **Start** carries on.

**What the Files tab shows** is this session's folder only: the code it changed
in its own workspace, its plan, agent and reports, and its results. Nothing
else in the repository — not this tool's source, not other sessions — can be
listed or read through it.

**The design agent is editable too.** A new session opens on the **Design
agent** tab *before* anything runs: the fixed graph that interviews you, plans
and designs, with the provider, model, timeouts and every prompt of its four
model nodes (discussor, researcher, planner, designer) — including the two
skills, the discussor's experiment-design checklist and the researcher's
tools. Change any of it, then press
**Start conversation**. Changes are saved per session in
`.agent/sessions/<name>/bootstrap.json` — only what differs from the defaults
— and are picked up by the terminal as well. `run --explain` prints the same
summary. They are locked while the session runs; pause it to edit, and the
next Start applies them.

## Your first run

**Try it with no API calls and no login at all.** The `fake` backend returns
canned answers instantly, including a real (small) plan and a real generated
agent, so you can watch the whole pipeline for free:

```bash
python main.py run /path/to/repo --backend fake
```

You'll be asked what you want, then:

```
[discussor] …asks every open question at once, each with a suggested answer…
you> 2: 5 seeds, not 3          ← answer only the ones you disagree with
you> /plan                     ← you decide when discussion ends; the rest
                                  of the suggestions stand

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

## Setup

Nothing here is required to start — `python main.py run <repo>` works with no
config at all. This is what to change when you want to.

### The session folder

**Created for you** on the first run, at `<repo>/.agent/`. You never make it by
hand.

```
<repo>/.agent/
    .gitignore              written on first run. COMMIT THIS.
    config.json             your settings (optional, gitignored)
    sessions/<session-id>/  one per session, new id every time. gitignored.
        brief.txt           OPTIONAL, written by YOU — see below
        request.txt         what you asked for — identifies the session
        plan.json           four stages, work in substeps — EDIT ME first
        brief.md            the designer's rewrite of it, for the agent
        agent/              the agent designed for this session
        artifacts/          where a run puts what it PRODUCES
        checkpoint.sqlite   both phases, under two thread ids
    agents/<name>/          promoted agents. NOT gitignored — commit them.
```

**What to commit.** The `.gitignore` the tool writes ignores `sessions/` and
`config.json`, and leaves `agents/` tracked — because a promoted agent is
something you want to keep and share, while a session is scratch. Commit
`.agent/.gitignore` and anything under `.agent/agents/`.

**One folder per session.** Every new session gets a new id — a few words of
the task, the time and a random suffix, e.g.
`survey-hotel-jobs-20261018-1405-a3f9` — so two sessions never share a folder,
however alike their tasks. Every model, in the design phase and in the agent it
designs, is also told which folder is its own and to leave the other sessions
alone.

**Where session folders go.** `~/research_agent_sessions/` unless you set a
sessions root: `"sessions_root": "~/agent-sessions"` in `config.json`,
`AGENT_SESSIONS_ROOT`, `--sessions-root PATH`, or the *Sessions folder* field in
the web UI's New session dialog (remembered in that browser). Nothing a session
does is written to the repository the server runs from -- not its folder, not
its results, not the mark that resumes it after a restart (that is in
`~/.codex-langgraph-agent/running/`).

**Each session is its own project, fully independent.** A new session gets
`<session>/workspace`: a new git repository that starts **empty**. Every model
turn runs there, and the code the run writes is that project. Nothing is copied
in from the repository the server runs from -- that is the agent's own code --
and the models are told that nothing outside the session folder is part of the
task: not the agent's code, not other sessions. They may still use installed
tools, install packages, and download data, models and papers into the session.
The run's results go to `<session>/artifacts/`. The Files tab's *Code* group is
everything in the workspace.

Sessions made before this, whose workspace is a git worktree of the repository
(branch `agent/<id>`), keep working as they were. antigravity's write mode has
no sandbox, so for it the boundary is enforced only by the instructions.

**Managing sessions.**

```bash
python main.py sessions .                    # list them, newest first, with what each was for
python main.py run . --session <name>        # resume one
```

**Deleting one:** in the web UI, **Open** → **Delete…** under the session →
**Delete for good**. It stops the session if it is running, then removes its
folder -- its project, results and paper with it -- and its resume mark. (An
older session's worktree goes too; its branch `agent/<id>` is kept unless you
tick *Also delete its git branch*.) By hand:

```bash
rm -rf <sessions folder>/<id>
rm -f ~/.codex-langgraph-agent/running/<id>.json
```

Sessions are safe to delete — they hold conversation history and run output,
never your project's source. But note `artifacts/` inside a session is **not in
git**, so deleting a session deletes its outputs permanently. Check the size
before a mass delete: a run that downloaded models can be gigabytes.

```bash
du -sh ~/research_agent_sessions/*
```

### Putting the session folder somewhere else

`--session-dir PATH` uses a directory of your choosing **as** the session
folder, with exactly the same contents:

```bash
mkdir -p ~/experiments/parser-tests
python main.py run . --session-dir ~/experiments/parser-tests
```

Use it when the session is the thing you care about — an experiment you want
sitting beside its data, a folder you want to keep after the checkout is gone,
a directory two people look at. For ordinary throwaway work `--session NAME`
is still the easier choice.

Three things behave differently:

- **Promoted agents still go to `<repo>/.agent/agents/`**, not into your
  folder. They are reusable across sessions, which is the whole point of
  promoting one; an external folder is usually a single experiment.
- **`python main.py sessions .` will not list it.** That command walks
  `.agent/sessions/`, and there is no index of folders you named yourself.
- **If the folder is inside the repo it is not gitignored**, so the checkpoint
  and artifacts show up in `git status`. You get a one-line warning; add the
  path to `.gitignore` yourself if that isn't what you meant.

A typo is caught rather than acted on: if neither the path nor its *parent*
exists, the run stops instead of quietly creating an empty session somewhere
you didn't mean.

### Starting from a written brief (`brief.txt`)

Drop a `brief.txt` into a session folder and it is read as the task — the
initial idea, exactly what you would otherwise have typed at the
*What do you want to build/change?* prompt.

```bash
mkdir -p ~/experiments/parser-tests
cat > ~/experiments/parser-tests/brief.txt <<'EOF'
Add pytest coverage for parser.py.

Focus on the error paths — the happy path is already covered by the
integration tests. Do not touch the lexer.
EOF

python main.py run . --session-dir ~/experiments/parser-tests
```

The run starts without asking you anything.

**Why a file.** A brief written in an editor can have paragraphs, lists and
second thoughts. `--task "…"` is one shell-quoted line, and the terminal
prompt is one line you cannot go back and edit. This is the same input, taken
seriously.

**It only works when the folder is known before the task is.** That means
`--session-dir PATH` or `--session NAME`. With neither, a new session is made
only once the task is known, so there is nowhere to look for the file yet.

**Precedence:** `--task` / `--task-file` beats `brief.txt`, which beats the
`request.txt` we saved on a previous run. Most explicit wins. An empty
`brief.txt` is reported and skipped rather than treated as an empty task.

**`brief.txt` and `brief.md` are different files**, and the pair is worth
learning once:

| | who writes it | when | what it is |
| --- | --- | --- | --- |
| `brief.txt` | **you**, by hand | before the first run | the initial idea — an input |
| `brief.md` | the **designer** | during the design phase | its rewrite of your idea, addressed to the generated agent — an output |

**Editing `brief.txt` after an agent has been designed** stops the run, on
purpose. That agent was built for the old text and would do confident,
plausible, wrong work against the new one. Delete `<folder>/agent/` to
redesign in place, or start in an empty folder.

### The config file

Optional. Two locations, both plain JSON:

| | scope |
| --- | --- |
| `~/.codex-langgraph-agent/config.json` | all your projects |
| `<repo>/.agent/config.json` | this repository only |

Start from the shipped example:

```bash
mkdir -p .agent && cp agent.example.json .agent/config.json
```

**Every key it accepts** (all optional):

```json
{
  "_comment": "Keys starting with _ are ignored, so you can annotate freely.",

  "default":  { "provider": "codex", "model": null, "options": {} },

  "roles": {
    "discussor":    { "provider": "codex" },
    "planner":      { "provider": "codex" },
    "designer":     { "provider": "codex" },
    "orchestrator": { "provider": "codex" },
    "executor":     { "provider": "codex", "model": "gpt-5.6-sol",
                      "options": { "timeout": 3600 } }
  },

  "session":              null,
  "recursion_limit":      1000,
  "work_recursion_limit": 200,
  "max_design_attempts":  3,
  "default_agent":        "default"
}
```

| key | default | what it does |
| --- | --- | --- |
| `default` | `{provider: "codex"}` | fallback for every role |
| `roles` | `{}` | per-role overrides, merged over `default` **field by field** |
| `session` | `null` | pin a session name; `null` derives one from the task |
| `recursion_limit` | `1000` | step budget for the design phase |
| `work_recursion_limit` | `200` | step budget for a generated agent — lower so a runaway loop fails fast |
| `max_design_attempts` | `3` | how many times the designer may retry a rejected folder |
| `default_agent` | `"default"` | folder used by `/use` and by `--pre-build-agent` with no name |

Role names are **not a fixed set.** The five above are the design phase's own;
a designed agent invents its own and declares one per node. The research
skeleton uses `coder`, `runner` and `checker` — `runner` deliberately separate
so experiment runs can go to a cheaper model without touching the graph:

```json
{ "roles": { "runner": { "model": "gpt-5.6-luna" } } }
```

An unconfigured role falls back to `default`, and says so at startup rather
than letting you assume otherwise. See `docs/AGENT_FOLDER.md`.

A **role** is any name a node declares as its `backend`. The built-in ones are
`discussor`, `planner`, `designer` (design phase) and `orchestrator`,
`executor` (the `default` agent) — but a generated agent invents its own, and
those are configurable the same way. An unknown name is a warning, not an
error, since the folder that defines it may not exist yet.

**Per-provider `options`:**

| provider | options |
| --- | --- |
| `codex` | `timeout` (300s, on SILENCE not total time), `max_seconds` (7200), `max_output_tokens` (60000), `credit_wait_hours` (24), `credit_wait_seconds` (60, the first wait), `busy_attempts` (4) |
| `claude_code` | `executable` (`"claude"`), `timeout`, `max_seconds`, `max_output_tokens`, `max_turns`, `max_budget_usd`, `credit_wait_hours` (24) |
| `antigravity` | `executable` (`"agy"`), `timeout`, `max_seconds`, `max_output_tokens`, `effort` (`low`/`medium`/`high`), `credit_wait_hours` (24) |
| `api` | `provider` (`"anthropic"`) — *stub* |

Typos are caught: the file is parsed strictly, so `"provdier"` is an error
naming the file and the key, not a silently ignored setting.

### Environment variables

| | |
| --- | --- |
| `AGENT_BACKEND` / `AGENT_MODEL` | every role |
| `AGENT_BACKEND_<ROLE>` / `AGENT_MODEL_<ROLE>` | one role, e.g. `AGENT_BACKEND_EXECUTOR=fake` |
| `CODEX_MODEL` | legacy alias for `AGENT_MODEL` |

`<ROLE>` is the role name uppercased, and any role works — including ones only
a generated agent has.

### Precedence

Later wins:

```
built-in defaults → ~/.codex-langgraph-agent/config.json → <repo>/.agent/config.json
    → --config FILE → environment → CLI flags
```

`--config` **replaces** the two default files rather than layering on them.

### Checking what you actually got

```bash
python main.py run . --explain      # resolved config + agent shape, spends nothing
```

It prints a line per role and a `settings from:` line naming every layer that
contributed — usually the fastest way to find out why a setting isn't taking.
`/config` shows the same thing mid-session.

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

Two other ways in, both covered under [Setup](#setup):

```bash
# put the session folder where you want it
python main.py run . --session-dir ~/experiments/parser-tests

# ...and write the task into it beforehand, instead of typing it
echo "Add pytest coverage for parser.py, error paths only." \
    > ~/experiments/parser-tests/brief.txt
python main.py run . --session-dir ~/experiments/parser-tests
```

### 2. Resume a session

Everything is checkpointed after every completed step. Ctrl-C, close the
laptop, come back tomorrow:

```bash
python main.py run . --session add-pytest-tests-for-a1b2c3
```

It picks up at the exact question it stopped on, with the conversation, the
plan and the agent intact. Resume **by id** — re-running the same `--task`
starts a new session, on purpose. `python main.py sessions .` lists the ids.

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
python main.py run . --backend-role executor=codex:gpt-5.6-sol  # one role
python main.py run . --explain                             # show, spend nothing
```

To see which models each provider actually offers right now — antigravity's
list changes as Google adds models, Gemini 4 and Claude among them:

```bash
python main.py models                # every provider
python main.py models antigravity    # one
```

The web UI asks the same thing: the New session dialog's **Models** section has
a provider and model dropdown per role, and the Design agent tab's model field
is a dropdown of that provider's own list. codex and antigravity are asked
live (`model/list`, `agy models`); claude_code has no listing command, so its
list is the documented aliases (`opus`, `sonnet`, … always the latest).

Or persistently, in `<repo>/.agent/config.json` (per repo) or
`~/.codex-langgraph-agent/config.json` (global) — see `agent.example.json`:

```json
{
  "default": { "provider": "codex" },
  "roles": {
    "executor": { "provider": "codex", "model": "gpt-5.6-sol",
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
| `codex` | **implemented** — the OpenAI Codex SDK |
| `claude_code` | **implemented** — shells out to the `claude` CLI |
| `antigravity` | **implemented** — shells out to the `agy` CLI (Gemini, GPT-OSS, Claude via Antigravity) |
| `fake` | **implemented** — canned answers, for learning and testing |
| `api` | stub; fails at startup with the steps to finish it. Full design in the module docstring |

`python main.py login` asks each of them directly — `codex login status`,
`claude auth status --json`, `agy models`. The measured details are in
`agent/backends/auth.py`, including the one that matters: `claude auth status`
exits 0 whether or not you are signed in, so the exit code cannot be the test.

The two CLI backends share `agent/backends/_cli.py` — one subprocess per turn,
NDJSON out — so each provider module is just vocabulary: an argv builder, an
access mapping, an event mapping and a usage mapping. Neither needs a Python
package installed; they need the CLI on `PATH` and logged in, and they say so
at startup if it is not.

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
python main.py login [provider]                who is signed in, and how to fix it
python main.py models [provider]              the models each provider offers
python main.py run <repo> [options]           the main command
python main.py web <repo> [options]           serve the browser UI
python main.py sessions <repo>                list sessions, newest first
python main.py promote <repo> <name>          keep this session's agent
```

`web` takes the same backend and supervisor flags as `run`, plus
`--sessions-root PATH`, `--host`, `--port`, and `--no-resume` (do not restart
the sessions that were running when the server last stopped).

`run` options:

| | |
| --- | --- |
| `--task "…"` / `--task-file F` | the task, instead of being prompted |
| `--session ID` | resume a specific session (default: a new one) |
| `--sessions-root PATH` | the folder session folders are made in |
| `--session-dir PATH` | use PATH as the session folder, anywhere on disk |
| `--pre-build-agent NAME\|PATH` | skip designing; run this agent |
| `--backend NAME` | provider for every role |
| `--model NAME` | model for every role |
| `--backend-role ROLE=PROV[:MODEL]` | override one role (repeatable) |
| `--config PATH` | config file (replaces the default two) |
| `--supervisor manual\|auto\|off` | what happens after each run (default manual) |
| `--no-research` | do not let the discussor ask the research agent first |
| `--design-topology hub\|pipeline` | the shape of agent to design (default hub: the discussor orchestrates, one validator, a paper) |
| `--max-iterations N` | most design-and-run iterations the supervisor may take |
| `--explain` | print config + agent shape and exit, spending nothing |

---

## Where everything lives

```
<repo>/.agent/
    .gitignore              committed; keeps sessions/ out of git
    config.json             per-repo backend config
    running/                which sessions to resume when the server starts
    sessions/<session-id>/  gitignored  (or under your sessions root, or --session-dir)
        brief.txt           optional; YOUR initial idea, written by hand
        request.txt         what you asked for
        options.json        the models / supervisor / agent it was started with
        workspace/          ITS OWN project: a git repository that started empty
        workspace.json      when it was made, and its first commit
        plan.json           four stages, work in substeps — EDIT ME first
        brief.md            the designer's rewrite of it, for the agent
        bootstrap.json      your changes to the design agent, if any
        agent/              the agent designed for this session
        artifacts/          where a run puts what it PRODUCES
            reports/<node>.md   each step's final report
            paper/              the ACL-style paper: main.tex, appendices, tables, figures
        orchestrator/memory.json  what the orchestrator knows, across runs (Orchestrator tab)
        activity/           every turn, in full, and timeline.jsonl
        iterations/<n>/     each run's review (the orchestrator's, or the supervisor's)
        checkpoint.sqlite   every phase, its own thread ids
    agents/<name>/          promoted, committed, reusable
```

**Code** (source, its tests) goes in the session's **workspace** — its own
project. **Run output** (data, caches, reports, plots, logs, the paper) goes in
the session's `artifacts/`. Neither is ever written to the repository.

**Rules every model gets**, appended by the loader so a generated agent can't
opt out:

- *every model turn*: the session is self-contained — work only inside its
  folder; its workspace started empty; never read, use or write the agent's
  own code or other sessions' folders.
- *every write-access node*: keep out of `.agent/`; put what the run produces
  in `artifacts/`; change project files only where that is the task; and
  **never revert, restore, reset, stash or delete a change it did not make** —
  added after a running session's coder "restored" five files somebody was
  editing in the checkout it shared with them.

---

## How it works

### Phase 1 — designing (hand-written, fixed)

```
  START → discussor ⇄ researcher          (when research is on)
          discussor → human ──┬── plain text ──→ discussor
                              ├── /plan ───────→ planner
                              └── /exit ───────→ END
  planner → human ────────────┬── /approve ────→ designer
                              ├── /revise ─────→ planner
                              └── /discuss ────→ discussor
  designer → writer → validator ──┬── valid ────→ END
                                  ├── problems ─→ designer   (repair loop)
                                  └── gave up ──→ human
```

**The research agent.** Before it asks you anything, the discussor can send the
**researcher** up to three questions that its suggestions depend on — the
standard datasets and splits, the baselines and the numbers they get, the
usual settings, what one run costs. The researcher answers from the literature
and the Hugging Face Hub, with a source for every finding, and the discussor
then asks you its questions with suggestions that cite them. One round per
answer you give, three per session. Each round is saved to
`<session>/research/round-N.md` (Files tab).

- **On or off** per session: the New session dialog's *Let the discussor ask
  the research agent first* box (ticked by default), `"research": false` in
  `config.json`, `AGENT_RESEARCH=0`, or `--no-research`.
- **Runs on antigravity** (`gemini-4-argon-medium`) by default, for its built-in
  web search; change the `researcher` role like any other. If it cannot start
  (agy not installed or signed out), the discussion goes on without it.
- **Its skill** is `skills/research/`: `research_tools.py`, one script with
  `papers` (Hugging Face paper search: fast, gives arXiv ids), `arxiv`
  (arXiv's own search: slow and rate-limited, so the fallback), `paper` (an
  abstract, code link and URLs), `cites` (OpenAlex citation counts, only when
  its record's title matches — LoRA's does not), `datasets` and `models` (the
  Hub); and `SKILL.md`, how to use them. Try one:
  `python skills/research/research_tools.py papers "lora linear probe"`.
- **Cost:** a round is a few minutes — 7 in a measured run, with 9 web
  searches and 7 lookups. A faster model for the `researcher` role
  (`gemini-3.8-flash-medium`) shortens it.

**The discussor** is a short persona plus a skill:
`skills/experiment-design/`, the checklist it works through (claim, code,
design, run, analysis, checks, gates). Edit the persona in the Design agent
tab, and the skill in its folder -- see **Skills** below.

### Skills

`skills/` at the top of the repository holds shared know-how, one folder per
skill with a `SKILL.md` -- the format Claude Code and antigravity use, read as
plain files by every provider (Claude Code is given the folder with
`--add-dir`; it is the one thing outside a session's own folder the models are
pointed at, read-only). It ships with `experiment-design`, `research` and
`acl-paper`; adding one is adding a folder -- see `skills/README.md`.

Every node -- the design agent's discussor, researcher, planner and designer,
and every node of a designed agent, the orchestrator included -- has a
**skills** setting, a dropdown in the UI (the **Design agent** tab for the
first, **Configure** for the second):

| choice | what the node is sent |
| --- | --- |
| No skills | nothing |
| Auto-discover | every skill's name, description and `SKILL.md` path; it reads the ones its task needs |
| Selected skills | the ones you tick, in full |

Skills are added to the first prompt of each conversation, read from the
folder at that moment -- so editing a skill applies without redesigning the
agent. The defaults are `skills/suggestions.json`: per design-agent node, and
per role in a designed agent (orchestrator, worker, runner, validator,
paper_writer, ...). The designer is shown every skill and starts each node
from those defaults; it can suggest others for a node, and you can change any
of them at the design review.

`writer` and `validator` call **no model**. The designer already produced the
folder as data, so `writer` is `json.dump` — which makes "no `exec`, no
`eval`, no generated Python" structural rather than a promise.

### The agent it builds: a hub, run by the discussor

By default (`design_topology: "hub"`) every designed agent has the same shape,
built by code from the approved plan:

```
                 ┌──────────── orchestrator ────────────┐   (the discussor, carried on)
                 │   decides every turn what happens next │
        dispatch │          validate │        ask │       │ finish
                 ▼                   ▼            ▼       ▼
   s1_… s2_… s3_… s4_…           validator      review    END
   (one per plan step)          (read-only)    (you)
                 └─────── every node reports back to the orchestrator ───────┘
                                  paper  (an ACL-style paper, last)
```

- **The orchestrator is the discussor.** The same conversation: once you
  approve the design, the discussor's provider conversation is continued with
  the orchestrator's instructions ("the discussion is over; you now run it"),
  so it knows everything that was discussed. The `orchestrator` role runs
  wherever the discussor ran unless you set it yourself (Runs on). It is
  read-only.
- **One validator** checks every step's work when the orchestrator sends it
  (its rule: validate each piece of work before relying on it). It reads the
  files itself and, when work fails, names the node where the fault
  **started** (`suspected_origin`) with its evidence.
- **Root-cause routing.** The orchestrator sends the fix to that node, not to
  the one just checked -- a wrong number in step 3 that comes from step 1's
  data goes back to step 1, with the validator's findings in its
  instructions. Later steps are marked **stale** and run again in order once
  the fix passes.
- **The paper.** When every step has passed, the orchestrator dispatches
  `paper`, which writes an ACL-style paper from the step reports and the
  results (see below), and has it validated. The run cannot finish before.
- **Guards, in code:** `max_turns` decisions per run (80), `max_visits` per
  node (8), `max_failures` in a row on one node (3) -- each asks you rather
  than looping on. **Gates** (`gate: true` on a plan step) are enforced by the
  orchestrator: it asks you to `/approve` before the step's node first runs.

#### Context: what the orchestrator keeps, and what it is sent

It keeps a **step book**, not transcripts: each step's status and, once
validated, its **final report** (every work node writes one when it finishes
its step -- what it did, results with file paths and numbers, deviations --
saved to `artifacts/reports/<node>.md`). Not the logs, not the diffs.

- **Validation gets the detail**: the step and its check, the node's output
  and report, the files its turn changed, the end of every command's output,
  and read access to the workspace. **After a pass it collapses** to one line:
  the evidence stays in the validator's own record. After a failure the
  orchestrator keeps the short problem, origin and fix until the step passes.
- **Pull on demand.** The orchestrator can ask for any node's `report`,
  `output`, `changes`, `log`, `prompt` or `turn` (`include`), for itself
  (`inspect`) or to pass on to the node it dispatches or to the validator --
  this is how it decides which node's context a turn needs.
- **Budgets come from the model**, not small fixed caps: up to about half of
  the model's context window per prompt (the window Codex reports, else a
  table: Claude 200k tokens, Gemini 1M, GPT-5.x 272k). Clipping is only a
  safety net for a huge single item, and keeps a log's head and tail.
- **Its conversation rolls** to a fresh one when it is 70% full
  (`roll_percent`), or when the provider changed: the new one is briefed from
  its **memory**, `<session>/orchestrator/memory.json` -- the thread, what
  was agreed, its notes, the step book, its last decisions and every
  iteration -- which survives runs, iterations and restarts.

The **Orchestrator** tab shows that memory: the step book with each node's
status and report, its notes, its last decisions, earlier iterations, and how
full its conversation is.

#### The end of a run

The orchestrator's `finish` is the review the supervisor used to write --
verdict `done`, `improve_experiment`, `improve_agent` or `stop`, a summary and
findings -- with everything it saw during the run behind it. Then, by the
`supervisor` setting: `manual` asks you (`/accept`, `/replan [what]`,
`/redesign [what]`, `/finish`, or `/continue [what]` to keep working); `auto`
acts on the verdict up to `max_iterations`; `off` stops. A re-design continues
the same conversation, so `/discuss` during it talks to the orchestrator that
just ran the experiment.

#### The paper

The `acl-paper` skill (`skills/acl-paper/SKILL.md`), selected for the paper
node by default: LaTeX in the official ACL style
(`acl.sty` and `acl_natbib.bst` fetched from `acl-org/acl-style-files`; a
plain two-column article, said so, when offline), `references.bib` with only
papers confirmed by the research agent or a lookup, tables and figures
generated by scripts from the result files, and `paper.md` to read without
LaTeX. The body keeps to 8 pages with the required **Limitations** section;
the **appendices are unlimited** and hold the full results (every
configuration, seed and run, failed ones included), the full experimental
settings, how to reproduce it, extra analyses, and the run record (the
approved plan, deviations, each step's report, the validator's findings).
`main.pdf` is compiled when `tectonic` or `pdflatex` is installed; otherwise
the folder is ready to upload to Overleaf. All of it is in
`<session>/artifacts/paper/`.

#### The older shape: a pipeline

`--design-topology pipeline` (or `"design_topology": "pipeline"`,
`AGENT_DESIGN_TOPOLOGY=pipeline`) designs the earlier shape -- each work node
followed by a checker that can only send work back to the step it watches,
and a separate supervisor after the run. Agents designed before hubs keep
running as they are. The two sections below describe it.

### How the designer builds the agent

For a hub, the graph is fixed by code and the designer decides what each node
is told: per node `instructions` (what to watch in this experiment -- for the
orchestrator, which step is expensive; for the validator, what to check
hardest), a `tier` and extra `outputs`, plus the agent's `task_brief` and any
`plan_issues`. For a pipeline, as follows.

The approved plan is the contract, and four rules hold it to that. They are
checked by code, not asked for in a prompt:

| goal | rule |
| --- | --- |
| follow the plan exactly | **one work node per top-level plan step**, created by code with a fixed name and owning exactly that step. A step with substeps is done **one substep per turn**, each in a fresh conversation and each checked against its own `check` before the checker sends the node on (`next`) to the following one. The designer cannot rename, merge, split or drop a work node, or add work the plan does not have. A step that is the wrong size goes in `plan_issues`, and `/revise` at the design review changes the plan. The planner is held to at most six top-level steps, so an agent is about a dozen nodes -- a four-stage plan is 9. |
| context per agent | each node is given only the outputs of the nodes it `reads` -- by default the nodes that lead straight into it. A checker's redo is shown to a worker as *why it came back*, not as input. |
| cheaper models | each work node has a `tier`. `light` runs as the `coder_light` role (gpt-5.6-terra by default); its checker still runs as `checker`. |
| audit | every work node is in some checker's `reads`; the checker is shown the files that node's turn changed (recorded by code, not reported) and must return `evidence`. |

How a design is made:

1. Code builds a **starting design** from the plan (`agent/bootstrap/blueprint.py`):
   the fixed work nodes, a read-only checker per stage (several for a stage
   of more than six steps -- steps are never merged), one `review` human node.
   It is run through the real validator before the designer sees it.
2. The designer returns the whole design in the same shape: the whole
   `graph.json` -- loops, routers, branches, gates -- and a short spec per
   node: `role`, `steps`, `tier`, `reads`, `instructions`, extra `outputs`,
   human `commands`, plus `plan_issues`.
3. Code writes every prompt and output field from that (`agent/bootstrap/spec.py`).
4. A rejected design is repaired from where it stands; if the attempts run
   out, the review stop shows the last agent that passed.

The design review shows a row per node: its step, role (which decides the
model), access, how many nodes it reads, and which checker checks it.

### The supervisor: review, then go round again (pipeline agents)

After every run of a pipeline agent, a supervisor (role `supervisor`,
read-only) reviews it -- a hub agent's orchestrator does this itself: the
brief, the plan and its checks, every node visit with the model that ran it
and the route it took, each node's last outputs and checkers' evidence, the
files under the artifacts directory, and earlier iterations' reviews. It
opens the result files itself. Its verdict:

| verdict | next |
| --- | --- |
| `done` / `stop` | finish |
| `improve_experiment` | **re-plan**: the planner gets the review as `/revise` feedback, then design and run again |
| `improve_agent` | **redesign**: the designer starts from the agent that just ran, with the findings, then run again |

`supervisor` in config, `--supervisor`, `AGENT_SUPERVISOR`, or the New
session dialog:

- `manual` (default) -- you see the review and answer `/accept`,
  `/replan [what]`, `/redesign [what]` or `/finish`;
- `auto` -- it acts on its own verdict, approving the new plan and design
  itself, up to `max_iterations` (default 3, `--max-iterations`);
- `off` -- one run, no review.

Each iteration is kept under `<session>/iterations/<n>/`: the plan, the
agent, the brief, that run's timeline, the report the supervisor read and
its review with the decision.

### What is recorded, for auditing a run

Under `<session>/activity/`:

- `<node>/NNNN.json` per turn: the exact prompt and instructions sent, the
  provider's events (commands, files, messages), token usage, the **whole
  output**, the **provider and model** that ran it, and the **conversation
  id** -- every earlier turn with the same id was in that model's context.
- `timeline.jsonl`: one line per node visit, in order -- node, model,
  conversation, route taken, seconds, output tokens; and for a human node,
  what the person answered and where it went. `GET /api/sessions/<id>/timeline`.

In the browser: the **Timeline** and **Iterations** tabs show the same
records, and **Files** shows the code the session changed in its workspace,
its plan, agent and reports, and its results — and nothing outside the session
folder.

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

**"Out of credits" or a usage limit** — on every provider, the run waits it out
rather than dying: until the reset time when the provider names one ("try
again in 2 hours", "resets 6pm"), otherwise 1, 2, 4, 8, 16 minutes and then every
30 minutes, for up to 24 hours. Top up and it carries on by itself. Pause or
Stop ends the wait at once; your progress is saved. Tune with
`credit_wait_hours` in a role's `options`.

**Delegating instead of waiting** — per role, and only if you choose: the web
UI's **Runs on ▸ If its provider runs out of credits, delegate to**, or
`--delegate coder=antigravity:claude-opus-5-5-high`. That role's turns then
move to the delegate, as a fresh conversation, the moment its provider is out
of credits.

**Leaving a run unattended** — a run lives in the server, not the browser:
close the tab and it keeps going. Keep the server itself alive (`screen`,
`tmux`, `nohup`). If the server stops anyway — a crash, a restart to pick up new
code — every session that was running carries on when `python main.py web`
starts again, from its last finished node (`--no-resume` to skip that).

**"No GPU" / "cannot initialize NVML" on a machine that has GPUs** — the node
runs on Codex, whose sandbox hides the GPUs. Tick **GPU** under the node's
**Runs on** (web UI), or in `config.json`:
`{"roles": {"coder": {"options": {"gpu": true}}}}`. Then check which GPUs runs
are given: they inherit `CUDA_VISIBLE_DEVICES` from the server's environment.

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
python tests/test_storage.py     # session folders, --session-dir, brief.txt
python -m pytest webui/tests -q  # the web UI: HTTP, SSE, and editing
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
