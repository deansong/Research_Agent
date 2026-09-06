"""
WHAT:  Builds a real Pydantic model at runtime from a node's declared output
       fields.
WHY:   The backend contract takes `output_model: type[BaseModel]` and turns its
       JSON Schema into a structured-output constraint. A node defined in JSON
       still needs one of those, so we synthesise it.
CONCEPT: Not LangGraph. This is the bridge between "fields described in JSON"
       and "the same guarantee a hand-written Pydantic class gives you".

The synthesised model uses extra="forbid", exactly like the hand-written ones
in agent/schemas.py, so it produces "additionalProperties": false and works
with strict structured-output modes.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from agent.agentfolder.schema import FieldSpec


def build_output_model(node_name: str, fields: list[FieldSpec]) -> type[BaseModel]:
    """Turn a list of FieldSpec into a Pydantic model class.

    Every field is given a default unless marked required, so a model that
    omits an optional field still validates -- the same forgiveness the
    hand-written schemas had (`question: str = ""`).
    """
    definitions: dict[str, tuple[object, object]] = {}

    for spec in fields:
        annotation = _annotation(spec)
        default = ... if spec.required else _default(spec)
        definitions[spec.name] = (
            annotation,
            Field(default, description=spec.description or None),
        )

    return create_model(
        f"{node_name.title().replace('_', '')}Output",
        __config__=ConfigDict(extra="forbid"),
        **definitions,
    )


def _annotation(spec: FieldSpec):
    if spec.type == "enum":
        # Literal["a", "b"] is what makes the provider pick one of the choices
        # rather than inventing a fifth action.
        return Literal[tuple(spec.choices)]  # type: ignore[misc]
    return {
        "string": str,
        "string_list": list[str],
        "integer": int,
        "boolean": bool,
    }[spec.type]


def _default(spec: FieldSpec):
    if spec.type == "enum":
        return spec.choices[0]
    return {
        "string": "",
        "string_list": [],
        "integer": 0,
        "boolean": False,
    }[spec.type]
