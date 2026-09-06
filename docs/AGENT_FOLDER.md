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
