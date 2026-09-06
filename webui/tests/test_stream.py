"""
WHAT:  Reads the SSE endpoint over a real socket, the way a browser does.
WHY:   Replay-on-reconnect is the one feature that is invisible until the day a
       connection drops, and then it is data loss. It has to be tested, and it
       cannot be tested through a mock: the behaviour lives in HTTP headers and
       chunked framing.
CONCEPT: A real uvicorn server on a real port, and raw `id:` / `event:` frames.

Run with:   python -m pytest webui/tests/test_stream.py -q
"""

from __future__ import annotations

import json
import os
import pathlib
import socket
import sys
import tempfile
import threading
import time

import pytest

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from webui import server  # noqa: E402


class _Server:
    """A uvicorn server on a free port, in a background thread."""

    def __init__(self, repo: pathlib.Path):
        import uvicorn

        class Args:
            backend = "fake"
            model = None
            backend_role = None
            config = None
            session = None

        server._RUNNERS.clear()
        self.port = _free_port()
        config = uvicorn.Config(server.create_app(repo, Args()),
                                host="127.0.0.1", port=self.port, log_level="error",
                                timeout_graceful_shutdown=5)
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def __enter__(self):
        self._thread.start()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self._server.started:
                return self
            time.sleep(0.05)
        raise AssertionError("uvicorn did not start")

    def __exit__(self, *exc):
        self._server.should_exit = True
        self._thread.join(timeout=15)

    # ---- plain requests --------------------------------------------------

    def post(self, path: str, body: dict) -> dict:
        import urllib.request

        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)

    def get(self, path: str) -> dict:
        import urllib.request

        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}{path}", timeout=15
        ) as response:
            return json.load(response)

    # ---- the streaming one -----------------------------------------------

    def stream_seqs(self, session_id: str, *, headers: str = "",
                    query: str = "", settle: float = 2.0) -> list[int]:
        """Open the SSE endpoint on a raw socket and collect the `id:` values.

        Raw sockets rather than a client library on purpose. An SSE response is
        an infinite chunked stream, and every convenience wrapper has its own
        idea of when to stop reading -- the first version of this check used
        urllib and silently reported zero events for a stream the server was
        demonstrably sending. A socket cannot lie about what arrived.
        """
        connection = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        connection.settimeout(settle)
        connection.sendall(
            f"GET /api/sessions/{session_id}/events{query} HTTP/1.1\r\n"
            f"Host: localhost\r\n"
            f"Accept: text/event-stream\r\n"
            f"{headers}"
            f"\r\n".encode()
        )

        raw = b""
        try:
            while True:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                raw += chunk
        except socket.timeout:
            pass  # the stream stays open; we read until it goes quiet
        finally:
            connection.close()

        assert b"200 OK" in raw.split(b"\r\n", 1)[0], raw[:200]
        return [int(line.split(b"id: ", 1)[1])
                for line in raw.splitlines() if line.startswith(b"id: ")]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_to_a_question(live: _Server, folder: pathlib.Path) -> str:
    session_id = live.post("/api/sessions", {
        "repo": str(folder), "task": "look around", "backend": "fake",
    })["id"]
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        detail = live.get(f"/api/sessions/{session_id}")
        if detail["waiting"]:
            return session_id
        if not detail["busy"]:
            raise AssertionError(f"finished early: {detail['error']}")
        time.sleep(0.05)
    raise AssertionError("no question in time")


def test_sse_headers_and_framing():
    """The response must be a never-ending text/event-stream, unbuffered.

    `X-Accel-Buffering: no` looks like superstition until you put this behind
    nginx, which buffers text/event-stream by default and turns a live feed into
    one enormous delivery at the very end.
    """
    with tempfile.TemporaryDirectory() as tmp:
        folder = pathlib.Path(tmp)
        with _Server(folder) as live:
            session_id = _run_to_a_question(live, folder)

            connection = socket.create_connection(("127.0.0.1", live.port), timeout=10)
            connection.settimeout(2.0)
            connection.sendall(
                f"GET /api/sessions/{session_id}/events HTTP/1.1\r\n"
                f"Host: localhost\r\n\r\n".encode()
            )
            raw = b""
            try:
                while b"\n\n" not in raw or raw.count(b"\n\n") < 3:
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    raw += chunk
            except socket.timeout:
                pass
            connection.close()

            head = raw.split(b"\r\n\r\n", 1)[0].lower()
            assert b"content-type: text/event-stream" in head, head
            assert b"cache-control: no-cache" in head, head
            assert b"x-accel-buffering: no" in head, head
            assert b"id: 1" in raw and b"event: " in raw, raw[:400]
            print("PASS  the stream is text/event-stream, unbuffered, and framed")


def test_reconnecting_with_last_event_id_loses_nothing_and_repeats_nothing():
    """The whole reason events carry a sequence number.

    A browser's EventSource reconnects by itself and sends the last id it saw
    back in a Last-Event-ID header. If the server ignores that, every dropped
    connection silently swallows however many events went past in the meantime.
    """
    with tempfile.TemporaryDirectory() as tmp:
        folder = pathlib.Path(tmp)
        with _Server(folder) as live:
            session_id = _run_to_a_question(live, folder)

            everything = live.stream_seqs(session_id)
            assert len(everything) > 4, everything
            assert everything == sorted(everything), everything

            cut = everything[len(everything) // 2]
            expected = [s for s in everything if s > cut]

            # Exactly what the browser sends after a drop.
            resumed = live.stream_seqs(
                session_id, headers=f"Last-Event-ID: {cut}\r\n")
            assert resumed == expected, (resumed, expected)

            # ...and the manual equivalent, for a client reconnecting on purpose.
            by_query = live.stream_seqs(session_id, query=f"?after_seq={cut}")
            assert by_query == expected, (by_query, expected)

            print(f"PASS  resuming after seq {cut} replays exactly "
                  f"{len(expected)} events, no gap and no repeat")


def test_a_late_subscriber_gets_the_whole_history():
    """Opening a tab after a run has started must not show an empty page.

    This is the ordinary case, not an edge case: you start a long run, go and
    make tea, and come back to a browser that has to catch up.
    """
    with tempfile.TemporaryDirectory() as tmp:
        folder = pathlib.Path(tmp)
        with _Server(folder) as live:
            session_id = _run_to_a_question(live, folder)
            detail = live.get(f"/api/sessions/{session_id}")

            seqs = live.stream_seqs(session_id)
            assert seqs[0] == 1, f"replay must start at the beginning: {seqs[:3]}"
            assert seqs[-1] == detail["last_seq"], (seqs[-1], detail["last_seq"])
            assert seqs == list(range(1, detail["last_seq"] + 1)), seqs
            print(f"PASS  a stream opened late replays all {len(seqs)} events")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
