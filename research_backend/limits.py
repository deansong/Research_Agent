"""
WHAT:  How much of each provider's usage limits is used, and when they reset.
WHY:   A run that hits a limit waits for hours (see quota.py). Knowing the
       limits before starting one -- or before choosing which provider a role
       runs on -- is cheaper.
CONCEPT: One reader per provider, each asking the provider itself; all of
       them return the same Quota. Never raises: a provider that cannot answer
       comes back with `error` saying why.

How each one answers, measured 2026-10-23:

    codex         the app-server's account/rateLimits/read: a 5-hour and a
                  weekly window, each with used % and reset time; the plan;
                  credits; free limit resets on hand.
    antigravity   `agy -p /quota`: one line per window,
                  "group<TAB>window<TAB>94%<TAB>2026-10-26T19:23:10Z",
                  where the % is what is LEFT.
    claude_code   no command reports it. `claude` sends a rate_limit_event
                  during each turn; every claude_code turn -- a session's or
                  the agent's -- keeps the last one, and that is what is
                  shown, with when it was seen.
    fake          a made-up window.
"""

from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone

from research_backend import home

#: Long enough for codex to start its app-server and answer.
TIMEOUT = 45.0


@dataclass(frozen=True)
class Window:
    """One usage limit: e.g. the 5-hour window, or the weekly one."""

    name: str
    used_percent: float | None = None
    """None when the provider does not say (claude_code, sometimes)."""
    resets_at: datetime | None = None
    status: str = ""
    """The provider's own word, when it has one: "allowed", "rejected"."""

    @property
    def left_percent(self) -> float | None:
        return None if self.used_percent is None else max(0.0, 100.0 - self.used_percent)

    def __str__(self, width: int = 0) -> str:
        used = "?" if self.used_percent is None else f"{self.used_percent:g}%"
        line = f"{self.name:<{width}}  {used:>4} used"
        if self.status:
            line += f"  ({self.status})"
        if self.resets_at:
            line += f", resets {_when(self.resets_at)}"
        return line

    def to_dict(self) -> dict:
        return {"name": self.name, "used_percent": self.used_percent,
                "left_percent": self.left_percent, "status": self.status,
                "resets_at": self.resets_at.isoformat() if self.resets_at else None}


@dataclass(frozen=True)
class Quota:
    """One provider's limits, as the provider reported them."""

    provider: str
    windows: tuple[Window, ...] = ()
    plan: str = ""
    credits: str = ""
    """In words: "no credits; 2 free limit resets available"."""
    source: str = ""
    """Where the numbers came from, in words."""
    checked_at: datetime | None = None
    """When the provider said so -- for claude_code, when a turn last did."""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def most_used(self) -> Window | None:
        known = [w for w in self.windows if w.used_percent is not None]
        return max(known, key=lambda w: w.used_percent) if known else None

    def __str__(self) -> str:
        head = self.provider + (f" ({self.plan} plan)" if self.plan else "")
        lines = [head]
        if self.error:
            lines.append(f"  {self.error}")
        width = max((len(w.name) for w in self.windows), default=0)
        lines += [f"  {w.__str__(width)}" for w in self.windows]
        if self.credits:
            lines.append(f"  {self.credits}")
        if self.source:
            seen = f", as of {_when(self.checked_at)}" if self.checked_at else ""
            lines.append(f"  [{self.source}{seen}]")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {"provider": self.provider, "plan": self.plan, "credits": self.credits,
                "windows": [w.to_dict() for w in self.windows], "source": self.source,
                "checked_at": self.checked_at.isoformat() if self.checked_at else None,
                "error": self.error}


# ---- one reader per provider ------------------------------------------------


def _codex() -> Quota:
    if not shutil.which("codex"):
        return Quota("codex", error="`codex` is not on PATH.")
    from openai_codex.generated.v2_all import GetAccountRateLimitsResponse

    from research_backend.backends import codex_client

    # The SDK has no method for this yet; the app-server does, and the SDK's
    # own client sends any request with its generated response model.
    answer = codex_client()._client.request(
        "account/rateLimits/read", None, response_model=GetAccountRateLimitsResponse)
    limits = answer.rate_limits

    windows = []
    for window in (limits.primary, limits.secondary):
        if window is not None:
            windows.append(Window(_span(window.window_duration_mins),
                                  float(window.used_percent), _epoch(window.resets_at)))
    # Other limits, when the plan has them (per model, say).
    for limit_id, other in (answer.rate_limits_by_limit_id or {}).items():
        if limit_id == limits.limit_id:
            continue
        label = getattr(other, "limit_name", None) or limit_id
        for window in (other.primary, other.secondary):
            if window is not None:
                windows.append(Window(f"{label}: {_span(window.window_duration_mins)}",
                                      float(window.used_percent), _epoch(window.resets_at)))

    said = []
    credits = limits.credits
    if credits is not None:
        if credits.unlimited:
            said.append("unlimited credits")
        elif credits.has_credits:
            said.append(f"credits: {credits.balance}" if credits.balance else "has credits")
        else:
            said.append("no credits")
    resets = answer.rate_limit_reset_credits
    if resets is not None and resets.available_count:
        said.append(f"{resets.available_count} free limit "
                    f"reset{'s' if resets.available_count != 1 else ''} available")
    if limits.rate_limit_reached_type:
        said.append(f"LIMIT REACHED: {_word(limits.rate_limit_reached_type)}")
    return Quota("codex", tuple(windows), plan=_word(limits.plan_type),
                 credits="; ".join(said), source="codex app-server (account/rateLimits/read)",
                 checked_at=_now())


def _antigravity(executable: str = "agy") -> Quota:
    from research_backend.auth import _run, _verdict

    if not shutil.which(executable):
        return Quota("antigravity", error=f"`{executable}` is not on PATH.")
    code, out, err = _run([executable, "-p", "/quota"], scrub=True)
    if code != 0:
        why = _verdict(err, out).removeprefix("Error:").strip()
        return Quota("antigravity", error=why or f"`{executable} -p /quota` exited {code}.")
    windows = tuple(parse_agy_quota(out))
    if not windows:
        return Quota("antigravity", error=f"`{executable} -p /quota` printed no limits: "
                                          f"{_verdict(out, err)[:200]}")
    return Quota("antigravity", windows, source=f"`{executable} -p /quota`",
                 checked_at=_now())


def parse_agy_quota(text: str) -> list[Window]:
    """`agy -p /quota` lines -> windows. The % agy prints is what is LEFT."""
    windows = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split("\t")]
        if len(parts) < 4 or not parts[2].endswith("%"):
            continue
        group, window, left, resets = parts[:4]
        if window.startswith(group):
            window = window[len(group):].strip()
        window = window.removesuffix("Remaining").strip()
        try:
            used = 100.0 - float(left.rstrip("%"))
        except ValueError:
            continue
        windows.append(Window(f"{group}: {window}", used, _iso(resets)))
    return windows


def _claude_code() -> Quota:
    seen = home.home() / "limits" / "claude_code.json"
    note = "the `claude` CLI reports its limits only during a turn"
    if not seen.exists():
        return Quota("claude_code", source=note,
                     error="No limits seen yet: they arrive with a turn. Send a "
                           "message on claude_code, then ask again.")
    saved = home.read_json(seen)
    return Quota("claude_code", claude_windows(saved.get("limits", {})),
                 source=f"{note}; the last one seen", checked_at=_iso(saved.get("seen_at")))


_CLAUDE_WINDOWS = {"five_hour": "5 hours", "seven_day": "weekly",
                   "seven_day_opus": "weekly (Opus)", "seven_day_sonnet": "weekly (Sonnet)"}


def claude_windows(limits: dict) -> tuple[Window, ...]:
    """Windows from the rate_limit_event infos a turn reported, keyed by type.

    Measured: each event names one window (`rateLimitType`, `status`,
    `resetsAt`) and carries every window's use under `unifiedWindows`
    ({"five_hour": {"utilization": 0.2, "resetsAt": ...}, ...}), utilization
    a fraction."""
    found: dict[str, Window] = {}
    for kind, info in limits.items():
        status = _word(info.get("status") or "")
        for name, window in (info.get("unifiedWindows") or {}).items():
            found[name] = Window(_CLAUDE_WINDOWS.get(name, _word(name)),
                                 _percent(window.get("utilization")),
                                 _epoch(window.get("resetsAt")),
                                 status=status if name == kind else
                                 getattr(found.get(name), "status", ""))
        if kind not in found:
            found[kind] = Window(_CLAUDE_WINDOWS.get(kind, _word(kind)),
                                 _percent(info.get("utilization")),
                                 _epoch(info.get("resetsAt")), status=status)
    return tuple(found.values())


def _percent(fraction) -> float | None:
    if not isinstance(fraction, (int, float)):
        return None
    return round(float(fraction) * 100, 1)


def remember_claude_limits(events: list[dict]) -> None:
    """Keep the limits a claude_code turn reported, newest per window."""
    infos = [e.get("info") for e in events
             if e.get("kind") == "rate_limit_event" and e.get("info")]
    if not infos:
        return
    path = home.home() / "limits" / "claude_code.json"
    saved = home.read_json(path) if path.exists() else {"limits": {}}
    for info in infos:
        saved["limits"][str(info.get("rateLimitType") or "limit")] = info
    saved["seen_at"] = _now().isoformat()
    home.write_json(path, saved)


def _fake() -> Quota:
    return Quota("fake", (Window("5 hours", 0.0), Window("weekly", 0.0)),
                 source="built in", checked_at=_now())


READERS = {
    "codex": _codex,
    "antigravity": _antigravity,
    "claude_code": _claude_code,
    "fake": _fake,
}


def usage_of(provider: str) -> Quota:
    reader = READERS.get(provider)
    if reader is None:
        return Quota(provider, error=f"Reading usage limits is not supported for '{provider}'.")
    try:
        return reader()
    except Exception as exc:  # a probe: every failure is an answer
        return Quota(provider, error=f"{type(exc).__name__}: {exc}")


def usage_all(providers) -> list[Quota]:
    """Several providers at once, in parallel -- each takes a few seconds."""
    names = list(dict.fromkeys(providers))
    with ThreadPoolExecutor(max_workers=max(1, len(names))) as pool:
        futures = [pool.submit(usage_of, name) for name in names]
        out = []
        for name, future in zip(names, futures):
            try:
                out.append(future.result(timeout=TIMEOUT))
            except Exception as exc:
                out.append(Quota(name, error=f"No answer: {exc}"))
        return out


# ---- small helpers -----------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _epoch(value) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), timezone.utc) if value else None
    except (TypeError, ValueError, OSError):
        return None


def _iso(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


def _span(minutes) -> str:
    minutes = int(minutes or 0)
    if minutes == 10080:
        return "weekly"
    if minutes and minutes % 1440 == 0:
        return f"{minutes // 1440} days"
    if minutes and minutes % 60 == 0:
        return f"{minutes // 60} hours"
    return f"{minutes} minutes"


def _word(value) -> str:
    value = getattr(value, "value", value)
    return str(value or "").replace("_", " ")


def _when(moment: datetime) -> str:
    """'Fri 25 Oct 10:17 (in 1d 22h)' in local time."""
    local = moment.astimezone()
    seconds = (moment - _now()).total_seconds()
    text = local.strftime("%a %d %b %H:%M")
    if seconds > 0:
        days, rest = divmod(int(seconds), 86400)
        hours, rest = divmod(rest, 3600)
        minutes = rest // 60
        span = (f"{days}d {hours}h" if days else
                f"{hours}h {minutes}m" if hours else f"{minutes}m")
        text += f" (in {span})"
    return text
