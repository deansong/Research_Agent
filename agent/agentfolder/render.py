"""
WHAT:  Fills {placeholders} in prompts, questions and recorded lines.
WHY:   A prompt in nodes.json needs to refer to state -- the task, what another
       node produced, what the human just said.
CONCEPT: Not LangGraph. But this is where a generated agent reads the work
       graph's state, so the vocabulary here IS the contract between the folder
       format and agent/work/state.py.

WHY NOT str.format():
    Prompts are prose written by a model. They routinely contain a stray brace
    or an embedded JSON example, and str.format() raises KeyError/IndexError on
    those -- turning "the model wrote a helpful example" into a crash.
    So we scan for a NARROW pattern instead: {name} or {a.b} or {a.b.c} in
    lowercase with underscores. `{ "json": "example" }` has a space after the
    brace and does not match, so it survives untouched. That behaviour is
    tested; do not "simplify" this to str.format.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

# Deliberately narrow: lowercase, digits, underscores and dots only, no spaces.
_TOKEN = re.compile(r"\{([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*)\}")

# The simple names. Anything else must be an {out.*} or {var.*} lookup.
SIMPLE_TOKENS = ("task_brief", "transcript", "last_answer", "repo_path", "argument")


def find_tokens(text: str) -> list[str]:
    """Every placeholder in `text`, in order of appearance."""
    return _TOKEN.findall(text or "")


def token_is_known(token: str, *, node_names: set[str], var_names: set[str]) -> bool:
    """Could this placeholder ever resolve? Used by the validator."""
    if token in SIMPLE_TOKENS:
        return True

    parts = token.split(".")
    if len(parts) == 3 and parts[0] == "out":
        # {out.<node>.<field>} -- the field itself is checked separately, since
        # `capture` adds fields that are not declared in `output`.
        return parts[1] in node_names
    if len(parts) == 2 and parts[0] == "var":
        return parts[1] in var_names
    return False


def render(text: str, context: Mapping[str, Any]) -> str:
    """Replace every known placeholder in `text`.

    `context` is built by the caller from work state:
        {"task_brief": ..., "transcript": ..., "last_answer": ...,
         "repo_path": ..., "argument": ...,
         "out": {node: {field: value}}, "var": {name: value}}

    An unresolvable placeholder becomes an EMPTY STRING rather than raising.
    That is correct -- {out.executor.summary} in the orchestrator's opening
    prompt legitimately has no value, because the executor has not run yet.
    The cost is that a semantically wrong prompt renders clean, which is why
    `--explain` can print every prompt rendered against empty state.
    """

    def replace(match: re.Match[str]) -> str:
        return _resolve(match.group(1), context)

    return _TOKEN.sub(replace, text or "")


def _resolve(token: str, context: Mapping[str, Any]) -> str:
    if token in SIMPLE_TOKENS:
        return _stringify(context.get(token, ""))

    parts = token.split(".")

    if len(parts) == 3 and parts[0] == "out":
        outputs = context.get("out") or {}
        node = outputs.get(parts[1]) or {}
        return _stringify(node.get(parts[2], ""))

    if len(parts) == 2 and parts[0] == "var":
        variables = context.get("var") or {}
        return _stringify(variables.get(parts[1], ""))

    # Unknown shape. The validator should have caught it; at render time we
    # leave the text alone so the mistake is visible in the prompt rather than
    # silently deleted.
    return "{" + token + "}"


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return "\n".join(f"- {_stringify(item)}" for item in value)
    return str(value)


def mermaid(folder) -> str:
    """Draw a folder's topology as a mermaid diagram.

    Deliberately drawn from the SPEC rather than from a compiled graph: it
    works before backends exist and before anything is compiled, which is what
    lets `--explain` and a failed design still show you the shape.

    (LangGraph can draw a compiled graph itself, but draw_ascii() needs the
    `grandalf` package and draw_mermaid() needs a compiled graph -- neither is
    available at the moment you most want the picture.)
    """
    from agent.agentfolder.schema import HumanNodeConfig

    lines = ["graph TD;", f"  __start__ --> {folder.graph.entry};"]

    for edge in folder.graph.edges:
        lines.append(f"  {edge.from_} --> {edge.to};")

    for branch in folder.graph.branches:
        for case in branch.cases:
            lines.append(f"  {branch.from_} -. {branch.route_on}={case.when} .-> {case.to};")
        lines.append(f"  {branch.from_} -. otherwise .-> {branch.default};")

    for name, config in folder.nodes.items():
        if isinstance(config, HumanNodeConfig):
            for command in config.commands:
                lines.append(f"  {name} -. /{command.name} .-> {command.to};")

    return "\n".join(lines)
