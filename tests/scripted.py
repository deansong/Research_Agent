"""
A backend that answers from a script, for the bootstrap tests: one queue of
answers per output class (DiscussorOutput, PlannerOutput, ...), and a record
of every call -- the prompt, the thread, the instructions.
"""

from __future__ import annotations

import itertools

import pytest

from research_backend.base import Access, StructuredRun, Usage


class Script:
    name = "fake"
    max_access = Access.FULL

    def __init__(self):
        self.answers: dict[str, list] = {}
        self.calls: list[dict] = []
        self._threads = itertools.count(1)

    def add(self, schema: str, *answers) -> "Script":
        self.answers.setdefault(schema, []).extend(answers)
        return self

    def run_structured(self, *, thread_id, repo_path, access, developer_instructions,
                       prompt, output_model):
        name = output_model.__name__
        self.calls.append({"schema": name, "thread": thread_id, "prompt": prompt,
                           "instructions": developer_instructions, "access": access,
                           "workdir": repo_path})
        queue = self.answers.get(name) or []
        if not queue:
            raise AssertionError(f"the script has no answer left for {name}")
        answer = queue.pop(0)
        answer = answer(prompt) if callable(answer) else answer
        return StructuredRun(
            data=output_model.model_validate(answer),
            thread_id=thread_id or f"thread-{next(self._threads)}",
            is_new_thread=thread_id is None,
            usage=Usage(input_tokens=1000, output_tokens=50, context_window=200_000),
            events=[])

    def prompts(self, schema: str) -> list[str]:
        return [c["prompt"] for c in self.calls if c["schema"] == schema]


@pytest.fixture
def script(monkeypatch):
    backend = Script()
    monkeypatch.setattr("research_backend.conversation._backend", lambda *a, **k: backend)
    return backend
