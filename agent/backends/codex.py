"""
WHAT:  The Codex backend -- the one fully implemented provider.
WHY:   Wraps the openai-codex SDK behind the neutral AgentBackend contract.
CONCEPT: An adapter.  Read this one first if you want to add a provider; then
       read antigravity.py, which is the same shape with the bodies removed.
"""

from __future__ import annotations

import contextlib
import threading
import time

from openai_codex import Codex, Sandbox

from agent.backends._progress import (_LiveText, flood_message, headline,
                                      poll_seconds, record,
                                      runaway_message, silent_message)
from agent.backends._schema import strict_json_schema
from agent.backends.base import (
    BackendCancelled,
    Access,
    BackendBusy,
    BackendError,
    BackendOutOfCredits,
    BackendTimeout,
    OutputT,
    StructuredRun,
    Usage,
)

# Out-of-credits and overload both arrive as a plain RuntimeError carrying the
# provider's message -- the SDK's own is_retryable_error() only recognises
# ServerBusyError, which these are not. So we read the message.
_OUT_OF_CREDITS = ("out of credits", "insufficient_quota", "billing", "add credits")
_BUSY = ("server busy", "overloaded", "rate limit", "429", "try again later")


def classify(exc: BaseException) -> BaseException:
    """Turn a provider error into one of our typed errors where we can.

    Message sniffing is unlovely, but the alternative is treating a topped-up
    account and a genuinely broken request identically -- and the difference
    matters, because one of them is fixed by waiting thirty seconds.
    """
    from openai_codex import is_retryable_error

    if is_retryable_error(exc):
        return BackendBusy(str(exc))

    text = str(exc).lower()
    if any(marker in text for marker in _OUT_OF_CREDITS):
        return BackendOutOfCredits(str(exc))
    if any(marker in text for marker in _BUSY):
        return BackendBusy(str(exc))
    return exc

# --------------------------------------------------------------------------
# MEASURED: developer_instructions has an effective size limit
# --------------------------------------------------------------------------
# Above roughly 6 KB, a Codex turn stops completing. Not an error, not a slow
# reply -- the notification stream simply never delivers a turn.completed and
# the call hangs until something else kills it.
#
# Measured on this stack, same task and same output schema each time:
#
#      3,998 chars  ->   32s  OK
#      5,918 chars  ->  149s  OK
#     10,468 chars  ->  STUCK (>240s)
#     13,806 chars  ->  STUCK (>240s)
#     13,940 chars  ->  STUCK (>240s)   <- plain prose, no JSON at all
#
# The last row is the important one: it is SIZE, not content. Ten kilobytes of
# harmless filler wedges the call exactly as thoroughly as a JSON example did.
#
# So: keep personas short, and put bulky reference material in the per-turn
# prompt instead. We warn rather than raise, because the real limit is the
# provider's and may move -- but a warning you can see beats a hang you cannot.
SAFE_INSTRUCTIONS_CHARS = 6000

# Our provider-neutral Access maps onto Codex's own sandbox levels.
# NONE has no Codex equivalent (Codex always runs in a working directory), so
# it is clamped to read_only -- the closest thing to "cannot change anything".
_SANDBOX: dict[Access, Sandbox] = {
    Access.NONE: Sandbox.read_only,
    Access.READ_ONLY: Sandbox.read_only,
    Access.WRITE: Sandbox.workspace_write,
    Access.FULL: Sandbox.full_access,
}


class CodexBackend:
    """Runs model turns through a Codex thread."""

    name = "codex"
    supports_repo_access = True
    max_access = Access.FULL

    def __init__(
        self,
        client: Codex,
        model: str | None = None,
        timeout: float = 300.0,
        max_seconds: float = 7200.0,
        max_output_tokens: int = 60000,
        credit_wait_attempts: int = 20,
        credit_wait_seconds: float = 60.0,
        busy_attempts: int = 4,
    ):
        self.client = client
        self.model = model

        self.on_progress = None
        """Optional callback, invoked while a long turn is still running.

        Signature: `on_progress(events, elapsed, idle, kinds, last)`.

        Set per call by whoever is about to run a turn -- the node template
        does it, because it knows which node and session this is and the
        backend does not. Safe because these graphs never run two nodes at
        once; the validator refuses fan-out precisely so that this kind of
        assumption holds.

        It exists so the detail behind a "still working" line is available
        BEFORE the turn ends. Without it the record was written only on
        completion, so during a twenty-minute turn -- exactly when you want to
        look -- there was nothing to look at.
        """

        self.cancel = None
        """An optional threading.Event meaning "stop, I have changed my mind".

        Set by whoever owns this backend -- the web runner arms the backends it
        built when you press Stop. Checked in the same loop as the timeout,
        because it needs exactly the same machinery: the SDK gives us a
        TurnHandle with interrupt(), and that is the only way to stop a turn
        that is already running.

        Without it, Stop could only take effect between nodes, so pressing it
        during a ten-minute executor turn would appear to do nothing at all --
        which is not a stop button, it is a suggestion.
        """
        self.credit_wait_attempts = int(credit_wait_attempts)
        """How many times to wait for credits to appear before giving up.
        20 x 60s is twenty minutes, which is enough time to notice, top up and
        have the run carry on by itself. Ctrl-C during a wait stops it."""

        self.credit_wait_seconds = float(credit_wait_seconds)
        self.busy_attempts = int(busy_attempts)
        self.timeout = float(timeout)
        """Seconds of SILENCE before a turn is abandoned -- not total runtime.

        This measures the wrong thing if you measure it the other way, and we
        found that out the hard way. It used to be a wall-clock deadline, and a
        node doing genuinely long work was killed at 600s while Codex was still
        streaming events; the heartbeat gaps in the log proved it (30s, 30s,
        65s -- that 65 means an event arrived and reset the clock). Killing a
        productive turn is the worst possible outcome, because a node is atomic:
        ten minutes of real work went in the bin.

        So the question is not "has this taken long?" but "is anything still
        happening?". Any event from the provider counts -- including the many
        that _progress.describe() deliberately does not print, because a turn
        that is thinking hard is still a turn that is working.

        300s of complete silence is a lot. A turn that quiet really is wedged."""

        self.max_seconds = float(max_seconds)
        """An absolute cap, as a backstop against a genuine runaway.

        Idle detection alone would let a provider that emits a keepalive every
        thirty seconds run for ever. Two hours is far beyond any legitimate
        single turn, so hitting this means something is actually wrong."""

        self.max_output_tokens = int(max_output_tokens)
        """A cap on how much ANSWER one turn may type.

        The failure this catches is invisible to both clocks above: a model
        that emits a complete document, decides it was a draft, and emits
        another. It is never idle and it can do that for an hour, well inside
        `max_seconds`, with nothing to show at the end.

        Grounded in a measurement rather than a guess: the largest design this
        project has produced is 49 KB, about 12,000 tokens. Sixty thousand is
        four times that -- comfortably past anything legitimate, and reached in
        roughly an hour at the streaming rate actually observed."""

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
        thread, is_new = self._get_thread(
            thread_id=thread_id,
            repo_path=repo_path,
            access=access,
            developer_instructions=developer_instructions,
        )

        # output_schema is what makes the reply parseable: Codex constrains
        # the model to emit JSON matching our Pydantic class's JSON Schema.
        #
        # strict_json_schema() rather than model_json_schema(): Codex rejects a
        # schema whose `required` omits any property, and Pydantic omits every
        # field that has a default. Measured against a live call --
        #   invalid_json_schema: ... Missing 'question'.
        result, events = self._run_with_recovery(
            thread, prompt, strict_json_schema(output_model)
        )

        if not result.final_response:
            raise BackendError("Codex returned no final response.")

        # model_validate_json is the second half of the guarantee: even with a
        # schema, we never hand unvalidated data to a node.
        data = output_model.model_validate_json(result.final_response)

        return StructuredRun(
            data=data,
            thread_id=thread.id,
            is_new_thread=is_new,
            usage=_to_usage(result.usage),
            events=events,
        )

    def close(self) -> None:
        # The Codex client is opened as a context manager in cli.py, which
        # owns closing it. Nothing to do here.
        return None

    def _run_with_recovery(self, thread, prompt: str, output_schema: dict):
        """Run a turn, waiting through the failures that waiting actually fixes.

        Two are worth surviving rather than crashing on:

          out of credits  someone can top up, and then the very same call
                          works. Dying with a traceback throws away the run's
                          place in the graph for a problem measured in minutes.
          provider busy   transient by definition.

        Everything else is raised straight away: retrying a malformed request
        just spends the same money twice.
        """
        credit_waits = 0
        busy_tries = 0

        while True:
            try:
                return self._run_turn(thread, prompt, output_schema)
            except BaseException as exc:  # noqa: BLE001 - re-raised below
                error = classify(exc)

                if isinstance(error, BackendOutOfCredits):
                    credit_waits += 1
                    if credit_waits > self.credit_wait_attempts:
                        raise BackendOutOfCredits(
                            f"Still out of credits after "
                            f"{self.credit_wait_attempts} checks. Stopping.\n"
                            f"Your progress is saved -- rerun with the same "
                            f"--session to carry on from here."
                        ) from None
                    self._wait("Out of credits", credit_waits,
                               self.credit_wait_attempts, self.credit_wait_seconds,
                               "Add credits and this will continue on its own.")
                    continue

                if isinstance(error, BackendBusy):
                    busy_tries += 1
                    if busy_tries > self.busy_attempts:
                        raise error from None
                    # Short exponential backoff: 5s, 10s, 20s, 40s.
                    self._wait("Provider busy", busy_tries, self.busy_attempts,
                               5.0 * (2 ** (busy_tries - 1)), "")
                    continue

                raise error from None

    def _wait(self, reason: str, attempt: int, limit: int, seconds: float, advice: str):
        """Sleep in short slices so ctrl-C stays responsive during a long wait."""
        print(f"\n    {reason}. Waiting {seconds:.0f}s, then retrying "
              f"({attempt}/{limit}).", flush=True)
        if advice:
            print(f"    {advice}", flush=True)
        print("    Ctrl-C to stop -- your progress is saved.", flush=True)

        remaining = seconds
        while remaining > 0:
            slice_ = min(5.0, remaining)
            time.sleep(slice_)
            remaining -= slice_

    def _run_turn(self, thread, prompt: str, output_schema: dict):
        """One turn, with a deadline and live progress.

        The SDK has no timeout parameter, but Thread.run() is just
        turn() + drain the notification stream -- and the TurnHandle that
        turn() returns has interrupt(). So we start the turn ourselves, drain
        it on a worker thread, and interrupt from here if the deadline passes.

        Draining it ourselves has a second payoff. Thread.run() collects the
        stream and hands back only the final answer, so everything Codex says
        about what it is DOING is discarded. We tee the stream through
        _progress.describe() on the way to the same collector, which turns a
        silent five-minute wait into a log of the commands it ran and the
        files it touched. See agent/backends/_progress.py.

        The elapsed-time heartbeat is kept, but only as a fallback for when
        the provider has genuinely gone quiet -- otherwise it just interleaves
        noise with the real output.
        """
        handle = thread.turn(prompt, output_schema=output_schema)
        outcome: dict[str, object] = {}
        started_at = time.monotonic()

        # Two clocks, and the distinction is the whole point. `last_event` is
        # touched by EVERY event, so it answers "is the provider still alive?".
        # `last_line` is touched only when we print something, so it answers
        # "does the human need reassurance?". Conflating them is what killed a
        # working turn: the old code only reset on printable events, so a node
        # that was reasoning steadily -- emitting events describe() ignores --
        # looked identical to one that had hung.
        last_event = [time.monotonic()]
        last_line = [time.monotonic()]
        events = [0]

        # The full record, kept as well as printed. See _progress.record for
        # why these are two different functions rather than one with a flag.
        recorded: list[dict] = []
        last_printed = [""]
        last_flush = [0.0]
        kinds: dict[str, int] = {}
        last_seen = [""]

        # Tokens of the answer being typed, counted but not kept. `events` is
        # therefore things the model DID, which is what everyone reading the
        # number assumed it already meant. See _progress.py for the three ways
        # conflating the two went wrong.
        streamed = [0]

        # The TEXT of those tokens, as a bounded tail per stream. Counting
        # them was half the job: a turn that streamed 16,608 tokens of a
        # finished design still reported "last: * user message", because the
        # only thing with a headline was our own request.
        live = _LiveText()

        def report(event) -> None:
            last_event[0] = time.monotonic()

            entry = record(event)
            if entry is None:
                return
            if entry.get("transient"):
                streamed[0] += 1
                live.add(entry.get("stream", "other"), entry.get("text", ""))
                return

            events[0] += 1
            entry["at"] = round(time.monotonic() - started_at, 2)
            recorded.append(entry)

            kind = str(entry.get("kind", "?"))
            if entry.get("phase") != "started":
                kinds[kind] = kinds.get(kind, 0) + 1

            line = headline(entry)
            if not line:
                return

            # Remembered even when the print is suppressed, so a quiet stretch
            # can still say what it is quiet ABOUT.
            last_seen[0] = line.strip()

            # Identical consecutive lines are the price of following updates as
            # well as completions: a reasoning item extended three times can
            # produce the same tail three times. Print the change, not the tick.
            if line == last_printed[0]:
                return
            last_printed[0] = line
            print(line, flush=True)
            last_line[0] = time.monotonic()

        def drain() -> None:
            stream = handle.stream()
            try:
                outcome["result"] = _collect(_observe(stream, report), handle.id)
            except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
                outcome["error"] = exc
            finally:
                stream.close()

        worker = threading.Thread(target=drain, daemon=True, name="codex-turn")
        worker.start()

        started = time.monotonic()
        poll = self._poll_seconds()
        while worker.is_alive():
            worker.join(timeout=poll)
            now = time.monotonic()
            if not worker.is_alive():
                break

            idle = now - last_event[0]
            elapsed = now - started

            # Checked FIRST: if you have asked to stop, no other verdict about
            # this turn is interesting.
            if self.cancel is not None and self.cancel.is_set():
                self._abandon(handle, worker)
                raise BackendCancelled(
                    f"Stopped by request after {elapsed:.0f}s "
                    f"({events[0]} events). Everything the graph had already "
                    f"finished is checkpointed; this node's turn is discarded."
                )

            if idle >= self.timeout:
                self._abandon(handle, worker)
                raise BackendTimeout(self._silent_message(
                    idle, elapsed, events[0], streamed[0], live))

            if elapsed >= self.max_seconds:
                self._abandon(handle, worker)
                raise BackendTimeout(self._runaway_message(elapsed, events[0]))

            if streamed[0] >= self.max_output_tokens:
                self._abandon(handle, worker)
                raise BackendTimeout(self._flood_message(streamed[0], elapsed))

            # Only speak up if the provider itself has said nothing for a while,
            # and say what we are actually waiting on. "still working (400s)" is
            # ambiguous between thinking and hung; the idle figure is not.
            # Flushed more often than the heartbeat prints: the point is that
            # the detail is THERE when somebody expands the line, and they
            # will expand it at a moment of their choosing rather than ours.
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
                except Exception:  # noqa: BLE001
                    # Reporting progress must never break the turn it reports.
                    pass

            if now - last_line[0] >= 30.0:
                # live.tail() first: "message: {\"task_brief\": \"Compare
                # hotel-employee skill..." beats the last line worth printing,
                # which during a long answer is whatever came before it.
                print(_heartbeat(elapsed, idle, events[0], kinds,
                                 live.tail() or last_seen[0], streamed[0]),
                      flush=True)
                last_line[0] = now

        if "error" in outcome:
            raise outcome["error"]  # type: ignore[misc]
        return outcome["result"], recorded


    def _poll_seconds(self) -> float:
        return poll_seconds(self.timeout, self.max_seconds, self.cancel)

    def _abandon(self, handle, worker) -> None:
        """Stop a turn we have given up on, and wait for its thread to notice."""
        handle.interrupt()
        worker.join(timeout=30.0)

    # ---- diagnostics ----------------------------------------------------
    # The words live in _progress.py now, because every one of them is true of
    # any provider that streams a turn -- only the name at the front was ours.
    # These stay as methods so the tests that assert on the text still call
    # them where they always did, and so a subclass could reword one.

    def _silent_message(self, idle: float, elapsed: float, events: int,
                        streamed: int = 0, live=None) -> str:
        return silent_message("Codex", idle, elapsed, events, streamed, live)

    def _flood_message(self, streamed: int, elapsed: float) -> str:
        return flood_message("Codex", streamed, elapsed)

    def _runaway_message(self, elapsed: float, events: int) -> str:
        return runaway_message("Codex", elapsed, events)

    def _get_thread(self, *, thread_id, repo_path, access, developer_instructions):
        if len(developer_instructions) > SAFE_INSTRUCTIONS_CHARS:
            print(
                f"    ! warning: these developer instructions are "
                f"{len(developer_instructions):,} characters. Past about "
                f"{SAFE_INSTRUCTIONS_CHARS:,}, Codex turns have been measured to hang "
                f"instead of replying.\n"
                f"      Move bulky reference material into the prompt instead. "
                f"See SAFE_INSTRUCTIONS_CHARS in agent/backends/codex.py."
            )

        common: dict[str, object] = {
            "cwd": repo_path,
            "sandbox": _SANDBOX[access],
            # BUG FIX: developer_instructions used to be passed only when
            # STARTING a thread, so editing agent/prompts.py had no effect on
            # any session that already existed -- a genuinely confusing thing
            # to debug. thread_resume() accepts them too, so send them every
            # time and the persona always matches the code you are reading.
            "developer_instructions": developer_instructions,
        }
        if self.model:
            common["model"] = self.model

        if thread_id:
            return self.client.thread_resume(thread_id, **common), False

        return self.client.thread_start(**common), True


def _observe(stream, report):
    """Pass every event through `report` on its way to the collector.

    A generator rather than a rewritten collector: the SDK still assembles the
    turn exactly as it always did, and we only watch what goes past. If a
    future SDK changes how turns are assembled, this keeps working.
    """
    for event in stream:
        try:
            report(event)
        except Exception:  # noqa: BLE001
            # Progress reporting must never break the turn it is describing.
            pass
        yield event


def _collect(stream, turn_id: str):
    """Drain a turn's notification stream into a TurnResult.

    This is what Thread.run() does internally after starting a turn. We do it
    ourselves because run() keeps its TurnHandle private and we need that
    handle to interrupt() on a timeout.

    Delegating to the SDK's own helper rather than reimplementing twenty lines
    of notification matching: it is private, so the import is local and
    explicit, and if a future SDK moves it the failure is an immediate
    ImportError naming this line rather than a subtle mismatch in how turns
    are assembled.
    """
    from openai_codex._run import _collect_turn_result

    return _collect_turn_result(stream, turn_id=turn_id)


def _to_usage(usage) -> Usage | None:
    """Convert the SDK's usage object into our neutral Usage.

    Doing the conversion HERE, rather than in telemetry.py, is what lets
    telemetry stay provider-agnostic: it only ever sees a Usage.
    """
    if usage is None:
        return None

    last = getattr(usage, "last", None)
    if last is None:
        return None

    return Usage(
        input_tokens=getattr(last, "input_tokens", 0) or 0,
        cached_input_tokens=getattr(last, "cached_input_tokens", 0) or 0,
        output_tokens=getattr(last, "output_tokens", 0) or 0,
        reasoning_tokens=getattr(last, "reasoning_output_tokens", 0) or 0,
        context_window=getattr(usage, "model_context_window", None),
    )


# ---------------------------------------------------------------------------
# Client lifecycle. These live here rather than in cli.py so that
# `from openai_codex import ...` appears in exactly ONE file in the project --
# which is what "agent/backends/ is the provider boundary" actually means.
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def open_client():
    """Open a Codex client for the life of a session.

    One client is shared by every role configured to use Codex; see
    build_backends() in agent/backends/__init__.py, which caches backend
    instances by (provider, model, options).
    """
    with Codex() as client:
        yield client


def login_chatgpt() -> None:
    """Interactive ChatGPT sign-in for Codex."""
    with Codex() as codex:
        login = codex.login_chatgpt()
        print("\nOpen this URL in your browser:")
        print(login.auth_url)
        print()
        login.wait()
        print("ChatGPT/Codex login successful.")


def _heartbeat(elapsed: float, idle: float, total: int,
               kinds: dict[str, int], last: str, streamed: int = 0) -> str:
    """What to say during a quiet stretch of a long turn.

    "still working (55s elapsed, 137 events, last one 11s ago)" is three
    numbers and no information: it says the turn is alive, which you could
    already see, and nothing about what it is doing, which you could not.
    Every one of those 137 events had a kind, and most had text.

    So: a breakdown by kind, and the last line worth printing -- usually a
    reasoning summary, which is the model's own account of what it is busy
    with. That turns a progress bar back into a progress report.
    """
    parts = [f"{elapsed:.0f}s"]
    if kinds:
        parts.append(", ".join(
            f"{count} {kind}" for kind, count in
            sorted(kinds.items(), key=lambda kv: -kv[1])[:4]
        ))
    elif total:
        parts.append(f"{total} events")
    # Said separately from the work, because it is not work: a big design is
    # ~12,000 tokens and streams at about fifteen a second, so "writing 12.0k"
    # is the difference between a turn that is stuck and one that is typing --
    # and it was previously reported as 12,000 "events".
    if streamed:
        parts.append(f"writing {streamed / 1000:.1f}k")
    parts.append(f"quiet {idle:.0f}s")

    line = f"    ... working: {' · '.join(parts)}"
    if last:
        # Trimmed hard: this repeats every thirty seconds, and a wrapped
        # paragraph in the middle of a log is worse than a short one.
        trimmed = last[:88] + ("\u2026" if len(last) > 88 else "")
        line += f"\n      last: {trimmed}"
    return line
