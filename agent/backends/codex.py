"""
WHAT:  The Codex backend -- the one fully implemented provider.
WHY:   Wraps the openai-codex SDK behind the neutral AgentBackend contract.
CONCEPT: An adapter.  Read this one first if you want to add a provider; then
       read antigravity.py, which is the same shape with the bodies removed.
"""

from __future__ import annotations

from openai_codex import Codex, Sandbox

from agent.backends.base import Access, BackendError, StructuredRun, Usage, OutputT

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

    def __init__(self, client: Codex, model: str | None = None):
        self.client = client
        self.model = model

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
        result = thread.run(prompt, output_schema=output_model.model_json_schema())

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

    def _get_thread(self, *, thread_id, repo_path, access, developer_instructions):
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
