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
    if type(item).__name__ == "CommandExecutionThreadItem":
        return _line("$", getattr(item, "command", ""))
    return None


def _completed(item: Any) -> str | None:
    if item is None:
        return None
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
