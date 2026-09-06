"""
WHAT:  A backend that returns canned answers instead of calling a model.
WHY:   So you can drive the whole graph -- commands, routing, interrupts,
       resume, the transcript reducer -- for free and instantly, while you
       are learning or while you are changing the graph's shape.
CONCEPT: A test double for the AgentBackend protocol.  Note that it does not
       import base.py at all: satisfying a Protocol means having the right
       attributes, not inheriting from anything.

Use it with:   python main.py run /path/to/repo --backend fake
"""

from __future__ import annotations

import itertools

from agent.backends.base import Access, StructuredRun, Usage, OutputT


class FakeBackend:
    """Fills every field of the requested schema with obvious placeholder text."""

    name = "fake"
    supports_repo_access = False
    # Claims FULL so it can stand in for any role, including the executor.
    # It never actually touches a file -- it never runs anything.
    max_access = Access.FULL

    def __init__(self, *, model: str | None = None, **options):
        self.model = model
        self.counter = itertools.count(1)
        self.orchestrator_turns = 0

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
        print(f"[fake] pretending to run {output_model.__name__} (turn {turn})")

        fields = _placeholder_fields(output_model, turn)

        # Special case so a fake run actually TERMINATES. A node with an
        # "action" enum is a router; left to the first choice it would pick
        # "execute" forever and bounce until the recursion limit. Detected by
        # FIELD NAME, not class name -- output models are built at runtime from
        # nodes.json and are named after whatever the folder called the node.
        if "action" in fields and "finish" in _choices(output_model, "action"):
            self.orchestrator_turns += 1
            fields["action"] = "execute" if self.orchestrator_turns == 1 else "finish"
            if "final_summary" in fields:
                fields["final_summary"] = "[fake] pretend work finished"

        data = output_model.model_validate(fields)

        return StructuredRun(
            data=data,
            thread_id=thread_id or f"fake-thread-{turn}",
            is_new_thread=thread_id is None,
            usage=Usage(input_tokens=100 * turn, cached_input_tokens=40 * turn,
                        output_tokens=10, context_window=200_000),
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
    import typing

    values: dict[str, object] = {}
    for name, field in output_model.model_fields.items():
        annotation = field.annotation
        origin = typing.get_origin(annotation)

        if origin is typing.Literal:
            values[name] = typing.get_args(annotation)[0]
        elif origin in (list, set, tuple):
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
    import typing

    field = output_model.model_fields.get(field_name)
    if field is None:
        return ()
    if typing.get_origin(field.annotation) is typing.Literal:
        return typing.get_args(field.annotation)
    return ()
