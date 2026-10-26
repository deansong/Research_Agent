"""
WHAT:  The shared half of every backend that drives a coding CLI.
WHY:   `claude` and `agy` are the same machine -- one subprocess per turn,
       NDJSON on stdout, a final envelope carrying the answer and the usage.
       Only the vocabulary differs, so only the vocabulary lives in the
       provider modules.
CONCEPT: Not LangGraph. A provider-boundary adapter, and a port of
       codex.py::_run_turn with a subprocess where the SDK handle was.

Read codex.py::_run_turn beside this: the supervising loop is deliberately the
same shape and the same ORDER, because that order is the design (cancel before
timeout, so somebody who pressed Stop is not told the turn failed).

--------------------------------------------------------------------------
MEASURED, 2026-09-08, against claude 2.1.252 and agy 1.1.27
--------------------------------------------------------------------------
Four of these cost a live turn to learn, and all four are load-bearing:

1. `claude -p` WAITS ON STDIN. It prints "no stdin data received in 3s" and
   carries on, but a turn that ever wants an answer there would block until
   the idle timeout and be reported as "went silent" -- a true statement that
   sends you looking for a network problem. So stdin is closed immediately
   after the prompt is written, and a CLI that wants input gets EOF and
   refuses instead of hanging.

2. `claude` REUSES THE PARENT'S SESSION when run from inside another Claude
   Code session: it inherits ~76 CLAUDE_*/ANTHROPIC_* variables and came back
   with the calling session's own session_id. Backends scrub them.

3. EXIT CODE 0 DOES NOT MEAN SUCCESS. `claude` returned
   {"is_error": true, "result": "Authentication error ..."} and exited 0.
   The envelope is the signal; the exit code is only useful when the process
   died before producing one.

4. agy's `status: "SUCCESS"` DOES NOT MEAN THERE IS AN ANSWER. A denied tool
   call gives SUCCESS, an empty `response`, and no `structured_output` at all.
   So "did we get a parseable answer" is the only success test worth running,
   which is what final_text() below is for.

Full evidence: docs/CLI_BACKENDS.md.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from typing import Any

from research_backend.backends._progress import (_LiveText, flood_message, notice, poll_seconds,
                                      runaway_message, silent_message,
                                      wait_or_cancel, LIVE_FLUSH_SECONDS)
from research_backend import quota
from research_backend.base import say
from research_backend.base import (Access, BackendBusy, BackendCancelled,
                                 BackendError, BackendOutOfCredits, BackendOutputError,
                                 BackendTimeout, BackendUnavailable, OutputT,
                                 StructuredRun, Usage)

#: Environment variables scrubbed from the child. See measured note 2: a CLI
#: run from inside a session of the same CLI inherits that session's identity.
#: Prefixes rather than names, because the list grows with every release.
SCRUBBED_PREFIXES = ("CLAUDE_", "ANTHROPIC_", "CODEX_", "AGENT_", "AI_AGENT")

def _last_line(text) -> str:
    """The last line of a command's output that carries anything.

    The last line is the one worth showing: a training script's tail is its
    result, and its head is a banner. Blank and whitespace lines are skipped
    because a trailing newline is universal and would otherwise make every
    tail empty.
    """
    if not text:
        return ""
    for line in reversed(str(text).replace("\r\n", "\n").split("\n")):
        line = line.strip()
        if line:
            return line
    return ""


#: How much stderr to keep. Enough to put the real reason in an error message,
#: bounded because a chatty child can produce megabytes of deprecation notices.
STDERR_TAIL = 8000

#: Linux's MAX_ARG_STRLEN: the kernel refuses any SINGLE argv entry longer
#: than this, however much room ARG_MAX leaves overall. A designer prompt is
#: ~45 KB today and every piece of it is now editable in the web UI, so this
#: is one long discussion away -- and what execve says about it is "Argument
#: list too long", which names neither the prompt nor the fix.
MAX_ARG_BYTES = 128 * 1024

#: How long to keep reading after the child has EXITED. A tool command the
#: CLI started in the background (`nohup python train.py &`) inherits our
#: stdout, so EOF does not arrive until THAT exits -- and without a bound the
#: finished turn sat there until the idle timeout and was reported as a stall.
EXIT_GRACE = 5.0


class CliBackend:
    """Everything two CLI-driven backends have in common.

    Subclasses supply vocabulary, never machinery: `argv`, `access_flags`,
    `session_args`, `record_for`, `final_text`, `usage_from`,
    `session_id_from`. Each is small and most are pure, which is what makes
    the access-mapping tests offline and instant.
    """

    name = "cli"
    supports_repo_access = True
    max_access = Access.FULL
    executable_default = ""
    #: Shown at the front of every timeout message.
    label = "The CLI"

    #: Send the prompt on stdin instead of as an argv entry. Only for a CLI
    #: measured to read it there -- see MAX_ARG_BYTES for why it matters.
    prompt_via_stdin = False

    #: Which config roles resolved to THIS instance -- stamped by
    #: build_backends, because instances are shared and only the factory knows
    #: the whole mapping. Read only by the timeout messages, so that the
    #: config snippet they print names a real role instead of "<role>". A
    #: class-level default means an instance built directly (a test, a script)
    #: still works, it just prints the generic advice.
    roles: tuple[str, ...] = ()


    def __init__(
        self,
        *,
        model: str | None = None,
        executable: str | None = None,
        timeout: float = 300.0,
        max_seconds: float = 7200.0,
        max_output_tokens: int = 60000,
        busy_attempts: int = 3,
        credit_wait_hours: float = quota.DEFAULT_HOURS,
        **options,
    ):
        self.model = model
        self.executable = executable or self.executable_default
        self.timeout = float(timeout)
        self.max_seconds = float(max_seconds)
        self.max_output_tokens = int(max_output_tokens)
        #: Retries for a turn the PROVIDER refused as busy -- a 503, "no
        #: capacity", "overloaded". Same policy codex.py has always had; the
        #: CLI backends used to fail the node on the first one.
        self.busy_attempts = int(busy_attempts)
        #: How long, in all, to wait out a usage limit or an empty account --
        #: see research_backend/quota.py.
        self.credit_wait_hours = float(credit_wait_hours)
        self.options = options

        # Set here rather than in the subclasses because activity.arm_progress
        # returns early unless the attribute EXISTS -- so a provider that
        # forgot it would silently produce no in-flight record, and nothing
        # anywhere would say so.
        self.on_progress = None
        self.cancel = None
        self.pause = None

        # Fail at startup, not three nodes into a task. Same contract the
        # stubs had; only the reason changes from "not written" to "not here".
        if not shutil.which(self.executable):
            raise BackendUnavailable(
                f"The '{self.name}' backend needs the `{self.executable}` "
                f"command, which is not on PATH.\n\n"
                f"Install it and log in, then try again. To use a different "
                f"binary:\n"
                f'    {{"roles": {{"<role>": {{"options": '
                f'{{"executable": "/path/to/{self.executable}"}}}}}}}}'
            )

    # ---- the parts a provider supplies ----------------------------------

    def argv(self, *, prompt: str, instructions: str, access: Access,
             schema_path: str, session: list[str]) -> list[str]:
        raise NotImplementedError

    def session_args(self, thread_id: str | None) -> tuple[list[str], str | None]:
        """(argv fragment, the id we minted) -- minted is None when the
        provider hands ids back instead of accepting them."""
        raise NotImplementedError

    def record_for(self, event: dict) -> dict | None:
        """One NDJSON line -> a _progress-style record, or None to ignore."""
        raise NotImplementedError

    def final_text(self, envelope: dict) -> str:
        raise NotImplementedError

    def usage_from(self, envelope: dict) -> Usage | None:
        return None

    def session_id_from(self, envelope: dict) -> str | None:
        return None

    def is_envelope(self, event: dict) -> bool:
        raise NotImplementedError

    def transient_error(self, envelope: dict) -> str:
        """The provider's message when this envelope is a retryable refusal
        (overloaded, no capacity), else "". Default: never retry."""
        return ""

    def refusal_text(self, envelope: dict) -> str:
        """The provider's error message when this envelope is a refusal, else
        "". Read for a usage limit, which is waited out for hours rather than
        retried like a busy server."""
        return ""

    # ---- the machinery ---------------------------------------------------

    def run_structured(
        self,
        *,
        thread_id: str | None,
        repo_path: str,
        access: Access,
        developer_instructions: str,
        prompt: str,
        output_model: type[OutputT],
    ) -> StructuredRun[OutputT]:
        import tempfile

        from research_backend.backends._schema import strict_json_schema

        schema = strict_json_schema(output_model)

        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".schema.json", delete=False, encoding="utf-8")
        try:
            json.dump(schema, handle)
            handle.close()

            busy_tries = 0
            credits = quota.CreditWait(hours=self.credit_wait_hours)
            while True:
                # Inside the loop: a retry of a FIRST turn must mint a fresh
                # session id, because the refused attempt may already have
                # claimed the old one and --session-id refuses a reused id.
                fragment, minted = self.session_args(thread_id)
                argv = self.argv(prompt=prompt, instructions=developer_instructions,
                                 access=access, schema_path=handle.name,
                                 session=fragment)
                recorded, envelope, streamed, live = self._run(
                    argv, repo_path,
                    stdin_text=prompt if self.prompt_via_stdin else None)

                refused = self.refusal_text(envelope) if envelope else ""
                if refused and quota.is_quota(refused):
                    try:
                        seconds, why = credits.next(refused)
                    except StopIteration:
                        raise BackendOutOfCredits(
                            f"{self.label} was still over its usage limit after "
                            f"waiting {credits.describe_budget()}. It said:\n\n"
                            f"    {refused[:600]}\n\nYour progress is saved -- "
                            f"Start the session again to carry on.") from None
                    notice(self, f"\n    {self.label} is over its usage limit. Waiting "
                                 f"{seconds / 60:.0f} min ({why}, until "
                                 f"{quota.until(seconds)}); check {credits.tries}, giving up "
                                 f"after {credits.describe_budget()}.\n    It said: "
                                 f"{refused[:300]}\n    Pause stops waiting (give the role a "
                                 f"backup, then carry on) -- your progress is saved.",
                           until=time.time() + seconds)
                    wait_or_cancel(seconds, self.cancel, self.pause)
                    continue

                reason = self.transient_error(envelope) if envelope else ""
                if not reason:
                    break
                busy_tries += 1
                if busy_tries > self.busy_attempts:
                    raise BackendBusy(
                        f"{self.label} was still refusing after "
                        f"{self.busy_attempts} retries. It said:\n\n    "
                        f"{reason[:600]}\n\nThat is the provider being full, "
                        f"not this node failing. Resume the session to try "
                        f"again, or point the role at another model.")
                seconds = 5.0 * (2 ** (busy_tries - 1))
                notice(self, f"\n    {self.label} is busy ({reason[:120]}). Waiting "
                             f"{seconds:.0f}s, then retrying ({busy_tries}/"
                             f"{self.busy_attempts}).", until=time.time() + seconds)
                wait_or_cancel(seconds, self.cancel, self.pause)
        finally:
            os.unlink(handle.name)

        if envelope is None:
            raise BackendError(
                f"{self.label} produced no result envelope. Either the command "
                f"failed before it began, or its output format has changed "
                f"since docs/CLI_BACKENDS.md was measured."
            )

        text = self.final_text(envelope)
        data = self._validate(text, output_model, envelope, access)

        resolved = minted or self.session_id_from(envelope) or ""
        return StructuredRun(
            data=data,
            thread_id=resolved,
            is_new_thread=thread_id is None,
            usage=self.usage_from(envelope),
            events=recorded,
        )

    def _validate(self, text: str, output_model: type[OutputT],
                  envelope: dict, access: Access | None = None) -> OutputT:
        """Parse, or raise BackendOutputError.

        No in-backend repair turn. designer.py and planner.py already catch
        this, feed the message back as `problems`, and bound the loop with
        max_design_attempts -- a second budget in here would nest two counters
        and make the attempt number a human sees a lie.
        """
        from pydantic import ValidationError

        from research_backend.backends._progress import _clip

        if text.strip():
            try:
                return output_model.model_validate_json(text)
            except ValidationError as exc:
                raise BackendOutputError(
                    f"{self.label} replied with something that does not match "
                    f"the schema.\n\n{exc}\n\nWhat it sent:\n{_clip(text)}"
                ) from None

        raise BackendOutputError(
            f"{self.label} returned no answer at all.\n\n"
            f"{self._why_empty(envelope, access)}"
        )

    def _why_empty(self, envelope: dict, access: Access | None = None) -> str:
        return ("The turn reported success but carried no structured output. "
                "The usual cause is a tool call it was not permitted to make.")

    def _check_argv(self, argv: list[str]) -> None:
        """Refuse, with the real reason, an argument the kernel will refuse."""
        if not sys.platform.startswith("linux"):
            return
        for index, arg in enumerate(argv):
            size = len(arg.encode("utf-8", "surrogateescape"))
            if size < MAX_ARG_BYTES:
                continue
            flag = argv[index - 1] if index and argv[index - 1].startswith("-") else ""
            raise BackendError(
                f"{self.label} cannot be started: one command-line argument"
                f"{f' (after {flag})' if flag else ''} is {size:,} bytes, and "
                f"Linux refuses any single argument over {MAX_ARG_BYTES:,}.\n\n"
                f"That is the prompt or the instructions this node sends. "
                f"Shorten them -- in the web UI, the Design agent tab shows "
                f"each one with its size -- or point the role at a provider "
                f"that takes its prompt another way (codex, claude_code).")

    def _run(self, argv: list[str], repo_path: str, stdin_text: str | None = None):
        """Spawn, stream, supervise. The port of codex.py::_run_turn."""
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(SCRUBBED_PREFIXES)}
        self._check_argv(argv)

        try:
            proc = subprocess.Popen(
                argv, cwd=repo_path or None, env=env, bufsize=1,
                # Decoded explicitly, never by locale. Under LANG=C the
                # default is ASCII, and the first "é" in a command's output
                # killed the reader thread with UnicodeDecodeError -- which
                # the caller then re-raised as the turn's failure.
                text=True, encoding="utf-8", errors="replace",
                # Measured note 1: never leave a CLI waiting on stdin. It gets
                # the prompt and then EOF, or EOF straight away.
                stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                # Its own process group, so abandoning a turn can reach the
                # commands it started. terminate() alone killed the CLI and
                # left the training run it had launched running, unowned,
                # on the GPU.
                start_new_session=os.name == "posix",
            )
        except OSError as exc:
            raise BackendError(f"Could not run {argv[0]!r}: {exc}") from None

        started = time.monotonic()
        last_event = [started]
        last_line = [started]
        last_flush = [0.0]

        recorded: list[dict] = []
        kinds: dict[str, int] = {}
        last_seen = [""]
        #: The tail of the most recent command output. Tracked separately from
        #: last_seen because it answers a different question: last_seen is
        #: WHAT IT IS DOING, this is WHAT IT FOUND OUT -- and on a training run
        #: that is the accuracy, the epoch, the loss. Preferred in the
        #: heartbeat, because "last: . step 171" repeated ten times was the
        #: complaint that started this.
        last_result = [""]
        streamed = [0]
        live = _LiveText()
        envelope: list[dict | None] = [None]
        errors: list[BaseException] = []
        #: Set once the turn is over. A background grandchild can keep our
        #: stdout open after that; the reader keeps draining it (so that
        #: process never blocks on a full pipe) but stops recording, because
        #: `recorded` has been handed back to the caller by then.
        finished = [False]

        # stderr gets its OWN thread. This is not tidiness: a child that writes
        # more than a pipe buffer (~64 KB) to stderr blocks on the write, stops
        # producing stdout, and looks exactly like a wedged turn. codex.py has
        # no analogue of this hazard, so it does not occur to you when porting.
        tail: deque[str] = deque(maxlen=200)

        def drain_stderr():
            try:
                for line in proc.stderr:
                    tail.append(line)
            except (OSError, ValueError):
                pass            # the pipe was closed under us; nothing to keep

        def feed_stdin():
            # A thread, not a write before the loop: a prompt bigger than the
            # pipe buffer blocks until the child reads it, and a child that
            # never does would otherwise hang us outside every timeout.
            try:
                proc.stdin.write(stdin_text)
            except (BrokenPipeError, OSError, ValueError):
                pass            # it exited first; its envelope says why
            finally:
                try:
                    proc.stdin.close()
                except (BrokenPipeError, OSError, ValueError):
                    pass

        def read_stdout():
            try:
                for line in proc.stdout:
                    if finished[0]:
                        continue
                    last_event[0] = time.monotonic()
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        # Banners, progress bars and update notices are normal
                        # on a CLI's stdout. "The parse died on line 1 because
                        # of a version notice" is not an acceptable failure.
                        recorded.append({"kind": "cli_stdout", "phase": "other",
                                         "text": line[:500]})
                        continue
                    if not isinstance(event, dict):
                        recorded.append({"kind": "cli_stdout", "phase": "other",
                                         "text": line[:500]})
                        continue

                    if self.is_envelope(event):
                        envelope[0] = event

                    entry = self.record_for(event)
                    if entry is None:
                        continue
                    if entry.pop("transient", False):
                        streamed[0] += 1
                        live.add(entry.get("stream", "other"),
                                 entry.get("text", ""))
                        continue

                    entry["at"] = round(time.monotonic() - started, 2)
                    recorded.append(entry)
                    if entry.get("phase") == "started":
                        # Cleared, not left standing. A result belongs to the
                        # command that produced it, and showing the PREVIOUS
                        # command's tail while a training run is in flight is
                        # worse than showing nothing: it looks like progress
                        # and it is forty minutes old. With it cleared the
                        # heartbeat falls back to last_seen -- "$ python
                        # train.py" -- which is the true answer to what is
                        # happening right now.
                        last_result[0] = ""
                    tail = _last_line(entry.get("output"))
                    if tail:
                        last_result[0] = tail
                    kind = str(entry.get("kind", "?"))
                    if entry.get("phase") != "started":
                        kinds[kind] = kinds.get(kind, 0) + 1

                    from research_backend.backends._progress import headline
                    line_out = headline(entry)
                    if line_out and line_out != last_seen[0]:
                        last_seen[0] = line_out.strip()
                        say(self, line_out, flush=True)
                        last_line[0] = time.monotonic()
            except (OSError, ValueError):
                # The pipe closed under us: _abandon killed the group, or the
                # turn finished and the stream was released. Not a failure of
                # the turn -- whatever it produced is already recorded.
                pass
            except BaseException as exc:  # noqa: BLE001 - re-raised on the caller
                errors.append(exc)

        threads = [
            threading.Thread(target=drain_stderr, daemon=True,
                             name=f"{self.name}-stderr"),
        ]
        if stdin_text is not None:
            threads.append(threading.Thread(target=feed_stdin, daemon=True,
                                            name=f"{self.name}-stdin"))
        worker = threading.Thread(target=read_stdout, daemon=True,
                                  name=f"{self.name}-stdout")
        for thread in threads:
            thread.start()
        worker.start()
        stderr_thread = threads[0]

        exited_at: float | None = None
        try:
            while worker.is_alive():
                poll = poll_seconds(self.timeout, self.max_seconds, self.cancel)
                if envelope[0] is not None:
                    # The answer is in; all that is left is noticing the exit.
                    poll = min(poll, 0.25)
                worker.join(timeout=poll)
                now = time.monotonic()
                if not worker.is_alive():
                    break

                # The CLI has exited but stdout is still open: something it
                # started holds the other end. Its answer is complete once
                # the envelope is in; give the pipe a moment to drain
                # otherwise, and then stop waiting on it.
                if proc.poll() is not None:
                    exited_at = exited_at or now
                    if envelope[0] is not None or now - exited_at >= EXIT_GRACE:
                        break
                    continue

                idle = now - last_event[0]
                elapsed = now - started

                # Cancel FIRST: telling somebody who pressed Stop that their turn
                # timed out reads as a failure when it was a decision.
                if self.cancel is not None and self.cancel.is_set():
                    self._abandon(proc, worker)
                    raise BackendCancelled(
                        f"Stopped by request after {elapsed:.0f}s "
                        f"({len(recorded)} events). Everything the graph had "
                        f"already finished is checkpointed; this turn is discarded."
                    )
                if idle >= self.timeout:
                    self._abandon(proc, worker)
                    raise BackendTimeout(silent_message(
                        self.label, idle, elapsed, len(recorded), streamed[0], live,
                        last_seen[0], self.roles, self.max_seconds))
                if elapsed >= self.max_seconds:
                    self._abandon(proc, worker)
                    raise BackendTimeout(runaway_message(
                        self.label, elapsed, len(recorded)))
                if streamed[0] >= self.max_output_tokens:
                    self._abandon(proc, worker)
                    raise BackendTimeout(flood_message(
                        self.label, streamed[0], elapsed))

                if self.on_progress is not None and now - last_flush[0] >= LIVE_FLUSH_SECONDS:
                    last_flush[0] = now
                    try:
                        self.on_progress({
                            "events": list(recorded),
                            "elapsed": elapsed,
                            "idle": idle,
                            "counts": dict(kinds),
                            "last": live.tail() or last_result[0] or last_seen[0],
                            "streamed": streamed[0],
                            "live": live.snapshot(),
                        })
                    except Exception:  # noqa: BLE001 - reporting must not break the turn
                        pass

                if now - last_line[0] >= 30.0:
                    from research_backend.backends.codex import _heartbeat
                    say(self, _heartbeat(elapsed, idle, len(recorded), kinds,
                                     live.tail() or last_result[0] or last_seen[0],
                                     streamed[0]),
                          flush=True)
                    last_line[0] = now
        except BaseException:
            # Ctrl-C, or anything else unwinding through here: the child must
            # not outlive the turn that owned it. Idempotent, so the typed
            # errors above -- which have already abandoned -- pass through
            # this harmlessly.
            self._abandon(proc, worker)
            raise
        finally:
            finished[0] = True

        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            # stdout closed but the process is still running: it has said all
            # it is going to. Do not leave it behind.
            self._abandon(proc, worker)
        # Short: the child has exited, so what it wrote is already in the
        # pipe. Anything still holding stderr open is a grandchild, and the
        # turn does not wait on those.
        stderr_thread.join(timeout=2)
        if errors:
            raise errors[0]

        if envelope[0] is None and proc.returncode not in (0, None):
            raise BackendError(
                f"{self.executable} exited {proc.returncode}.\n\n"
                f"{''.join(tail)[-STDERR_TAIL:].strip()}"
            )
        return list(recorded), envelope[0], streamed[0], live

    def _abandon(self, proc, worker) -> None:
        """Stop a turn we have given up on, and make sure the child is gone.

        The whole process GROUP, not just the child: the commands a turn runs
        are the CLI's children, and they are what hold the GPU. TERM first so
        they can clean up, then KILL for whatever ignored it. Safe to call
        twice, and on a process that has already exited.
        """
        self._signal(proc, signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        # Unconditionally: the CLI exiting on TERM does not mean the training
        # run it started did too.
        self._signal(proc, getattr(signal, "SIGKILL", signal.SIGTERM))
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        worker.join(timeout=5.0)

    @staticmethod
    def _signal(proc, sig) -> None:
        try:
            if os.name == "posix":
                os.killpg(proc.pid, sig)
            elif sig == signal.SIGTERM:
                proc.terminate()
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass    # already gone -- which is what we wanted

    def close(self) -> None:
        return None


def usage_from_keys(source: dict[str, Any], **mapping) -> Usage:
    """Build a Usage by naming each provider's spelling of each field."""
    values = {ours: int(source.get(theirs) or 0)
              for ours, theirs in mapping.items() if theirs}
    return Usage(**values)
