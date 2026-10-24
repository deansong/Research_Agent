"""
WHAT:  Converts a Pydantic model's JSON Schema into the strict dialect that
       structured-output modes demand.
WHY:   Found the hard way against a live Codex call:

           invalid_json_schema: 'required' is required to be supplied and to
           be an array including every key in properties. Missing 'question'.

       Pydantic omits a field from `required` when it has a default, which is
       exactly how our schemas are written (`question: str = ""`). Strict mode
       does not accept that.
CONCEPT: Not LangGraph. A provider-boundary detail, so it lives in backends/
       and every backend that wants strict output can share it.

The two rules, at EVERY level including nested models and array items:
    1. `required` must list every key in `properties`.
    2. `additionalProperties` must be false.

Marking an optional field "required" in the wire schema is harmless: the model
is simply obliged to send something, and Pydantic still applies its own
defaults and validation on the way back. What we lose is the model's ability
to omit a field it has nothing to say about -- so it sends "" instead, which
is what the defaults would have produced anyway.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Return `model`'s JSON Schema, tightened for strict structured output."""
    return _tighten(model.model_json_schema())


def _tighten(node: Any) -> Any:
    if isinstance(node, list):
        return [_tighten(item) for item in node]
    if not isinstance(node, dict):
        return node

    out = {key: _tighten(value) for key, value in node.items()}

    # $defs holds sibling schemas; they are tightened by the walk above.
    if "properties" in out and isinstance(out["properties"], dict):
        out["required"] = list(out["properties"].keys())
        out["additionalProperties"] = False
        out.setdefault("type", "object")

    return out
