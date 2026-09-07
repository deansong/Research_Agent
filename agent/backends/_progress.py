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

    Returning None for most events is the point: the stream carries a great
    deal that is not worth a line, and a progress report that prints
    everything is just noise with extra steps.
    """
    payload = getattr(event, "payload", event)
    name = type(payload).__name__

    if name == "ItemStartedNotification":
        return _started(getattr(payload, "item", None))
    if name == "ItemCompletedNotification":
        return _completed(getattr(payload, "item", None))
    if name == "ErrorNotification":
        return _line("!", getattr(payload, "message", "error"))
    return None


def _started(item: Any) -> str | None:
    """Announce slow things WHEN THEY START, not when they finish.

    A command is the one thing worth showing up front: if it hangs, you want
    to know what it was.
    """
    if item is None:
        return None
    item = _unwrap(item)
    if type(item).__name__ == "CommandExecutionThreadItem":
        return _line("$", getattr(item, "command", ""))
    return None


def _completed(item: Any) -> str | None:
    if item is None:
        return None
    item = _unwrap(item)
    kind = type(item).__name__

    if kind == "CommandExecutionThreadItem":
        code = getattr(item, "exit_code", None)
        # The command was already printed when it started; only say something
        # more if it failed.
        if code not in (None, 0):
            return _line("!", f"exited {code}: {getattr(item, 'command', '')}")
        return None

    if kind == "FileChangeThreadItem":
        paths = []
        for change in getattr(item, "changes", None) or []:
            path = getattr(change, "path", None) or getattr(change, "file_path", None)
            if path:
                paths.append(str(path).rsplit("/", 1)[-1])
        return _line("~", ", ".join(paths) or "edited files") if paths else None

    if kind == "ReasoningThreadItem":
        summary = getattr(item, "summary", None) or []
        if summary:
            return _line("·", str(summary[0]))
        return None

    if kind == "AgentMessageThreadItem":
        text = (getattr(item, "text", "") or "").strip()
        # The final structured answer comes back through the normal return
        # path; echoing a big JSON blob here would bury everything else.
        if text.startswith("{"):
            return None
        return _line(">", text) if text else None

    if kind == "WebSearchThreadItem":
        return _line("?", f"searched: {getattr(item, 'query', '')}")

    if kind == "PlanThreadItem":
        return _line("=", getattr(item, "text", ""))

    return None


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

#: Output longer than this is truncated in the RECORD too. A single pytest run
#: can emit megabytes, and the point is to be readable afterwards, not to be a
#: second copy of the terminal.
MAX_OUTPUT = 4000


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
    if name == "ErrorNotification":
        return {"kind": "error", "phase": "completed",
                "message": str(getattr(payload, "message", "error"))}
    return None


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
        return {"kind": "message", "phase": phase, "text": _clip(text),
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


def _clip(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    half = MAX_OUTPUT // 2
    return f"{text[:half]}\n...[{len(text) - MAX_OUTPUT} characters omitted]...\n{text[-half:]}"


def _snake(class_name: str) -> str:
    name = class_name.removesuffix("ThreadItem").removesuffix("Notification")
    out = []
    for i, ch in enumerate(name):
        if ch.isupper() and i:
            out.append("_")
        out.append(ch.lower())
    return "".join(out) or "unknown"
