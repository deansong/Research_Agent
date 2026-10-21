"""
WHAT:  Compiles a design -- the whole graph.json plus a short spec per node --
       into a complete agent folder.
WHY:   Measured on real sessions: a model asked for ~30 complete node entries
       (instructions, output fields, two prompts full of placeholders) sends
       one, or none, while saying it sent them all. Asked for ~30 short specs
       it sends all of them, first time. So the model decides everything that
       needs judgement -- the topology, any loops and branches, what each node
       does and reads -- and this file writes the bulk, the same way every
       time.
CONCEPT: Not LangGraph. The graph passes through untouched; only nodes.json
       is generated, and everything in it is derived from the graph and the
       specs, so the two files cannot disagree.

--------------------------------------------------------------------------
WHAT IS DERIVED, AND FROM WHAT
--------------------------------------------------------------------------
    access, backend          the role
    output fields            the role's standard ones, the spec's `outputs`,
                             and -- for a node that branches -- its route_on
                             field as an enum whose choices are exactly the
                             graph's `when` values
    prompts.first            brief, own steps, plan outline, the outputs of
                             the nodes it reads, and why it was sent here
    prompts.next             why it came back: the fields of every node that
                             routes to it, any /command value aimed at it,
                             and what the person last said
    checker / router prompt  every choice spelled out with where it goes
"""

from __future__ import annotations

import re

from agent.agentfolder.render import _TOKEN
from agent.agentfolder.schema import FIELD_NAME, RESERVED_OUTPUT_FIELDS
from agent.bootstrap.schemas import DesignerSpec

ROLE = {"worker": "coder", "runner": "runner", "checker": "checker", "router": "checker"}
#: The role a "light" worker runs as -- configured to a cheaper model in
#: agent/config.py's DEFAULT_ROLE_BACKENDS, and overridable like any role.
LIGHT_ROLE = "coder_light"
WORK_ROLES = ("worker", "runner")
ACCESS = {"worker": "write", "runner": "write", "checker": "read_only", "router": "read_only"}

#: name, type, description -- per role. The first is what a reader is shown.
STANDARD_FIELDS = {
    "worker": [("summary", "What you did and what a reader should know."),
               ("files", "Every path you created or changed, one per line."),
               ("how_to_run", "The exact command that runs or reproduces it.")],
    "runner": [("summary", "The headline numbers, or why there are none."),
               ("results_path", "Where under the artifacts directory the results went."),
               ("log_tail", "The last ~40 lines, or the whole error if it failed.")],
    "checker": [("problem", "One sentence. Empty when the work is fine."),
                ("detail", "What you looked at, and what to change."),
                ("evidence", "Each command you ran or file you read to decide, "
                             "and what it showed. Not a summary of the report.")],
    "router": [("reason", "Why this is the next thing to do."),
               ("next_task", "What the node you send the run to should do.")],
}
#: The enum a role decides with when the graph does not name one.
DECISION = {"checker": ("verdict", ["ok", "redo", "blocked"]),
            "router": ("action", ["finish"]),
            "runner": ("ran", ["finished", "failed"])}

RULES = {
    "worker": (
        "You do one piece of a larger piece of work: the plan steps you are "
        "given, and nothing else. Source code goes in the repository as normal "
        "code with its own tests; anything the run PRODUCES -- results, logs, "
        "figures, reports -- goes under the artifacts directory. Read what "
        "already exists before writing anything. Run what you write: code that "
        "has never been executed is not finished."),
    "runner": (
        "You run experiments and record what happened. You do not change code: "
        "if a run fails, report it with enough of the error to act on. Write "
        "results only under the artifacts directory, one file per "
        "configuration and seed, named so a second run ADDS a file rather than "
        "replacing one, with the full log beside it."),
    "checker": (
        "You decide whether someone else's work counts. You never fix "
        "anything: you are read-only, and the point of you is that you did not "
        "do the work. Read the files and results themselves, not the report "
        "about them, run the tests, and apply each step's own check."),
    "router": (
        "You decide what happens next, and do no work yourself: you are "
        "read-only. Look at what has been done -- the reports below, and the "
        "files and results themselves -- and pick the next action. Say why, "
        "and tell the node you choose exactly what to do."),
}


class SpecError(ValueError):
    """The design cannot be compiled. Each line is one problem, for the repair."""

    def __init__(self, problems: list[str]):
        super().__init__("\n".join(problems))
        self.problems = problems


def compile_design(design: DesignerSpec | dict, plan: dict) -> dict:
    """The design as a proposal {task_brief, rationale, graph, nodes} for the writer."""
    from agent.bootstrap.nodes.planner import steps_for

    if isinstance(design, dict):
        design = DesignerSpec.model_validate(design)
    # exclude_none: an edge with no question has no `ask` key at all, which is
    # the canonical form the web editor writes -- so a designed agent saved
    # unchanged in the editor is byte-identical, not a diff of "ask": null.
    graph = design.graph.model_dump(mode="json", by_alias=True, exclude_none=True)
    kinds = {ref["name"]: ref["kind"] for ref in graph["nodes"]}
    order = [ref["name"] for ref in graph["nodes"]]
    specs = {}
    problems = []

    for spec in design.nodes:
        if spec.name in specs:
            problems.append(f"node {spec.name!r} has two specs. Send one per node.")
        specs[spec.name] = spec
    for name in order:
        if name not in specs:
            problems.append(f"node {name!r} is in graph.nodes but has no spec in "
                            f"`nodes`. Every node needs exactly one.")
    for name in specs:
        if name not in kinds:
            problems.append(f"node {name!r} has a spec but is not in graph.nodes. "
                            f"Add it to the graph, or drop the spec.")
    for name, spec in specs.items():
        kind = kinds.get(name)
        if kind == "human" and spec.role != "human":
            problems.append(f"node {name!r} is kind human in the graph, so its role must be human.")
        if kind == "agent" and spec.role == "human":
            problems.append(f"node {name!r} has role human, so its graph kind must be human.")
        if spec.role != "human" and spec.commands:
            problems.append(f"node {name!r}: only a human node has commands.")
        for read in spec.reads:
            if kinds.get(read) != "agent":
                problems.append(f"node {name!r} reads {read!r}, which is not an agent node.")
        for extra in spec.outputs:
            if not re.fullmatch(FIELD_NAME, extra.name) or extra.name in RESERVED_OUTPUT_FIELDS:
                problems.append(f"node {name!r}: output {extra.name!r} is not a usable "
                                f"field name (lowercase, digits, underscores).")
    problems += _plan_contract(specs, plan)
    if problems:
        raise SpecError(problems)

    reads_of = {name: (list(spec.reads) or _inputs(name, specs, graph))
                for name, spec in specs.items() if spec.role != "human"}
    checked = {r for name, reads in reads_of.items()
               if specs[name].role == "checker" for r in reads}
    unchecked = [n for n, spec in specs.items()
                 if spec.role in WORK_ROLES and n not in checked]
    if unchecked:
        raise SpecError([f"work node {n!r} is not in any checker's `reads`, so "
                         f"nothing independent ever looks at its work. Add it "
                         f"to a checker that comes after it." for n in unchecked])

    branch_of = {b["from"]: b for b in graph.get("branches", [])}

    # A work node whose step has substeps does them ONE PER TURN, and the
    # checker that sends it round again is the one with a `next` case to it.
    looping = {n for n, spec in specs.items()
               if spec.role in WORK_ROLES and _has_substeps(spec, plan)}
    loop_checker = {}
    for name in looping:
        sources = [b["from"] for b in graph.get("branches", [])
                   if any(c["when"] == "next" and c["to"] == name for c in b["cases"])]
        if not sources:
            problems.append(f"work node {name!r} does its step's substeps one per "
                            f"turn, so the checker that reads it needs a case "
                            f"`next` back to it, to send it on to the next substep.")
        else:
            loop_checker[name] = sources[0]
    if problems:
        raise SpecError(problems)

    fields = {name: _fields(spec, branch_of.get(name), name in looping)
              for name, spec in specs.items() if spec.role != "human"}

    # Who sends the run to whom, for "why you are here".
    senders: dict[str, list[str]] = {}
    for branch in graph.get("branches", []):
        for case in branch["cases"]:
            senders.setdefault(case["to"], []).append(branch["from"])
    feedback_vars: dict[str, list[str]] = {}
    for name, spec in specs.items():
        for command in spec.commands:
            for pair in command.sets:
                feedback_vars.setdefault(command.to, []).append(pair.name)

    entries = []
    for name in order:
        spec = specs[name]
        if spec.role == "human":
            entries.append({"name": name, "commands": [c.model_dump(mode="json")
                                                       for c in spec.commands]})
            continue
        reads = reads_of[name]
        entries.append(_entry(spec, fields, reads, sorted(set(senders.get(name, []))),
                              sorted(set(feedback_vars.get(name, []))),
                              branch_of.get(name), specs, plan, steps_for,
                              loop_checker=loop_checker.get(name, "")))

    return {"task_brief": design.task_brief.strip(), "rationale": design.rationale.strip(),
            "graph": graph, "nodes": entries}


def _plan_contract(specs: dict, plan: dict) -> list[str]:
    """The work nodes are exactly the plan's leaf steps, one each, unchanged.

    The designer is not allowed to re-cut the work: a node per step is what
    makes "the agent did the plan" checkable, node by node. Problems with the
    plan's own cutting go in `plan_issues` and back to the person.
    """
    from agent.bootstrap.blueprint import work_nodes

    if not (plan or {}).get("steps"):
        return []
    out = []
    fixed = {w["name"]: w for w in work_nodes(plan)}
    for name, w in fixed.items():
        spec = specs.get(name)
        if spec is None:
            out.append(f"work node {name!r} (plan step {w['step']}) is missing. The "
                       f"work nodes are fixed: keep every one, with its name.")
        elif spec.role not in WORK_ROLES:
            out.append(f"work node {name!r} must have role worker or runner, not "
                       f"{spec.role}.")
        elif list(spec.steps) != [w["step"]]:
            out.append(f"work node {name!r} must own exactly step {w['step']}, "
                       f"not {list(spec.steps)}.")
    for name, spec in specs.items():
        if name in fixed:
            continue
        if spec.role in WORK_ROLES:
            out.append(f"node {name!r} is a {spec.role} but not one of the plan's "
                       f"work nodes. Work the plan does not have cannot be added; "
                       f"put it in plan_issues instead.")
        elif spec.steps:
            out.append(f"node {name!r} is a {spec.role} and owns no plan steps; "
                       f"remove {list(spec.steps)}.")
    return out


def _has_substeps(spec, plan: dict) -> bool:
    """Whether a work node's step has substeps -- even one: the same rule
    blueprint.work_nodes uses, so a starting design and its compile agree."""
    owned = set(spec.steps)
    return any(str(step.get("id")) in owned and step.get("substeps")
               for step in (plan or {}).get("steps", []))


def _fields(spec, branch, looping: bool = False) -> list[dict]:
    """The output fields: the role's, the spec's extras, and the branch's enum."""
    out = [_field(n, d, required=True) for n, d in STANDARD_FIELDS[spec.role]]
    if looping:
        out.append(_field("substep", "The id of the ONE substep you did this turn.",
                          required=True))
    if branch and any(c["when"] == "next" for c in branch["cases"]):
        out.append(_field("next_substep", "The substep the node does next: the "
                                          "following one after 'next', the same "
                                          "one after 'redo'.", required=True))
    decision = DECISION.get(spec.role)
    if branch:
        choices = list(dict.fromkeys(c["when"] for c in branch["cases"]))
        decision = (branch["route_on"], choices)
    if decision:
        name, choices = decision
        out = [f for f in out if f["name"] != name]
        out.insert(0, {"name": name, "type": "enum", "choices": choices,
                       "description": "", "required": True})
    taken = {f["name"] for f in out}
    out += [_field(e.name, e.description) for e in spec.outputs if e.name not in taken]
    return out[:12]


def _inputs(name: str, specs: dict, graph: dict, seen=None) -> list[str]:
    """What a node reads when its spec does not say: the nodes that lead
    straight into it -- and through a checker or router, to the work THAT
    judged. A worker after a stage's checker reads that stage's work, not the
    checker's verdict, which only said "ok"."""
    seen = set(seen or ()) | {name}
    order = [ref["name"] for ref in graph["nodes"]]
    into = [e["from"] for e in graph.get("edges", []) if e["to"] == name]
    into += [b["from"] for b in graph.get("branches", [])
             if any(c["to"] == name for c in b["cases"])]
    # Only nodes EARLIER in graph.nodes are inputs. A later one leading here
    # is a loop back -- a checker's redo, a router going round again -- and
    # that is feedback, which the node is shown separately as "why it came
    # back", not something it consumes.
    into = [n for n in into if n in order and order.index(n) < order.index(name)]
    out = []
    for source in dict.fromkeys(into):
        spec = specs.get(source)
        if spec is None or spec.role == "human" or source in seen:
            continue
        if spec.role in ("checker", "router") and specs[name].role in WORK_ROLES:
            out += list(spec.reads) or _inputs(source, specs, graph, seen)
        else:
            out.append(source)
    return [n for n in dict.fromkeys(out) if n != name]


def _report(name: str, fields: dict, specs: dict | None = None) -> str:
    """One node's outputs, as another node is shown them -- and for a work
    node, the files its turn actually changed, recorded by code."""
    lines = [f"### {name}"]
    for field in fields[name]:
        lines.append(f"{field['name']}: {{out.{name}.{field['name']}}}")
    if specs and specs[name].role in WORK_ROLES:
        lines.append(f"files its turn changed (recorded, not reported):\n"
                     f"{{out.{name}.turn_changes}}")
    return "\n".join(lines)


def _choices(branch) -> str:
    if not branch:
        return ""
    lines = [f"Return `{branch['route_on']}` as one of:"]
    for case in branch["cases"]:
        target = "the end of the run" if case["to"] == "__end__" else case["to"]
        lines.append(f"- '{case['when']}': the run goes to {target}")
    return "\n".join(lines)


def _entry(spec, fields, reads, senders, variables, branch, specs, plan, steps_for,
           *, loop_checker: str = "") -> dict:
    name, role = spec.name, spec.role
    reports = "\n\n".join(_report(r, fields, specs if role == "checker" else None)
                          for r in reads if r in fields)
    why = "\n\n".join(_report(s, fields) for s in senders if s in fields and s != name)

    if role in ("worker", "runner"):
        first = ("{task_brief}\n\nYOUR STEP -- the only work that is yours:\n{my_steps}\n\n"
                 + (f"This step has substeps, and you do exactly ONE per turn, each "
                    f"checked before the next. This turn: substep "
                    f"{{out.{loop_checker}.next_substep}} -- or, if that is blank, "
                    f"the first one. Do only that substep, and put its id in "
                    f"`substep`.\n\n" if loop_checker else "")
                 + "The whole plan, for context only:\n{plan_outline}\n\n"
                 "The repository is at {repo_path}. What the run produces goes under "
                 "{artifacts_dir}.")
        if reports:
            first += f"\n\nWHAT THE NODES BEFORE YOU REPORTED:\n\n{reports}"
        if why:
            first += f"\n\nWHY YOU WERE SENT HERE:\n\n{why}"
        unit = "substep" if loop_checker else "step"
        first += (f"\n\nRun your {unit}: results under {{artifacts_dir}}, nothing "
                  f"overwritten, no edits to the code. Report what happened."
                  if role == "runner" else
                  f"\n\nDo your {unit} completely, run it, and report every file you "
                  f"created or changed, the command that runs it, and a summary.")
        parts = ["Your step again:\n{my_steps}\n\nIt came back."]
        if why:
            parts.append(f"WHY IT CAME BACK:\n\n{why}")
        parts += [f"The person's note:\n{{var.{v}}}" for v in variables]
        parts.append("What the person said last, if anything:\n{last_answer}")
        parts.append("Fix exactly that -- do not start again from scratch, and do not "
                     "change what the complaint does not mention. Then report as before.")
        following = "\n\n".join(parts)
    else:
        if role == "checker":
            asked = steps_for(plan or {}, [s for r in reads if r in specs for s in specs[r].steps])
            first = (f"Check this work against what was asked.\n\nWHAT WAS ASKED:\n"
                     f"{_defuse(asked) or '(see each report)'}\n\n")
        else:
            first = "{task_brief}\n\nThe plan:\n{plan_outline}\n\n"
        first += (f"WHAT WAS REPORTED:\n\n{reports or '(nothing yet)'}\n\n"
                  f"The repository is at {{repo_path}}; results are under "
                  f"{{artifacts_dir}}.\n\n{_choices(branch)}")
        if role == "checker" and branch and any(c["when"] == "next" for c in branch["cases"]):
            first += ("\n\nThe node works through its substeps ONE PER TURN and names "
                      "the one it did in `substep`. Check THAT substep against its "
                      "own check. 'next' when it passes and substeps remain, with "
                      "`next_substep` set to the following one; 'ok' only when the "
                      "LAST substep has passed; 'redo' with `next_substep` set to the "
                      "same substep.")
        if role == "checker":
            first += ("\n\nOnly send work on if each step's check is satisfied AND "
                      "each node changed only what its step needs -- compare the "
                      "files its turn changed with what was asked. A complaint "
                      "must be specific enough to act on. In `evidence`, list "
                      "what you ran or read and what it showed.")
        else:
            first += ("\n\nSay why in `reason`, and in `next_task` exactly what the "
                      "node you choose should do.")
        following = (f"This came back after your last decision.\n\nWHAT WAS REPORTED:"
                     f"\n\n{reports or '(nothing yet)'}\n\n{_choices(branch)}\n\n"
                     f"You have seen this before. If the same problem is still there, "
                     f"do not send it round the same loop again: choose the option "
                     f"that stops for a person, if there is one.")

    instructions = RULES[role]
    if spec.instructions.strip():
        instructions += "\n\nFor this node in particular:\n" + _defuse(spec.instructions.strip())
    backend = LIGHT_ROLE if role == "worker" and spec.tier == "light" else ROLE[role]
    return {
        "name": name, "backend": backend, "access": ACCESS[role],
        "steps": list(spec.steps), "instructions": instructions,
        "output": fields[name], "prompts": {"first": first, "next": following},
        # A looping node starts a fresh conversation per substep, so its
        # context is one substep's, not the whole step's history; a redo of
        # the same substep continues that substep's conversation.
        "thread_key": f"{{out.{loop_checker}.next_substep}}" if loop_checker else "",
        "refresh_on": "", "bump": [],
        # A work node's own changes, recorded by code for its checker: what it
        # actually did, rather than what it says it did.
        "capture": ["turn_changes"] if role in WORK_ROLES else [], "record": "",
        "announce": (f"working on step {', '.join(spec.steps)}..." if spec.steps
                     else f"{name}..."),
        "commands": [],
    }


def _field(name, description, *, required=False) -> dict:
    return {"name": name, "type": "string", "choices": [], "description": description,
            "required": required}


def _defuse(text: str) -> str:
    """Stop prose from the plan or a model being read as a placeholder.

    "{seed}" in a step's detail would otherwise be an unknown placeholder and
    fail validation; "{ seed }" is not a placeholder and reads the same.
    """
    return _TOKEN.sub(lambda m: "{ " + m.group(1) + " }", text)
