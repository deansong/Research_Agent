"""
WHAT:  Driving `codex login` / `claude auth login` from the browser.
WHY:   The status panel can say "not logged in" in one line; fixing it should
       not mean leaving the page, finding the terminal the server is running
       in, and knowing which of three commands to type.
CONCEPT: Not LangGraph, not FastAPI.  One child process on a pseudo-terminal,
       plus the two things a human has to see (a link, a code) pulled out of
       what it printed.

--------------------------------------------------------------------------
WHY A PTY AND NOT A PIPE
--------------------------------------------------------------------------
MEASURED: with stdout on a pipe, both `codex login` and `claude auth login`
print NOTHING AT ALL and sit there until killed. They check isatty() and
render an interactive flow or nothing.  On a pty both print immediately.

The pty is made with pty.openpty() and handed to a normal Popen, rather than
pty.fork().  pty.fork() forks the whole interpreter, and this one is a web
server full of threads holding locks; a child that deadlocked between fork and
exec would hang a request with no way to see why.  Measured: openpty + Popen +
start_new_session is enough for both CLIs, so the fork is not needed.

--------------------------------------------------------------------------
WHAT THIS DELIBERATELY DOES NOT DO
--------------------------------------------------------------------------
Antigravity.  `agy` has no login subcommand at all -- signing in means running
the bare TUI, which opens the alternate screen buffer and negotiates the kitty
keyboard protocol.  Driving that from a browser means writing a terminal
emulator, and a half-working one would be worse than the sentence telling you
to run `agy` yourself.  agent/backends/auth.py says so per provider, in
`browser_login`, and the page reads it from there rather than hardcoding it.

--------------------------------------------------------------------------
SUCCESS IS RE-PROBED, NOT ASSUMED
--------------------------------------------------------------------------
A login is finished when the PROVIDER SAYS SO -- the flow re-runs the status
probe after the process exits and reports that. Exit code 0 has already been
caught lying once in this project (see _cli.py's measured note 3, and the
`claude auth status` note in backends/auth.py), and "we ran the command" is
not the same claim as "you are logged in".
"""

from __future__ import annotations

import errno
import fcntl
import os
import pty
import re
import select
import shutil
import struct
import subprocess
import termios
import threading
import time
from dataclasses import dataclass, field

from agent.backends.auth import _ANSI, AuthStatus, status_for

#: Codex says its device code "expires in 15 minutes", so nothing is gained by
#: holding the child open longer than that.
LOGIN_TIMEOUT = 900.0

#: How much of the child's output to keep. These flows print a screen, not a
#: log; this is generous and still bounded.
OUTPUT_TAIL = 20000

#: A wide, tall terminal. Not cosmetic: at 80 columns `claude` wraps its OAuth
#: URL across lines, and a wrapped URL is one nobody can click and the
#: extraction below cannot find.
WINSIZE = (50, 200)

#: The first link the flow prints is the one to open. Trailing punctuation is
#: excluded so a URL at the end of a sentence stays clickable.
_URL = re.compile(r"https?://[^\s\"'<>]+[^\s\"'<>.,)]")

#: Codex's one-time device code, e.g. "CVYX-6ZWYE", alone on its line.
#: The \s* is load-bearing, not tidiness: codex INDENTS the code under its
#: numbered step, so an expression anchored hard at column 0 finds nothing and
#: the page shows a sign-in link with no code to type into it.
_CODE = re.compile(r"^[ \t]*([A-Z0-9]{4,8}-[A-Z0-9]{4,8})[ \t]*$", re.MULTILINE)


@dataclass(frozen=True)
class Flow:
    """How to sign one provider in."""

    argv: list[str]
    #: Mirrors the backend's own environment handling, for the same reason the
    #: status probe does: log in to the credentials file the turns will read.
    #: See backends/auth.py, measured note 2.
    scrub: bool
    #: Whether the human has to bring a code back from the browser. `claude`
    #: prompts "Paste code here"; `codex --device-auth` polls and needs nothing.
    expects_code: bool
    #: Shown next to the input box, so the box says what to put in it.
    input_label: str = ""


FLOWS: dict[str, Flow] = {
    "codex": Flow(
        # --device-auth rather than the default browser flow: the default opens
        # a browser on the SERVER and waits on a localhost callback there,
        # which is exactly wrong when the page is being viewed over an SSH
        # tunnel from a laptop. A device code travels.
        argv=["codex", "login", "--device-auth"],
        scrub=False,
        expects_code=False,
    ),
    "claude_code": Flow(
        argv=["claude", "auth", "login"],
        scrub=True,
        expects_code=True,
        input_label="Paste the code from the browser",
    ),
}


#: Outcomes nothing may overwrite. See _finish.
_TERMINAL = frozenset({"done", "failed", "cancelled", "timeout"})


class LoginError(RuntimeError):
    """Something that stops a flow starting. The message is shown verbatim."""


def _plain(text: str) -> str:
    """Terminal output as a human would see it on screen.

    Two things, both measured rather than guessed:

    1. A pty translates every newline to CRLF on the way out (ONLCR), so every
       line arrives ending in "\r". This cost an hour: `$` in a multiline
       pattern matches before the "\n" and leaves the "\r" sitting there, so
       an expression for "the device code, alone on its line" matched nothing
       and the page offered a sign-in link with no code beside it.

    2. A lone "\r" is a carriage return -- the tool rewriting the line it just
       printed, which is how progress lines work. Keeping what follows the last
       one is what the screen would show; keeping everything shows the tool's
       working out.
    """
    lines = []
    for line in text.replace("\r\n", "\n").split("\n"):
        # rstrip BEFORE the rewrite rule. A tool that writes its own "\r\n"
        # gets it translated again on the way out, arriving as "\r\r\n"; one
        # "\r" is consumed as the line ending and the other is left dangling
        # at the end. Treating that leftover as a rewrite empties the line --
        # which is how the device code vanished a SECOND time, from a line the
        # transcript showed perfectly.
        line = line.rstrip("\r")
        lines.append(line.rsplit("\r", 1)[-1] if "\r" in line else line)
    return "\n".join(lines)


class LoginSession:
    """One running login. Started once, read many times, ends by itself."""

    def __init__(self, provider: str):
        flow = FLOWS.get(provider)
        if flow is None:
            raise LoginError(
                f"There is no browser login for '{provider}'. "
                f"See the panel for what to run in a terminal instead."
            )
        if not shutil.which(flow.argv[0]):
            raise LoginError(f"`{flow.argv[0]}` is not on PATH on the server.")

        self.provider = provider
        self.flow = flow
        self.started = time.monotonic()
        self.state = "starting"
        self.result: AuthStatus | None = None
        self.error = ""

        self._lock = threading.Lock()
        self._text = ""
        #: Everything the human typed, so it can be kept out of the transcript.
        #: The pty echoes input back, and for `claude` that input is a
        #: single-use OAuth code -- no reason to leave it sitting in a buffer
        #: that any later GET can read.
        self._secrets: list[str] = []
        self._proc: subprocess.Popen | None = None
        self._master = -1

        self._spawn()

    # ---- lifecycle -------------------------------------------------------

    def _spawn(self) -> None:
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", *WINSIZE, 0, 0))

        # ECHO off, so a pasted code does not come straight back out of the
        # pty into the transcript. Belt and braces with _secrets below: a child
        # that resets termios itself would defeat this, and nothing warns you.
        attrs = termios.tcgetattr(slave)
        attrs[3] &= ~termios.ECHO           # lflag
        termios.tcsetattr(slave, termios.TCSANOW, attrs)

        env = dict(os.environ)
        if self.flow.scrub:
            from agent.backends._cli import SCRUBBED_PREFIXES
            env = {k: v for k, v in env.items()
                   if not k.startswith(SCRUBBED_PREFIXES)}

        try:
            self._proc = subprocess.Popen(
                self.flow.argv, stdin=slave, stdout=slave, stderr=slave,
                env=env, start_new_session=True, close_fds=True,
            )
        except OSError as exc:
            os.close(master)
            os.close(slave)
            raise LoginError(f"Could not run {self.flow.argv[0]!r}: {exc}") from None

        os.close(slave)
        self._master = master
        self.state = "running"

        thread = threading.Thread(target=self._read, name=f"login-{self.provider}",
                                  daemon=True)
        thread.start()

    def _read(self) -> None:
        """Drain the pty until the child goes, then ask whether it worked."""
        try:
            while True:
                if time.monotonic() - self.started > LOGIN_TIMEOUT:
                    self._finish("timeout", "The login took too long and was "
                                            "cancelled. Start it again.")
                    return

                ready, _, _ = select.select([self._master], [], [], 0.5)
                if ready:
                    try:
                        chunk = os.read(self._master, 65536)
                    except OSError as exc:
                        # EIO on Linux is how a pty says "the child closed it".
                        if exc.errno != errno.EIO:
                            raise
                        chunk = b""
                    if not chunk:
                        break
                    self._append(chunk.decode("utf-8", "replace"))

                if self._proc.poll() is not None:
                    # One more non-blocking drain: the exit and the last write
                    # race, and the last write is usually the interesting one.
                    self._drain()
                    break
        except Exception as exc:                      # pragma: no cover
            self._finish("failed", f"The login flow broke: {exc}")
        finally:
            # The reader owns the master fd, start to finish. Closing it from
            # cancel() -- which is what a request thread calls -- would pull it
            # out from under the select() above, and the EBADF that follows
            # looks like a crash rather than a cancellation.
            self._close_master()

        self._settle()

    def _drain(self) -> None:
        while True:
            ready, _, _ = select.select([self._master], [], [], 0)
            if not ready:
                return
            try:
                chunk = os.read(self._master, 65536)
            except OSError:
                return
            if not chunk:
                return
            self._append(chunk.decode("utf-8", "replace"))

    def _settle(self) -> None:
        """The child is gone. Ask the provider whether it actually worked."""
        if self.state not in ("running", "starting"):
            return                                    # cancelled or timed out

        status = status_for(self.provider)
        if status.logged_in:
            self._finish("done", "", status)
        else:
            self._finish(
                "failed",
                status.detail or "The login did not complete.",
                status,
            )

    def _finish(self, state: str, error: str, result: AuthStatus | None = None) -> None:
        """Settle on an outcome, once.

        The guard is not defensive: cancel() comes from a request thread while
        the reader thread is mid-loop, and without it the reader's own "the
        pipe broke" would land afterwards and overwrite "cancelled" with
        "failed" -- telling somebody their own Cancel button was an error.
        """
        with self._lock:
            if self.state in _TERMINAL:
                return
            self.state = state
            self.error = error
            self.result = result
            proc, self._proc = self._proc, None

        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

    def _close_master(self) -> None:
        with self._lock:
            fd, self._master = self._master, -1
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass

    # ---- what the browser does to it -------------------------------------

    def send(self, text: str) -> None:
        """Type a line at the child. CR, because that is what Enter is."""
        with self._lock:
            if self.state != "running" or self._master < 0:
                raise LoginError("That login is no longer running. Start it again.")
            if text:
                self._secrets.append(text.strip())
            # Under the lock, so the reader cannot close the fd between the
            # check and the write.
            os.write(self._master, (text.strip() + "\r").encode())

    def cancel(self) -> None:
        self._finish("cancelled", "")

    # ---- what the browser reads off it -----------------------------------

    def _append(self, text: str) -> None:
        with self._lock:
            self._text = (self._text + text)[-OUTPUT_TAIL:]

    def transcript(self) -> str:
        with self._lock:
            text, secrets = self._text, list(self._secrets)
        text = _plain(_ANSI.sub("", text))
        for secret in secrets:
            if secret:
                text = text.replace(secret, "…")
        return text

    def snapshot(self) -> dict:
        text = self.transcript()
        urls = _URL.findall(text)
        codes = _CODE.findall(text)
        running = self.state == "running"

        return {
            "provider": self.provider,
            "state": self.state,
            "error": self.error,
            "elapsed": round(time.monotonic() - self.started, 1),
            "output": text.strip(),
            # The first, not the last: codex prints the sign-in link first and
            # a support link in its footer.
            "url": urls[0] if urls else "",
            "code": codes[0] if codes else "",
            "expects_code": self.flow.expects_code,
            "input_label": self.flow.input_label,
            "accepts_input": running and self.flow.expects_code,
            "running": running,
            "command": " ".join(self.flow.argv),
        }


# ---- one flow per provider, process-wide ----------------------------------

_SESSIONS: dict[str, LoginSession] = {}
_LOCK = threading.Lock()


def start(provider: str, *, restart: bool = False) -> LoginSession:
    """Begin a login, or hand back the one already going.

    One at a time per provider: two concurrent `codex login` flows would race
    to write the same auth.json, and the second device code would invalidate
    the first -- so the second person just gets shown the first one's code.
    """
    with _LOCK:
        existing = _SESSIONS.get(provider)
        if existing is not None and existing.state == "running" and not restart:
            return existing
        if existing is not None:
            existing.cancel()
        session = LoginSession(provider)
        _SESSIONS[provider] = session
        return session


def get(provider: str) -> LoginSession | None:
    with _LOCK:
        return _SESSIONS.get(provider)


def shutdown() -> None:
    """Stop every flow. Called when the server goes down, so a pty and a child
    process do not outlive it."""
    with _LOCK:
        sessions = list(_SESSIONS.values())
        _SESSIONS.clear()
    for session in sessions:
        session.cancel()
