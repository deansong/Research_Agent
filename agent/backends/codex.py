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

from agent.backends._progress import describe
from agent.backends._schema import strict_json_schema
from agent.backends.base import (
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
        timeout: float = 600.0,
        credit_wait_attempts: int = 20,
        credit_wait_seconds: float = 60.0,
        busy_attempts: int = 4,
    ):
        self.client = client
        self.model = model
        self.credit_wait_attempts = int(credit_wait_attempts)
        """How many times to wait for credits to appear before giving up.
        20 x 60s is twenty minutes, which is enough time to notice, top up and
        have the run carry on by itself. Ctrl-C during a wait stops it."""

        self.credit_wait_seconds = float(credit_wait_seconds)
        self.busy_attempts = int(busy_attempts)
        self.timeout = float(timeout)
        """Seconds before a single turn is abandoned.

        There was no timeout at all before, and it showed: a designer call ran
        for twenty minutes with no output and no way to tell a slow request
        from a wedged one. The CLI just sat there."""

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
        result = self._run_with_recovery(thread, prompt, strict_json_schema(output_model))

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
        last_output = [time.monotonic()]

        def report(event) -> None:
            line = describe(event)
            if line:
                print(line, flush=True)
                last_output[0] = time.monotonic()

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
        while worker.is_alive():
            worker.join(timeout=5.0)
            now = time.monotonic()
            if not worker.is_alive():
                break
            if now - started >= self.timeout:
                handle.interrupt()
                worker.join(timeout=30.0)
                raise BackendTimeout(
                    f"Codex did not finish within {self.timeout:.0f}s.\n\n"
                    f"This is usually the task being big rather than anything being "
                    f"broken -- a node asked to implement a lot in one turn can "
                    f"legitimately run for a long time.\n\n"
                    f"To give it longer, put this in <repo>/.agent/config.json:\n"
                    f'    {{"roles": {{"<role>": {{"options": {{"timeout": 3600}}}}}}}}\n'
                    f"and use the role name this node declares as its `backend`.\n\n"
                    f"If it times out even then, the node is probably being asked to do "
                    f"too much at once. Split the work across more nodes, or narrow the "
                    f"brief."
                )
            # Only speak up if the provider itself has said nothing for a while.
            if now - last_output[0] >= 30.0:
                print(f"    ... still working ({now - started:.0f}s)", flush=True)
                last_output[0] = now

        if "error" in outcome:
            raise outcome["error"]  # type: ignore[misc]
        return outcome["result"]


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
