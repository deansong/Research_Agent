"""
WHAT:  Reading, checking and writing the two editable artifacts -- plan.json
       and the agent folder -- plus the per-node context the inspector shows.
WHY:   Both are already Pydantic models with `extra="forbid"`, so an editor
       that round-trips them carelessly corrupts them in ways that only show up
       later. All that care belongs in one file.
CONCEPT: Validate on the way in, and validate from DISK.

--------------------------------------------------------------------------
THREE TRAPS, ALL PREVIOUSLY PAID FOR
--------------------------------------------------------------------------
1. **`from` is a reserved word in Python.** EdgeSpec and BranchSpec call the
   field `from_` with `alias="from"`. Dump without `by_alias=True` and you
   write `"from_"` into graph.json, which the loader then rejects -- so the
   agent you just "saved" no longer loads.

2. **`extra="forbid"` everywhere.** A stray key the browser round-trips is a
   hard validation error, not an ignored field. Good: it catches drift early.
   It does mean the editor must build its payload from the schema.

3. **Duplicate JSON keys collapse silently.** `{"a": 1, "a": 2}` parses to
   `{"a": 2}` with no complaint, so a nodes.json with two `worker` entries
   loses one without a word. Only a write-then-reload catches it, which is why
   saving goes through the staging directory rather than validating in memory.
   (`agent/bootstrap/nodes/validator.py` makes the same point for the same
   reason.)

--------------------------------------------------------------------------
SAVE IS ALLOWED TO PRODUCE AN INVALID AGENT
--------------------------------------------------------------------------
`save_folder` writes even when validation fails, and returns the problems. That
is deliberate: you cannot rewire a graph without passing through states where a
node is briefly unreachable, and an editor that refuses those is an editor you
cannot use. Running is what is gated -- `agent/cli.py` already refuses to run a
folder with blocking problems and says why, so a half-wired agent on disk is a
clear message rather than a crash.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.agentfolder.load import AgentFolderError, load_agent_folder
from agent.agentfolder.schema import (
    END_TARGET,
    AgentNodeConfig,
    GraphFile,
    HumanNodeConfig,
    graph_document,
    node_entry,
)
from agent.agentfolder.validate import Problem, validate_folder
from agent.bootstrap.nodes.planner import outline, steps_for
from agent.bootstrap.schemas import PlannerOutput

#: How every JSON file in a session folder is written. Matching the writer's
#: formatting matters more than it looks: an editor that reformats a file makes
#: `git diff` on a promoted agent unreadable.
INDENT = 2


def dump(data: Any) -> str:
    return json.dumps(data, indent=INDENT) + "\n"


# ---------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------


def read_plan(paths) -> dict:
    """plan.json, or {} if there is none yet or it is unreadable."""
    if not paths.plan.exists():
        return {}
    try:
        return json.loads(paths.plan.read_text())
    except json.JSONDecodeError:
        return {}


def write_plan(paths, plan: dict) -> list[Problem]:
    """Validate against PlannerOutput and write, or return problems and don't.

    Unlike the folder, the plan IS gated on validity -- and the asymmetry is
    intentional. A plan is a small tree with no cross-references, so there is no
    half-finished state to pass through; anything that fails here is a genuine
    mistake rather than a step on the way somewhere.

    Note where this lands: `agent/bootstrap/nodes/human.py` re-reads plan.json
    from disk when you type /approve, and says "using your edited plan.json" if
    it differs from what the planner produced. The editor is not a new feature
    so much as a front end for a hook that already existed.
    """
    try:
        checked = PlannerOutput.model_validate(plan)
    except Exception as exc:  # pydantic.ValidationError
        return [Problem(code="bad_plan", where=str(paths.plan),
                        message=_pydantic_message(exc))]

    problems = list(_plan_warnings(checked))
    paths.plan.write_text(dump(checked.model_dump(mode="json")))
    return problems


def _plan_warnings(plan: PlannerOutput):
    """Things worth saying about a valid plan.

    Duplicate ids are the one that bites: `steps_for()` matches on id, so two
    steps numbered "2" means a node asking for step 2 gets both -- silently.
    """
    seen: dict[str, str] = {}
    for step in plan.steps:
        for identifier, what in [(step.id, f"step {step.id}")] + [
            (sub.id, f"substep {sub.id}") for sub in step.substeps
        ]:
            if identifier in seen:
                yield Problem(
                    code="duplicate_step_id",
                    where=what,
                    message=(f"id {identifier!r} is used twice. A node asking for "
                             f"it gets both, with no warning."),
                    warning=True,
                )
            seen[identifier] = what


def plan_step_ids(plan: dict) -> set[str]:
    ids: set[str] = set()
    for step in (plan or {}).get("steps", []):
        ids.add(step.get("id", ""))
        ids.update(sub.get("id", "") for sub in step.get("substeps", []))
    return ids - {""}


# ---------------------------------------------------------------------------
# the agent folder
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SaveResult:
    saved: bool
    problems: list[Problem]
    error: str = ""


def read_folder(path: Path) -> dict:
    """The whole agent as one document: {"graph": ..., "nodes": ...}.

    One document rather than two endpoints because the browser has to keep them
    consistent -- graph.json's node list and nodes.json's keys must match -- and
    fetching them separately invites a UI that has one of them stale.
    """
    folder = load_agent_folder(path)
    return {
        "path": str(path),
        "graph": folder.graph.model_dump(mode="json", by_alias=True),
        "nodes": {
            name: config.model_dump(mode="json", by_alias=True)
            for name, config in folder.nodes.items()
        },
    }


def save_folder(path: Path, document: dict, *, staging: Path) -> SaveResult:
    """Write an edited agent, checking it the way the design phase does.

    The staging dance is not ceremony. We write both files to `staging`, load
    them BACK from disk, and validate what came back -- because a duplicate key
    in nodes.json vanishes during parsing and can only be caught by a round
    trip. Only then does staging replace the real folder.

    If anything is structurally unreadable we refuse and keep the old folder.
    If it merely has validation PROBLEMS we still save it, and say so; see the
    module docstring on why an editor must allow that.
    """
    try:
        graph = GraphFile.model_validate({"format_version": 1, **document.get("graph", {})})
    except Exception as exc:  # pydantic.ValidationError
        return SaveResult(saved=False, problems=[], error=_pydantic_message(exc))

    kinds = {ref.name: ref.kind for ref in graph.nodes}
    nodes: dict[str, Any] = {}
    for name, raw in (document.get("nodes") or {}).items():
        kind = kinds.get(name)
        model = HumanNodeConfig if kind == "human" else AgentNodeConfig
        try:
            checked = model.model_validate(raw)
        except Exception as exc:  # pydantic.ValidationError
            return SaveResult(saved=False, problems=[],
                              error=f"node {name!r}: {_pydantic_message(exc)}")
        # node_entry, not a bare model_dump: the design phase writes nodes.json
        # through the same function, and when the two disagreed a no-op save
        # here rewrote every node in the file.
        # exclude_defaults reaches NESTED models too, which is the only way to
        # drop `"choices": []` and `"required": false` from every output field
        # and `"ask": null` from every edge. node_entry then orders what is
        # left and forces `access` back in, in position.
        nodes[name] = node_entry(
            kind,
            checked.model_dump(mode="json", by_alias=True, exclude_defaults=True),
        )

    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    (staging / "graph.json").write_text(
        dump(graph_document(
            graph.model_dump(mode="json", by_alias=True, exclude_defaults=True)
        ))
    )
    (staging / "nodes.json").write_text(dump(nodes))

    # Load back from disk. This is the only way to catch a nodes.json whose
    # duplicate keys collapsed during the write.
    try:
        reloaded = load_agent_folder(staging)
    except AgentFolderError as exc:
        return SaveResult(saved=False, problems=[], error=str(exc))

    problems = list(validate_folder(reloaded))

    if path.exists():
        shutil.rmtree(path)
    staging.rename(path)

    return SaveResult(saved=True, problems=problems)


def graph_view(folder, plan: dict | None = None) -> dict:
    """The topology as nodes and edges a graph library can draw.

    Built from the folder SPEC, exactly as `render.mermaid` is, so it works
    before backends exist and before anything is compiled -- which is precisely
    when you most want to look at a graph that will not run.

    Three edge kinds, and keeping them distinct is the point:
      edge     an unconditional transition
      case     one arm of a branch, labelled with the value that selects it
      command  a slash command on a human node, which lives in nodes.json
               rather than graph.json -- so a picture drawn from graph.json
               alone would show human nodes as dead ends.
    """
    plan_ids = plan_step_ids(plan or {})
    nodes = []

    for ref in folder.graph.nodes:
        config = folder.nodes.get(ref.name)
        steps = list(getattr(config, "steps", []) or [])
        nodes.append({
            "id": ref.name,
            "kind": ref.kind,
            "entry": ref.name == folder.graph.entry,
            "backend": getattr(config, "backend", ""),
            "access": getattr(config, "access", ""),
            "announce": getattr(config, "announce", ""),
            "steps": steps,
            # Nothing in the agent itself checks this -- steps_for() just drops
            # an id it does not recognise -- so an editor is the only place it
            # can be caught.
            "unknown_steps": sorted(s for s in steps if plan_ids and s not in plan_ids),
            "commands": [c.name for c in getattr(config, "commands", [])],
        })

    nodes.append({"id": END_TARGET, "kind": "end", "entry": False,
                  "steps": [], "unknown_steps": [], "commands": []})

    edges = []
    for edge in folder.graph.edges:
        edges.append({"from": edge.from_, "to": edge.to, "kind": "edge",
                      "label": "", "asks": bool(edge.ask)})

    for branch in folder.graph.branches:
        for case in branch.cases:
            edges.append({"from": branch.from_, "to": case.to, "kind": "case",
                          "label": f"{branch.route_on}={case.when}",
                          "asks": bool(case.ask)})
        edges.append({"from": branch.from_, "to": branch.default, "kind": "case",
                      "label": "otherwise", "asks": False})

    for name, config in folder.nodes.items():
        if isinstance(config, HumanNodeConfig):
            for command in config.commands:
                edges.append({"from": name, "to": command.to, "kind": "command",
                              "label": f"/{command.name}", "asks": False})

    return {"name": folder.graph.name, "description": folder.graph.description,
            "entry": folder.graph.entry, "nodes": nodes, "edges": edges}


# ---------------------------------------------------------------------------
# what one node actually sees
# ---------------------------------------------------------------------------


def node_context(folder, name: str, *, state: dict, artifacts_dir: str = "",
                 session_dir: str = "") -> dict:
    """The prompt a node will really receive, rendered against current state.

    The reason this is worth a whole endpoint: what you type into `nodes.json`
    is a template, and what the model reads is that template with eight kinds of
    placeholder filled in from live state. Reading the template tells you very
    little. A placeholder that resolves to nothing renders as an empty string
    rather than raising -- correct, since a node may legitimately not have run
    yet -- so a semantically wrong prompt validates perfectly clean and only
    looks wrong here.
    """
    from agent.agentfolder.render import render
    from agent.work.compile import infrastructure_rules
    from agent.work.state import render_context

    config = folder.nodes.get(name)
    if config is None:
        raise KeyError(name)

    if isinstance(config, HumanNodeConfig):
        return {
            "name": name,
            "kind": "human",
            "commands": [c.model_dump(mode="json") for c in config.commands],
        }

    context = render_context(state, steps=list(config.steps))
    plan = state.get("plan") or {}

    # A write-access node's real instructions are longer than its file says:
    # the compiler appends the working rules so a generated agent cannot opt
    # out of them. Showing only nodes.json would misrepresent what runs.
    appended = ""
    if config.access == "write":
        appended = infrastructure_rules(artifacts_dir, session_dir)

    return {
        "name": name,
        "kind": "agent",
        "backend": config.backend,
        "access": config.access,
        "steps": list(config.steps),
        "instructions": config.instructions,
        "appended_instructions": appended,
        "prompts": {
            "first": {"template": config.prompts.first,
                      "rendered": render(config.prompts.first, context)},
            "next": {"template": config.prompts.next,
                     "rendered": render(config.prompts.next or config.prompts.first,
                                        context)},
        },
        "my_steps": steps_for(plan, list(config.steps)),
        "plan_outline": outline(plan),
        "thread_key": {"template": config.thread_key,
                       "rendered": render(config.thread_key, context)},
        "output": [field.model_dump(mode="json") for field in config.output],
        "last_output": (state.get("outputs") or {}).get(name, {}),
        "usage": {k: v for k, v in (state.get("usage") or {}).items()
                  if k.startswith(f"{name}/")},
    }


def _pydantic_message(exc: Exception) -> str:
    """Pydantic errors as one readable line per problem.

    `str(ValidationError)` is a multi-paragraph block with a docs URL in it;
    fine in a terminal, wrong in a toast.
    """
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return str(exc)
    return "; ".join(
        f"{'.'.join(str(p) for p in e.get('loc', ())) or 'body'}: {e.get('msg', '')}"
        for e in errors()
    )
