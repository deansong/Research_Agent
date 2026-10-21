"""
WHAT:  The STARTING design: built from the approved plan by code, with no
       model involved, in the designer's own answer format -- the whole
       graph.json and a short spec per node. spec.py compiles it.
WHY:   The designer used to write the whole folder from a blank page in one
       structured turn, and on this repo's own sessions it failed on
       bookkeeping, not judgement:

           graph.json lists 30 nodes and nodes.json has 1
           node 'build_report': listed in graph.json but has no entry
           branch on 'verify_code_stage': default points at 'review',
               which is not a node

       Now it starts from the design this module builds, which compiles to a
       folder that passes the validator, and changes it -- see
       nodes/designer.py. A design can then never end worse than this.
CONCEPT: Not LangGraph. plan -> blueprint (a chain of nodes) -> design.

The blueprint is an ordered list of nodes, each one of three shapes:

    worker    access write,     role "coder"    writes code/configs/reports
    runner    access write,     role "runner"   runs experiments, never edits
    checker   access read_only, role "checker"  judges the nodes it `checks`,
                                                 and branches on a verdict

Nodes run in list order. A checker's verdict sends the run on (ok), back to
one of the nodes it checks (redo), or to a person (blocked). One human node,
`review`, is added at the end; it is also where every gate and every
`blocked` stops, told apart by the ask's `purpose`.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agent.agentfolder.schema import NODE_NAME

#: The folder format's own caps (agentfolder/schema.py), less what expand()
#: adds itself: one `review` node, and nothing else.
MAX_NODES = 47
#: One branch per checker. graph.json allows 16.
MAX_CHECKERS = 16
#: A branch takes at most 8 cases: ok, blocked, and one redo per checked node.
MAX_CHECKED = 6
#: `review` takes at most 12 commands: approve (findings), revise, exit, and
#: one approve per gate.
MAX_GATES = 9
#: Top-level steps a plan should have. Each is a node and (usually) a checker,
#: so this is what keeps a designed agent around a dozen nodes.
MAX_STEPS = 6

REVIEW = "review"
FEEDBACK_VAR = "review_feedback"

#: A hub agent's fixed nodes besides the work: the orchestrator at its
#: centre, the one validator, the node that writes the paper. See
#: agent/work/templates/orchestrator_node.py.
ORCHESTRATOR = "orchestrator"
VALIDATOR = "validator"
PAPER = "paper"
HUB_FIXED = (ORCHESTRATOR, VALIDATOR, PAPER, REVIEW)

#: Which shape of agent the designer starts from (config `design_topology`).
TOPOLOGIES = ("hub", "pipeline")


#: A unit whose title STARTS with one of these verbs runs something, and
#: becomes a runner -- "Run the Llama baseline matrix", "Execute the primary
#: panel". Anywhere in the title was too eager: on a real plan it made
#: runners of "Freeze the run matrix" and "Define smoke tests", which write
#: configs. Only the starting point: the designer can reshape any node.
_RUN_WORDS = re.compile(
    r"^\s*(run|execute|train|launch|sweep|fine-tune|finetune|benchmark|"
    r"evaluate|reproduce)\b", re.I)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BlueprintNode(_Strict):
    name: str
    shape: Literal["worker", "runner", "checker"]
    steps: list[str] = Field(default_factory=list, max_length=20)
    """Plan step ids this node owns. Empty for a checker."""
    checks: list[str] = Field(default_factory=list, max_length=MAX_CHECKED)
    """For a checker: the EARLIER nodes whose work it judges. Empty otherwise."""
    instructions: str = ""
    """What is special about this node, in a few sentences. The shape already
    supplies the standing rules, the plan supplies the step detail."""
    loop: bool = False
    """A work node whose step has substeps: it does one per turn, and its
    checker's `next` case sends it round for the following one."""


class Blueprint(_Strict):
    """The starting agent, before expand() turns it into a folder."""

    name: str
    description: str = ""
    task_brief: str
    rationale: str = ""
    nodes: list[BlueprintNode] = Field(min_length=1, max_length=MAX_NODES)


class BlueprintError(ValueError):
    pass


# ---------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------

def _steps(plan: dict) -> list[dict]:
    return list((plan or {}).get("steps") or [])


def _leaves(step: dict) -> list[dict]:
    """The pieces of work in a step: its substeps, or the step itself."""
    return list(step.get("substeps") or []) or [step]


def leaf_ids(plan: dict) -> list[str]:
    return [str(leaf["id"]) for step in _steps(plan) for leaf in _leaves(step)]


def _covers(step_id: str, plan: dict) -> list[str]:
    """The leaf ids one owned id stands for: a stage id owns its substeps."""
    for step in _steps(plan):
        if str(step["id"]) == step_id:
            return [str(leaf["id"]) for leaf in _leaves(step)]
        for sub in step.get("substeps") or []:
            if str(sub["id"]) == step_id:
                return [step_id]
    return []


def gated_ids(plan: dict) -> list[str]:
    """Step and substep ids a person must approve."""
    out = []
    for step in _steps(plan):
        if step.get("gate"):
            out.append(str(step["id"]))
        out += [str(s["id"]) for s in step.get("substeps") or [] if s.get("gate")]
    return out


# ---------------------------------------------------------------------------
# the blueprint, from the plan
# ---------------------------------------------------------------------------

def work_nodes(plan: dict) -> list[dict]:
    """The FIXED work nodes for a plan: one per TOP-LEVEL step, in plan order.

    [{"name", "step", "title", "role", "substeps"}]. A step's substeps are
    done INSIDE its node, one per turn, each checked before the next -- so the
    graph stays small (a node per step, not per substep) while every turn
    stays one substep's worth of work. Deterministic, because the designer is
    held to it: a design must contain exactly these nodes, each owning
    exactly its step.
    """
    out, used = [], {REVIEW}
    for step in _steps(plan):
        title = str(step.get("title", "")).strip()
        subs = [str(sub["id"]) for sub in step.get("substeps") or []]
        out.append({"name": _unique(_slug(f"s{step['id']}_{title}"), used),
                    "step": str(step["id"]), "title": title,
                    "role": "runner" if _RUN_WORDS.search(title) else "worker",
                    "substeps": subs})
    return out


def plan_issues(plan: dict) -> list[str]:
    """What is wrong with the plan itself, found by code. Shown at review."""
    out = []
    missing = [str(leaf["id"]) for stage in _steps(plan) for leaf in _leaves(stage)
               if not str(leaf.get("check", "")).strip()]
    if missing:
        out.append(f"Steps with no `check`, so their checker has nothing concrete "
                   f"to apply: {', '.join(missing)}.")
    if len(_steps(plan)) > MAX_STEPS:
        out.append(f"The plan has {len(_steps(plan))} top-level steps; each is a "
                   f"node and a checker. Group them into at most {MAX_STEPS}.")
    return out


def from_plan(plan: dict, *, request: str = "", requirements: str = "") -> Blueprint:
    """The starting blueprint: a node per top-level step, and their checkers.

    A step WITH substeps loops -- its node does one substep a turn and its
    checker sends it round again -- so it gets a checker of its own (the
    `next` case points back at exactly one node). Steps without substeps are
    single turns, and consecutive ones share a checker, up to six.
    """
    if not _steps(plan):
        raise BlueprintError("there is no plan to build an agent from")
    work = work_nodes(plan)

    chunks: list[list[dict]] = []
    single: list[dict] = []
    for w in work:
        if w["substeps"]:
            if single:
                chunks.append(single)
                single = []
            chunks.append([w])
        else:
            single.append(w)
            if len(single) == MAX_CHECKED:
                chunks.append(single)
                single = []
    if single:
        chunks.append(single)
    # Two branches are left spare for the designer to add a loop or router.
    limit = MAX_CHECKERS - 2
    while len(chunks) > limit:
        sizes = [(len(chunks[i]) + len(chunks[i + 1]), i) for i in range(len(chunks) - 1)
                 if not chunks[i][0]["substeps"] and not chunks[i + 1][0]["substeps"]]
        if not sizes or min(sizes)[0] > MAX_CHECKED:
            break
        _, i = min(sizes)
        chunks[i:i + 2] = [chunks[i] + chunks[i + 1]]
    if len(chunks) > limit or len(work) + len(chunks) > MAX_NODES:
        raise BlueprintError(
            f"the plan has {len(work)} steps, which with their checkers do not fit "
            f"in one agent. Group them into at most {MAX_STEPS} steps.")

    nodes: list[BlueprintNode] = []
    used = {REVIEW} | {w["name"] for w in work}
    for chunk in chunks:
        for w in chunk:
            nodes.append(BlueprintNode(name=w["name"], shape=w["role"], steps=[w["step"]],
                                       loop=bool(w["substeps"])))
        first, last = chunk[0]["step"], chunk[-1]["step"]
        label = first if first == last else f"{first}_{last}"
        nodes.append(BlueprintNode(
            name=_unique(_slug(f"check_{label}"), used), shape="checker",
            checks=[w["name"] for w in chunk]))

    summary = str((plan or {}).get("summary", "")).strip()
    return Blueprint(
        name=_slug(request or summary or "designed_agent")[:64] or "designed_agent",
        description=summary[:500],
        task_brief=default_brief(plan, request=request, requirements=requirements),
        rationale=("Built mechanically from the plan: one node per step, working "
                   "through its substeps one per turn, each checked."),
        nodes=nodes,
    )


def default_brief(plan: dict, *, request: str = "", requirements: str = "") -> str:
    """A self-contained brief from what is already written down.

    The starting agent's brief, until the designer writes a better one. It
    has to stand on its own: the generated agent never sees the conversation.
    """
    parts = []
    if request.strip():
        parts.append(f"GOAL\n{request.strip()}")
    if requirements.strip():
        parts.append(f"REQUIREMENTS AGREED WITH THE PERSON\n{requirements.strip()}")
    summary = str((plan or {}).get("summary", "")).strip()
    if summary:
        parts.append(f"APPROACH\n{summary}")
    parts.append("DONE WHEN\nEvery step of the plan is complete and its check "
                 "passes, with everything the run produced under the "
                 "artifacts directory.")
    return "\n\n".join(parts)


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    if not slug or not slug[0].isalpha():
        slug = f"n_{slug}"
    return slug[:32].rstrip("_")


def _unique(name: str, used: set[str]) -> str:
    candidate, n = name, 2
    while candidate in used:
        suffix = f"_{n}"
        candidate = name[:32 - len(suffix)].rstrip("_") + suffix
        n += 1
    used.add(candidate)
    return candidate


# ---------------------------------------------------------------------------
# checking a blueprint -- a guard for expand()
# ---------------------------------------------------------------------------

def problems(blueprint: Blueprint, plan: dict) -> list[str]:
    out: list[str] = []
    nodes = blueprint.nodes
    seen: dict[str, int] = {}

    for index, node in enumerate(nodes):
        where = f"node {index + 1} ({node.name!r})"
        if not re.fullmatch(NODE_NAME, node.name):
            out.append(f"{where}: names are lowercase letters, digits and "
                       f"underscores, start with a letter, at most 32 characters.")
        if node.name == REVIEW:
            out.append(f"{where}: {REVIEW!r} is added automatically; use another name.")
        if node.name in seen:
            out.append(f"{where}: the name is already used by node {seen[node.name] + 1}.")
        seen.setdefault(node.name, index)

        if node.shape == "checker":
            if node.steps:
                out.append(f"{where}: a checker owns no steps -- it judges the "
                           f"steps of the nodes in `checks`. Move {node.steps} to "
                           f"the node that does that work.")
            if not node.checks:
                out.append(f"{where}: a checker needs `checks`: the earlier nodes it judges.")
            for target in node.checks:
                earlier = {n.name: n for n in nodes[:index]}
                if target not in earlier:
                    out.append(f"{where}: checks {target!r}, which is not an "
                               f"EARLIER node. A checker comes after the work it judges.")
                elif earlier[target].shape == "checker":
                    out.append(f"{where}: checks {target!r}, which is a checker. "
                               f"Check the work, not the judge.")
        else:
            if node.checks:
                out.append(f"{where}: only a checker has `checks`.")
            if not node.steps and _steps(plan):
                out.append(f"{where}: a {node.shape} needs at least one plan step in `steps`.")

    if sum(n.shape == "checker" for n in nodes) > MAX_CHECKERS:
        out.append(f"At most {MAX_CHECKERS} checkers: each is a branch, and a "
                   f"graph takes {MAX_CHECKERS}. Give one checker several nodes.")

    if _steps(plan):
        valid = {str(s["id"]) for s in _steps(plan)} | {
            str(sub["id"]) for s in _steps(plan) for sub in s.get("substeps") or []}
        owners: dict[str, list[str]] = {}
        for node in nodes:
            for step_id in node.steps:
                if step_id not in valid:
                    out.append(f"node {node.name!r}: step {step_id!r} is not in the plan.")
                for leaf in _covers(step_id, plan):
                    owners.setdefault(leaf, []).append(node.name)
        for leaf in leaf_ids(plan):
            who = owners.get(leaf, [])
            if not who:
                out.append(f"Plan step {leaf} is owned by no node. Add it to one node's `steps`.")
            elif len(who) > 1:
                out.append(f"Plan step {leaf} is owned by {', '.join(who)}. Exactly one node owns a step.")

        try:
            if len(_gates(blueprint, plan)) > MAX_GATES:
                out.append(f"More than {MAX_GATES} separate approval points. "
                           f"Put gated steps in fewer nodes.")
        except BlueprintError as exc:
            out.append(str(exc))

    return out


# ---------------------------------------------------------------------------
# expanding it
# ---------------------------------------------------------------------------

def starting_design(plan: dict, *, request: str = "", requirements: str = "",
                    topology: str = "hub") -> dict:
    """The starting design for `plan`, in the designer's own answer format:
    {task_brief, rationale, graph, nodes: [spec]}. spec.compile_design()
    turns it into a folder; the designer is shown it to edit.

    `topology` "hub" (the default): an orchestrator dispatching the work
    nodes, one validator, a paper; "pipeline": the older chain of workers and
    checkers."""
    if topology == "pipeline":
        design = expand(from_plan(plan, request=request, requirements=requirements), plan)
    else:
        design = hub_design(plan, request=request, requirements=requirements)
    design["plan_issues"] = plan_issues(plan)
    return design


# ---------------------------------------------------------------------------
# a hub agent
# ---------------------------------------------------------------------------

def hub_design(plan: dict, *, request: str = "", requirements: str = "") -> dict:
    """The hub agent for `plan`: one work node per top-level step, an
    orchestrator that decides each turn which runs next, one validator, the
    paper node, and the person. No branches and no checkers: routing is the
    orchestrator's, judging is the validator's."""
    if not _steps(plan):
        raise BlueprintError("there is no plan to build an agent from")
    work = work_nodes(plan)
    if len(work) + len(HUB_FIXED) > MAX_NODES + 1:
        raise BlueprintError(f"the plan has {len(work)} steps; group them into at most "
                             f"{MAX_STEPS}.")
    summary = str((plan or {}).get("summary", "")).strip()
    specs = [_spec(ORCHESTRATOR, "orchestrator")]
    specs += [_spec(w["name"], w["role"], steps=[w["step"]]) for w in work]
    specs += [_spec(VALIDATOR, "validator"), _spec(PAPER, "paper_writer"),
              {**_spec(REVIEW, "human"), "commands": hub_review_commands()}]
    return {
        "task_brief": default_brief(plan, request=request, requirements=requirements),
        "rationale": ("A hub: the orchestrator sends each step's work to its node, "
                      "has the validator check it, sends a fault back to the step it "
                      "started in, and has the paper written at the end."),
        "graph": hub_graph(_slug(request or summary or "designed_agent")[:64]
                           or "designed_agent", summary[:500], [w["name"] for w in work]),
        "nodes": specs,
    }


def hub_graph(name: str, description: str, work: list[str], *,
              orchestrator: str = ORCHESTRATOR, validator: str = VALIDATOR,
              paper: str = PAPER, review: str = REVIEW) -> dict:
    """graph.json for a hub agent. Fixed by code: every spoke has one edge,
    back to the orchestrator, which decides each turn where to go."""
    spokes = list(work) + [validator, paper]
    return {
        "name": name or "designed_agent",
        "description": description,
        "entry": orchestrator,
        "nodes": ([{"name": orchestrator, "kind": "orchestrator"}]
                  + [{"name": n, "kind": "agent"} for n in spokes]
                  + [{"name": review, "kind": "human"}]),
        "edges": [{"from": n, "to": orchestrator} for n in spokes],
        "branches": [],
    }


def hub_review_commands() -> list[dict]:
    """The person's commands in a hub agent. Every answer goes back to the
    orchestrator, except the end-of-run decisions, which end the run and
    say what happens next (Runtime.conclude_iteration)."""
    asked = ["orchestrator", "gate", "guard", "finish_review", "unrouted"]
    return [
        _command("continue", ORCHESTRATOR, asked, "Carry on (with guidance, if any)",
                 argument="[guidance]", sets={"answer": "{argument}"}),
        _command("approve", ORCHESTRATOR, ["gate"], "Approve the gated step",
                 argument="[note]", sets={"gate": "approved", "answer": "{argument}"}),
        _command("accept", "__end__", ["finish_review"], "Do what the review recommends",
                 argument="[note]", sets={"note": "{argument}"}, outcome="accept"),
        _command("replan", "__end__", ["finish_review"],
                 "Change the plan, then redesign and run again",
                 argument="[what to change]", sets={"note": "{argument}"}, outcome="replan"),
        _command("redesign", "__end__", ["finish_review"],
                 "Keep the plan, redesign the agent, and run again",
                 argument="[what to change]", sets={"note": "{argument}"},
                 outcome="redesign"),
        _command("finish", "__end__", ["finish_review"], "Stop here; keep everything",
                 outcome="finish"),
        _command("exit", "__end__", [], "Stop the run (everything finished is saved)",
                 aliases=["quit", "q"], outcome="exit"),
    ]


def _spec(name: str, role: str, *, steps=()) -> dict:
    return {"name": name, "role": role, "steps": list(steps), "tier": "standard",
            "reads": [], "instructions": "", "outputs": [], "commands": []}


def expand(blueprint: Blueprint, plan: dict) -> dict:
    """The blueprint as a design: the wired graph, and a spec per node."""
    found = problems(blueprint, plan)
    if found:
        raise BlueprintError("\n".join(found))

    nodes = blueprint.nodes
    names = [n.name for n in nodes]
    by_name = {n.name: n for n in nodes}
    workers = [n for n in nodes if n.shape != "checker"]
    last_worker = workers[-1].name

    gates = _gates(blueprint, plan)              # gate point -> (owner, ids)
    review_commands: list[dict] = [
        _command("approve", "__end__", ["findings"], "Accept the result and finish"),
        _command("revise", last_worker, ["findings"], f"Send {last_worker} back to rework it",
                 argument="[what to change]", sets={FEEDBACK_VAR: "{argument}"}),
        _command("exit", "__end__", [], "Stop the run (everything finished is saved)",
                 aliases=["quit", "q"]),
    ]

    edges: list[dict] = []
    branches: list[dict] = []
    for index, node in enumerate(nodes):
        after = names[index + 1] if index + 1 < len(names) else None
        target, ask = _onward(node, after, gates, last_worker, by_name, review_commands)

        if node.shape != "checker":
            edge = {"from": node.name, "to": target}
            if ask:
                edge["ask"] = ask
            edges.append(edge)
            continue

        redo = ([("redo", node.checks[0])] if len(node.checks) == 1
                else [(f"redo_{t}", t) for t in node.checks])
        # A looping node's checker sends it round for its next substep.
        loop = [("next", node.checks[0])] if by_name[node.checks[0]].loop else []
        ok_case = {"when": "ok", "to": target}
        if ask:
            ok_case["ask"] = ask
        branches.append({
            "from": node.name, "route_on": "verdict",
            "cases": [ok_case]
                     + [{"when": when, "to": t} for when, t in loop + redo]
                     + [{"when": "blocked", "to": REVIEW, "ask": {
                         "purpose": "blocked", "resume_to": node.checks[0],
                         "question": (f"{{out.{node.name}.problem}}\n\n{node.name} "
                                      f"says this cannot be fixed by retrying. Type "
                                      f"guidance (it goes back to {node.checks[0]}), "
                                      f"or /exit."),
                         "context": f"{{out.{node.name}.detail}}"}}],
            "default": REVIEW,
        })

    specs = [{"name": n.name, "role": n.shape, "steps": list(n.steps),
              "tier": "standard", "reads": list(n.checks),
              "instructions": n.instructions, "outputs": [], "commands": []}
             for n in nodes]
    specs.append({"name": REVIEW, "role": "human", "steps": [], "tier": "standard",
                  "reads": [], "instructions": "", "outputs": [],
                  "commands": review_commands})

    return {
        "task_brief": blueprint.task_brief.strip(),
        "rationale": blueprint.rationale.strip(),
        "graph": {
            "name": blueprint.name[:64] or "designed_agent",
            "description": blueprint.description,
            "entry": names[0],
            "nodes": [{"name": n, "kind": "agent"} for n in names]
                     + [{"name": REVIEW, "kind": "human"}],
            "edges": edges,
            "branches": branches,
        },
        "nodes": specs,
    }


def _gates(blueprint: Blueprint, plan: dict) -> dict[str, tuple[str, list[str]]]:
    """gate point -> (the node whose work is approved, the gated step ids).

    The gate goes after the work's checker when it has one -- approving work
    nobody has checked yet would ask the person to do the checker's job.
    """
    wanted = gated_ids(plan)
    if not wanted:
        return {}
    nodes = blueprint.nodes
    out: dict[str, tuple[str, list[str]]] = {}
    for step_id in wanted:
        leaves = set(_covers(step_id, plan))
        owners = [n for n in nodes if n.shape != "checker"
                  and leaves & {leaf for s in n.steps for leaf in _covers(s, plan)}]
        if not owners:
            continue                       # problems() reports the missing owner
        owner = owners[-1]
        point = owner.name
        index = next(i for i, n in enumerate(nodes) if n.name == owner.name)
        for later in nodes[index + 1:]:
            if later.shape == "checker" and owner.name in later.checks:
                point = later.name
                break
        if point == nodes[-1].name:
            continue                       # the final review already stops there
        held = out.setdefault(point, (owner.name, []))
        held[1].append(step_id)
    return out


def _onward(node, after, gates, last_worker, by_name, review_commands):
    """Where `node` goes when it is done (or, for a checker, when it says ok)."""
    if after is None:
        result = by_name[last_worker]
        context = (f"{{out.{last_worker}.results_path}}" if result.shape == "runner"
                   else f"{{out.{last_worker}.files}}")
        return REVIEW, {
            "purpose": "findings", "resume_to": last_worker,
            "question": (f"The run is finished.\n\n{{out.{last_worker}.summary}}\n\n"
                         f"/approve to finish, /revise <what to change> to send "
                         f"{last_worker} back, or /exit."),
            "context": context,
        }

    if node.name not in gates:
        return after, None

    owner, ids = gates[node.name]
    purpose = f"approve_{len([c for c in review_commands if c['name'] == 'approve'])}"
    review_commands.insert(1, _command(
        "approve", after, [purpose], f"Approve step {', '.join(ids)} and continue"))
    return REVIEW, {
        "purpose": purpose, "resume_to": owner,
        "question": (f"Step {', '.join(ids)} needs your approval before the run "
                     f"goes on.\n\n{{out.{owner}.summary}}\n\n/approve to continue, "
                     f"type what to change (it goes back to {owner}), or /exit."),
        "context": "",
    }


def _command(name, to, purposes, summary, *, aliases=(), argument="", sets=None,
             outcome=""):
    return {"name": name, "to": to, "purposes": list(purposes), "aliases": list(aliases),
            "argument": argument, "summary": summary, "record": "",
            "sets": [{"name": k, "value": v} for k, v in (sets or {}).items()],
            "outcome": outcome}
