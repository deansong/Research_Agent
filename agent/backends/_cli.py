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
import subprocess
import threading
import time
from collections import deque
from typing import Any

from agent.backends._progress import (_LiveText, flood_message, poll_seconds,
                                      runaway_message, silent_message)
from agent.backends.base import (Access, BackendCancelled, BackendError,
                                 BackendOutputError, BackendTimeout,
                                 BackendUnavailable, OutputT, StructuredRun,
                                 Usage)

#: Environment variables scrubbed from the child. See measured note 2: a CLI
#: run from inside a session of the same CLI inherits that session's identity.
#: Prefixes rather than names, because the list grows with every release.
SCRUBBED_PREFIXES = ("CLAUDE_", "ANTHROPIC_", "CODEX_", "AGENT_", "AI_AGENT")

#: How much stderr to keep. Enough to put the real reason in an error message,
#: bounded because a chatty child can produce megabytes of deprecation notices.
STDERR_TAIL = 8000


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

    def __init__(
        self,
        *,
        model: str | None = None,
        executable: str | None = None,
        timeout: float = 300.0,
        max_seconds: float = 7200.0,
        max_output_tokens: int = 60000,
        **options,
    ):
        self.model = model
        self.executable = executable or self.executable_default
        self.timeout = float(timeout)
        self.max_seconds = float(max_seconds)
        self.max_output_tokens = int(max_output_tokens)
        self.options = options

        # Set here rather than in the subclasses because activity.arm_progress
        # returns early unless the attribute EXISTS -- so a provider that
        # forgot it would silently produce no in-flight record, and nothing
        # anywhere would say so.
        self.on_progress = None
        self.cancel = None

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

        from agent.backends._schema import strict_json_schema

        schema = strict_json_schema(output_model)
        fragment, minted = self.session_args(thread_id)

        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".schema.json", delete=False, encoding="utf-8")
        try:
            json.dump(schema, handle)
            handle.close()

            argv = self.argv(prompt=prompt, instructions=developer_instructions,
                             access=access, schema_path=handle.name,
                             session=fragment)
            recorded, envelope, streamed, live = self._run(argv, repo_path)
        finally:
            os.unlink(handle.name)

        if envelope is None:
            raise BackendError(
                f"{self.label} produced no result envelope. Either the command "
                f"failed before it began, or its output format has changed "
                f"since docs/CLI_BACKENDS.md was measured."
            )

        text = self.final_text(envelope)
        data = self._validate(text, output_model, envelope)

        resolved = minted or self.session_id_from(envelope) or ""
        return StructuredRun(
            data=data,
            thread_id=resolved,
            is_new_thread=thread_id is None,
            usage=self.usage_from(envelope),
            events=recorded,
        )

    def _validate(self, text: str, output_model: type[OutputT],
                  envelope: dict) -> OutputT:
        """Parse, or raise BackendOutputError.

        No in-backend repair turn. designer.py and planner.py already catch
        this, feed the message back as `problems`, and bound the loop with
        max_design_attempts -- a second budget in here would nest two counters
        and make the attempt number a human sees a lie.
        """
        from pydantic import ValidationError

        from agent.backends._progress import _clip

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
            f"{self._why_empty(envelope)}"
        )

    def _why_empty(self, envelope: dict) -> str:
        return ("The turn reported success but carried no structured output. "
                "The usual cause is a tool call it was not permitted to make.")

    def _run(self, argv: list[str], repo_path: str):
        """Spawn, stream, supervise. The port of codex.py::_run_turn."""
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(SCRUBBED_PREFIXES)}

        try:
            proc = subprocess.Popen(
                argv, cwd=repo_path or None, env=env, text=True, bufsize=1,
                stdin=subprocess.DEVNULL,      # measured note 1
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
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
        streamed = [0]
        live = _LiveText()
        envelope: list[dict | None] = [None]
        errors: list[BaseException] = []

        # stderr gets its OWN thread. This is not tidiness: a child that writes
        # more than a pipe buffer (~64 KB) to stderr blocks on the write, stops
        # producing stdout, and looks exactly like a wedged turn. codex.py has
        # no analogue of this hazard, so it does not occur to you when porting.
        tail: deque[str] = deque(maxlen=200)

        def drain_stderr():
            for line in proc.stderr:
                tail.append(line)

        def read_stdout():
            try:
                for line in proc.stdout:
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
                    kind = str(entry.get("kind", "?"))
                    if entry.get("phase") != "started":
                        kinds[kind] = kinds.get(kind, 0) + 1

                    from agent.backends._progress import headline
                    line_out = headline(entry)
                    if line_out and line_out != last_seen[0]:
                        last_seen[0] = line_out.strip()
                        print(line_out, flush=True)
                        last_line[0] = time.monotonic()
            except BaseException as exc:  # noqa: BLE001 - re-raised on the caller
                errors.append(exc)

        stderr_thread = threading.Thread(target=drain_stderr, daemon=True,
                                         name=f"{self.name}-stderr")
        worker = threading.Thread(target=read_stdout, daemon=True,
                                  name=f"{self.name}-stdout")
        stderr_thread.start()
        worker.start()

        while worker.is_alive():
            worker.join(timeout=poll_seconds(self.timeout, self.max_seconds,
                                             self.cancel))
            now = time.monotonic()
            if not worker.is_alive():
                break

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
                    self.label, idle, elapsed, len(recorded), streamed[0], live))
            if elapsed >= self.max_seconds:
                self._abandon(proc, worker)
                raise BackendTimeout(runaway_message(
                    self.label, elapsed, len(recorded)))
            if streamed[0] >= self.max_output_tokens:
                self._abandon(proc, worker)
                raise BackendTimeout(flood_message(
                    self.label, streamed[0], elapsed))

            if self.on_progress is not None and now - last_flush[0] >= 5.0:
                last_flush[0] = now
                try:
                    self.on_progress({
                        "events": list(recorded),
                        "elapsed": elapsed,
                        "idle": idle,
                        "counts": dict(kinds),
                        "last": live.tail() or last_seen[0],
                        "streamed": streamed[0],
                        "live": live.snapshot(),
                    })
                except Exception:  # noqa: BLE001 - reporting must not break the turn
                    pass

            if now - last_line[0] >= 30.0:
                from agent.backends._progress import headline  # noqa: F401
                from agent.backends.codex import _heartbeat
                print(_heartbeat(elapsed, idle, len(recorded), kinds,
                                 live.tail() or last_seen[0], streamed[0]),
                      flush=True)
                last_line[0] = now

        proc.wait(timeout=30)
        stderr_thread.join(timeout=5)
        if errors:
            raise errors[0]

        if envelope[0] is None and proc.returncode not in (0, None):
            raise BackendError(
                f"{self.executable} exited {proc.returncode}.\n\n"
                f"{''.join(tail)[-STDERR_TAIL:].strip()}"
            )
        return recorded, envelope[0], streamed[0], live

    def _abandon(self, proc, worker) -> None:
        """Stop a turn we have given up on, and make sure the child is gone."""
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        worker.join(timeout=30.0)

    def close(self) -> None:
        return None


def usage_from_keys(source: dict[str, Any], **mapping) -> Usage:
    """Build a Usage by naming each provider's spelling of each field."""
    values = {ours: int(source.get(theirs) or 0)
              for ours, theirs in mapping.items() if theirs}
    return Usage(**values)
