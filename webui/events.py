"""
WHAT:  The events a session emits, and the buffer that makes them replayable.
WHY:   A browser tab that loses its connection for four seconds must not lose
       four seconds of a run -- and must not see those four seconds twice.
CONCEPT: Server-Sent Events (SSE), with sequence numbers.

--------------------------------------------------------------------------
SSE IN ONE PARAGRAPH
--------------------------------------------------------------------------
SSE is an HTTP response that never ends. The server writes text frames and the
browser's built-in EventSource hands each one to a callback. Three fields
matter, one per line, blank line between frames:

    id: 42                  <- the browser remembers this
    event: log              <- which callback fires
    data: {"text": "..."}   <- the payload
                            <- blank line ends the frame

When the connection drops, EventSource reconnects **on its own** and sends the
last id it saw back as a `Last-Event-ID` request header. That is the whole
protocol, and it is why `seq` is not decoration: it is what makes reconnection
lossless. We keep every event in a ring buffer, so a reconnecting client asks
for "everything after 42" and gets it before the live feed resumes.

We chose this over WebSockets because the traffic is one-way. The only thing
the browser sends is an answer, and an answer is an ordinary POST.

--------------------------------------------------------------------------
WHY A CLOSED VOCABULARY
--------------------------------------------------------------------------
`EVENT_TYPES` is deliberately a fixed list. Free-form log lines are easy to
emit and impossible to render: the front end ends up pattern-matching on
English. A small closed set means the browser can decide "this is a question,
show the input box" without parsing prose.
"""

from __future__ import annotations

import itertools
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

#: Every event type the server may emit. The browser switches on these, so
#: adding one means teaching static/js/api.js about it -- which is the point.
EVENT_TYPES = (
    "status",    # a phase changed: designing, running, finished
    "log",       # a line of output from a node or a provider
    "question",  # the graph interrupted; a human must answer
    "state",     # a state snapshot worth re-rendering on (plan, agent, outputs)
    "usage",     # token/cache accounting
    "error",     # something went wrong; payload is a Problem-shaped dict
    "done",      # this session's run finished; no more events until it restarts
)

#: How many events one session keeps for replay. A long run with a chatty
#: executor produces a few thousand lines; 5000 is roughly "the whole run"
#: without letting a runaway loop eat memory. Older events are dropped, and a
#: client that asks for one gets told to reload rather than shown a gap.
BUFFER_SIZE = 5000


@dataclass(frozen=True)
class Event:
    """One thing that happened, with a sequence number for replay."""

    seq: int
    type: str
    data: dict[str, Any]
    time: float = field(default_factory=time.time)

    def sse(self) -> str:
        """Format as an SSE frame.

        `id:` carries seq -- that is the field EventSource remembers and sends
        back as Last-Event-ID. Getting this wrong (or omitting it) is what makes
        reconnection lose data, and it is invisible until a connection drops.
        """
        payload = json.dumps({"seq": self.seq, "time": self.time, **self.data},
                             separators=(",", ":"))
        return f"id: {self.seq}\nevent: {self.type}\ndata: {payload}\n\n"


class EventBus:
    """A per-session log of events, with replay.

    One bus per session, written by that session's worker thread and read by
    however many browser tabs are watching. Thread-safe because those really
    are different threads: the worker is inside `graph.invoke()` while the
    request handler is streaming.

    Readers POLL (`replay(cursor)` in a loop) rather than block on a condition
    variable, and that is a deliberate correction rather than laziness. The
    first version had a `follow()` generator that waited on a Condition, and it
    deadlocked the server on shutdown: uvicorn waits for open connections before
    running lifespan shutdown, the connections were waiting inside follow(), and
    follow() was waiting for the shutdown to close the bus. A blocking read also
    cannot be cancelled when a browser tab closes, because it runs in a thread.

    Polling from an async handler has neither problem -- it is cancellable at
    every await -- and at a quarter-second tick the latency is invisible next to
    a model turn that takes minutes.
    """

    def __init__(self, buffer_size: int = BUFFER_SIZE):
        self._lock = threading.Lock()
        self._events: list[Event] = []
        self._counter = itertools.count(1)
        self._buffer_size = buffer_size
        self._closed = False

    # ---- writing ---------------------------------------------------------

    def emit(self, type_: str, **data: Any) -> Event:
        """Append one event and wake every reader."""
        if type_ not in EVENT_TYPES:
            # Loud, because a typo here means the browser silently ignores an
            # event -- the worst kind of bug to chase from the other side.
            raise ValueError(
                f"Unknown event type {type_!r}. Known: {', '.join(EVENT_TYPES)}"
            )
        with self._lock:
            event = Event(seq=next(self._counter), type=type_, data=data)
            self._events.append(event)
            if len(self._events) > self._buffer_size:
                del self._events[: len(self._events) - self._buffer_size]
            return event

    def close(self) -> None:
        """No more events are coming. Readers stop rather than poll forever."""
        with self._lock:
            self._closed = True

    def reopen(self) -> None:
        """A finished session is being driven again -- keep the history and the
        sequence numbers, just start accepting writes."""
        with self._lock:
            self._closed = False

    # ---- reading ---------------------------------------------------------

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._events[-1].seq if self._events else 0

    @property
    def first_seq(self) -> int:
        """The oldest event still buffered. A client asking for anything below
        this has fallen off the back and needs a full reload."""
        with self._lock:
            return self._events[0].seq if self._events else 0

    def replay(self, after_seq: int) -> list[Event]:
        with self._lock:
            return [e for e in self._events if e.seq > after_seq]

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed
