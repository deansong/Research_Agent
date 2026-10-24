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

The orchestrator decides what the team does next. Code holds the gates:
- **planning** needs the hypotheses, rubric and protocol approved;
- **designing** needs the plan approved;
- **finishing** needs the design approved.

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
| `/exit` | finish; the run stays in its folder |

Ctrl-C stops a run, and `--resume <run>` carries on. A step is saved in
`checkpoint.sqlite` as soon as it finishes.

## Which model does what

| role | default |
| --- | --- |
| discussor | claude_code, opus |
| orchestrator | codex |
| researcher | antigravity, given the `research` skill |
| planner | codex, high effort, given experiment-design skills |
| designer | codex |

To change them, pass `--role planner=codex:gpt-5.6-sol:high` (or
`all=claude_code:sonnet`), or edit `<run>/config.json`. A run keeps the roles
it started with.

## A run's folder

```
~/research_runs/<date>-<idea>/
    config.json          the roles
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
    config.py           roles: provider, model, effort
    cli.py              the command line
    bootstrap/
        schemas.py      what each node answers with
        prompts.py      each role's standing instructions
        views.py        each node's brief, built from the state
        nodes.py        the five nodes: one research_backend call each, answers checked
        verify.py       checking arXiv sources
        graph.py        the LangGraph graph, gates and limits
        session.py      start, answer, approve, retry, resume
```
