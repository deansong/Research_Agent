"""
WHAT:  Reads an agent folder off disk and schema-parses it.
WHY:   One place that knows the two filenames and turns bad input into a
       message naming the file and the offending key.
CONCEPT: Not LangGraph. Loading is separate from validating on purpose: this
       answers "is it well-formed JSON of the right shape?", validate.py
       answers "is it a sensible graph?". The bootstrap validator node needs
       them separate so it can report shape problems back to the designer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from agent.agentfolder.schema import (
    AgentNodeConfig,
    GraphFile,
    HumanNodeConfig,
    NodeConfig,
)

GRAPH_FILE = "graph.json"
NODES_FILE = "nodes.json"


class AgentFolderError(RuntimeError):
    """The folder could not be read or does not match the schema."""


@dataclass(frozen=True)
class AgentFolder:
    """A parsed agent folder. Lives in memory only -- never in graph state."""

    path: Path
    graph: GraphFile
    nodes: dict[str, NodeConfig]

    def node_names(self) -> list[str]:
        return [ref.name for ref in self.graph.nodes]

    def agent_nodes(self) -> dict[str, AgentNodeConfig]:
        return {
            name: config
            for name, config in self.nodes.items()
            if isinstance(config, AgentNodeConfig)
        }


def load_agent_folder(path: Path) -> AgentFolder:
    """Read <path>/graph.json and <path>/nodes.json.

    Raises AgentFolderError with the file and the JSON pointer on any problem.
    Does NOT check graph shape -- see validate.py.
    """
    path = Path(path)
    graph_raw = _read_json(path / GRAPH_FILE)
    nodes_raw = _read_json(path / NODES_FILE)

    try:
        graph = GraphFile.model_validate(graph_raw)
    except ValidationError as exc:
        raise AgentFolderError(f"{path / GRAPH_FILE}:\n{_format(exc)}") from None

    if not isinstance(nodes_raw, dict):
        raise AgentFolderError(f"{path / NODES_FILE}: expected a JSON object keyed by node name.")

    # Which model to use per entry comes from graph.json's `kind`. A name in
    # nodes.json with no entry in graph.json has no kind, so it is parsed
    # leniently here and reported by validate.py as `config_extra` -- one
    # clear message instead of a confusing schema error.
    kinds = {ref.name: ref.kind for ref in graph.nodes}
    nodes: dict[str, NodeConfig] = {}

    for name, entry in nodes_raw.items():
        model = HumanNodeConfig if kinds.get(name) == "human" else AgentNodeConfig
        if name not in kinds:
            nodes[name] = _lenient(name, entry)
            continue
        try:
            nodes[name] = model.model_validate(entry)
        except ValidationError as exc:
            raise AgentFolderError(
                f"{path / NODES_FILE}, node {name!r}:\n{_format(exc)}"
            ) from None

    return AgentFolder(path=path, graph=graph, nodes=nodes)


def _lenient(name: str, entry: object) -> NodeConfig:
    """Best-effort parse for a node graph.json does not mention."""
    for model in (AgentNodeConfig, HumanNodeConfig):
        try:
            return model.model_validate(entry)
        except ValidationError:
            continue
    return HumanNodeConfig()


def _read_json(path: Path):
    if not path.exists():
        raise AgentFolderError(f"Missing {path.name}: {path}")
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise AgentFolderError(f"{path}: not valid JSON ({exc}).") from None


def _format(exc: ValidationError) -> str:
    lines = []
    for error in exc.errors():
        where = ".".join(str(part) for part in error["loc"]) or "(root)"
        lines.append(f"  {where}: {error['msg']}")
    return "\n".join(lines)
