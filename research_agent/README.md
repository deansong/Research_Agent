# research_agent: the bootstrap stage

Turns an idea into an approved research design: a goal, constraints, evidence
with checked sources, hypotheses, a rubric, an evaluation protocol, a plan, and
the design of the agent that will carry the plan out.

```
python -m research_agent bootstrap "Does LoRA rank matter for small-data sentiment classification?"
python -m research_agent bootstrap --resume ~/research_runs/20261024-0930-does-lora-rank-matter
```

## The team

| role | talks to the user | keeps a conversation | sees |
| --- | --- | --- | --- |
| **discussor** | yes, the only one | yes: the whole chat, for the whole run | your words, and what changed in the research state since it last spoke |
| **orchestrator** | no | no: a new conversation each call | the research state, the requests the discussor passed on, the last result |
| **researcher** | no | no | the goal, constraints, its questions, the hypotheses and the evidence already found |
| **planner** | no | no | for `hypothesize`: goal, constraints, assumptions, evidence, decisions, current hypotheses, rubric, protocol. For `plan`: the hypotheses to test, rubric, protocol, constraints, current plan |
| **designer** | no | no | the approved plan, hypotheses, rubric, protocol, constraints, the skills catalogue |

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

The orchestrator decides what the team does next. Code holds the gates:
- **planning** needs the hypotheses, rubric and protocol approved;
- **designing** needs the plan approved;
- **finishing** happens once the design is approved: questions about carrying
  the plan out are left for the execution stage. Asking for a change reopens the work.

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
| rubric | R1… | criterion, measure, pass threshold, weight |
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
the version you approved, so a later change shows as "not approved".
`state/history/` keeps every change: who made it, when, and what it was.

## In the terminal

The discussor's reply comes first. Then one line for each piece of work the
team does, and a status line:

```
[discussing | 4 evidence, 2 hypotheses, 0 plan steps | hypotheses: v1, not approved | rubric: ...]
```

| command | |
| --- | --- |
| `/state` | the whole research state |
| `/show hypotheses rubric` | some sections |
| `/approve hypotheses rubric protocol` | approve them as they stand (or say so to the discussor) |
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

## Configuration: models and skills

`research_agent/bootstrap/config.toml` holds the defaults, with comments:

```toml
[skills]
dir = "skills"                      # a folder of <name>/SKILL.md, from the repository root
discussor = "auto"                  # shown every skill; reads the ones it needs
orchestrator = ["research-orchestration"]
researcher = ["research"]           # given these in full
planner_hypothesize = ["hypothesis-generation", "experiment-design", "statistical-power"]
planner_plan = ["experiment-design"]
designer = "none"                   # sees the skills catalogue anyway, to choose for the execution nodes

[roles.researcher]
provider = "antigravity"
model = "gemini-3.8-flash-high"     # antigravity's ids carry the effort level
```

| role | default |
| --- | --- |
| discussor | claude_code, opus |
| orchestrator | codex |
| researcher | antigravity, gemini-3.8-flash-high |
| planner | codex, high effort |
| designer | codex |

A run copies what it starts with into `<run>/config.json`. Edit that file to
change one run; it is read again on `--resume`. For one start, the command
line overrides both:

```
--role planner=codex:gpt-5.6-sol:high      provider[:model[:effort]]; all=... for every role
--skills planner_plan=experiment-design,statistical-power
--skills discussor=none                    none, auto, or skill names
--skills-dir ~/my_skills
```

Names are checked when a run starts: an unknown role, node or skill, or a
missing skills folder, stops it with what is wrong.

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
`RB(session="planner-002", home="<run>/rb").history()`.

## The code

```
research_agent/
    state.py            the research state, its operations, rendering, history
    cli.py              the command line
    bootstrap/
        config.toml     the defaults: models and skills
        config.py       reading config.toml, <run>/config.json and the flags
        schemas.py      what each node answers with
        prompts.py      each role's standing instructions
        views.py        each node's brief, built from the state
        nodes.py        the five nodes: one research_backend call each, answers checked
        verify.py       checking arXiv sources
        graph.py        the LangGraph graph, gates and limits
        session.py      start, answer, approve, retry, resume
```
