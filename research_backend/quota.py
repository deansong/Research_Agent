"""
WHAT:  How long to wait when a provider says the account is out of credits or
       past its usage limit -- for every backend.
WHY:   Waiting used to be 20 checks a minute apart, then give up. A usage
       limit resets in hours (a 5-hour window, a daily quota), so a run left
       overnight died twenty minutes in, every time. claude_code and
       antigravity did not even tell a quota from a busy server, and gave up
       after ~35 seconds of "busy" retries.
CONCEPT: Wait until the time the provider names, when it names one; otherwise
       back off -- 1, 2, 4, 8, 16, then every 30 minutes -- for up to
       `hours` in all (default 24). Every wait is cut short by Stop or Pause.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta

#: How a provider says the account cannot run anything until later. Lower case.
QUOTA_MARKERS = (
    "out of credits", "insufficient_quota", "add credits", "billing",
    "usage limit", "limit reached", "hit your limit", "quota",
    "exhausted your capacity", "credit balance is too low",
)

FIRST_WAIT = 60.0
MAX_GAP = 1800.0
DEFAULT_HOURS = 24.0


def is_quota(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in QUOTA_MARKERS)


_UNITS = {"d": 86400, "day": 86400, "h": 3600, "hr": 3600, "hour": 3600,
          "m": 60, "min": 60, "minute": 60, "s": 1, "sec": 1, "second": 1}
_SPAN = re.compile(r"(\d+(?:\.\d+)?)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)(?![a-z])")


def reset_delay(message: str, now: float | None = None) -> float | None:
    """Seconds until the limit resets, if the message says; else None.

    Understands the shapes providers actually use:
      "...|1760799600"                       (claude: epoch seconds)
      "try again in 2 hours 13 minutes", "reset after 2h13m5s",
      "retry after 120 seconds"
      "resets 6pm", "try again at 3:45 PM", "resets at 15:00 (Europe/London)"
    """
    text = message or ""
    now = time.time() if now is None else now

    epoch = re.search(r"\|(\d{10})\b", text)
    if epoch:
        return max(0.0, int(epoch.group(1)) - now)

    after = re.search(r"\b(?:in|after)\s+((?:\d+(?:\.\d+)?\s*[a-z]+[\s,]*(?:and\s+)?)+)",
                      text, re.I)
    if after:
        total = 0.0
        for amount, unit in _SPAN.findall(after.group(1).lower()):
            key = unit if unit == "s" else unit.rstrip("s")
            total += float(amount) * _UNITS.get(key, 0)
        if total > 0:
            return total

    clock = re.search(r"\b(?:resets?|again)\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?"
                      r"(?:\s*\(([A-Za-z_]+/[A-Za-z_]+)\))?", text, re.I)
    if clock:
        hour, minute = int(clock.group(1)), int(clock.group(2) or 0)
        meridiem = (clock.group(3) or "").lower().replace(".", "")
        if meridiem == "pm" and hour < 12:
            hour += 12
        if meridiem == "am" and hour == 12:
            hour = 0
        if hour > 23 or minute > 59 or (not meridiem and not clock.group(2)):
            return None  # "resets 5" is not a time
        zone = None
        if clock.group(4):
            try:
                from zoneinfo import ZoneInfo

                zone = ZoneInfo(clock.group(4))
            except Exception:  # noqa: BLE001 -- unknown zone: use local time
                zone = None
        current = datetime.fromtimestamp(now, zone)
        target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= current:
            target += timedelta(days=1)
        return (target - current).total_seconds()
    return None


class CreditWait:
    """The waiting policy for one turn. `next(message)` returns how long to
    sleep before trying again, or raises once `hours` have been spent."""

    def __init__(self, *, first: float = FIRST_WAIT, max_gap: float = MAX_GAP,
                 hours: float = DEFAULT_HOURS, attempts: int | None = None):
        self.first, self.max_gap = float(first), float(max_gap)
        self.budget = float(hours) * 3600
        self.attempts = attempts
        self.tries = 0
        self.waited = 0.0

    def next(self, message: str) -> tuple[float, str]:
        """(seconds, why) for the next wait. Raises StopIteration when spent."""
        self.tries += 1
        remaining = self.budget - self.waited
        if remaining <= 0 or (self.attempts is not None and self.tries > self.attempts):
            raise StopIteration
        named = reset_delay(message)
        if named is not None:
            # A little past the named time: limits reset on the provider's
            # clock, not ours.
            seconds, why = named + 30, "until the limit resets"
        else:
            seconds = min(self.first * (2 ** (self.tries - 1)), self.max_gap)
            why = "then checking again"
        seconds = max(0.0, min(seconds, remaining))
        self.waited += seconds
        return seconds, why

    def describe_budget(self) -> str:
        hours = self.budget / 3600
        return f"{hours:g} hour(s)" if hours >= 1 else f"{self.budget / 60:g} minute(s)"


def until(seconds: float) -> str:
    """'14:05' (or 'Tue 14:05' beyond today) -- when a wait ends, for the log."""
    end = datetime.now() + timedelta(seconds=seconds)
    fmt = "%H:%M" if end.date() == datetime.now().date() else "%a %H:%M"
    return end.strftime(fmt)
