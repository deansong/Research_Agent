"""
WHAT:  Sends whatever a worker thread prints to that thread's event bus.
WHY:   The most useful output in the whole system -- which command the executor
       ran, which file it touched, how many tokens a turn cost -- is written
       with print(), from inside graph nodes, and never passes through the
       GraphSession seam at all.
CONCEPT: A thread-routed stdout proxy. Not LangGraph; plumbing.

--------------------------------------------------------------------------
BE HONEST ABOUT WHAT THIS IS
--------------------------------------------------------------------------
This is a pragmatic hack, and you should know that before you copy it.

`agent/terminal.py`'s header claims "no graph ever calls input() or print() for
control flow". The input() half is true. The print() half is not: there are
about twenty print() calls inside nodes --

    work/templates/agent_node.py       the announce banner, the summary, git diff
    bootstrap/nodes/*.py               every phase narrates itself
    telemetry.py:79                    the [tokens:...] line
    backends/_progress.py              via codex.py, one line per command run

None of them is control flow, so nothing breaks -- but they do not reach a
browser either. The two ways to fix that:

  1. Thread an `emit` callable through every node signature, every template,
     every backend. Correct, explicit, and a diff touching most of the project
     -- in a codebase whose stated purpose is being readable while you learn.

  2. Replace sys.stdout with something that knows which thread is writing.
     One file, zero changes anywhere else.

We do (2), with the mitigation that makes it defensible: **routing is per
thread**. sys.stdout is process-global, but a write is delivered to the bus
registered for the thread that made it. The terminal REPL, three concurrent
session workers, and a stray library thread all stay separate, and anything
from an unregistered thread falls through to the real stdout -- so output is
never silently swallowed, only ever redirected.

If this project grows a second reason to intercept node output, do (1) instead.
"""

from __future__ import annotations

import sys
import threading
from contextlib import contextmanager
from typing import Iterator

#: thread ident -> the sink that thread's prints go to. Only ever touched under
#: `_lock`, and only ever holding threads that are currently inside a run.
_sinks: dict[int, "LineSink"] = {}
_lock = threading.Lock()


class LineSink:
    """Buffers partial writes into whole lines, then hands them to a callback.

    Needed because `print("a", "b")` is several write() calls and a lone "\\n".
    An event per write() would produce three fragments where a human sees one
    line, so we join them up and emit on newline.
    """

    def __init__(self, on_line):
        self._on_line = on_line
        self._partial = ""

    def write(self, text: str) -> None:
        self._partial += text
        while "\n" in self._partial:
            line, self._partial = self._partial.split("\n", 1)
            self._on_line(line)

    def flush(self) -> None:
        """Emit a trailing fragment.

        This matters more than it looks: the Codex progress reporter writes with
        `print(..., flush=True)` precisely so a long turn is visible as it
        happens, and a heartbeat that ends without a newline would otherwise sit
        in the buffer until the next line -- which may be minutes away.
        """
        if self._partial:
            line, self._partial = self._partial, ""
            self._on_line(line)


class _RoutingStdout:
    """A stdout stand-in that dispatches by calling thread.

    Wraps whatever stdout it displaced rather than the process's original one,
    so a thread with no sink registered still reaches wherever output was going
    before we interfered.
    """

    def __init__(self, underlying):
        self.underlying = underlying

    def write(self, text: str) -> int:
        sink = _sinks.get(threading.get_ident())
        if sink is None:
            return self.underlying.write(text)
        sink.write(text)
        return len(text)

    def flush(self) -> None:
        sink = _sinks.get(threading.get_ident())
        if sink is not None:
            sink.flush()
        self.underlying.flush()

    # Some libraries check these before writing; answer for the real stream.
    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        return self.underlying.fileno()

    @property
    def encoding(self) -> str:
        return getattr(self.underlying, "encoding", "utf-8")


def install() -> None:
    """Make sure sys.stdout is ours, wrapping whatever is there now.

    The check is `is our proxy still installed?`, NOT `have we installed once?`,
    and the difference is a real bug rather than a nicety. Plenty of things
    replace sys.stdout wholesale -- pytest does it between every test,
    contextlib.redirect_stdout does it, notebooks do it. A one-shot flag means
    the first such swap silently unhooks us for the rest of the process, and the
    symptom is a browser that simply goes quiet.

    Deliberately never uninstalled: a background worker may still be mid-print
    when a request finishes, and swapping the stream out from under it is how
    you lose the last line of a run.
    """
    if not isinstance(sys.stdout, _RoutingStdout):
        sys.stdout = _RoutingStdout(sys.stdout)


@contextmanager
def routed_to(on_line) -> Iterator[None]:
    """Within this block, THIS thread's prints go to `on_line` instead."""
    install()
    ident = threading.get_ident()
    sink = LineSink(on_line)
    with _lock:
        previous = _sinks.get(ident)
        _sinks[ident] = sink
    try:
        yield
    finally:
        sink.flush()
        with _lock:
            if previous is None:
                _sinks.pop(ident, None)
            else:
                _sinks[ident] = previous
