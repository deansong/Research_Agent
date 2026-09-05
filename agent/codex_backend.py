from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

from openai_codex import Codex, Sandbox
from pydantic import BaseModel


OutputT = TypeVar("OutputT", bound=BaseModel)


@dataclass(frozen=True)
class StructuredRun(Generic[OutputT]):
    data: OutputT
    thread_id: str
    is_new_thread: bool
    usage: object | None


class CodexBackend:
    """Small provider adapter. LangGraph nodes never call the SDK directly."""

    def __init__(self, client: Codex, model: str | None = None):
        self.client = client
        self.model = model

    def run_structured(
        self,
        *,
        thread_id: str | None,
        repo_path: str,
        sandbox: Sandbox,
        developer_instructions: str,
        prompt: str,
        output_model: type[OutputT],
    ) -> StructuredRun[OutputT]:
        thread, is_new = self._get_thread(
            thread_id=thread_id,
            repo_path=repo_path,
            sandbox=sandbox,
            developer_instructions=developer_instructions,
        )

        result = thread.run(
            prompt,
            output_schema=output_model.model_json_schema(),
        )

        if not result.final_response:
            raise RuntimeError("Codex returned no final response.")

        data = output_model.model_validate_json(result.final_response)

        return StructuredRun(
            data=data,
            thread_id=thread.id,
            is_new_thread=is_new,
            usage=result.usage,
        )

    def _get_thread(
        self,
        *,
        thread_id: str | None,
        repo_path: str,
        sandbox: Sandbox,
        developer_instructions: str,
    ):
        common: dict[str, object] = {
            "cwd": repo_path,
            "sandbox": sandbox,
        }
        if self.model:
            common["model"] = self.model

        if thread_id:
            return self.client.thread_resume(thread_id, **common), False

        return (
            self.client.thread_start(
                developer_instructions=developer_instructions,
                **common,
            ),
            True,
        )
