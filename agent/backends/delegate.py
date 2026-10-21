"""
WHAT:  Delegation: when a role's provider is out of credits, run its turns on
       another provider and model -- only if you said so, for that role.
WHY:   Out of credits used to mean one thing: wait (up to a day, see quota.py).
       With a second account available -- antigravity's Claude, say -- a
       person may rather the run carried on there. It must stay a CHOICE: a
       delegate is a different model, and silently swapping the model an
       experiment step runs on is how results stop being comparable.
CONCEPT: A wrapper in front of the role's backend. Its primary is told not to
       wait for credits. The first out-of-credits error switches the wrapper
       to the delegate for the rest of the run and raises Delegated; the node
       (work/templates/agent_node.py) runs the same turn again, on the
       delegate, as a NEW conversation with its first prompt -- a Codex thread
       id means nothing to agy, and the delegate has seen none of it.
"""

from __future__ import annotations

from agent.backends.base import BackendError, BackendOutOfCredits

#: Set on BOTH backends, so whichever is active has them: Stop and Pause,
#: the live-progress callback, the extra writable folders.
_SHARED = ("cancel", "pause", "on_progress", "writable_roots")


class Delegated(BackendError):
    """The role's provider is out of credits and the role delegates: run this
    turn again -- on the delegate, fresh."""


class DelegatingBackend:
    def __init__(self, primary, delegate, role: str):
        object.__setattr__(self, "primary", primary)
        object.__setattr__(self, "delegate", delegate)
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "active", primary)
        # Fail fast on credits: the point is not to wait.
        if hasattr(primary, "credit_wait_hours"):
            primary.credit_wait_hours = 0

    # What the rest of the code reads off a backend -- the active one's.
    @property
    def name(self) -> str:
        return getattr(self.active, "name", "")

    @property
    def model(self):
        return getattr(self.active, "model", None)

    @property
    def delegated(self) -> bool:
        return self.active is self.delegate

    def __getattr__(self, attr):
        return getattr(self.active, attr)

    def __setattr__(self, attr, value):
        if attr in _SHARED:
            setattr(self.primary, attr, value)
            setattr(self.delegate, attr, value)
        else:
            object.__setattr__(self, attr, value)

    def run_structured(self, **kwargs):
        if self.delegated:
            return self.delegate.run_structured(**kwargs)
        try:
            return self.primary.run_structured(**kwargs)
        except BackendOutOfCredits as exc:
            object.__setattr__(self, "active", self.delegate)
            print(f"\n    {self.role}: {getattr(self.primary, 'name', '?')} is out of "
                  f"credits -- delegating to {getattr(self.delegate, 'name', '?')}"
                  f"/{getattr(self.delegate, 'model', '') or 'default'} for the rest "
                  f"of this run, as this role is set to.\n    It said: {str(exc)[:200]}",
                  flush=True)
            raise Delegated(str(exc)) from None

    def close(self) -> None:
        for backend in (self.primary, self.delegate):
            closing = getattr(backend, "close", None)
            if closing:
                closing()
