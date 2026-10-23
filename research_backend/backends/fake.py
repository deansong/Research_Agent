"""
WHAT:  A provider that answers without a model: free text is an echo, a
       schema is filled with placeholder values.
WHY:   So code built on research_backend -- sessions, the agent, tests -- can
       be run for free, offline and instantly.
CONCEPT: A test double for the AgentBackend protocol. The agent has a fuller
       one (agent/backends/fake.py) that also plays its planner, designer and
       orchestrator; it builds on the helpers here.
"""

from __future__ import annotations

import itertools
import typing

from research_backend.base import Access, OutputT, StructuredRun, Usage


class FakeBackend:
    """Echoes free text; fills every field of any other schema."""

    name = "fake"
    supports_repo_access = False
    # Claims FULL so it can stand in for any role. It never touches a file.
    max_access = Access.FULL
    #: Every level any provider has, so a fake can stand in for any of them.
    efforts: tuple[str, ...] = ("none", "minimal", "low", "medium", "high", "xhigh", "max",
                                "ultra")

    def __init__(self, *, model: str | None = None, effort: str | None = None, **options):
        self.model = model
        self.effort = effort
        self.counter = itertools.count(1)
        #: Turns so far, per conversation -- so a continued conversation
        #: visibly continues.
        self.turns: dict[str, int] = {}

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
        turn = next(self.counter)
        thread = thread_id or f"fake-thread-{turn}"
        self.turns[thread] = self.turns.get(thread, 0) + 1

        fields = _placeholder_fields(output_model, turn)
        if set(output_model.model_fields) == {"text"}:
            fields = {"text": f"[fake] reply {self.turns[thread]} in this conversation, "
                              f"to: {prompt.strip()[:200]}"}
        return StructuredRun(
            data=output_model.model_validate(fields),
            thread_id=thread,
            is_new_thread=thread_id is None,
            usage=Usage(input_tokens=100 * turn, cached_input_tokens=40 * turn,
                        output_tokens=10, context_window=200_000),
            events=_fake_events(turn),
        )

    def close(self) -> None:
        return None


def _placeholder_fields(output_model, turn: int) -> dict:
    """Build a dict that satisfies `output_model`, whatever its fields are.

    EVERY field is filled, not just the required ones. That matters: a node's
    prompts read other nodes' outputs via {out.x.y}, and its thread_key may be
    "{out.planner.workstream}". If optional fields came back empty, prompts
    would render blank and every thread key would collapse to the same value --
    so a fake run would not exercise the parts most likely to be wrong.
    """
    values: dict[str, object] = {}
    for name, field in output_model.model_fields.items():
        annotation = field.annotation
        origin = typing.get_origin(annotation)

        if origin is typing.Literal:
            values[name] = typing.get_args(annotation)[0]
        elif origin in (list, set, tuple):
            (item,) = typing.get_args(annotation) or (str,)
            if isinstance(item, type) and hasattr(item, "model_fields"):
                values[name] = [_placeholder_fields(item, turn) for _ in range(2)]
            else:
                values[name] = [f"[fake {name} 1]", f"[fake {name} 2]"]
        elif annotation is bool:
            values[name] = False
        elif annotation is int:
            values[name] = turn
        elif name == "workstream":
            # A stable value, so per-workstream thread keys actually group.
            values[name] = "main"
        else:
            values[name] = f"[fake {name} #{turn}]"

    return values


def _choices(output_model, field_name: str) -> tuple:
    """The Literal choices of one field, or () if it is not an enum."""
    field = output_model.model_fields.get(field_name)
    if field is None:
        return ()
    if typing.get_origin(field.annotation) is typing.Literal:
        return typing.get_args(field.annotation)
    return ()


def _fake_events(turn: int) -> list[dict]:
    """A plausible turn's worth of provider events.

    Not decoration. The activity record -- what the provider actually ran, and
    what came back -- is the part of a system built on this that most needs
    testing, and without these it could only be exercised against a real paid
    provider.

    Shapes match research_backend/backends/_progress.py::record exactly,
    including the started/completed pairing that `Turn.counts()` has to
    collapse.
    """
    return [
        {"kind": "reasoning", "phase": "completed", "at": 0.4,
         "summary": [f"[fake] working out what turn {turn} needs",
                     "[fake] second summary line, which describe() drops"]},
        {"kind": "command", "phase": "started", "at": 0.5,
         "command": "ls -la"},
        {"kind": "command", "phase": "completed", "at": 0.9,
         "command": "ls -la", "exit_code": 0,
         "output": "total 8\ndrwxr-xr-x  2 fake fake 4096 .\n"},
        {"kind": "command", "phase": "started", "at": 1.0,
         "command": "python -m pytest -q"},
        {"kind": "command", "phase": "completed", "at": 2.2,
         "command": "python -m pytest -q", "exit_code": 1,
         "output": "1 failed, 3 passed\nE   AssertionError: [fake] a failure "
                   "you can only see in the activity record\n"},
        {"kind": "file_change", "phase": "completed", "at": 2.4,
         "changes": [{"path": "fake/module.py", "kind": "modified"}]},
        {"kind": "message", "phase": "completed", "at": 2.5,
         "text": f"[fake] finished turn {turn}", "is_final_json": False},
    ]
