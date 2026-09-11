"""
WHAT:  Turns Codex's notification stream into readable progress lines.
WHY:   A turn can take minutes. "... still working (135s)" tells you nothing --
       not whether it is reading files, running tests, or stuck in a loop. The
       provider is already telling us all of that; we were throwing it away.
CONCEPT: Not LangGraph. This is the difference between a progress bar and a
       progress REPORT.

Thread.run() collects the stream and returns only the final answer. We tee the
same stream through here on the way past, so nothing about collection changes
-- we just stop discarding what goes by.

Output is deliberately terse and prefixed, so a long turn reads as a log:

    $ python -m pytest -q            a command it ran
    ~ test_shapes.py                 a file it changed
    · checking the module layout     a summary of its reasoning
    > Added 6 tests, all passing     something it said
"""

from __future__ import annotations

from typing import Any

MAX_LINE = 100


def _unwrap(item: Any) -> Any:
    """Get the real item out of the SDK's ThreadItem wrapper.

    ThreadItem is a Pydantic RootModel: every concrete item -- a command
    execution, a file change, a reasoning summary -- arrives wrapped, and
    type(item).__name__ is always the literal string "ThreadItem".

    This cost a whole live run to find. The first version of this module
    matched on the wrapper's name, so it matched nothing at all and printed
    not one line while the executor was visibly running commands.
    """
    return getattr(item, "root", item)


def describe(event: Any) -> str | None:
    """One progress line for one stream event, or None to stay quiet.

    Derived from record() rather than matching events itself, and that is the
    fix for a specific complaint: a turn reported "137 events, last one 11s
    ago" and printed almost nothing, because this function knew about three
    notification types and six item kinds while the stream carries far more.
    Two independent matchers meant the narrower one silently decided what you
    were allowed to see.

    Now record() decides what an event IS and this decides how to say it, so
    an item kind nobody has taught us about still gets a line with its name on
    it -- which is how you find out it exists.
    """
    return headline(record(event))


#: Kinds that would be noise rather than progress.
#:  turn_started/turn_completed  bracket the turn we already announced
#:  token_count                  arrives constantly and says nothing
_QUIET_KINDS = frozenset({"turn_started", "turn_completed", "token_count"})

_MARKERS = {
    "command": "$",
    "file_change": "~",
    "reasoning": "·",
    "message": ">",
    "web_search": "?",
    "plan": "=",
    "error": "!",
}


def headline(entry: dict | None) -> str | None:
    """A printable line for one recorded event, or None to stay quiet."""
    if not entry:
        return None

    kind = entry.get("kind", "")
    phase = entry.get("phase", "")
    if kind in _QUIET_KINDS:
        return None

    if kind == "command":
        # Announced when it STARTS, because that is when you want to know what
        # is taking so long. On completion, only a failure is worth a line.
        if phase == "started":
            return _line("$", entry.get("command", ""))
        code = entry.get("exit_code")
        if phase == "completed" and code not in (None, 0):
            return _line("!", f"exited {code}: {entry.get('command', '')}")
        return None

    if phase == "started":
        # Everything else is only interesting once it has some content.
        return None

    if kind == "file_change":
        names = [str(c.get("path", "")).rsplit("/", 1)[-1]
                 for c in entry.get("changes") or []]
        return _line("~", ", ".join(n for n in names if n) or "edited files")

    if kind == "reasoning":
        summary = entry.get("summary") or []
        # The LAST line, not the first. On an update the tail is the new part,
        # and on completion it is the conclusion -- either way it is the bit
        # you did not already see.
        return _line("·", summary[-1]) if summary else None

    if kind == "message":
        if entry.get("is_final_json"):
            return None       # comes back through the normal return path
        return _line(">", entry.get("text", ""))

    if kind == "web_search":
        return _line("?", f"searched: {entry.get('query', '')}")

    if kind == "plan":
        return _line("=", entry.get("text", ""))

    if kind == "error":
        return _line("!", entry.get("message", "error"))

    # An unrecognised kind, named. Better a line saying "something happened,
    # and here is what it was called" than silence.
    return _line("*", kind.replace("_", " "))


def _line(marker: str, text: str) -> str | None:
    """One clean, bounded line. Multi-line values collapse to their first line."""
    text = " ".join(str(text).split())
    if not text:
        return None
    if len(text) > MAX_LINE:
        text = text[: MAX_LINE - 1] + "…"
    return f"    {marker} {text}"


# ---------------------------------------------------------------------------
# The same stream, kept rather than summarised
# ---------------------------------------------------------------------------
#
# describe() answers "what is one line worth printing?" and returns None for
# most events -- which is right for a terminal and wrong for a record. It also
# throws away the parts you most want afterwards: a command's OUTPUT, every
# reasoning summary after the first, the full text of a message, and every
# successful command's result.
#
# record() answers a different question: "what happened?". It returns something
# for every event it recognises, untruncated, so a node's whole turn can be
# read back later. The two are deliberately separate functions rather than one
# with a flag, because the line format is allowed to stay terse and opinionated.

#: COMMAND output longer than this is truncated in the record. A single pytest
#: run can emit megabytes, and the point is to be readable afterwards, not to
#: be a second copy of the terminal.
MAX_OUTPUT = 4000

#: A MESSAGE is different, and sharing the 4 KB cap with command output was
#: wrong. It is the model's answer -- the thing most worth reading in full --
#: and it is bounded by the provider's own output limit rather than by how
#: chatty a test suite is. A 16,608-token design clipped to 4 KB loses the
#: middle of the only artefact anybody wanted to see, and the clip is silent
#: apart from a "...[N characters omitted]..." nobody expects mid-JSON.
MAX_MESSAGE = 200_000

# --------------------------------------------------------------------------
# WHY STREAMING DELTAS ARE MARKED TRANSIENT
# --------------------------------------------------------------------------
# `agent_message_delta` is one token of the answer being typed. A measured
# designer turn emitted 31,001 "events" of which 30,900 were these, and
# counting them broke three separate things:
#
# 1. **The number lied.** "31001 events" reads as furious activity. The real
#    work was 12 commands and 4 reasoning steps; everything after 145s was the
#    model typing. A 49 KB design document is ~12,000 tokens, and at the
#    observed 15 tokens/second that is fourteen minutes of pure output -- which
#    is the honest answer to "why is this suddenly so slow", and was invisible.
#
# 2. **The idle clock stopped working.** "last one 0s ago" for thirty-five
#    minutes: a model streaming tokens is never silent, so silence-based
#    timeouts cannot see this failure at all.
#
# 3. **The record became enormous.** Each delta carries nothing but its own
#    name -- no text, no index -- and 31,000 of them made the in-flight record
#    a multi-megabyte file rewritten every five seconds.
#
# So they are counted, not kept: the tally becomes "writing 31.0k" in the
# heartbeat, which says the same thing in a way that is true.
#
# What they are NOT is contentless. AgentMessageDeltaNotification carries
# `delta: str` -- the actual text -- and so do the reasoning, plan and
# command-output delta streams. That was discarded along with the events for
# a while, which is how a turn that streamed 16,608 tokens of a finished
# design could report "5 events, last: * user message" and then be killed for
# going quiet. The text is now accumulated into a bounded tail per stream and
# the count stays a count.

#: delta kind -> which running text it belongs to. Several notification types
#: feed the same buffer: the model's reasoning arrives as both
#: `reasoning_text` and `reasoning_summary_text`, and there is no useful
#: distinction when what you want is "what is it saying right now".
_DELTA_STREAMS = {
    "agent_message_delta": "message",
    "reasoning_text_delta": "reasoning",
    "reasoning_summary_text_delta": "reasoning",
    "plan_delta": "plan",
    "command_execution_output_delta": "output",
    "command_exec_output_delta": "output",
}

#: Per stream, while the turn is still running. The full text arrives in the
#: record when the turn ends; this is what there is to look at before then.
#:
#: 8 KB rather than the 2 KB it started at: the cost is 8 KB per stream in the
#: in-flight file, rewritten every five seconds, which is nothing next to the
#: multi-megabyte rewrites that came of keeping one record per token -- and
#: two kilobytes of a document being written is about four paragraphs.
LIVE_TAIL = 8000


class _LiveText:
    """The tail of each text stream a turn is producing, as it produces it.

    Deliberately a tail and not a log: a design turn writes tens of thousands
    of tokens, and the question this answers is "what is it doing NOW", which
    the last couple of thousand characters answer completely.
    """

    def __init__(self, limit: int = LIVE_TAIL):
        self.limit = limit
        self.streams: dict[str, str] = {}

    def add(self, stream: str, text: str) -> None:
        if not text:
            return
        current = self.streams.get(stream, "") + text
        self.streams[stream] = current[-self.limit:]

    def tail(self, chars: int = 120) -> str:
        """The most useful single line: what it is writing, right now.

        `message` wins over `reasoning` when both are live, because by then
        the model has stopped thinking and started answering.
        """
        for stream in ("message", "plan", "reasoning", "output"):
            text = self.streams.get(stream, "").strip()
            if text:
                flat = " ".join(text.split())
                return f"{stream}: {flat[-chars:]}"
        return ""

    def snapshot(self) -> dict[str, str]:
        return dict(self.streams)

    def total(self) -> int:
        return sum(len(v) for v in self.streams.values())


def record(event: Any) -> dict | None:
    """A structured record of one stream event, or None if it carries nothing.

    Shapes are all `{"kind": ..., "phase": "started"|"completed", ...}` so a
    reader can switch on `kind` without knowing the SDK's class names.
    """
    payload = getattr(event, "payload", event)
    name = type(payload).__name__

    if name == "ItemStartedNotification":
        return _item(getattr(payload, "item", None), "started")
    if name == "ItemCompletedNotification":
        return _item(getattr(payload, "item", None), "completed")
    if name == "ItemUpdatedNotification":
        # Progressive updates to an item already in flight -- a command's
        # output arriving, reasoning being extended. Previously ignored
        # entirely by both functions, which is a large part of why a turn with
        # 137 events showed almost nothing: the interesting middle of a long
        # turn is mostly updates.
        return _item(getattr(payload, "item", None), "updated")
    if name == "ErrorNotification":
        return {"kind": "error", "phase": "completed",
                "message": str(getattr(payload, "message", "error"))}

    # Anything else, by name. Returning None here is how a whole class of
    # event stays invisible -- and "137 events, 4 of them printed" is what
    # that looks like from the outside.
    if getattr(payload, "item", None) is not None:
        return _item(payload.item, "other")

    kind = _snake(name)
    if kind.endswith("_delta"):
        # A chunk of text being written. TRANSIENT -- not kept as a record,
        # for the three reasons in the note above -- but its TEXT is carried
        # out, because that text is the answer being written and we were
        # dropping it on the floor. See _LiveText.
        return {"kind": kind, "phase": "other", "transient": True,
                "stream": _DELTA_STREAMS.get(kind, "other"),
                "text": str(getattr(payload, "delta", "") or "")}
    return {"kind": kind, "phase": "other"}


def _item(item: Any, phase: str) -> dict | None:
    if item is None:
        return None
    item = _unwrap(item)
    kind = type(item).__name__

    if kind == "CommandExecutionThreadItem":
        out = {"kind": "command", "phase": phase,
               "command": str(getattr(item, "command", ""))}
        if phase == "completed":
            out["exit_code"] = getattr(item, "exit_code", None)
            # The output is the whole reason to keep a record. describe() never
            # shows it, so a failed command in the terminal tells you only that
            # it failed.
            text = getattr(item, "aggregated_output", None) or getattr(item, "output", None)
            if text:
                out["output"] = _clip(str(text))
        return out

    if kind == "FileChangeThreadItem":
        changes = []
        for change in getattr(item, "changes", None) or []:
            path = getattr(change, "path", None) or getattr(change, "file_path", None)
            if path:
                changes.append({"path": str(path),
                                "kind": str(getattr(change, "kind", "") or "")})
        return {"kind": "file_change", "phase": phase, "changes": changes} if changes else None

    if kind == "ReasoningThreadItem":
        # EVERY summary line, not just the first. This is the model's own
        # account of what it was doing, and it is the most useful thing in the
        # record when a turn went somewhere unexpected.
        summary = [str(s) for s in (getattr(item, "summary", None) or []) if s]
        return {"kind": "reasoning", "phase": phase, "summary": summary} if summary else None

    if kind == "AgentMessageThreadItem":
        text = (getattr(item, "text", "") or "").strip()
        if not text:
            return None
        return {"kind": "message", "phase": phase,
                "text": _clip(text, MAX_MESSAGE),
                # The final structured answer comes back through the normal
                # return path; flagged so a reader can skip the duplicate.
                "is_final_json": text.startswith("{")}

    if kind == "WebSearchThreadItem":
        return {"kind": "web_search", "phase": phase,
                "query": str(getattr(item, "query", ""))}

    if kind == "PlanThreadItem":
        return {"kind": "plan", "phase": phase, "text": str(getattr(item, "text", ""))}

    # Deliberately recorded rather than dropped: an unrecognised event is how
    # you find out the SDK grew a new item type, and a bare name is enough to
    # notice that.
    return {"kind": _snake(kind), "phase": phase}


def _clip(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n...[{len(text) - limit} characters omitted]...\n{text[-half:]}"


def _snake(class_name: str) -> str:
    name = class_name.removesuffix("ThreadItem").removesuffix("Notification")
    out = []
    for i, ch in enumerate(name):
        if ch.isupper() and i:
            out.append("_")
        out.append(ch.lower())
    return "".join(out) or "unknown"


# --------------------------------------------------------------------------
# WHAT A TURN SAYS WHEN IT GOES WRONG
# --------------------------------------------------------------------------
# These were methods on CodexBackend. They moved here when a second and third
# backend appeared, because every word in them is true of any provider that
# streams a turn -- only the name at the front was Codex's. Copying 120 lines
# of the most carefully worded diagnostics in the project into each new
# backend would have been the wrong trade.
#
# They take `provider` first and are called from thin delegating methods, so
# the existing tests that assert on the message text still reach them the way
# they always did.


def silent_message(provider: str, idle: float, elapsed: float, events: int,
                   streamed: int = 0, live=None) -> str:
    """What went quiet, and -- the part that was missing -- what it had
    already said.

    The first version of this reported "5 events" and concluded "a request
    that stopped responding rather than one that was slow". Both true and
    together misleading: that turn had streamed 16,608 tokens of a finished
    design, which the counters could not see because tokens are not events.
    Stalling AFTER writing an answer and never starting one are different
    failures with different remedies, so the message has to tell them apart.
    """
    head = (f"{provider} went silent for {idle:.0f}s (turn ran {elapsed:.0f}s, "
            f"{events} events")
    head += f", {streamed:,} tokens written).\n\n" if streamed else ").\n\n"

    if streamed:
        body = (
            f"It was NOT idle for most of that: it wrote {streamed:,} "
            f"tokens and then stopped, which is a stall after the work "
            f"rather than a request that never started. Raising the "
            f"timeout will not help -- there was nothing left to wait "
            f"for. Run it again.\n\n"
        )
        tail = (live.tail(400) if live is not None else "")
        if tail:
            body += f"The last thing it wrote:\n    {tail}\n\n"
    else:
        body = (
            f"Nothing arrived at all, so this is a request that stopped "
            f"responding rather than one that was slow -- the limit is on "
            f"SILENCE, and a turn that keeps streaming is never killed "
            f"for taking long.\n\n"
        )

    return (
        head + body
        + f"The graph checkpoints after every completed node, so nothing "
          f"before this is lost.\n\n"
          f"To allow longer silences, put this in "
          f"<repo>/.agent/config.json:\n"
          f'    {{"roles": {{"<role>": {{"options": {{"timeout": 600}}}}}}}}\n'
          f"using the role name this node declares as its `backend` -- "
          f"which may well be shared with other nodes."
    )


def flood_message(provider: str, streamed: int, elapsed: float) -> str:
    return (
        f"{provider} typed {streamed:,} tokens of answer in one turn "
        f"({elapsed:.0f}s) and hit the output cap.\n\n"
        f"This is not slowness. Something makes the model emit a whole "
        f"document, treat it as a draft, and emit another -- so it is "
        f"never idle and never finishes. The largest legitimate design "
        f"this project has produced is about 12,000 tokens.\n\n"
        f"Most likely the plan asks one node for far too much, so the "
        f"document it has to write is enormous. Split the biggest steps "
        f"and design again.\n\n"
        f"To raise the cap, in <repo>/.agent/config.json:\n"
        f'    {{"roles": {{"<role>": {{"options": '
        f'{{"max_output_tokens": 120000}}}}}}}}'
    )


def runaway_message(provider: str, elapsed: float, events: int) -> str:
    return (
        f"{provider} ran {elapsed:.0f}s on one turn ({events} events) and hit the "
        f"absolute cap.\n\n"
        f"It was still producing output the whole time, so this is not a "
        f"hang -- the node is being asked to do too much in a single turn. "
        f"A node is atomic: it either finishes or its work is discarded, so "
        f"a turn this long is a bad bet however patient you are.\n\n"
        f"Split it across more nodes -- give each one fewer plan steps and "
        f"fewer output fields. The Wiring panel in the web UI does this "
        f"without hand-editing JSON.\n\n"
        f"To raise the cap anyway:\n"
        f'    {{"roles": {{"<role>": {{"options": {{"max_seconds": 14400}}}}}}}}'
    )


def poll_seconds(timeout: float, max_seconds: float, cancel=None) -> float:
    """How often to check whether a turn is still going.

    Five seconds for real settings -- cheap, and nobody minds noticing a
    stall five seconds late. But it must stay comfortably below whichever
    limit is shortest, or the limit cannot fire at all: a fixed 5s poll with
    a 0.2s limit checks once, after the turn is already over. That mattered
    first in a test, and a limit that silently cannot trigger is worth ruling
    out in the code rather than in the test setup.
    """
    limit = min(5.0, timeout / 4, max_seconds / 4)
    if cancel is not None:
        # Armed for cancellation, so responsiveness is the point. Five seconds
        # between checks means a Stop button that takes up to five seconds to
        # do anything, which is long enough to make someone press it again. A
        # join timeout costs nothing to shorten.
        limit = min(limit, 0.25)
    return max(0.02, limit)
