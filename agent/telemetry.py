from __future__ import annotations

from typing import Any



def usage_to_dict(usage: Any) -> dict[str, Any]:
    if usage is None:
        return {}

    last = usage.last
    total = usage.total

    last_input = getattr(last, "input_tokens", 0) or 0
    last_cached = getattr(last, "cached_input_tokens", 0) or 0
    context_window = getattr(usage, "model_context_window", None)

    return {
        "last_input_tokens": last_input,
        "last_cached_input_tokens": last_cached,
        "last_output_tokens": getattr(last, "output_tokens", 0) or 0,
        "last_reasoning_tokens": getattr(last, "reasoning_output_tokens", 0) or 0,
        "cache_percent": round((last_cached / last_input) * 100, 1) if last_input else 0.0,
        "total_input_tokens": getattr(total, "input_tokens", 0) or 0,
        "total_cached_input_tokens": getattr(total, "cached_input_tokens", 0) or 0,
        "total_output_tokens": getattr(total, "output_tokens", 0) or 0,
        "model_context_window": context_window,
        "context_percent": (
            round((last_input / context_window) * 100, 1) if context_window else None
        ),
    }


def record_usage(
    current: dict[str, dict[str, Any]],
    role: str,
    usage: Any,
) -> dict[str, dict[str, Any]]:
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
        print(message)

    return usage_map
