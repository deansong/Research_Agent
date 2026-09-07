# The agent folder format

An agent is a directory containing two JSON files. No Python, no code
generation — `kind` selects a node template that already exists in the
codebase, and everything else is configuration.

```
graph.json    topology: which nodes exist and how control flows
nodes.json    per node: which backend, what instructions, what prompts
```

The shipped example is `agent/builtin_agents/default/`, which is the original
four-role agent expressed in this format. It is also the format's acceptance
test: `tests/test_graph.py` builds a graph from it and asserts the same things
the hand-written version had to satisfy.

---

## graph.json

```json
{
  "format_version": 1,
  "name": "reviewer",
  "description": "One-line summary shown by --explain.",
  "entry": "collect",

  "nodes": [
    {"name": "collect", "kind": "agent"},
    {"name": "check",   "kind": "agent"},
    {"name": "human",   "kind": "human"}
  ],

  "edges": [
    {"from": "collect", "to": "check"}
  ],

  "branches": [
    {"from": "check", "route_on": "verdict", "default": "__end__",
     "cases": [
       {"when": "clean",  "to": "__end__"},
       {"when": "issues", "to": "human",
        "ask": {"purpose": "review", "resume_to": "check",
                "question": "Found: {out.check.summary}. Fix them?",
                "context":  "{out.check.detail}"}}
     ]}
  ]
}
```

| key | meaning |
| --- | --- |
| `entry` | which node runs first |
| `nodes` | a **list**, so duplicates are detectable |
| `edges` | unconditional: after A, always B |
| `branches` | conditional: switch on one enum output field of a node |
| `"__end__"` | as a target, finishes the run |
| `ask` | required on any transition **into** a human node; forbidden elsewhere |

`ask.resume_to` is where a plain-text answer goes — normally the node that
asked the question.

### Branching has no expression language

Every node writes one string into a `route` channel, and every conditional
edge is a table lookup on it. `route_on` must name a field declared with
`"type": "enum"`, which is what lets the validator prove that **every possible
value has somewhere to go** — you cannot ship a router whose fourth action
falls off the end.

---

## nodes.json

An **object keyed by node name** (pleasant to hand-edit), with one entry per
node in `graph.json`. The loader checks the two name sets match, which also
catches a duplicate JSON key silently collapsing.

### An `agent` node

```json
"check": {
  "backend": "reviewer",
  "access": "read_only",
  "instructions": "You are a code reviewer. Report problems, do not fix them.",

  "output": [
    {"name": "verdict", "type": "enum", "choices": ["clean", "issues"], "required": true},
    {"name": "summary", "type": "string"},
    {"name": "detail",  "type": "string"}
  ],

  "prompts": {
    "first": "Review this:\n\n{out.collect.files}\n\nBrief:\n{task_brief}",
    "next":  "The human said:\n\n{last_answer}\n\nRe-check."
  },

  "capture":  ["git_status", "git_diff"],
  "record":   "{out.check.summary}",
  "announce": "reviewing...",

  "thread_key": "",
  "refresh_on": "",
  "bump": []
}
```

| key | meaning |
| --- | --- |
| `backend` | a **role name**, resolved through the normal config stack — so `--backend-role reviewer=codex:gpt-5.4` configures a role that only exists inside this folder |
| `access` | `none`, `read_only` or `write`. Checked against the backend's capability **before anything is constructed** |
| `output` | becomes a Pydantic model at runtime; its JSON Schema constrains the reply |
| `prompts` | see the one rule below |
| `capture` | asks for `git_status` / `git_diff` after the turn. A node may not declare these as output fields |
| `record` | a line to append to the transcript |
| `announce` | printed while the node runs |

### The one rule about prompts

> `prompts.first` is used when this node has **no conversation yet**, or when
> the counter named in `refresh_on` has **changed** since this conversation
> last ran. Otherwise `prompts.next`.

That single rule covers both interesting cases. Send a long briefing once and
short follow-ups after — a large token saving on a long task.

`thread_key`, `bump` and `refresh_on` are three names for one idea, and the
executor in `default/` is the example to read:

```json
"executor": {
  "thread_key": "{out.planner.workstream}",
  "refresh_on": "plan_revision"
},
"planner": {
  "bump": ["plan_revision"]
}
```

`thread_key` gives the executor **one conversation per workstream** rather than
one overall. `bump` on the planner increments a counter every time it produces
a plan; `refresh_on` on the executor means "when that counter moves, resend the
whole plan instead of just the next task". Together they are the declarative
form of what used to be a hand-written generation check.

### A `human` node

```json
"human": {
  "commands": [
    {"name": "fix",  "to": "check", "purposes": ["review"],
     "summary": "Have another go", "sets": {"note": "{argument}"},
     "argument": "[what to focus on]"},
    {"name": "exit", "to": "__end__", "aliases": ["quit", "q"],
     "summary": "Stop here"}
  ]
}
```

Only the commands are configurable. Routing is not, deliberately:

- plain text → back to whoever asked (`ask.resume_to`)
- a valid command → that command's `to`
- anything else → ask again

Making routing configurable would let a folder describe a human node you
cannot escape from. `/help` is generated from these entries, so it can never
drift from what the agent actually accepts.

`purposes` gates a command by the `purpose` of the question being asked, so
`/fix` only appears when there is something to fix. `sets` writes values
readable later as `{var.<name>}`. Always give the human a way to `__end__`.

---

## Placeholders

These, and nothing else:

| | |
| --- | --- |
| `{task_brief}` | the job this agent was started with |
| `{transcript}` | the conversation so far |
| `{last_answer}` | what the human last typed |
| `{repo_path}` | the repository path |
| `{artifacts_dir}` | where this run should put what it **produces** |
| `{out.<node>.<field>}` | another node's output field |
| `{var.<name>}` | a value set by a human command |
| `{argument}` | only inside a command's `record` or `sets` |

> **Idiom worth knowing.** An `ask.question` made up of nothing but a
> placeholder renders blank whenever that field is empty, and the human gets a
> generic "Your input is required." Always append a static sentence:
> `"{out.discussor.question}\n\nAnything to add? Type /plan when ready."`
> The shipped `default/` agent does exactly this, after a live run where it
> did not.

A placeholder that cannot resolve yet renders as **empty**, not an error —
`{out.executor.summary}` legitimately has no value before the executor has
run. The cost is that a semantically wrong prompt validates clean, so use
`--explain` to see what each prompt actually looks like.

Note that `{ "json": "example" }` inside a prompt is left completely alone.
Only a narrow lowercase pattern is treated as a placeholder, precisely because
model-written prompts contain braces. This is why the renderer does not use
`str.format`.

---

## What the validator checks

LangGraph's own `compile()` catches unknown edge targets, a missing entrypoint,
reserved node names, and duplicate `add_node` calls. It does **not** catch:

- an unreachable node
- a graph that can never reach `__end__` (compiles fine, then dies with
  `GraphRecursionError` telling you nothing useful)
- a dead-end node (silently behaves as `END`)
- the node names `""` and `"A-B"`

So we check those, plus: the two files agreeing, `route_on` naming a real enum
field, every enum choice having somewhere to go, `ask` blocks matching human
nodes, unknown placeholders, reserved output fields, a `refresh_on` naming a
counter nothing bumps, and a node's `access` exceeding its backend.

Every problem is reported **together**, so one repair round can fix them all.

---

## What the design phase is told

All three design-phase nodes are pointed at machine-learning research, and each
at a different part of it (`agent/bootstrap/prompts.py`):

| node | what it is told to get right |
| --- | --- |
| discussor | the claim and what would **refute** it, the baseline, the data, the model, **the compute budget**, the metrics and how many seeds, what is out of scope, what the deliverable is |
| planner | `environment → data → code → run → analyse → report`; every step names the file it produces; reproduce the baseline as its own step; running and analysing are separate steps; pin the config and the seeds; keep the hours-long steps alone |
| designer | the expensive node is the one that *runs* something and does nothing else; results go under `{artifacts_dir}`, one file per config and seed, never overwritten; whoever decides what a number means is not whoever produced it |

Compute and metrics are the two a person leaves out, and the two that waste the
most time when they are wrong — the discussor asks about both.

**Each one also has an explicit way out.** A refactor, a tool, a bug fix is not
an experiment, and pointing three prompts at research is exactly how every bug
fix starts acquiring a baseline and a seed sweep. All three say so.

### The interview, and what it produces

The discussor works through two lists, one question per turn. The first settles
what the experiment *is* (above). The second settles what the **steps** are,
and its answers become plan steps and then nodes, close to one each:

| | |
| --- | --- |
| **data** | needed at all? downloaded or on disk? preprocessed into what file? |
| **environment** | what must be installed or built first, and what is already done |
| **code** | what has to be *written*, split by kind — training, evaluation, loading, analysis — and what already exists in the repo |
| **experiments** | which models, baselines, ablations, hyperparameters, how many seeds. Be pedantic: "a few configurations" becomes a graph that cannot say when it is finished |
| **checks** | for each of the above, how would you *tell* it worked |
| **gates** | which of them must a **person** approve before the run continues |

The last two are why the plan's steps carry `check` and `gate`:

```json
{
  "id": "4",
  "title": "Run the LoRA sweep over 3 seeds",
  "check": "8 files under results/sweep/, each with a top1 field",
  "gate": true
}
```

`check` is how you would tell the step worked, concretely enough that somebody
else could apply it — `"pytest tests/test_loader.py passes"`, not `"the code is
correct"`. The designer turns it into the node that applies it, so a vague check
produces a node that rubber-stamps. It's rendered into the owning node's own
prompt too, so the node doing the work knows how it will be judged.

`gate` is `true` only when a person must approve before the run goes on —
expensive or irreversible. Each gate halts the whole run until somebody comes
back to it, so three gates in a ten-step plan is a run that mostly sits waiting.

Both are editable in the web UI's plan panel, and both reach disk, which is what
`/approve` re-reads.

**A gated step that cannot reach a human node is a design error**, caught before
you ever see the graph (`bootstrap/nodes/validator.py::_gate_problems`). It
checks *reachability* from the node that owns the step, not "is there a human
node anywhere" — a graph almost always has one for its exit, and passing on that
basis would make the check decorative. Without it the failure is the worst shape
available: the run goes straight through, looks successful, and the approval
nobody asked for is discovered afterwards if at all.

### Two shapes, and which to use

The designer is told to choose, rather than defaulting to whichever example it
saw last:

**A fixed pipeline** when the plan already fixes the order — plain edges, with
a branch only where something can fail. The research skeleton below is one.
Preferred: every node is checkpointed and a person can read the graph.

**An orchestrator** when the *length* of the work is not known in advance —
"keep trying configurations until the eval passes". One `read_only` node routes
on its own `action` output and every worker's edge goes back to it. The shipped
default agent is exactly this, and its `graph.json` is in the prompt.

A branch takes **at most 8 cases** (`BranchSpec.cases`, `max_length=8`), so one
orchestrator can dispatch to at most seven workers. Past that, stage the graph
and give each stage a verifier — which is why this project uses verifiers per
stage rather than one central orchestrator over twenty nodes.

### Example nodes the designer can copy

A topology is not a design: `graph.json` says a node called `run_exp_a` runs an
experiment and says nothing about what its prompt must contain for that to
happen. So the prompt carries four complete `nodes.json` entries
(`prompts.py::RESEARCH_NODES`) — `write_code_a`, `run_exp_a`, `check_a` and the
`review` human node. The b, c, d triples are those with the names and steps
changed.

They are **validated**: `tests/test_bootstrap.py` runs them through
`NodeProposal` (the designer's own strict output model) and then the real
`writer._entry_for`, writes them to disk and validates the folder strictly. So
every `{out.x.y}` in them names a field node `x` really declares. An exemplar
with a broken placeholder would teach the designer to write broken
placeholders, and its repair loop would then fight the example it was handed.

Three details in them are the point:

- `write_code_a`'s `prompts.next` carries `{out.check_a.problem}`. Without it a
  `redo` repeats the mistake it was never told about.
- `run_exp_a` may not edit code; `check_a` is `read_only`. Three nodes, one job
  each, and the judge is not whoever did the work.
- `check_a`'s `prompts.next` is where the loop can **end** — it has its own
  conversation, so a second identical complaint becomes `blocked`.

### Branches that are not "did it work"

Three uses a pass/fail framing misses, all from how experiments really fail:

- **A resource failure is not a code failure.** Out of memory, out of disk, a
  run past its time budget — that goes to a node that shrinks the
  configuration, *not* to the node that wrote the code. The code is correct;
  rewriting it cannot free memory, and the loop cannot converge because nothing
  in it changes the thing that is wrong.
- **A sweep is a loop.** The runner writes one result, then a node asks "is
  anything left?" and branches back. That node decides by **looking at
  `{artifacts_dir}`** — counters exist (`bump`) but cannot be rendered into a
  prompt, so the filesystem is the only progress a prompt can read.
- **An exit from the redo loop.** A `redo` with no way out spins to the step
  limit and dies with nothing. A node keeps its own conversation across calls
  (`prompts.next`, same thread), so the checker remembers what it already
  rejected — that conversation is the only place an attempt count can live.

---

## The research skeleton

Work in this project is computer-science research, so the designer is shown a
starting shape for it, alongside the worked example
(`agent/bootstrap/prompts.py::research_skeleton`):

```
setup_env -> inspect_data -> write_code_a -> run_exp_a -> check_a
                                                            |
       ok -> write_code_b -> run_exp_b -> check_b -> report -> review
     redo -> write_code_a          (carrying {out.check_a.problem})
  blocked -> review
```

One **write / run / check** triple per experiment *type*, chained
sequentially — `check_a`'s `ok` goes to `write_code_b`, never to two nodes at
once, because the validator refuses fan-out. `inspect_data` is dropped when
there is no dataset; the code node writes its own tests, so there is no
separate test-writing node.

Two details in it are load-bearing:

**The retry is judged by a third node.** `write_code → run_exp → check →
write_code`, not `write_code → run_exp → write_code`. A run node deciding
whether its own run was any good is `self_assessment`, which the validator
rejects at design time. `check_*` is `read_only` with no plan steps, which is
also exactly what the missing-verifier warning looks for — so the shape
satisfies both rules by construction.

**Experiment runs are on their own backend role.** `run_exp_*` declares
`"backend": "runner"` and `check_*` declares `"checker"`, separate from the
`"coder"` that writes code. Nothing configures those roles, so they fall back
to the default until you say otherwise:

```json
{
  "roles": {
    "runner":  { "provider": "codex", "model": "gpt-5.4-mini" },
    "checker": { "provider": "codex", "model": "gpt-5.4-mini" }
  }
}
```

Until then the run prints
`[config] checker, runner not configured, using the default (codex/gpt-5.4)`
— because a silent fallback here would have you believing experiments run on a
small model while every turn goes to the big one.

It is a **skeleton, not a mould.** The designer is told to check it against
the plan first, drop what does not apply, add what the plan needs (a baseline
to reproduce, an ablation, a figure builder) and say in `rationale` what it
changed. A template that must be obeyed produces a graph shaped like the
example instead of like the work.

---

## Where output goes

A write-access node changes two very different kinds of thing, and conflating
them makes a mess of someone's repository:

| | goes in |
| --- | --- |
| **project changes** — source, its tests, its docs | the repository, as normal |
| **run output** — generated data, caches, reports, plots, logs, scratch | `.agent/sessions/<session>/artifacts/` |

The loader appends this rule to every `write` node's instructions itself, so a
generated agent cannot opt out of it, and the path is available to prompts as
`{artifacts_dir}`. When it is ambiguous, the rule says prefer the run
directory: an unwanted file there is thrown away with the session, an unwanted
file in the repository is not.

This exists because a real run did the other thing — it invented top-level
`artifacts/`, `configs/` and `docs/` directories plus a model cache in a
project that had none of them.

## When the provider runs out of credits

The run does not die. It waits, says so, and retries — so you can top up and it
carries on by itself:

```
    Out of credits. Waiting 60s, then retrying (3/20).
    Add credits and this will continue on its own.
    Ctrl-C to stop -- your progress is saved.
```

Twenty checks a minute apart by default. Tune it per role:

```json
{ "roles": { "executor": { "options": {
    "credit_wait_attempts": 40, "credit_wait_seconds": 30 } } } }
```

Provider-overload errors back off the same way, but briefly (5s, 10s, 20s, 40s)
since those clear on their own. A **genuine** error — a malformed request, a bad
schema — is raised immediately and never retried: retrying it just spends the
same money twice.

If it does give up, or you press ctrl-C, you get the resume command rather than
a traceback. Every completed node is checkpointed, so resuming re-runs only what
had not finished.

## When a node times out

A node that is asked to implement a lot in one turn can legitimately run for a
long time. The default budget is 600 seconds per turn; a write-heavy node
building something substantial will exceed that.

Give a role longer in `<repo>/.agent/config.json`:

```json
{ "roles": { "executor": { "options": { "timeout": 3600 } } } }
```

The key is the **role name the node declares as its `backend`**, which for a
generated agent is whatever the designer chose — check `nodes.json`.

If it still times out, the design is usually the problem rather than the
budget: one node has been asked to do a whole project. Split the work across
more nodes so each turn is a step rather than the entire job. `/graph` shows
you the shape you actually got.

## Working with folders

```bash
# Run one directly, skipping the design phase
python main.py run . --pre-build-agent default --task "add tests"

# See a folder's shape and every resolved backend, spending nothing
python main.py run . --pre-build-agent ./my-agent --explain

# Keep the agent this session designed
python main.py promote . my-reviewer
```

A designed agent lands in `.agent/sessions/<session>/agent/`. `promote` copies
it to `.agent/agents/<name>/`, which is **not** gitignored — commit it. Then
edit the JSON by hand: change a prompt, add a command, rewire an edge. Rerun
and it takes effect, with no code changed. If you break it, the validator tells
you exactly what and where before a single token is spent.
