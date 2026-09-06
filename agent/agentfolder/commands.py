"""
WHAT:  Turns a folder's human-node commands into slash-command registry rows.
WHY:   /help inside a generated agent is generated from that agent's own JSON,
       so it can never drift from what the agent actually accepts.
CONCEPT: Not LangGraph. This is the bridge between the folder format and
       agent/commands.py, which is why commands.parse() gained a registry=
       parameter in step 0.
"""

from __future__ import annotations

from agent.agentfolder.load import AgentFolder
from agent.agentfolder.schema import HumanNodeConfig
from agent.commands import REGISTRY, SlashCommand


def build_registry(folder: AgentFolder) -> tuple[SlashCommand, ...]:
    """Terminal-scope commands + whatever this folder's human nodes accept.

    The terminal-scope rows (/help /usage /state /config /transcript) are
    universal -- they inspect the run rather than steer it -- so every phase
    gets them. Everything else comes from the folder.
    """
    rows: list[SlashCommand] = [c for c in REGISTRY if c.scope == "terminal"]
    seen = {c.name for c in rows}

    for config in folder.nodes.values():
        if not isinstance(config, HumanNodeConfig):
            continue
        for command in config.commands:
            if command.name in seen:
                # A folder may not shadow /help or /usage. Silently skipping is
                # right: the folder is still usable, and validate.py has
                # already told the designer about the clash.
                continue
            seen.add(command.name)
            rows.append(
                SlashCommand(
                    name=command.name,
                    scope="graph",
                    summary=command.summary or f"Go to {command.to}",
                    aliases=tuple(command.aliases),
                    argument=command.argument,
                    contexts=tuple(command.purposes) if command.purposes else None,
                )
            )

    return tuple(rows)
