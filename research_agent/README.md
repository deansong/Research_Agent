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
| **planner**, the study | no | no | **the brief you approved**, goal, constraints, assumptions, decisions, the current hypotheses, rubric and protocol, reviews, evidence (one line each) |
| **planner**, the steps | no | no | the brief, **the study it just wrote** (hypotheses, rubric, protocol), goal, constraints, assumptions, decisions, reviews, the current steps, and what each step's subagent will be told |
| **designer**, the team | no | no | **the steps** (title, kind, order, criteria, detail), the rubric, the constraints, the current team, the skills catalogue (to choose for nodes) |

Before anything is designed, you and the discussor agree a **brief**. Once the
research is in, the orchestrator hands the discussor everything the team
found -- the researcher's reports, its answers, the evidence. The discussor
explains it in general terms and drafts the brief: what to build or test, the
scope, the main choices. You revise it in conversation and approve it, and
only then is the design made, from that brief. The researcher's full report
goes to the discussor, not the planner.

The design is three calls, run back to back, each with one job and its own
prompt: the **planner** writes the study -- hypotheses (none for a pure build),
rubric and protocol -- then, from it, the steps; the **designer** chooses the
team of nodes that carries the steps out. Each step is written as a subagent's
whole context, and a fast **reader** model, given only that context, lists
what it would have to ask; its questions send the steps back
(`[design] reader` in `bootstrap/config.toml`). You approve the whole design
once. A change starts at the part it touches and redoes the parts after it:
a change to the team alone goes straight to the designer. Each part, revised,
is given whole; what it leaves out goes.

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
| rubric | R1… | the measurement plan: criterion, measure, expectation (`threshold`: a prediction, never a bar), weight, and the one step that measures it (`assessed_at`) |
| protocol | | baselines, metrics, statistical test, seeds, stop criteria |
| plan | steps 1… | what, check, depends on, rubric ids, cost, gate, alternatives (other routes, taken in order at a dead end) |
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

## Fields: skills and voice

A run belongs to a research field, `nlp` by default (`--field`, or the New run
dialog). A field is a folder, `research_agent/fields/<field>/`:

- `field.toml`: `skills`, the skills the run's team may use (the designer's
  catalogue, and what a node can be given); `[roles]`, each built-in role's
  skills by default. A role not listed gets none: **no node discovers skills
  by itself** unless you set it to `auto`.
- `prompts/`: overrides of any default prompt, and `voice.md`, added to every
  role's instructions. NLP's tells the team to talk as ACL and EMNLP papers
  do (dev/test split, ablation, macro-F1, seeds), not in words the field
  rarely uses.

A new field is a new folder: its skill list, its roles' skills, its voice.

| role (NLP) | skills by default |
| --- | --- |
| discussor, planner, designer | none |
| orchestrator | research-orchestration |
| researcher | research |
| reviewers | scientific-critical-thinking |
| paper | acl-paper, ml-paper-writing |

## Configuration: models

`research_agent/bootstrap/config.toml` and `research_agent/execute/config.toml`
hold the defaults, with comments:

| role | default |
| --- | --- |
| discussor, orchestrators (both stages) | codex, gpt-6.1-sol |
| researcher (both stages) | antigravity, gemini-3.8-flash-high |
| planner, designer, reviewer | codex, gpt-6.1-terra, high effort |
| reader (the design's) | codex, gpt-6.1-terra, low effort |
| worker, writer, validator, paper | codex, gpt-6.1-terra |

A run copies what it starts with into `<run>/config.json`. Edit that file to
change one run; it is read again on `--resume`. For one start, the command
line overrides both:

```
--role designer=codex:gpt-5.6-sol:high     provider[:model[:effort]]; all=... for every role
--skills designer=experiment-design,statistical-power
--skills discussor=none                    none, auto (it reads what it needs), or names
--field nlp                                the research field (a new run)
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
| **dispatch** | -- (the orchestrator's model) | new for each run or retry, read-only | before a step starts: looks at what the work it depends on left on disk, names the exact files its owner reads (code checks they exist) and what else it must be told -- or what is missing, and the step does not start |
| **step owners** | each `worker` or `writer` node (an older design's `runner` runs as a worker): its instructions, skills, steps | one per attempt (`step05-a1`), continued when its jobs finish | writes the code in `work/`, launches jobs, writes `artifacts/step_05/` |
| **wait** | -- (code only) | none | polls the step's jobs and counts their GPU-hours |
| **validator** | its `validator` node | new for each verdict, read-only | checks, from the code, configs, logs and files, that the step was done **as told** and its results are **real**: `done`, `deviated`, `broken` or `dead_end`; records every measurement as a finding, against its expectation |
| **reroute** | -- (code only) | none | a confirmed dead end: takes the step's next planned alternative and tells you; with none left, the discussor proposes a new route |
| **replan** | -- (the orchestrator's model) | new, read-only | once you agree a new route: replaces every step not yet done (`orchestrator (replan)`) |
| **results** | -- (the discussor) | the discussor's conversation | once a step passes: tells you its results in the chat, by default, and the work goes on -- no gate, nothing waits |
| **reviewer** | -- (always there) | new, read-only | once every step is settled: reviews the study as a NeurIPS/ICML/ICLR reviewer |

```
orchestrator --run/retry 5--> dispatch --ready--> owner of 5 --done/dead end--> validator --done--> results --> orchestrator
                                                                                    --deviated/broken--> orchestrator
                                                                                    --dead_end--> reroute --alternative--> orchestrator
                                                                                                          --none left--> discussor --> you
discussor --replan (you agreed)--> replan --> orchestrator
                                                         --launched--> wait --jobs finished--> owner of 5 (same session)
                                      --missing--> orchestrator (missing again: discussor --> you)
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
- **What a step is judged on.** Fidelity, not numbers: was it done as its step and its dispatch told (the models, data, splits, settings and procedure -- a silent fallback, a subset or a skipped seed is a deviation), are its outputs complete and its numbers recomputable from the saved files, and is it clean (no final-test data early, nothing made up). The measurement-plan items taken at that step are recorded as findings, each against its expectation: "as expected", "not as expected", "no expectation" or "unclear". A result against expectation is a finding, never a failure.
- **Attempts.** A failed step is retried at most `max_attempts` times in all (default 3). Each retry starts a new owner session and passes it the validator's verdict.
- **Inputs.** A step starts only once its dispatch is ready: every file it names exists, and a step that depends on earlier work names the files it reads from it. A step that could not start uses no attempt; the second time in a row, you are asked.
- **Your go.** A step the plan gates waits for you (`/go`, or tell the discussor). `--confirm all` makes every step wait; `--confirm none` makes none wait.
- **Passing.** Only the validator's `done` passes a step. Before the validator looks, code checks that every file the owner lists exists, and that every job it names was launched for that step.
- **Budget.** The `--wall-hours` and `--gpu-hours` limits are measured from the job records. Going over stops the jobs and the step, and asks you what to do. Raise the limit in `execute.json` and resume to go on.
- **Rewind.** You can start over from any step that has run (the step table, or tell the discussor): it and every step that needs it go back to pending with fresh attempts; their outputs are kept in `artifacts/_rewound/`.
- **Amendments.** The goal, hypotheses and brief do not change here. A pending or failed step's detail, check or title, a rubric criterion, or a protocol part can be amended: by the discussor on its own when a step fails or is held on something that is not its research (a check no step can meet, a record only the system keeps, a wrong path or name), with another attempt for the failed step it fixes; anything else -- what a step tests, the rubric, the protocol -- only on your word (or the autopilot's). Nothing is amended once a verdict has judged it: a criterion after its step is checked, a protocol part after a step that follows it. Each amendment is logged in `execution.amendments` (from, to, why, by whom), the design is approved again as amended, and the paper's writer reports every one as a deviation. A larger change goes back through the bootstrap.

- **Routes.** An owner that finds its route cannot work however it is retried (access refused, divergence at every setting, it cannot fit the machine) reports `dead_end`; the validator confirms it from the files. The step then takes its next planned alternative at once -- its detail and check replaced, fresh attempts -- and you are told in the chat. With none left, the discussor proposes a new route for the rest of the study and asks you; on your yes (never the autopilot's) the replanner replaces every step not yet done with new steps, numbered on, owned by the team's nodes, each measurement still due taken at exactly one of them. A replan waits until no step is at work. Every change of route is kept in `execution.routes`, the design is approved again as changed, and the paper reports each dead end as a result.

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

`state/research.json` gains an `execution` section: one record per step, holding its owner, status (`replaced` once a new route took its place), route, attempts, sessions, artifacts, jobs, GPU-hours, verdict and findings; and `routes`, every change of route.

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
