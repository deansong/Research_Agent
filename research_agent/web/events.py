"""
WHAT:  The events an open run sends to the browser, and the buffer that makes
       them replayable.
WHY:   A browser tab that loses its connection for a few seconds must not lose
       those seconds of a run, nor see them twice.
CONCEPT: Server-Sent Events with sequence numbers, as in v1's web UI. Each
       frame carries `id: <seq>`; EventSource reconnects by itself and sends
       the last id back as Last-Event-ID, and the server replays what came
       after it. A closed vocabulary, so the browser switches on types rather
       than parsing prose:

    status   the run's stage, phase, busy / waiting / awaiting, outcome, error
    chat     a message in the conversation: the user's, or the discussor's
    call     a model call started or ended (label, model, session, seconds)
    step     a graph node finished (node, a one-line summary)
    note     a line about the work that is not a model call (waiting on jobs)
    state    the research state changed (its section versions)
    error    something went wrong
"""

from __future__ import annotations

import itertools
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

EVENT_TYPES = ("status", "chat", "call", "step", "note", "state", "error")
BUFFER_SIZE = 5000


@dataclass(frozen=True)
class Event:
    seq: int
    type: str
    data: dict[str, Any]
    time: float = field(default_factory=time.time)

    def sse(self) -> str:
        payload = json.dumps({"seq": self.seq, "time": self.time, **self.data},
                             ensure_ascii=False, default=str)
        return f"id: {self.seq}\nevent: {self.type}\ndata: {payload}\n\n"


class EventBus:
    """One per open run: thread-safe, bounded, replayable."""

    def __init__(self):
        self._events: list[Event] = []
        self._seq = itertools.count(1)
        self._lock = threading.Lock()

    def emit(self, type_: str, **data) -> Event:
        if type_ not in EVENT_TYPES:
            raise ValueError(f"Unknown event type {type_!r}; known: {', '.join(EVENT_TYPES)}")
        with self._lock:
            event = Event(next(self._seq), type_, data)
            self._events.append(event)
            if len(self._events) > BUFFER_SIZE:
                del self._events[: len(self._events) - BUFFER_SIZE]
            return event

    def replay(self, after_seq: int = 0) -> list[Event]:
        with self._lock:
            return [e for e in self._events if e.seq > after_seq]

    @property
    def first_seq(self) -> int:
        with self._lock:
            return self._events[0].seq if self._events else 0

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._events[-1].seq if self._events else 0
