"""
WHAT:  The single source of truth for every slash command the human can type.
WHY:   Command handling used to be split between terminal.py (which matched
       "/usage" by exact string equality) and nodes/human.py (which matched a
       set of quit-words).  Help text was hardcoded separately and had already
       drifted.  One registry + one parser fixes all three problems at once.
CONCEPT: Not a LangGraph concept -- this is plain data + a parser.  Keeping it
       free of LangGraph/IO imports is what lets BOTH the terminal loop and a
       graph node import it without an import cycle.

--------------------------------------------------------------------------
HOW COMMANDS FLOW THROUGH THE SYSTEM
--------------------------------------------------------------------------

    you type  ->  terminal.py: _ask_user()
                        |
                        +-- commands.parse(raw, purpose=...)
                        |
                        +-- scope == "terminal"  -> run it right here,
                        |                           print, and re-prompt.
                        |                           The graph never wakes up.
                        |
                        +-- kind == "rejected"   -> print the error,
                        |                           re-prompt.  The model
                        |                           never sees the typo.
                        |
                        +-- otherwise            -> hand the RAW STRING back
                                                    to drive_graph(), which
                                                    calls
                                                    graph.invoke(Command(resume=raw))
                                                            |
                                                            v
                                             nodes/human.py: human_input()
                                                            |
                                             commands.parse(raw, purpose=...)
                                                            |
                                             dispatch on parsed.command.name

Note that parse() is called TWICE on the same string, once on each side.
That is deliberate, not an oversight:

  * The value passed to Command(resume=...) is written into the sqlite
    checkpoint.  A plain `str` is the smallest, most durable thing we can
    store there -- a structured object would bloat every checkpoint and
    freeze the parser's dataclass shape into saved sessions forever.
  * The graph must stay safe when driven by something that is not our
    terminal (a test, a future web UI).  Re-parsing inside the node means
    the rules are enforced no matter who resumes the graph.

Because both sides call the SAME function, there is no duplicated logic --
only a duplicated call.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Literal, Sequence


# "terminal" commands are answered by the REPL itself and never resume the
# graph.  "graph" commands are forwarded into the graph, where human_input()
# interprets them.  Deciding this per-command is what keeps /usage from
# costing a super-step and what keeps /exit out of terminal.py.
Scope = Literal["terminal", "graph"]


@dataclass(frozen=True)
class SlashCommand:
    """One row of the registry below."""

    name: str
    """Command name WITHOUT the leading slash, e.g. "plan"."""

    scope: Scope
    """Where this command is executed.  See the Scope alias above."""

    summary: str
    """One line shown by /help."""

    aliases: tuple[str, ...] = ()
    """Other names that resolve to this command, also without slashes."""

    argument: str = ""
    """Placeholder shown in help, e.g. "[guidance]".
    An empty string means the command takes NO argument, and supplying one
    is an error (rule 7 in parse())."""

    contexts: tuple[str, ...] | None = None
    """Which human "purpose" values this command is valid in.
    None means "valid everywhere".  The purpose comes from state["human"]
    ["purpose"] -- it is the reason the graph stopped to ask you something,
    e.g. "discussion" or "orchestrator".  This is what lets /plan exist only
    while you are talking to the discussor."""


@dataclass(frozen=True)
class ParsedInput:
    """The result of parsing one line the human typed."""

    kind: Literal["text", "command", "rejected"]
    """"text"     -> an ordinary answer for the agent
       "command"  -> a recognised, valid slash command
       "rejected" -> a slash command we refuse to run; see .error"""

    raw: str
    """Exactly what the human typed, stripped of surrounding whitespace."""

    text: str = ""
    """For kind="text": the answer.
    For kind="command": the command's argument ("" if none was given)."""

    command: SlashCommand | None = None
    """The registry entry, when kind == "command"."""

    error: str = ""
    """A ready-to-print explanation, when kind == "rejected"."""


# ---------------------------------------------------------------------------
# The registry.  Adding a command means adding ONE row here plus one handler.
# /help is generated from this tuple, so help can never drift from behaviour.
# ---------------------------------------------------------------------------

REGISTRY: tuple[SlashCommand, ...] = (
    # ---- terminal scope: answered locally, the graph never wakes up --------
    SlashCommand(
        name="help",
        scope="terminal",
        summary="Show the commands available right now",
        aliases=("h", "?"),
    ),
    SlashCommand(
        name="usage",
        scope="terminal",
        summary="Token and cache usage per role",
    ),
    SlashCommand(
        name="state",
        scope="terminal",
        summary="Show where the graph is and what it is holding",
        argument="[full]",
    ),
    SlashCommand(
        name="config",
        scope="terminal",
        summary="Show which backend and model each role is using",
    ),
    SlashCommand(
        name="graph",
        scope="terminal",
        summary="Draw the agent's shape (mermaid)",
    ),
    SlashCommand(
        name="transcript",
        scope="terminal",
        summary="Print the discussion so far",
        argument="[all]",
    ),
    # ---- graph scope: forwarded into the graph, handled in nodes/human.py --
    SlashCommand(
        name="plan",
        scope="graph",
        summary="Stop discussing and turn the discussion into a plan",
        argument="[extra guidance for the planner]",
        contexts=("discussion",),
    ),
    SlashCommand(
        name="discuss",
        scope="graph",
        summary="Go back to talking things through with the discussor",
        contexts=("orchestrator", "next_task"),
    ),
    SlashCommand(
        name="replan",
        scope="graph",
        summary="Throw out the current plan and make a new one",
        argument="[what was wrong with it]",
        contexts=("orchestrator",),
    ),
    SlashCommand(
        name="exit",
        scope="graph",
        summary="End the session (your progress is saved)",
        aliases=("quit", "q"),
    ),
    SlashCommand(
        name="new",
        scope="graph",
        summary="Abandon this task and start a new one in the same repo",
        argument="<what you want next>",
    ),
)


def lookup(name: str, registry: Sequence[SlashCommand] = REGISTRY,
           *, purpose: str = "") -> SlashCommand | None:
    """Find a command by its name or any of its aliases.  Case-insensitive.

    One NAME may appear more than once when the rows are valid in different
    contexts, so the purpose picks between them. A designed agent asked for
    this the first time it had two gates: /approve at a design review goes to
    the implementation node, /approve at the findings review goes to
    "__end__". Same word, same meaning to the person typing it, two targets.

    Order matters. A row naming this purpose wins; then a row valid
    everywhere; then the first match by name, so Rule 6 in parse() can still
    say where the command IS valid instead of "unknown command".
    """
    wanted = name.strip().lower()
    matches = [c for c in registry
               if wanted == c.name or wanted in c.aliases]
    if not matches:
        return None

    for command in matches:
        if command.contexts is not None and purpose in command.contexts:
            return command
    for command in matches:
        if command.contexts is None:
            return command
    return matches[0]


def all_names(registry: Sequence[SlashCommand] = REGISTRY) -> list[str]:
    """Every name AND alias, for the "did you mean ...?" suggestion."""
    names: list[str] = []
    for command in registry:
        names.append(command.name)
        names.extend(command.aliases)
    return names


def available(
    purpose: str | None,
    registry: Sequence[SlashCommand] = REGISTRY,
) -> list[SlashCommand]:
    """The commands valid in a given context, in registry order.

    `purpose is None` means "don't filter" -- used when we genuinely do not
    know the context, e.g. printing help before the graph has started.
    """
    if purpose is None:
        return list(registry)
    return [c for c in registry if c.contexts is None or purpose in c.contexts]


def parse(
    raw: str,
    *,
    purpose: str = "",
    registry: Sequence[SlashCommand] = REGISTRY,
) -> ParsedInput:
    """Turn one typed line into a ParsedInput.

    The rules are applied strictly in order.  Each `if` below is one rule, and
    the numbering matches the comments so you can follow it top to bottom.
    """
    text = raw.strip()

    # Rule 1 -- empty input is not an answer and not a command.  The terminal
    # re-prompts on this rather than resuming the graph with "".
    if not text:
        return ParsedInput(kind="text", raw=text, text="")

    # Rule 2 -- "//foo" is the escape hatch for answering with a literal
    # leading slash.  Without this you could never tell the agent to look at,
    # say, "/etc/hosts" as your whole answer.
    if text.startswith("//"):
        return ParsedInput(kind="text", raw=text, text=text[1:])

    # Rule 3 -- anything not starting with "/" is an ordinary answer.
    if not text.startswith("/"):
        return ParsedInput(kind="text", raw=text, text=text)

    # Rule 4 -- split "/name rest of the line" into name + argument.
    # split(maxsplit=1) handles any run of whitespace, so "/plan   go" works.
    body = text[1:]
    parts = body.split(maxsplit=1)
    if not parts:
        # The human typed a bare "/".
        return ParsedInput(
            kind="rejected",
            raw=text,
            error="Type a command name after the slash, e.g. /help.",
        )
    name = parts[0].lower()
    argument = parts[1].strip() if len(parts) > 1 else ""

    command = lookup(name, registry, purpose=purpose)

    # Rule 5 -- unknown command.  Never forward it to the model: a typo would
    # silently become an expensive answer.  Suggest the closest real name.
    if command is None:
        suggestion = difflib.get_close_matches(name, all_names(registry), n=1, cutoff=0.6)
        hint = f" Did you mean /{suggestion[0]}?" if suggestion else ""
        return ParsedInput(
            kind="rejected",
            raw=text,
            error=f"Unknown command /{name}.{hint} Type /help for the list.",
        )

    # Rule 6 -- known command, wrong context.  Say where it IS valid so the
    # message teaches instead of just refusing.
    if command.contexts is not None and purpose not in command.contexts:
        where = ", ".join(command.contexts)
        return ParsedInput(
            kind="rejected",
            raw=text,
            error=(
                f"/{command.name} is not available here. "
                f"It works during: {where}. Type /help to see what is."
            ),
        )

    # Rule 7 -- an argument was supplied to a command that takes none.  This
    # usually means a misunderstanding, so it is better to say so than to
    # silently drop the text the human typed.
    if not command.argument and argument:
        return ParsedInput(
            kind="rejected",
            raw=text,
            error=f"/{command.name} takes no arguments. Usage: /{command.name}",
        )

    # Rule 8 -- a REQUIRED argument is missing.  By convention the placeholder
    # is wrapped in <angle brackets> when required and [square brackets] when
    # optional, so the registry row itself declares which it is.
    if command.argument.startswith("<") and not argument:
        return ParsedInput(
            kind="rejected",
            raw=text,
            error=f"/{command.name} needs an argument. Usage: /{command.name} {command.argument}",
        )

    # Rule 9 -- a valid command.
    return ParsedInput(kind="command", raw=text, text=argument, command=command)


def render_help(
    purpose: str | None = None,
    registry: Sequence[SlashCommand] = REGISTRY,
) -> str:
    """Build the /help text from `registry`, filtered to the current context."""
    commands = available(purpose, registry)

    # Pre-compute the left column so the summaries line up.
    def usage(command: SlashCommand) -> str:
        line = f"/{command.name}"
        if command.argument:
            line += f" {command.argument}"
        return line

    width = max((len(usage(c)) for c in commands), default=0)

    lines = ["Commands:"]
    for command in commands:
        alias_note = ""
        if command.aliases:
            alias_note = "  (also " + ", ".join(f"/{a}" for a in command.aliases) + ")"
        lines.append(f"  {usage(command):<{width}}  {command.summary}{alias_note}")

    lines.append("")
    lines.append("Anything that does not start with / is sent to the agent as your answer.")
    lines.append("Start an answer with // if you really need it to begin with a slash.")
    return "\n".join(lines)
