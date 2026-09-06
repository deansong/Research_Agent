"""
WHAT:  merge_section() -- the hand-rolled stand-in for a reducer.
WHY:   Shared by the bootstrap graph, which still groups state into
       sub-TypedDicts. Kept in its own module so agent/work/state.py can point
       at it as the counter-example.
CONCEPT: Reducers, and when NOT to use one.

--------------------------------------------------------------------------
THE RULE, stated once, in the one file both sides can point at
--------------------------------------------------------------------------
    Hand-merge when your code KNOWS the keys.
    Use a reducer when the keys come from DATA.

agent/bootstrap/state.py knows its keys: they are spelled in the source, and a
node updating one field of `discussion` can safely read the section and return
the whole thing. merge_section() is right there.

agent/work/state.py does NOT know its keys -- node names come from JSON. A node
returning the whole `threads` dict would erase entries it has never heard of.
Reducers (operator.or_) are right there.

Both files are correct. The difference is only where the keys come from.
"""

from __future__ import annotations

from typing import Any


def merge_section(section: dict[str, Any] | None, **changes: Any) -> dict[str, Any]:
    """Return a COPY of one state section with `changes` applied.

    Copying rather than mutating matters: the state object a node receives may
    be shared, and mutating it in place would corrupt the checkpoint.
    """
    merged = dict(section or {})
    merged.update(changes)
    return merged
