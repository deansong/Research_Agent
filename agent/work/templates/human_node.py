"""
WHAT:  The `human` node template -- pause, show a question, interpret the reply.
WHY:   Every generated agent that needs a person uses this one function, so
       there is exactly one interrupt point per graph and one place that
       parses commands.
CONCEPT: interrupt() and Command(resume=...).

Routing here is deliberately NOT configurable, and that is the one place the
format is less general on purpose:

    plain text            -> go to pending.resume_to (whoever asked)
    a valid slash command -> go to that command's `to`
    anything else         -> come back here and ask again

Making that configurable would let a folder describe a human node you cannot
escape from. The commands themselves are fully configurable; the skeleton is
not.
"""

from __future__ import annotations

from typing import Any

from langgraph.types import interrupt

from agent import commands as command_lib
from agent.agentfolder.render import render
from agent.agentfolder.schema import HumanNodeConfig
from agent.work.state import WorkState, render_context


def make_human_node(name: str, config: HumanNodeConfig, *, registry):
    """Build the node function for one `kind: "human"` entry."""

    by_name = {c.name: c for c in config.commands}
    for command in config.commands:
        for alias in command.aliases:
            by_name[alias] = command

    def human_node(state: WorkState) -> dict[str, Any]:
        # ---- step 1: what are we asking? (runs again on every resume) -----
        # Everything above interrupt() re-executes when the graph is resumed,
        # so it must stay free of side effects: no model calls, no writes.
        pending = dict(state.get("pending", {}))
        purpose = pending.get("purpose", "")

        payload = {
            "type": "human_input",
            "purpose": purpose,
            "question": pending.get("question") or "Your input is required.",
            "context": pending.get("context", ""),
        }

        # ---- step 2: STOP. Below here runs once, after a resume. ----------
        answer = str(interrupt(payload)).strip()

        # Parsed with THIS agent's registry, built from its own nodes.json --
        # so /help and what is actually accepted cannot drift apart.
        parsed = command_lib.parse(answer, purpose=purpose, registry=registry)

        if parsed.kind == "rejected":
            # Route back to ourselves. Measured: re-entering a node that calls
            # interrupt() produces a FRESH interrupt rather than replaying the
            # old resume value, which is what makes "ask again" work.
            print(f"\n{parsed.error}")
            return {"route": {name: name}}

        if parsed.kind == "command":
            command = by_name.get(parsed.command.name)
            if command is None:
                # A terminal-scope command reached the graph. Should not happen
                # via our REPL, but a different driver might do it.
                return {"route": {name: name}}
            return _apply_command(name, command, parsed.text, state)

        # ---- step 3: a plain answer goes back to whoever asked ------------
        target = pending.get("resume_to") or name
        return {
            "route": {name: target},
            "last_answer": parsed.text,
            "transcript": [{"role": "human", "text": parsed.text}],
            "pending": {},
        }

    return human_node


def _apply_command(name: str, command, argument: str, state: WorkState) -> dict[str, Any]:
    context = render_context(state, argument=argument)
    update: dict[str, Any] = {"route": {name: command.to}, "pending": {}}

    if command.sets:
        update["vars"] = {key: render(value, context) for key, value in command.sets.items()}
    if command.record:
        text = render(command.record, context).strip()
        if text:
            update["transcript"] = [{"role": "human", "text": text}]
    if command.outcome:
        update["outcome"] = command.outcome
        if command.outcome == "new_task":
            update["next_request"] = argument
    elif command.to == "__end__":
        update["outcome"] = "exit"

    return update
