"""
WHAT:  The terminal REPL. Starts or resumes a graph, shows whatever it asks,
       reads a line, feeds it back in.
WHY:   All human I/O in one file, so no graph ever calls input() or print() for
       control flow -- which keeps every graph drivable by a test or a future
       web UI.
CONCEPT: interrupt() / Command(resume=...) -- LangGraph's human-in-the-loop
       pause and resume. See the long comment on drive() below.

This file knows nothing about either state schema. Everything schema-specific
arrives through the GraphSession adapter (agent/session.py), which is what
lets the same loop drive the bootstrap graph and a generated agent.
"""

from __future__ import annotations

from typing import Any, Callable

from langgraph.types import Command

from agent import commands
from agent.session import GraphSession

# Returned by _ask_user when the human hits ctrl-D / ctrl-C. It is NOT "/exit":
# /exit runs the graph to END and finishes the session, whereas this leaves the
# graph parked exactly where it is so the next run can pick it up. The
# difference is the whole point of a checkpointer.
ABORT = object()


def drive(session: GraphSession, cfg) -> dict[str, Any]:
    """Run one graph until it ends. Returns its final state values.

    ------------------------------------------------------------------
    THE INTERRUPT / RESUME CYCLE -- the most important idea here
    ------------------------------------------------------------------
    A node can call `interrupt(payload)` to stop the whole graph and hand
    `payload` back to whoever called invoke(). Progress up to that point is
    already saved by the checkpointer, so the process can exit and come back.

    To continue you call invoke(Command(resume=value)). LangGraph re-runs the
    interrupted node, and this time its interrupt() call RETURNS `value`
    instead of stopping.

    Read that again, because it catches everybody:

        THE NODE FUNCTION RE-RUNS FROM ITS FIRST LINE.

    So anything a node does before interrupt() happens again on every resume.
    That is why the human templates build a payload and nothing else before
    interrupting -- no model calls, no writes.

    The loop below is just: invoke -> got an interrupt? -> ask -> resume.
    """
    graph, config = session.graph, session.config
    snapshot = graph.get_state(config)

    print(f"\n===== {session.title} =====")

    if snapshot.values and snapshot.next:
        # Non-empty `next` means the graph stopped mid-run last time, i.e. it
        # is parked on an interrupt waiting for an answer.
        print(f"(resuming {config['configurable']['thread_id']})")
        payload = session.resume_payload(snapshot.values)
        if payload is not None:
            answer = _ask_user(session, cfg, payload)
            if answer is ABORT:
                return graph.get_state(config).values or {}
            result = graph.invoke(Command(resume=answer), config=config)
        else:
            result = graph.invoke(None, config=config)
    else:
        result = graph.invoke(session.initial_state, config=config)

    while True:
        payload = _interrupt_payload(result)
        if payload is None:
            break
        answer = _ask_user(session, cfg, payload)
        if answer is ABORT:
            # Leave the graph parked on its interrupt. Nothing is invoked, so
            # the checkpoint still says "waiting at human", and the next run
            # with this --session resumes at the same question.
            break
        result = graph.invoke(Command(resume=answer), config=config)

    return graph.get_state(config).values or {}


def _interrupt_payload(result: dict[str, Any]) -> dict[str, Any] | None:
    """Pull the interrupt payload out of an invoke() result, or None.

    LangGraph reports interrupts under the reserved "__interrupt__" key. It is
    a list because several branches could interrupt in one step; our graphs
    have a single interrupt point and no fan-out, so index 0 is always right.
    """
    interrupts = result.get("__interrupt__")
    if not interrupts:
        return None
    item = interrupts[0]
    value = getattr(item, "value", item)
    return value if isinstance(value, dict) else {"question": str(value), "context": ""}


# ---------------------------------------------------------------------------
# Reading a line from the human
# ---------------------------------------------------------------------------

def _ask_user(session: GraphSession, cfg, payload: dict[str, Any]):
    """Show the question and return the human's raw reply.

    Terminal-scope commands are handled here and we loop; they never reach the
    graph and never cost a super-step. Everything else -- plain answers AND
    graph-scope commands -- is returned verbatim so drive() can resume with it.
    """
    purpose = str(payload.get("purpose", "") or "")
    _show_prompt(payload)

    while True:
        try:
            raw = input("\nyou> ")
        except (EOFError, KeyboardInterrupt):
            # ctrl-D or ctrl-C: stop WITHOUT answering. The graph stays parked
            # on this interrupt, so re-running with the same --session picks up
            # at this exact question. Use /exit to finish a session properly.
            print("\n(stopped -- rerun with the same --session to pick up here)")
            return ABORT

        parsed = commands.parse(raw, purpose=purpose, registry=session.registry)

        if parsed.kind == "rejected":
            print(f"\n{parsed.error}")
            continue

        if parsed.kind == "text" and not parsed.text:
            continue

        if parsed.kind == "command" and parsed.command.scope == "terminal":
            handler = TERMINAL_HANDLERS.get(parsed.command.name)
            if handler is None:
                print(f"\n[bug] /{parsed.command.name} has no terminal handler.")
                continue
            print()
            handler(session, cfg, parsed.text, purpose)
            continue

        return parsed.raw


def _show_prompt(payload: dict[str, Any]) -> None:
    print("\n======================================")
    print("HUMAN INPUT")
    print("======================================")

    context = str(payload.get("context", "")).strip()
    if context:
        print(context)
        print()

    print(str(payload.get("question", "Your input is required.")).strip())
    print()
    print("Type /help for commands.")


# ---------------------------------------------------------------------------
# Terminal-scope command handlers
#
# These live here, not in commands.py, so commands.py stays free of LangGraph
# and IO imports and can be imported from inside a node. Uniform signature:
#     handler(session, cfg, argument, purpose) -> None
# ---------------------------------------------------------------------------

def _values(session: GraphSession) -> dict:
    return session.graph.get_state(session.config).values or {}


def _handle_help(session, cfg, argument: str, purpose: str) -> None:
    print(commands.render_help(purpose or None, session.registry))


def _handle_usage(session, cfg, argument: str, purpose: str) -> None:
    print_usage_table(session.usage(_values(session)))


def _handle_state(session, cfg, argument: str, purpose: str) -> None:
    snapshot = session.graph.get_state(session.config)
    print("GRAPH STATE")
    print("-" * 78)
    # snapshot.next is the tuple of nodes LangGraph will run when resumed.
    print(f"{'next node(s)':20} {', '.join(snapshot.next) or '(finished)'}")
    print(session.state_report(snapshot.values or {}, argument.strip().lower() == "full"))


def _handle_transcript(session, cfg, argument: str, purpose: str) -> None:
    entries = session.transcript(_values(session))
    if not entries:
        print("Nothing recorded yet.")
        return
    print("TRANSCRIPT")
    print("-" * 78)
    for entry in entries:
        print(f"\n{entry.get('role', '?')}:")
        print(_indent(entry.get("text", "")))


def _handle_config(session, cfg, argument: str, purpose: str) -> None:
    from agent.config import describe

    print(describe(cfg))


TERMINAL_HANDLERS: dict[str, Callable[..., None]] = {
    "help": _handle_help,
    "usage": _handle_usage,
    "state": _handle_state,
    "transcript": _handle_transcript,
    "config": _handle_config,
}


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _indent(text: str, prefix: str = "  ") -> str:
    text = str(text).strip()
    if not text:
        return prefix + "(empty)"
    return "\n".join(prefix + line for line in text.splitlines())


def print_usage_table(usage_map: dict[str, dict[str, Any]]) -> None:
    if not usage_map:
        print("No token usage recorded yet.")
        return

    print("\nTOKEN / CACHE USAGE")
    print("-" * 78)

    for key, usage in sorted(usage_map.items()):
        if not usage:
            continue
        line = (
            f"{key:24} "
            f"input={usage.get('last_input_tokens', 0):>8,} "
            f"cached={usage.get('last_cached_input_tokens', 0):>8,} "
            f"cache={usage.get('cache_percent', 0):>5}%"
        )
        if usage.get("context_percent") is not None:
            line += f" context={usage['context_percent']:>5}%"
        print(line)
