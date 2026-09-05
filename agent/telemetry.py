"""
WHAT:  Turns a backend's Usage into a dict for state, and prints it.
WHY:   Token cost is the main thing you tune in an agent, so it should be
       visible after every turn rather than at the end.
CONCEPT: Not LangGraph -- but note that usage_by_role IS a state channel, so
       these dicts get checkpointed and survive a restart.

Every provider reports usage differently.  Each backend converts to the
neutral Usage dataclass (agent/backends/base.py) and this module only ever
sees that one shape.
"""

from __future__ import annotations

from typing import Any

from agent.backends.base import Usage


def usage_to_dict(usage: Usage | None) -> dict[str, Any]:
    """Flatten Usage into plain values, adding the two derived percentages.

    Plain dicts (not the dataclass) because this goes into graph state, and
    state is serialised into the checkpoint -- the fewer custom classes in
    there, the fewer surprises when loading an old session.
    """
    if usage is None:
        return {}

    inp = usage.input_tokens
    cached = usage.cached_input_tokens
    window = usage.context_window

    return {
        "last_input_tokens": inp,
        "last_cached_input_tokens": cached,
        "last_output_tokens": usage.output_tokens,
        "last_reasoning_tokens": usage.reasoning_tokens,
        # How much of the input was served from cache. High is good and cheap;
        # it is the payoff for reusing one long-lived thread per role.
        "cache_percent": round((cached / inp) * 100, 1) if inp else 0.0,
        "model_context_window": window,
        # How full the context window is. When this climbs toward 100 the
        # thread needs restarting, not a bigger prompt.
        "context_percent": round((inp / window) * 100, 1) if window else None,
        "cost_usd": usage.cost_usd,
    }


def record_usage(
    current: dict[str, dict[str, Any]],
    role: str,
    usage: Usage | None,
) -> dict[str, dict[str, Any]]:
    """Return a NEW usage map with `role` updated, and print a one-line report.

    Returns a copy rather than mutating: `current` came out of graph state and
    mutating it in place would corrupt the checkpoint.

    Note this stores the LAST turn per role, not a running total. Per-turn
    numbers are what tell you whether caching is working; the totals are
    available from the provider.
    """
    usage_map = dict(current)
    data = usage_to_dict(usage)
    usage_map[role] = data

    if data:
        message = (
            f"[tokens:{role}] "
            f"input={data['last_input_tokens']:,} "
            f"cached={data['last_cached_input_tokens']:,} "
            f"cache={data['cache_percent']}%"
        )
        if data["context_percent"] is not None:
            message += f" context={data['context_percent']}%"
        if data.get("cost_usd") is not None:
            message += f" cost=${data['cost_usd']:.4f}"
        print(message)

    return usage_map
