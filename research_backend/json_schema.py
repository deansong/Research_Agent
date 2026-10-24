"""
WHAT:  A plain JSON Schema as a reply's shape -- a dict, JSON text, or a .json
       file -- wherever a Pydantic class is accepted.
WHY:   Every backend asks for a Pydantic class (it sends that class's schema
       and validates the answer with it). A schema written by hand, or kept
       in a file, should not have to be rewritten as a class first.
CONCEPT: model_for(schema) makes a class that stands for the schema: what it
       SENDS is the schema as written; what it gives BACK is the reply as a
       plain dict, checked against the schema.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from pydantic import RootModel, model_validator


def load(schema) -> dict:
    """A schema from a dict, JSON text, or the path of a .json file."""
    if isinstance(schema, dict):
        return copy.deepcopy(schema)
    if isinstance(schema, Path) or (isinstance(schema, str) and not schema.lstrip().startswith("{")):
        path = Path(schema).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"No JSON schema file at {path}.")
        schema = path.read_text(encoding="utf-8")
    try:
        loaded = json.loads(schema)
    except json.JSONDecodeError as exc:
        raise ValueError(f"That JSON schema is not valid JSON: {exc}") from None
    if not isinstance(loaded, dict):
        raise ValueError("A JSON schema is a JSON object.")
    return loaded


def model_for(schema) -> type[RootModel]:
    """A Pydantic class standing for `schema`. Its instances' `.root` is the
    reply, a plain dict."""
    raw = load(schema)
    if raw.get("type") != "object":
        raise ValueError(
            'The providers answer with a JSON object, so the schema\'s top level must be '
            '{"type": "object", ...}. To get a list, put it in a property: '
            '{"type": "object", "properties": {"items": {"type": "array", ...}}}.')
    checker = _checker(raw)

    class JsonSchemaReply(RootModel[dict[str, Any]]):
        """A reply shaped by a JSON schema."""

        @classmethod
        def model_json_schema(cls, *args, **kwargs) -> dict:
            return copy.deepcopy(raw)

        @model_validator(mode="after")
        def _matches(self):
            if checker is not None:
                problems = sorted(checker.iter_errors(self.root), key=lambda e: list(e.path))
                if problems:
                    raise ValueError("; ".join(
                        f"{'/'.join(map(str, e.path)) or 'the reply'}: {e.message}"
                        for e in problems[:5]))
            return self

    JsonSchemaReply.__name__ = raw.get("title") or "JsonSchemaReply"
    JsonSchemaReply.raw_schema = raw
    return JsonSchemaReply


def is_raw(output_model) -> bool:
    return hasattr(output_model, "raw_schema")


def _checker(raw: dict):
    """A validator for `raw`, or None when the jsonschema package is missing
    (the providers enforce the schema themselves; this checks their work)."""
    try:
        import jsonschema
    except ImportError:
        return None
    cls = jsonschema.validators.validator_for(raw)
    try:
        cls.check_schema(raw)
    except jsonschema.SchemaError as exc:
        raise ValueError(f"That is not a valid JSON schema: {exc.message}") from None
    return cls(raw)


def example(schema: dict, turn: int = 1, name: str = "value", root: dict | None = None):
    """A value that fits `schema` -- for the fake backend."""
    root = schema if root is None else root
    if "$ref" in schema:
        target = root
        for part in schema["$ref"].lstrip("#/").split("/"):
            target = target.get(part, {})
        return example(target, turn, name, root)
    if "enum" in schema:
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]
    for key in ("anyOf", "oneOf", "allOf"):
        if schema.get(key):
            return example(schema[key][0], turn, name, root)
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), "null")
    if kind == "object" or "properties" in schema:
        return {key: example(sub, turn, key, root)
                for key, sub in (schema.get("properties") or {}).items()}
    if kind == "array":
        return [example(schema.get("items") or {}, turn, name, root)
                for _ in range(max(1, schema.get("minItems", 1)))]
    if kind == "integer":
        return max(turn, schema.get("minimum", turn))
    if kind == "number":
        return float(max(turn, schema.get("minimum", turn)))
    if kind == "boolean":
        return False
    if kind == "null":
        return None
    return f"[fake {name} #{turn}]"
