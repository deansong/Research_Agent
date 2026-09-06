"""
WHAT:  Writes the designed folder to disk. Calls NO model.
WHY:   The designer already produced the folder as data. Putting a model here
       would be the one place generated Python could sneak in -- so this node
       is json.dump and nothing else. That makes "no exec, no eval, no
       importlib" a structural property rather than a promise.
CONCEPT: A LangGraph node with no LLM in it. They are allowed, and often the
       most important ones.

It writes to agent.tmp/, NOT agent/. The validator renames it only once the
folder passes. That rename is the durable signal that design succeeded: on
resume, "does sessions/<s>/agent/graph.json exist?" is an unambiguous answer,
where a half-written directory would not be.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from agent.bootstrap.state import BootstrapState
from agent.statelib import merge_section

# graph.json carries these; everything else in a NodeProposal belongs to
# nodes.json.
_GRAPH_KEYS = {"name", "description", "entry", "nodes", "edges", "branches"}

_AGENT_KEYS = (
    "backend", "access", "instructions", "output", "prompts",
    "thread_key", "refresh_on", "bump", "capture", "record", "announce",
)


def make_writer(paths):
    def writer(state: BootstrapState):
        design = dict(state.get("design", {}))
        proposal = design.get("proposal") or {}

        if not proposal:
            # The designer failed to produce anything. Pass straight through;
            # the validator takes the repair branch.
            return {}

        staging = Path(paths.staging)
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)

        graph = {"format_version": 1, **{k: v for k, v in proposal["graph"].items()
                                         if k in _GRAPH_KEYS}}
        kinds = {node["name"]: node["kind"] for node in graph.get("nodes", [])}

        nodes: dict[str, dict] = {}
        for entry in proposal.get("nodes", []):
            name = entry.get("name")
            if not name:
                continue
            if name in nodes:
                # The list->object conversion is where a duplicate name would
                # silently collapse, so we catch it here rather than letting
                # the second entry win.
                return _fail(design, f"Two nodes are both called {name!r}.")
            nodes[name] = _entry_for(kinds.get(name), entry)

        (staging / "graph.json").write_text(json.dumps(graph, indent=2) + "\n")
        (staging / "nodes.json").write_text(json.dumps(nodes, indent=2) + "\n")

        brief = design.get("task_brief", "").strip()
        if brief:
            Path(paths.brief).write_text(brief + "\n")

        print(f"[writer] wrote {len(nodes)} nodes to {staging}")

        return {"design": merge_section(design, written_to=str(staging))}

    return writer


def _entry_for(kind: str | None, entry: dict) -> dict:
    """Pick the subset of a NodeProposal that belongs to this kind of node.

    The proposal carries every field because a discriminated union is awkward
    for strict structured-output modes. Splitting here keeps the file on disk
    clean -- an agent node with a stray empty `commands` list would validate,
    but it would be confusing to read and to hand-edit.
    """
    if kind == "human":
        return {"commands": [_command(c) for c in entry.get("commands", [])]}

    out: dict = {}
    for key in _AGENT_KEYS:
        value = entry.get(key)
        if value in (None, "", [], {}):
            continue
        out[key] = value
    out.setdefault("access", "read_only")
    return out


def _command(proposal: dict) -> dict:
    """Convert a CommandProposal into the on-disk CommandSpec shape.

    Two conversions, both because strict structured output cannot express the
    nicer on-disk form:
      - `sets` arrives as name/value pairs, and becomes an object;
      - `purposes` arrives as a list, where empty means "everywhere", and
        becomes None (which is what CommandSpec uses for that).
    """
    out = {k: v for k, v in proposal.items()
           if k not in ("sets", "purposes") and v not in (None, "", [])}

    pairs = proposal.get("sets") or []
    if pairs:
        out["sets"] = {pair["name"]: pair["value"] for pair in pairs}

    purposes = proposal.get("purposes") or []
    if purposes:
        out["purposes"] = purposes

    return out


def _fail(design: dict, message: str) -> dict:
    return {
        "design": merge_section(
            design,
            proposal={},
            attempt=int(design.get("attempt", 0)),
            problems=message,
        )
    }
