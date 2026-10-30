# research_agent: the bootstrap and execution stages

Turns an idea into an approved research design: a goal, constraints, evidence
with checked sources, hypotheses, a rubric, an evaluation protocol, a plan, and
the design of the agent that will carry the plan out.

```
python -m research_agent bootstrap "Does LoRA rank matter for small-data sentiment classification?"
python -m research_agent bootstrap --resume ~/research_runs/20261024-0930-does-lora-rank-matter
python -m research_agent bootstrap --from ~/research_runs/20261024-0930-does-lora-rank-matter   # the next run, from its review
```

## The team

| role | talks to the user | keeps a conversation | sees |
| --- | --- | --- | --- |
| **discussor** | yes, the only one | yes: the whole chat, for the whole run | your words, and what changed in the research state since it last spoke |
| **orchestrator** | no | no: a new conversation each call | the research state, the requests the discussor passed on, the last result |
| **researcher** | no | no | the goal, constraints, its questions, the hypotheses and the evidence already found |
| **designer** | no | no | **the brief you approved**, goal, constraints, assumptions, decisions, evidence (one line each), the current design (hypotheses, rubric, protocol, steps, nodes), reviews, the skills catalogue (to choose for nodes; no skills of its own) |

Before anything is designed, you and the discussor agree a **brief**. Once the
research is in, the orchestrator hands the discussor everything the team
found -- the researcher's reports, its answers, the evidence. The discussor
explains it in general terms and drafts the brief: what to build or test, the
scope, the main choices. You revise it in conversation and approve it, and
only then does the designer design, from that brief. The researcher's full
report goes to the discussor, not the designer.

The designer writes the whole design in one call -- hypotheses (none for a
pure build), rubric, protocol, the steps, and the nodes that carry them out --
so the study and the agent are thought out together. Each revision gives the
whole design again; what it leaves out goes.

Every role's standing instructions are short (under 1,200 characters): who it
is, what it owns, and the few facts code relies on. The models are trusted to
do the rest.

This is the context management. Your conversation stays with the discussor.
It records what you settled in the research state, and passes work on as
clean requests. Every other node starts fresh, with a brief built from the
state, so nothing stale or chatty reaches it.

The discussor decides whether its answer goes back to you -- it answered a
question, or asked one -- or on to the team, with a request or your approval.
A new idea goes to research first: the discussor says what will be looked
into, and explains once the evidence is in, unless the idea is too unclear to
research. When it is passing on the orchestrator's message it always answers
you, so the two cannot loop.

The orchestrator decides what the team does next: research, the brief,
design, ask you, or finish. Code holds two gates: **designing** needs the
brief approved, and **finishing** needs the design approved -- one approval
for all of it (hypotheses, rubric, protocol, steps, nodes).
Asking for a change reopens the work, and a revision needs approving again. A design that passes
the code checks goes straight to you: no model judges whether it is "ready"
(one that did, seeing the design compacted, sent it back for 20 rewrites).

The team stops to ask you after 6 steps without hearing from you.

## The research state

`<run>/state/research.json` is the record of the study. `research.md` is the
same, readable.

| section | ids | what |
| --- | --- | --- |
| goal | | question, motivation, scope, deliverable |
| constraints | | compute, budget, deadline, data, models |
| assumptions | A1… | marked unverified, checked or false |
| evidence | E1… | claim, source, and `verified`: arXiv ids are checked with the research skill's tool |
| hypotheses | H1… | statement, falsifiable prediction, rationale, evidence ids, novelty, status |
| rubric | R1… | criterion, measure, pass threshold, weight, and the one step that completes it (`assessed_at`), where it is checked |
| protocol | | baselines, metrics, statistical test, seeds, stop criteria |
| plan | steps 1… | what, check, depends on, rubric ids, cost, gate |
| design | | the execution agent's nodes: role, steps, skills, access, instructions |
| decisions | D1… | what, why, by whom |
| open questions | Q1… | for the user or for research |
| risks | K1… | and mitigations |
| results | N1… | each piece of work: what was asked, what came back, its session |
| status | | the orchestrator's summary and next step |

Nodes never edit the state. They propose changes by id, and code checks the
ids and applies the changes, versioning every section. An approval records
the version you approved, so a later change shows as "not approved". You
approve the design as one: that stamps every part of it.
`state/history/` keeps every change: who made it, when, and what it was.

## In the terminal

The discussor's reply comes first. Then one line for each piece of work the
team does, and a status line:

```
[discussing | 4 evidence, 2 hypotheses, 3 plan steps | design (hypotheses, rubric, protocol, steps, nodes): v1, not approved]
```

| command | |
| --- | --- |
| `/state` | the whole research state |
| `/show hypotheses rubric` | some sections |
| `/approve [brief\|design]` | approve the brief, or the design, as it stands (or say so to the discussor) |
| `/sessions` | every model call of this run |
| `/usage` | the providers' usage limits |
| `/retry` | run a failed step again |
| `/trace on` / `/trace off` | print every model call's full input and output, or stop |
| `/exit` | finish; the run stays in its folder |

Every model call is announced as it starts and when it answers:

```
  > researcher: asking antigravity:gemini-3.8-flash-high (session researcher-001)...
  < researcher: answered in 512s
```

`--trace` (or `/trace on`) also prints each call's full input -- the brief,
and the role's standing instructions the first time -- its full output, and
what the model does in between: the commands it runs, the files it changes.
`--progress` prints only that in-between activity. Nothing is only on screen: every call's input and
output is kept in `<run>/rb/sessions/`.

Ctrl-C stops a run, and `--resume <run>` carries on. A step is saved in
`checkpoint.sqlite` as soon as it finishes.

## Prompts

Every role's instructions and the longer notes the code sends models are
Markdown files in `research_agent/prompts/` (see its README). To change one
for a single run, copy it to `<run>/prompts/<same path>`; for all your runs,
to a folder named by `RESEARCH_AGENT_PROMPTS`. The node window in the web UI
shows the instructions a node actually gets.

## Configuration: models and skills

`research_agent/bootstrap/config.toml` holds the defaults, with comments:

```toml
[skills]
dir = "skills"                      # a folder of <name>/SKILL.md, from the repository root
discussor = "auto"                  # shown every skill; reads the ones it needs
orchestrator = ["research-orchestration"]
researcher = ["research"]           # given these in full
designer = "none"                   # designs from the brief; its message lists every skill for the nodes

[roles.researcher]
provider = "antigravity"
model = "gemini-3.8-flash-high"     # antigravity's ids carry the effort level
```

| role | default |
| --- | --- |
| discussor | claude_code, opus |
| orchestrator | codex |
| researcher | antigravity, gemini-3.8-flash-high |
| designer | codex, high effort |

A run copies what it starts with into `<run>/config.json`. Edit that file to
change one run; it is read again on `--resume`. For one start, the command
line overrides both:

```
--role designer=codex:gpt-5.6-sol:high     provider[:model[:effort]]; all=... for every role
--skills designer=experiment-design,statistical-power
--skills discussor=none                    none, auto, or skill names
--skills-dir ~/my_skills
```

Names are checked when a run starts: an unknown role, node or skill, or a
missing skills folder, stops it with what is wrong.

### Long conversations

A conversation that is continued (the discussor's, the autopilot's, a step
owner's across its waits) starts afresh before it outgrows the model's
context window: when its last turn used more than 60% of the window, or the
server says the window is exceeded. The old one is kept as
`<session>-<provider>-NNN`, and the new one is handed the recent
conversation and the whole research state. Briefs keep long lists short:
every decision of yours (and the autopilot's), the team's 20 newest, open
questions, and the 10 newest answered; the rest stay in `state/research.md`.

### Backups

A role can name backups, for when its model runs out of credits or cannot be
reached:

```toml
[roles.discussor]
provider = "codex"
backup = ["codex_strata:strata", "claude_code:sonnet"]
```

The call moves to the next backup at once, and the model is benched until its
limit resets: the time the provider names, else an hour. A model at capacity
("Selected model is at capacity", the provider's servers full, not your
quota) is retried once, then benched for ten minutes. The last backup waits
for credits as usual. A conversation moved to a backup, such as the
discussor's, is handed over: the new model gets the recent conversation and
the whole state. In the web UI, each role's backups are under it in the Run
tab.

## A run's folder

```
~/research_runs/<date>-<idea>/
    config.json          the models and skills this run uses
    state/               research.json, research.md, history/
    rb/sessions/         every model call: discussor (one), orchestrator-001, ...
    research/            the researcher's working folder
    checkpoint.sqlite    where the run is, for --resume
```

To read any call afterwards:
`RB.list_sessions(home="<run>/rb")` and
`RB(session="designer-002", home="<run>/rb").history()`.

# The execution stage

Once the design is approved, the execution stage carries out the plan with
the design's nodes:

```
python -m research_agent execute --run ~/research_runs/<run> --gpu-hours 140 --wall-hours 24
```

The same command starts it and carries it on.

The design is not turned into code. Code reads it, checks it again, and
builds the graph from it: one graph node per step owner, plus fixed nodes.

| node | from the design | session | does |
| --- | --- | --- | --- |
| **discussor** | -- (the bootstrap's) | the bootstrap's conversation, continued | the only one who talks to you: what happened, verdicts, asks for your go |
| **orchestrator** | its `orchestrator` node's instructions and skills | new for each decision | picks the next move from those code allows |
| **step owners** | each `worker` or `writer` node (an older design's `runner` runs as a worker): its instructions, skills, steps | one per attempt (`step05-a1`), continued when its jobs finish | writes the code in `work/`, launches jobs, writes `artifacts/step_05/` |
| **wait** | -- (code only) | none | polls the step's jobs and counts their GPU-hours |
| **validator** | its `validator` node | new for each verdict, read-only | checks the step's files against its check and rubric items |
| **reviewer** | -- (always there) | new, read-only | once every step is settled: reviews the study as a NeurIPS/ICML/ICLR reviewer |

```
orchestrator --run/retry 5--> owner of 5 --done--> validator --> orchestrator
                                         --launched--> wait --jobs finished--> owner of 5 (same session)
             --skip 5--> orchestrator (artifacts/step_05/SKIPPED.md says why)
             --ask_user / a gated step or breakpoint / stop--> discussor --> you
             --done--> reviewer --> discussor --> you
validator --a breakpoint after the step--> discussor --> you
```

**Talk to the discussor at any time.** In a terminal, you can type while the
team works. The discussor answers on the side, in the same conversation, and
the work goes on: ask what a step does, how it went, or what a number means.
If you ask for something -- "stop after the smoke test, I want to look", "stop",
"also log the per-class counts" -- it goes to `<run>/inbox.json`. The graph
applies it at its next step. `/stop` also stops the running jobs at once.
`/steps`, `/jobs`, `/logs`, `/budget` and `/review` work at any time too.

**Breakpoints.** The work stops for you before a step starts, or after a step
is checked. Set one by asking the discussor, or with
`/break [before|after] <step> [off]`. The discussor also sets one itself when a
step's result is yours to judge, such as a tier or a costly branch. `/go`
carries on.

**The review, and the next run.** Once every step has passed or been skipped,
the reviewer reviews the study. By default it runs on codex with high effort,
has the peer-review skill, and can only read. It reads the files, not only
the reports, and writes a review in the form a major ML conference asks for:
a summary, strengths, weaknesses, questions, limitations, soundness,
presentation and contribution (1-4), a rating (1-10) and its confidence (1-5).
It ends with **requests** for the next run (must, should, could). The review
is kept in the state (`reviews`, V1...) and as `<run>/reviews/V1.md`.

The next run starts from it:

```
python -m research_agent bootstrap --from ~/research_runs/<run>
```

This makes `<run>-next` and carries the research state over, with the
review: the goal, constraints, evidence, hypotheses and their results, rubric,
protocol, plan, design, decisions and risks. The step table and the node calls
stay behind; the old folder keeps the files. Nothing stays approved. The
orchestrator works through the review's requests: the designer revises the
design for them, and you approve it again. The new run's owners can read the
earlier run's files.

Code holds what a model cannot change:
- **Order.** A step runs only once the steps it depends on have passed or been skipped. A step waiting on a failed one is not skipped around it.
- **What a step is judged on.** Its own check, plus the rubric criteria assessed at that step: each criterion is checked at the one step that completes it, never at the steps that only lead up to it.
- **Attempts.** A failed step is retried at most `max_attempts` times in all (default 3). Each retry starts a new owner session and passes it the validator's verdict.
- **Your go.** A step the plan gates waits for you (`/go`, or tell the discussor). `--confirm all` makes every step wait; `--confirm none` makes none wait.
- **Passing.** Only the validator's verdict passes a step. Before the validator looks, code checks that every file the owner lists exists, and that every job it names was launched for that step.
- **Budget.** The `--wall-hours` and `--gpu-hours` limits are measured from the job records. Going over stops the jobs and the step, and asks you what to do. Raise the limit in `execute.json` and resume to go on.
- **Rewind.** You can start over from any step that has run (the step table, or tell the discussor): it and every step that needs it go back to pending with fresh attempts; their outputs are kept in `artifacts/_rewound/`.
- **Frozen design.** The goal, hypotheses, rubric, protocol, plan and design do not change here. A changed plan goes back through the bootstrap and its approvals.

Long jobs. An owner launches anything long through the jobs helper, which picks free GPUs and runs the command detached:

```
python research_agent/execute/jobs.py --run RUN launch --step 5 --gpus 2 --name seed17 -- python train.py
python research_agent/execute/jobs.py --run RUN status
```

The graph waits on the job records, without calling a model. Ctrl-C leaves the jobs running; `execute --run` picks them up again by pid.

Access. A `write` owner runs commands on the GPUs and writes outside its workspace, so it gets research_backend's `full` access. Claude Code's `write` cannot run commands without asking. The validator is `read_only`.

| role | default (`research_agent/execute/config.toml`) |
| --- | --- |
| orchestrator | codex |
| worker, writer | claude_code, opus (idle timeout 20 min) |
| validator | codex |

Commands:

| command | what |
| --- | --- |
| `/steps` | the step table: status, attempts, GPU-hours, last verdict |
| `/go` | let the step waiting for you start, or carry on after a breakpoint |
| `/stop` | stop the running jobs and steps |
| `/break [before\|after] <step> [off]` | a breakpoint |
| `/review` | the reviewer's review |
| `/jobs`, `/logs <job or step>` | the jobs, and the end of their logs |
| `/budget` | hours and GPU-hours used, and the limits |
| `/state`, `/show`, `/sessions`, `/usage`, `/trace`, `/retry`, `/exit` | as in the bootstrap |

The run folder gains:

```
    execute.json         the execution's models and limits (edit, then resume)
    work/                the code: a git repository, committed after every owner turn
    artifacts/step_NN/   each step's outputs: what the validator checks
    jobs/                one record, log and exit code per job
    reviews/V1.md        the reviewer's review
    inbox.json           what you said while the team worked, until the graph takes it
```

`state/research.json` gains an `execution` section: one record per step, holding its owner, status, attempts, sessions, artifacts, jobs, GPU-hours and verdict.

## The web UI

`python -m research_agent web` shows every run in one page. The discussor is
always there to talk to, also while the team works, and the research state has
a tab and a window of its own. See [web/README.md](web/README.md).

## The code

```
research_agent/
    state.py            the research state, its operations, rendering, history
    run.py              one run's model calls: briefs, schemas, checks, retries
    cli.py              the command line
    inbox.py            what the user says while a graph works
    web/                the web UI (see web/README.md)
    bootstrap/
        config.toml     the defaults: models and skills
        config.py       reading config.toml, <run>/config.json and the flags
        schemas.py      what each node answers with
        prompts.py      each role's standing instructions
        views.py        each node's brief, built from the state
        nodes.py        the five nodes: one research_backend call each, answers checked
        verify.py       checking arXiv sources
        carry.py        starting a run from a reviewed one (--from)
        graph.py        the LangGraph graph, gates and limits
        session.py      start, answer, approve, retry, resume
    execute/
        config.toml     the defaults: models and limits
        config.py       reading config.toml, <run>/execute.json and the flags
        schemas.py      what each node answers with
        prompts.py      each role's standing instructions
        views.py        each node's brief
        nodes.py        the design read as nodes; the orchestrator, owner, validator, discussor
        jobs.py         background jobs on free GPUs (also a plain script)
        graph.py        the LangGraph graph built from the design
        session.py      start, answer, go, stop, resume
```
