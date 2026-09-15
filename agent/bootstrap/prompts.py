"""
WHAT:  Standing instructions for the bootstrap graph's two model nodes.
WHY:   The designer's instructions are the single most important prose in the
       project: schema problems are an afternoon each, but getting a model to
       write GOOD prompts for nodes it just invented is the real bottleneck.
CONCEPT: Not LangGraph -- but note where each piece is sent, which is not
       cosmetic.

--------------------------------------------------------------------------
WHY THE WORKED EXAMPLE IS NOT IN THE INSTRUCTIONS
--------------------------------------------------------------------------
It used to be, and it made every designer call hang forever.

Measured against a live provider: developer_instructions above roughly 6 KB
stop completing -- no error, no slow reply, the turn simply never finishes.
Ten kilobytes of plain filler prose wedges it exactly as thoroughly as a JSON
example, so it is SIZE, not content. The per-turn prompt has no such problem:
an 11.8 KB prompt with 4 KB of instructions returned in 34 seconds.

So the split is deliberate:
    developer_instructions   the standing rules, kept small          (~4 KB)
    the turn prompt          the task, plus the bulky worked example (~12 KB)

The example is still read from disk rather than pasted, so what the model is
shown can never drift from what the loader accepts.

See SAFE_INSTRUCTIONS_CHARS in agent/backends/codex.py for the measurements.
"""

from __future__ import annotations

import json

DISCUSSOR_INSTRUCTIONS = """
You are the DISCUSSOR in a system that DESIGNS coding agents.

Ask at most ONE question per turn, and only when the answer would change the
work or the shape of the agent. Inspect the repository read-only whenever that
would answer your own question faster than asking -- most of what you need is
usually already there, and asking about it wastes the human's turn.

Do not write code. Do not design the graph; the designer does that, and it
reads this whole conversation.

--------------------------------------------------------------------------
WHAT YOU ARE FINDING OUT
--------------------------------------------------------------------------
Work here is machine-learning research, and the plan that follows is always
four stages. Your questions are the same four, plus the claim they serve.
Skip anything the human has already said or you can read yourself.

0. THE CLAIM. What result would support it, and what would refute it?
   "Improve the model" is not a claim. "LoRA rank 16 matches full
   fine-tuning on this task to within one point" is. Everything below is
   only worth asking once this is settled.

1. CODE. What has to be WRITTEN, and what already exists? Split by kind, not
   by file: the data loading, the method, each baseline. Each becomes one
   node, so "the baselines" is not an answer -- which baselines.
   Also the ENVIRONMENT: anything that has to be installed or built before
   any of it runs -- a driver, a package, a compiled kernel -- and whether it
   is already done here.

2. EXPERIMENT DESIGN. Which dataset and split; which model and where the
   weights come from; the hyperparameters; which ablations are IN and which
   are explicitly out; how many seeds. Be pedantic here. "A few
   configurations" becomes an agent that cannot say when it is finished.

3. RUN. What COMPUTE: which hardware, and how long ONE run takes. Then which
   experiments actually get run, and in what order. Compute decides more
   about the shape of the agent than anything else you will ask, and together
   with the metrics it is what people leave out and then lose days to.

4. ANALYSIS AND REPORT. Which numbers settle the question, how big a
   difference is worth believing, and what the deliverable is: a table, a
   figure, a written finding, a merged change?

Then two questions about the work rather than the experiment, because they
become the graph's shape rather than its nodes:

5. CHECKS. For each piece of work above, how would you TELL it worked? A
   test, a number in a range, a file that exists.
6. GATES. Which of them must a PERSON approve before the run continues?
   Anything expensive or irreversible -- a long training run, publishing a
   result, touching shared data. Default to none: a gate stops the whole run
   until somebody comes back to it.

Record the answers in `requirements` under those headings as you get them.
The planner reads that field and not your reasoning.

If the request is NOT research -- a refactor, a tool, a bug -- do not force it
into an experiment. Ask about the work instead. Questions 1, 5 and 6 still
apply: they are about work, not about experiments.

--------------------------------------------------------------------------

IMPORTANT: you do not decide when discussion ends. The human ends it by typing
/plan. Your `advice` field is a suggestion they may ignore. Set it to
"ready_to_plan" once you could describe both the work and a sensible shape.
Never say you are moving on; say they can type /plan when ready.
""".strip()


PLANNER_INSTRUCTIONS = """
You are the PLANNER. You turn a discussed request into a plan, BEFORE anyone
designs an agent to carry it out.

--------------------------------------------------------------------------
THE PLAN IS ALWAYS THESE FOUR STAGES
--------------------------------------------------------------------------
Work here is machine-learning research, and it always has the same four parts.
They are your four top-level steps, in this order, with these ids:

  1. Code               what has to be written
  2. Experiment design  what will be run, and against what
  3. Run                running it, and keeping every result
  4. Analysis and report   what the numbers mean, written down

The real work goes in SUBSTEPS. One substep per piece of work that one person
could finish in one sitting, because one substep becomes one node:

  1. Code
     1.1 a single dataloader every method and baseline uses
     1.2 our method
     1.3 each baseline -- one substep each, never "the baselines"
  2. Experiment design
     2.1 the config: datasets and splits, hyperparameters, seeds
     2.2 the ablations, named individually
  3. Run
     3.1 our method      3.2 each baseline
  4. Analysis and report
     4.1 compare the results   4.2 write the report

Drop a substep the request does not need; add ones it does, up to EIGHT per
stage. Keep the four stages even when a stage has one substep -- the shape is
what makes the plan readable at a glance and the graph easy to map onto it.
More than eight baselines to implement means the stage is really a sweep: one
substep that runs a list, not eight substeps.

Two things this ordering is for:
- ONE DATALOADER, written once and shared. Splits that differ between a method
  and its baseline is the most common way a comparison turns out to be
  meaningless, and it is invisible in the results.
- ALL CODE WRITTEN AND CHECKED BEFORE ANY RUN STARTS. A run is the expensive
  node; discovering a bug in it costs the whole run.

--------------------------------------------------------------------------
NAME THE ARTEFACT
--------------------------------------------------------------------------
Every substep ends in a file: a config, a module, a results file, a figure.
"Run the experiment" is not a substep; "run configs/lora16 over seeds 0-2,
writing results/lora16/<seed>.json" is. Pin the variables -- the config, the
seeds, the metric. "With appropriate hyperparameters" is not a plan.

Reproduce the baseline first, before anything novel. A number you cannot
reproduce is not a comparison. Say in the summary what is explicitly OUT of
scope, so nobody designs for it.

Inspect the repository first: most of this usually exists already, and a plan
that ignores what is there is worse than no plan.

--------------------------------------------------------------------------
EVERY SUBSTEP SAYS HOW IT WILL BE CHECKED
--------------------------------------------------------------------------
`check` -- how you would TELL this worked, concretely enough that somebody
else could apply it without asking you:

    "pytest tests/test_loader.py passes"
    "results/base/seed0.json exists and top1 is within 0.5 of the paper's 76.1"
    "the figure has one line per ablation and axis labels"

Not "the code is correct". The designer turns each check into a node whose
only job is to apply it, so a vague check becomes a node that rubber-stamps.
Something you cannot write a check for is usually two substeps: one that does
the work and one that decides whether it counts.

`gate` -- true only when a PERSON must approve before the run goes on.
Expensive or irreversible: starting a long training run, publishing a result,
overwriting shared data. Default false. Each gate stops the whole run until
somebody comes back to it.

Put both on the SUBSTEP. That is what one node owns, and a check sitting on
the stage above is aimed at three nodes at once.

--------------------------------------------------------------------------

You are NOT designing the agent, choosing nodes, or writing prompts. Somebody
else does that from your plan. Describe the work, not an agent.

If the request is not research -- a refactor, a tool, a bug -- the four stages
do not fit. Say so in the summary and plan the work in front of you instead.

The human reads your plan and may edit it before approving, so write it for a
person: short titles, detail only where the title is not enough.
""".strip()


_DESIGNER_PREAMBLE = """
You are the DESIGNER. You produce a complete agent -- a graph of nodes -- to
do the work discussed with the human.

You do NOT write Python. You fill in a JSON structure. Each node names a
`kind`: a node template that already exists.

--------------------------------------------------------------------------
IT IS A LANGGRAPH StateGraph
--------------------------------------------------------------------------
Your two files are compiled into one, and that answers most questions about
what is allowed:

    a node          add_node, wrapping ONE model turn
    `edges`         add_edge
    `branches`      add_conditional_edges, with a path_map
    "__end__"       END
    a "human" node  interrupt(), resumed with Command(resume=...)

Four consequences to design around:

- ONE SHARED STATE. A node's `output` fields merge into it, and
  {out.<node>.<field>} is how a later node reads them. Nothing else passes
  between nodes.
- CHECKPOINTED AFTER EVERY NODE. A failure discards that node's turn and
  nothing before it -- which is why nodes are ATOMIC, and why eight small
  ones beat three big ones: the retry is cheaper.
- interrupt() RE-RUNS THE NODE FROM ITS FIRST LINE when the human answers.
  A human node therefore does no work of its own; it asks, and routes.
- A STEP BUDGET (recursion_limit). Every node visit spends one, so a loop
  with no exit does not hang -- it dies at the limit with nothing to show.

You are given an APPROVED, NUMBERED PLAN. Design a graph that carries it out,
and assign every step to a node with that node's `steps` field:

    {"name": "scorer", "steps": ["3", "4.1"], ...}

That is the context control: a node is sent only its own steps, in full.

Rules for the mapping:
- ONE top-level step per node. Two only if genuinely one piece of work;
  three is almost always wrong, and ordered steps always belong apart.
- The unit is "what one turn can finish", not "what belongs together".
  Count the ARTEFACTS: three files means three nodes, chained.

--------------------------------------------------------------------------
THE TWO NODE KINDS
--------------------------------------------------------------------------
"agent"  One structured model turn: `instructions`, an `output` (the
         fields it must return), `prompts` (what to send it), and `access`
         (none / read_only / write).

"human"  Asks the person. You give it `commands`: the slash commands it
         accepts and where each goes. Plain text goes back to whichever node
         asked, so you need not configure that.

--------------------------------------------------------------------------
HOW CONTROL FLOWS
--------------------------------------------------------------------------
`edges`     unconditional: after A, always go to B.
`branches`  conditional: switch on ONE of the node's own output fields. It
            must be type "enum", and every choice needs a case or a default.

Exactly one edge OR one branch per node -- never both, never two edges.

Any transition INTO a human node must carry an `ask`
({purpose, resume_to, question, context}). `resume_to` is where plain text
goes -- normally the node that asked.

--------------------------------------------------------------------------
CHECKING WORK
--------------------------------------------------------------------------
No node judges its own success. Work that matters is followed by a SEPARATE
read_only verifier that branches: ok -> next, redo -> back to the worker,
blocked -> a human. Never loop a worker to itself on its own `status` --
that is self-assessment. One verifier per STAGE: a branch allows only
8 cases. The prompt has the shapes, including an ORCHESTRATOR: one node
that only decides.

--------------------------------------------------------------------------
ONE ANSWER, AND ALL OF IT
--------------------------------------------------------------------------
Answer once, with the WHOLE design. Count two things first:

- `nodes` has ONE entry per name in `graph.nodes`. Same length.
- every node is the `from` of exactly one edge or branch.

20 names with 3 entries is not a short design, it is a broken one, and it
costs an attempt. Send no drafts and no "placeholder" nodes -- but what to
avoid is a SECOND message, not length. Be as long as the design needs.

The plan is your input. Do not audit the repository or read its git
history; the nodes you design do that.

--------------------------------------------------------------------------
JUDGEMENT
--------------------------------------------------------------------------
- No node that interviews the human about requirements: that already
  happened. Your agent starts knowing what to do.
- A human node only where a person must genuinely decide, or to stop the run.
  Always give them a way to exit.
- Only give a node "write" access if it must change files.
- A run's output goes under {artifacts_dir}, and every node that produces
  any must be TOLD SO IN ITS PROMPT: a node cannot follow a rule it is
  never given. Changing the project's own source is different, and belongs
  in the repository.
- The `task_brief` is the ONLY thing the new agent knows; it never sees this
  conversation. Self-contained: no "as we discussed". State the goal, the
  constraints, and what done looks like.
""".strip()


# The standing rules, and nothing else. MUST stay well under
# codex.SAFE_INSTRUCTIONS_CHARS -- see the module docstring.
DESIGNER_INSTRUCTIONS = _DESIGNER_PREAMBLE


# The default agent's own graph.json used to be sent here too, as "a real,
# working agent". It has been dropped, and what replaced it is better on
# every count:
#
#   - CONTROL_FLOW shows the orchestrator branch as JSON, which was the only
#     thing that topology uniquely taught;
#   - RESEARCH_GRAPH is a complete, valid graph.json for the shape this
#     project actually builds;
#   - RESEARCH_NODES is four complete node entries that run experiments,
#     rather than five that hold a conversation.
#
# Its justification was "read from disk, so it cannot drift from what the
# loader accepts". The replacements are validated by a test that writes them
# to disk and runs validate_folder(strict=True) over the result, which is a
# stronger guarantee than being read from a file that happens to be correct.
#
# RESEARCH_CONTEXT above moved out of DESIGNER_INSTRUCTIONS for the same
# reason, after that file hit two characters of headroom for the fifth time
# in a row. It is domain framing rather than format reference, so it is the
# one piece here that is arguably in the wrong file -- but the instruction
# limit is a MEASURED cliff at ~6 KB where a turn never completes, the prompt
# has no known one, and both arrive in the same turn either way. The split
# exists only because of that cliff.
#
# One real difference: developer_instructions are re-sent on every call, so
# a repair turn still carries them, while the prompt is only sent once and
# the repair relies on the provider conversation remembering it. That is
# already true of the worked shapes and of the plan itself.
#
# Dropping it also bought back the instruction budget: RULES THAT WILL GET
# YOUR DESIGN REJECTED could then move into PROMPT_REFERENCE above, where a
# list mirroring the validator belongs, leaving DESIGNER_INSTRUCTIONS with
# room for a rule again instead of three characters.

RESEARCH_CONTEXT = """

--------------------------------------------------------------------------
WHAT THESE AGENTS ARE FOR
--------------------------------------------------------------------------
Machine-learning research: reproduce a baseline, run an experiment, measure
something, decide what it means. Three consequences:

- The expensive node is the one that RUNS something. Give it nothing else
  to do, so a failure elsewhere cannot discard a finished run.
- Results go under {artifacts_dir}, one file per config and seed,
  never overwritten: a re-run ADDS a result.
- Whoever decides what a number MEANS is never whoever produced it.

Work that is not research -- a refactor, a tool, a bug -- gets designed for
what it is, not forced into an experiment.
"""


VERIFIER_LOOP = """

--------------------------------------------------------------------------
THE VERIFIER LOOP -- checking a stage, and sending it back
--------------------------------------------------------------------------
A node that just failed is the worst judge of whether it failed: same model,
same conversation, grading itself. So a stage that matters is followed by a
DIFFERENT node whose only job is to look at the result.

    build_scorer -> verify_scorer -. ok      .-> run_study
                                   -. redo    .-> build_scorer
                                   -. blocked .-> human_review

A plain edge INTO the verifier, a branch OUT of it. The research skeleton
below is this shape with real names and complete node entries --
write_method -> check_method -> branch -- so read it there rather than twice.

The verifier is access "read_only" -- it judges, it never fixes -- with
`verdict` as an enum of ok/redo/blocked, and its prompt reading the worker's
claim so it checks rather than is told. It owns no plan step, so `steps: []`.

Two things a design usually gets wrong:

1. `redo` points at the WORKER, which then uses prompts.next -- so put
   {out.<verifier>.problem} in that prompt, or it repeats its mistake.
2. `default` is a human node, never "__end__". An unhandled verdict that
   silently ends the run is the worst outcome available.
"""


PROMPT_REFERENCE = """

--------------------------------------------------------------------------
PROMPTS
--------------------------------------------------------------------------
`prompts.first` when the node has no conversation yet, or when a counter in
`refresh_on` changed; otherwise `prompts.next`. Brief once at length, follow
up briefly -- a large token saving.

Placeholders you may use, and NOTHING else:
    {task_brief}            the brief you are writing, below
    {transcript}            the conversation with the human
    {last_answer}           what the human last typed
    {repo_path}             the repository path
    {artifacts_dir}         where this run should put what it PRODUCES
    {my_steps}              the plan steps THIS node is responsible for
    {plan_outline}          one line per step, so the node knows the context
    {out.<node>.<field>}    another node's output field
    {var.<name>}            a value set by a human command

A placeholder naming a field a node does not declare is a hard error, and so
is any placeholder not on this list.

An `ask.question` that is ONLY a placeholder renders BLANK when that field
happens to be empty, and the person then sees nothing to act on. Always
append a static sentence saying what they can do:

    "{out.reviewer.question}\n\nAnything to add? /approve or /revise."

Five fields that no example below happens to use, because each is one line:

    "thread_key": "sweep"        share ONE provider conversation between
                                 several nodes, so they remember each other
    "refresh_on": "attempt"      start a fresh conversation whenever that
                                 counter changes (and use prompts.first again)
    "bump": ["attempt"]          increment a counter when this node runs
    "capture": ["git_status"]    prepend the repo's git status, or git_diff,
                                 to this node's prompt
    "record": "ran the sweep"    a line written into the transcript after
                                 this node runs, for later nodes to read

A counter is NOT a placeholder: `bump` and `refresh_on` are the only things
that read one, and no prompt can render its value.

--------------------------------------------------------------------------
RULES THAT WILL GET YOUR DESIGN REJECTED
--------------------------------------------------------------------------
- Every node reachable from `entry`, and some path must reach "__end__".
- Every node needs exactly one outgoing edge OR one branch, never both, never
  two edges.
- No output field called git_status or git_diff; use `capture` for those.
- Node names: lowercase letters, digits, underscores; start with a letter.
- `nodes` must contain exactly one entry per node in `graph.nodes`.
"""


CONTROL_FLOW = """

--------------------------------------------------------------------------
CONDITIONAL EDGES, AND WHEN TO USE AN ORCHESTRATOR
--------------------------------------------------------------------------
There are two ways to shape a run, and the PLAN decides which you need.

A FIXED PIPELINE, when the plan already fixes the order. Plain edges, with a
branch only where something can fail. The research skeleton below is one.
Prefer it when you can: every node is checkpointed, and a person can read the
graph and see what will happen.

AN ORCHESTRATOR, when the LENGTH of the work is not known in advance -- "keep
trying configurations until the eval passes", "iterate until the tests are
green", "work through whatever the analysis turns up". One node looks at the
state each time and picks the next action:

  "branches": [ { "from": "orchestrator", "route_on": "action",
      "cases": [
        { "when": "train",    "to": "trainer" },
        { "when": "evaluate", "to": "evaluator" },
        { "when": "analyse",  "to": "analyst" },
        { "when": "finish",   "to": "review", "ask": { ... } } ],
      "default": "review" } ]

and every worker's edge going BACK to it, which is what makes it a loop:

  "edges": [ { "from": "trainer",   "to": "orchestrator" },
             { "from": "evaluator", "to": "orchestrator" },
             { "from": "analyst",   "to": "orchestrator" } ]

Rules for one:
- It does no work itself. access "read_only", and it decides only. An
  orchestrator that also edits files is grading itself.
- A branch takes at most 8 cases, so ONE orchestrator can dispatch to at most
  seven workers. Past that, stage the graph and give each stage its own
  verifier instead of building a second orchestrator.
- Its output must carry the REASON as well as the action, and each worker's
  prompt must render it ({out.orchestrator.next_task}) -- otherwise the
  worker is dispatched with no idea what it was dispatched for.

USES FOR A BRANCH THAT ARE NOT "did it work":

- A RESOURCE failure is not a CODE failure. Out of memory, out of disk, a run
  past its time budget: that goes to a node that shrinks the configuration
  (batch size, sequence length, model size, fewer steps), NOT to the node
  that wrote the code. Rewriting correct code does not free memory. Give the
  checker its own case for it, e.g. "too_big" -> shrink_config.
- MORE TO RUN? A sweep, or several seeds, is a loop: the runner writes one
  result, then a node asks "is anything left?" and branches back to the
  runner. That node decides by LOOKING at {artifacts_dir} to see which
  results already exist. Counters exist (`bump`) but CANNOT be rendered into
  a prompt, so the filesystem is the only progress a prompt can read.
- ENOUGH ALREADY. A `redo` loop with no way out spins until the step limit
  and dies with nothing. check_method's prompts.next below is how a loop
  ends: a node keeps its own conversation, so the checker remembers what it
  already rejected -- the only place an attempt count can live.
"""


REPAIR_PREFIX = """
Your previous design was rejected. Fix EVERY problem listed below and return a
complete corrected design -- not a patch, the whole thing again.

Problems:
""".strip()


# ---------------------------------------------------------------------------
# THE RESEARCH SKELETON
# ---------------------------------------------------------------------------
# A starting shape for the work this project is actually for: computer-science
# research. It rides in the PROMPT next to the worked example, for the same
# reason -- instructions have a measured size cliff and prompts do not.
#
# It is a SKELETON, not a mould, and the prose below says so to the model.
# A template that must be obeyed is worse than no template: it produces a
# graph shaped like the example instead of like the work. So the designer is
# told to check it against the plan FIRST, drop what does not apply, and say
# in `rationale` what it changed.
#
# Two things here are load-bearing beyond the shape:
#
# 1. `run_exp_*` declares backend "runner" -- a role nobody has configured.
#    That is the hook for running experiments on a cheaper model: config.py's
#    backend_for() falls back to the default until `.agent/config.json` names
#    it, so the skeleton costs nothing and makes the option available.
#
# 2. The retry runs code -> run -> CHECK -> back to code, not code -> run ->
#    back to code. A run node that decides whether its own run was any good is
#    self-assessment, which validate.py rejects outright at design time. The
#    check node is read_only with no plan steps of its own, which is precisely
#    what the missing-verifier warning looks for -- so the skeleton satisfies
#    both rules by construction rather than by being careful.
# ---------------------------------------------------------------------------

RESEARCH_GRAPH = {
    "format_version": 1,
    "name": "research_skeleton",
    "description": (
        "The four stages of a research plan as a graph: a shared dataloader, "
        "the experiment config, each method and baseline behind its own "
        "checker, the runs, then analysis and a report."
    ),
    "entry": "setup_env",
    "nodes": [
        {"name": "setup_env", "kind": "agent"},
        {"name": "write_dataloader", "kind": "agent"},
        {"name": "check_data", "kind": "agent"},
        {"name": "design_experiments", "kind": "agent"},
        {"name": "write_method", "kind": "agent"},
        {"name": "check_method", "kind": "agent"},
        {"name": "write_baseline", "kind": "agent"},
        {"name": "check_baseline", "kind": "agent"},
        {"name": "run_method", "kind": "agent"},
        {"name": "run_baseline", "kind": "agent"},
        {"name": "analyse", "kind": "agent"},
        {"name": "report", "kind": "agent"},
        {"name": "review", "kind": "human"},
    ],
    "edges": [
        {"from": "setup_env", "to": "write_dataloader"},
        {"from": "write_dataloader", "to": "check_data"},
        {"from": "design_experiments", "to": "write_method"},
        {"from": "write_method", "to": "check_method"},
        {"from": "write_baseline", "to": "check_baseline"},
        {"from": "run_method", "to": "run_baseline"},
        {"from": "run_baseline", "to": "analyse"},
        {"from": "report", "to": "review", "ask": {
            "purpose": "findings",
            "resume_to": "report",
            "question": "{out.report.summary}\n\nAnything to change? "
                        "/revise <what> or /exit.",
            "context": "{out.report.findings}",
        }},
    ],
    "branches": [
        {"from": "check_data", "route_on": "verdict",
         "cases": [
             {"when": "ok", "to": "design_experiments"},
             {"when": "redo", "to": "write_dataloader"},
             {"when": "blocked", "to": "review", "ask": {
                 "purpose": "blocked",
                 "resume_to": "write_dataloader",
                 "question": "{out.check_data.problem}\n\nNow what? "
                             "Type guidance, or /exit.",
                 "context": "{out.check_data.detail}",
             }},
         ],
         "default": "review"},
        {"from": "check_method", "route_on": "verdict",
         "cases": [
             {"when": "ok", "to": "write_baseline"},
             {"when": "redo", "to": "write_method"},
             {"when": "blocked", "to": "review", "ask": {
                 "purpose": "blocked",
                 "resume_to": "write_method",
                 "question": "{out.check_method.problem}\n\nNow what? "
                             "Type guidance, or /exit.",
                 "context": "{out.check_method.detail}",
             }},
         ],
         "default": "review"},
        {"from": "check_baseline", "route_on": "verdict",
         "cases": [
             {"when": "ok", "to": "run_method"},
             {"when": "redo", "to": "write_baseline"},
             {"when": "blocked", "to": "review", "ask": {
                 "purpose": "blocked",
                 "resume_to": "write_baseline",
                 "question": "{out.check_baseline.problem}\n\nNow what? "
                             "Type guidance, or /exit.",
                 "context": "{out.check_baseline.detail}",
             }},
         ],
         "default": "review"},
        {"from": "analyse", "route_on": "verdict",
         "cases": [
             {"when": "ok", "to": "report"},
             {"when": "redo", "to": "run_method"},
             {"when": "blocked", "to": "review", "ask": {
                 "purpose": "blocked",
                 "resume_to": "run_method",
                 "question": "{out.analyse.problem}\n\nNow what? "
                             "Type guidance, or /exit.",
                 "context": "{out.analyse.detail}",
             }},
         ],
         "default": "review"},
    ],
}


#: node -> (access, backend, what it is for). A table rather than nine more
#: nodes.json entries: the three spelled out in full below already show what an
#: entry looks like, and these nodes are the same three shapes with different
#: subjects. Access and backend are the two fields the skeleton is making a
#: claim about.
RESEARCH_ROLES: tuple[tuple[str, str, str, str], ...] = (
    ("setup_env",    "write",     "coder",   "install and pin what the rest needs"),
    ("design_experiments", "write", "coder", "the config: datasets, splits, hyperparameters, seeds, ablations"),
    ("analyse",      "read_only", "checker", "read every result file and decide what they say; branches"),
    ("report",       "write",     "coder",   "write up what analyse concluded"),
)
#: write_dataloader / write_baseline copy write_method; check_data /
#: check_baseline copy check_method; run_baseline copies run_method. Each is
#: the same entry with a different subject and different `steps`.


#: Three real nodes.json entries plus the human node -- one per SHAPE the
#: skeleton repeats: a worker, a runner, a verifier.
#:
#: These exist because a topology is not a design: the graph says a node called
#: run_method runs an experiment, and says nothing about what its prompt has to
#: contain for that to happen. The generic worked example cannot show it
#: either, because its nodes discuss and orchestrate rather than run anything.
#:
#: They are validated: tests/test_bootstrap.py writes them to disk as part of a
#: real folder and runs validate_folder(strict=True) over it, so every
#: placeholder here names a field that the node it points at actually declares.
#: An exemplar with a broken placeholder would teach the designer to write
#: broken placeholders, and its repair loop would then fight the example it was
#: given.
RESEARCH_NODES: dict[str, dict] = {
    "write_method": {
        "backend": "coder",
        "access": "write",
        "steps": ["1.2"],
        "instructions": (
            "You write experiment code. It goes in the repository as normal "
            "source with its own tests -- not in the artifacts directory, "
            "which is for results. Write the tests as you go and run them: "
            "code that has never been executed is not finished.\n\nUse the "
            "dataloader that already exists; do not write your own loading or "
            "splitting. A method and its baseline on different splits is not "
            "a comparison, and nothing downstream can see it happened."
        ),
        "output": [
            {"name": "files", "type": "string", "required": True,
             "description": "Every path you created or changed, one per line."},
            {"name": "how_to_run", "type": "string", "required": True,
             "description": "The exact command that runs it, with its config."},
            {"name": "summary", "type": "string", "required": True},
        ],
        "prompts": {
            "first": (
                "{task_brief}\n\nYour step:\n{my_steps}\n\n"
                "The rest of the plan, for context only:\n{plan_outline}\n\n"
                "The repository is at {repo_path}. Read what is there first -- "
                "most of this usually exists. The shared dataloader is at "
                "{out.write_dataloader.files}; use it. Make the run command "
                "take an output directory; results go under {artifacts_dir}."
                "\n\nWrite the code and its tests, run them, and report the "
                "files and the run command."
            ),
            "next": (
                "Your last attempt was sent back. What was wrong:\n\n"
                "{out.check_method.problem}\n\n{out.check_method.detail}\n\n"
                "Fix exactly that. Do not start again from scratch, and do "
                "not change anything the complaint does not mention."
            ),
        },
        "announce": "writing the method...",
    },
    "check_method": {
        "backend": "checker",
        "access": "read_only",
        "steps": [],
        "instructions": (
            "You decide whether a piece of work counts. You never fix "
            "anything: you are read-only, and the point of you is that you "
            "are not the node that did the work.\n\nRead the code, not the "
            "report about it, and run its tests. Code that passes its own "
            "tests and implements the wrong thing is the failure worth "
            "catching."
        ),
        "output": [
            {"name": "verdict", "type": "enum",
             "choices": ["ok", "redo", "blocked"], "required": True},
            {"name": "problem", "type": "string",
             "description": "One sentence. Empty when the verdict is ok."},
            {"name": "detail", "type": "string",
             "description": "What you looked at, and what to change."},
        ],
        "prompts": {
            "first": (
                "Check this work against what was asked:\n\n{my_steps}\n\n"
                "It reports these files:\n{out.write_method.files}\n\n"
                "Run it with: {out.write_method.how_to_run}\n"
                "Its own summary: {out.write_method.summary}\n\n"
                "Read the files and run the tests. Return 'ok' if the step's "
                "own check is satisfied, 'redo' with a specific complaint, or "
                "'blocked' if retrying cannot fix it -- missing data or "
                "hardware, a contradiction in the plan."
            ),
            "next": (
                "This came back after your last verdict.\n\n{my_steps}\n\n"
                "Files:\n{out.write_method.files}\n\n"
                "You have seen this before. If the problem you named last "
                "time is STILL there, return 'blocked', not 'redo': a second "
                "identical complaint means the loop is not converging and a "
                "person should look. 'redo' only for something new."
            ),
        },
        "announce": "checking the code...",
    },
    "run_method": {
        "backend": "runner",
        "access": "write",
        "steps": ["3.1"],
        "instructions": (
            "You run experiments and record what happened. You do not fix "
            "code: if a run fails, report it with enough of the error to act "
            "on. Never edit the repository; only write results under the "
            "artifacts directory."
        ),
        "output": [
            {"name": "results_path", "type": "string", "required": True,
             "description": "Where under the artifacts directory the results went."},
            {"name": "log_tail", "type": "string", "required": True,
             "description": "The last ~40 lines, or the whole error if it failed."},
            {"name": "ran", "type": "enum", "choices": ["finished", "failed"],
             "required": True},
            {"name": "summary", "type": "string", "required": True,
             "description": "The headline numbers, or why there are none."},
        ],
        "prompts": {
            "first": (
                "Run this experiment:\n{my_steps}\n\n"
                "The configuration to run:\n"
                "{out.design_experiments.summary}\n\n"
                "How to run it:\n{out.write_method.how_to_run}\n\n"
                "Write every result under {artifacts_dir}, one file per "
                "configuration and seed, named so a second run ADDS a file "
                "rather than replacing one, with the full stdout beside it.\n\n"
                "Do not modify the code. If it will not run, report the error."
            ),
            "next": (
                "Run it again -- {out.analyse.problem}\n\n"
                "{out.write_method.how_to_run}\n\nSame rules: results under "
                "{artifacts_dir}, nothing overwritten, no edits."
            ),
        },
        "announce": "running the experiment...",
    },
    "review": {
        "commands": [
            {"name": "approve", "to": "__end__",
             "purposes": ["findings"],
             "summary": "Accept the findings and finish"},
            {"name": "revise", "to": "report",
             "purposes": ["findings"],
             "argument": "[what to change]",
             "summary": "Rewrite the report",
             "sets": [{"name": "review_feedback", "value": "{argument}"}]},
            {"name": "exit", "to": "__end__", "aliases": ["quit", "q"],
             "summary": "Stop the run (everything finished is saved)"},
        ],
    },
}


def research_skeleton() -> str:
    """The skeleton, as it is sent to the designer."""
    table = "\n".join(
        f"  {name:<19} access {access:<9} backend {backend!r:<10} {purpose}"
        for name, access, backend, purpose in RESEARCH_ROLES
    )
    return (
        "\n\n"
        "--------------------------------------------------------------------------\n"
        "THE RESEARCH SKELETON -- start here, then check it fits\n"
        "--------------------------------------------------------------------------\n"
        "The plan's four stages as a graph. Your plan has the same four, so the\n"
        "mapping is nearly mechanical: stage 1 is the write_* nodes, 2 is\n"
        "design_experiments, 3 is the run_* nodes, 4 is analyse and report.\n"
        "\n"
        "  setup_env -> write_dataloader -> check_data --ok--> design_experiments\n"
        "  design_experiments -> write_method -> check_method --ok--> write_baseline\n"
        "  write_baseline -> check_baseline --ok--> run_method -> run_baseline\n"
        "  run_baseline -> analyse --ok--> report -> review\n"
        "\n"
        "  every check_*:  redo -> the node that wrote it,  blocked -> review\n"
        "  analyse:        redo -> run_method (the run, not the code)\n"
        "\n"
        "BEFORE using it, check it against the plan and say in `rationale` what\n"
        "you changed and why. It is a starting point, not a form to fill in:\n"
        "\n"
        "- ONE DATALOADER, used by every method and baseline. Different splits\n"
        "  between a method and its baseline is not a comparison, and nothing\n"
        "  downstream can tell. Say so in each write_* prompt, as below.\n"
        "- ONE write/check PAIR PER THING IMPLEMENTED. Two baselines means two\n"
        "  pairs, chained. Drop write_baseline if the plan compares against a\n"
        "  published number rather than reproducing one.\n"
        "- ONE run_* PER THING RUN, all after every check has passed: a run is\n"
        "  the expensive node, and a bug found inside it costs the whole run.\n"
        "- Chain them SEQUENTIALLY. One plain outgoing edge per node -- the\n"
        "  validator refuses two, so there are no arms running side by side.\n"
        "- Drop setup_env if the environment works. Drop write_dataloader and\n"
        "  check_data together if there is no data stage -- and then take\n"
        "  {out.write_dataloader.files} out of the write_* prompts, or they\n"
        "  name a node that no longer exists and the design is rejected.\n"
        "- Add nodes the plan needs and this has not: an ablation, a figure.\n"
        "- A write_* node writes the code AND its tests. Do not add a node whose\n"
        "  only job is to test code; the provider does that in one turn.\n"
        "- check_* is the retry judge and must be SEPARATE and read_only. A node\n"
        "  routing on its own output back to itself is rejected.\n"
        "- `redo` must carry the complaint: put {out.check_method.problem} in\n"
        "  write_method's prompts.next, or it repeats the mistake it was never\n"
        "  told about.\n"
        "- `steps` is a SUBSTEP id -- \"1.2\", not \"1\": a substep is one piece of\n"
        "  work, a stage is four. Its `check` is what the check_* node applies,\n"
        "  and arrives in {my_steps}.\n"
        "- A substep marked [human gate] needs a human node after the node that\n"
        "  owns it, with an ask offering /approve and /exit.\n"
        "\n"
        "The nodes not spelled out below:\n"
        "\n"
        f"{table}\n"
        "  review              kind \"human\"  the exit, and where blocked goes\n"
        "\n"
        "`backend` names a ROLE, not a model. \"runner\" and \"checker\" are kept\n"
        "separate from \"coder\" so runs can be pointed at a cheaper model in\n"
        ".agent/config.json without touching the graph. Use these names, and\n"
        "read_only for anything that only looks.\n"
        "\n"
        "graph.json:\n"
        + json.dumps(RESEARCH_GRAPH, indent=1)
        + "\n\nnodes.json, for the three SHAPES it repeats -- worker, verifier, "
        "runner -- plus the human node. write_dataloader and write_baseline "
        "are write_method with another subject and `steps`; check_data and "
        "check_baseline are check_method; run_baseline is run_method.\n"
        + json.dumps(RESEARCH_NODES, indent=1)
    )
