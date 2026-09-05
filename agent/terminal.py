"""
WHAT:  The terminal REPL that drives the graph: start or resume a session,
       show whatever the graph is asking, read a line, feed it back in.
WHY:   Keeping ALL terminal I/O in one file means the graph itself never calls
       input() or print() for control flow, so it stays drivable by a test or
       a future web UI.
CONCEPT: interrupt() / Command(resume=...) -- LangGraph's human-in-the-loop
       pause and resume.  See the big comment on drive_graph() below.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from langgraph.types import Command

from agent import commands
from agent.config import describe
from agent.state import create_initial_state


def drive_graph(graph, repo_path: Path, cfg) -> None:
    """Run one interactive session against `graph`.

    ------------------------------------------------------------------
    THE INTERRUPT / RESUME CYCLE -- the single most important idea here
    ------------------------------------------------------------------
    A LangGraph node can call `interrupt(payload)` to stop the whole graph
    and hand `payload` back to whoever called `invoke()`.  The graph's
    progress up to that point is already saved in the checkpointer, so the
    process can even exit and come back later.

    To continue, you call `invoke(Command(resume=value))`.  LangGraph
    re-runs the interrupted node, and this time its `interrupt()` call
    RETURNS `value` instead of stopping.

    Read that last sentence again, because it catches everybody:

        THE NODE FUNCTION RE-RUNS FROM ITS FIRST LINE.

    So anything a node does *before* its interrupt() call happens again on
    every resume.  That is why nodes/human.py does nothing but build a
    payload before interrupting -- no model calls, no writes.

    The loop below is just: invoke -> did we get an interrupt? -> ask the
    human -> resume -> repeat.
    """
    config = {
        # thread_id is what makes a session resumable: the checkpointer keys
        # every saved step by it.  Two --session names = two independent
        # conversations sharing one sqlite file.
        "configurable": {"thread_id": cfg.session},
        # LangGraph raises GraphRecursionError after this many super-steps in
        # ONE invoke() call. Ours is high because orchestrator -> executor ->
        # orchestrator is a legitimate long loop.
        "recursion_limit": cfg.recursion_limit,
    }


    snapshot = graph.get_state(config)

    if snapshot.values and snapshot.next:
        # `snapshot.next` is non-empty when the graph stopped mid-run last
        # time -- i.e. it is parked on an interrupt waiting for an answer.
        result = _resume_existing(graph, config, snapshot, cfg)
    else:
        request = input(
            f"\nRepository: {repo_path}\n\nWhat do you want to build/change?\n\nyou> "
        ).strip()
        if not request:
            print("No request supplied.")
            return

        initial_state = create_initial_state(
            repo_path=repo_path,
            user_request=request,
            previous=dict(snapshot.values) if snapshot.values else {},
        )
        result = graph.invoke(initial_state, config=config)

    while True:
        payload = _interrupt_payload(result)
        if payload is None:
            # No interrupt in the result means the graph reached END.
            break

        answer = _ask_user(graph, config, payload, cfg)
        result = graph.invoke(Command(resume=answer), config=config)

    print("\nAgent session ended.")
    final_snapshot = graph.get_state(config)
    if final_snapshot.values.get("usage_by_role"):
        print_usage_table(final_snapshot.values["usage_by_role"])


def _resume_existing(graph, config: dict[str, Any], snapshot, cfg):
    """Pick up a session that was interrupted in a previous process."""
    human = snapshot.values.get("human", {})
    if human.get("question"):
        print(f"\nResuming LangGraph session: {config['configurable']['thread_id']}")
        answer = _ask_user(
            graph,
            config,
            {
                "question": human.get("question", ""),
                "context": human.get("context", ""),
                "purpose": human.get("purpose", ""),
            },
            cfg,
        )
        return graph.invoke(Command(resume=answer), config=config)

    # Parked somewhere that is not a question: just let it continue.
    return graph.invoke(None, config=config)


def _interrupt_payload(result: dict[str, Any]) -> dict[str, Any] | None:
    """Pull the interrupt payload out of an invoke() result, or None.

    LangGraph reports interrupts under the reserved "__interrupt__" key.  It
    is a list because several branches could interrupt in the same step; our
    graph has exactly one interrupt point and no parallel branches, so index
    0 is always the right one.
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

def _ask_user(graph, config: dict[str, Any], payload: dict[str, Any], cfg) -> str:
    """Show the graph's question and return the human's raw reply.

    Terminal-scope commands are handled right here and we loop; they never
    reach the graph and never cost a super-step.  Everything else -- plain
    answers AND graph-scope commands like /plan -- is returned verbatim so
    drive_graph() can resume the graph with it.
    """
    purpose = str(payload.get("purpose", "") or "")
    _show_prompt(payload)

    while True:
        try:
            raw = input("\nyou> ")
        except (EOFError, KeyboardInterrupt):
            # ctrl-D or ctrl-C. Treat it as /exit rather than dumping a
            # traceback: the session is already checkpointed, so this is a
            # perfectly normal way to stop, and --session picks it back up.
            print("\n(interrupted -- your session is saved)")
            return "/exit"

        parsed = commands.parse(raw, purpose=purpose)

        # A rejected command: explain and ask again.  The model never sees it.
        if parsed.kind == "rejected":
            print(f"\n{parsed.error}")
            continue

        # Empty line: just re-prompt rather than resuming with "".
        if parsed.kind == "text" and not parsed.text:
            continue

        # A terminal-scope command: run it here and ask again.
        if parsed.kind == "command" and parsed.command.scope == "terminal":
            handler = TERMINAL_HANDLERS.get(parsed.command.name)
            if handler is None:
                # Only reachable if someone adds a registry row and forgets
                # the handler -- say so loudly rather than silently ignoring.
                print(f"\n[bug] /{parsed.command.name} has no terminal handler.")
                continue
            print()
            handler(graph, config, parsed.text, purpose, cfg)
            continue

        # Everything else goes into the graph.  We return the RAW string, not
        # the parsed object -- see the long note at the top of commands.py.
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
# These live here, not in commands.py, so that commands.py stays free of
# LangGraph and IO imports and can be imported from inside a graph node
# without an import cycle.  Signature is uniform:
#     handler(graph, config, argument, purpose, cfg) -> None
# where `config` is LangGraph's run config and `cfg` is our AgentConfig.
# ---------------------------------------------------------------------------

def _handle_help(graph, config, argument: str, purpose: str, cfg) -> None:
    print(commands.render_help(purpose or None))


def _handle_usage(graph, config, argument: str, purpose: str, cfg) -> None:
    snapshot = graph.get_state(config)
    print_usage_table(snapshot.values.get("usage_by_role", {}))


def _handle_state(graph, config, argument: str, purpose: str, cfg) -> None:
    snapshot = graph.get_state(config)
    values = snapshot.values or {}
    full = argument.strip().lower() == "full"

    print("GRAPH STATE")
    print("-" * 78)
    # snapshot.next is the tuple of nodes LangGraph will run when resumed.
    print(f"{'next node(s)':24} {', '.join(snapshot.next) or '(finished)'}")
    print(f"{'task cycle':24} {values.get('task_cycle', '-')}")
    print(f"{'repo':24} {values.get('repo_path', '-')}")

    human = values.get("human", {})
    print(f"{'waiting for':24} {human.get('purpose', '-')}")

    planning = values.get("planning", {})
    print(f"{'plan generation':24} {planning.get('generation', 0)}")

    execution = values.get("execution", {})
    print(f"{'workstream':24} {execution.get('workstream', '-')}")
    print(f"{'current task':24} {_short(execution.get('current_task', ''))}")

    if full:
        print("\nrequest:")
        print(f"  {values.get('user_request', '')}")
        print("\nrequirements:")
        print(_indent(values.get("discussion", {}).get("requirements", "")))
        print("\nplan:")
        print(_indent(planning.get("plan", "")))


def _handle_transcript(graph, config, argument: str, purpose: str, cfg) -> None:
    """Print the human <-> discussor conversation.

    `transcript` is the one state channel with a reducer (see agent/state.py):
    nodes append to it and it is never rewritten, so this is a complete log.
    Entries are tagged with the task cycle they belong to; by default we show
    only the current one, and /transcript all shows every cycle.
    """
    snapshot = graph.get_state(config)
    values = snapshot.values or {}
    entries = values.get("transcript", [])
    show_all = argument.strip().lower() == "all"
    current = int(values.get("task_cycle", 1))

    if not show_all:
        entries = [e for e in entries if e.get("cycle") == current]

    if not entries:
        print("Nothing discussed yet.")
        return

    print("DISCUSSION" + ("  (all task cycles)" if show_all else f"  (task #{current})"))
    print("-" * 78)
    last_cycle = None
    for entry in entries:
        if show_all and entry.get("cycle") != last_cycle:
            last_cycle = entry.get("cycle")
            print(f"\n--- task #{last_cycle} ---")
        print(f"\n{entry.get('role', '?')}:")
        print(_indent(entry.get("text", "")))


def _handle_config(graph, config, argument: str, purpose: str, cfg) -> None:
    """Show which backend is serving each role, and where that came from."""
    print(describe(cfg))


TERMINAL_HANDLERS: dict[str, Callable[..., None]] = {
    "help": _handle_help,
    "usage": _handle_usage,
    "state": _handle_state,
    "transcript": _handle_transcript,
    "config": _handle_config,
}


# ---------------------------------------------------------------------------
# Small formatting helpers
# ---------------------------------------------------------------------------

def _short(text: str, limit: int = 52) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


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

    for role, usage in usage_map.items():
        line = (
            f"{role:24} "
            f"input={usage.get('last_input_tokens', 0):>8,} "
            f"cached={usage.get('last_cached_input_tokens', 0):>8,} "
            f"cache={usage.get('cache_percent', 0):>5}%"
        )
        if usage.get("context_percent") is not None:
            line += f" context={usage['context_percent']:>5}%"
        print(line)
