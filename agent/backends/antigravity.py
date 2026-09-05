"""
WHAT:  A stub backend for Google Antigravity.
WHY:   Two reasons.  First, it makes `"provider": "antigravity"` a
       configuration you can WRITE today, failing with a useful message
       instead of a NameError.  Second, it is the worked example for
       "how do I add a provider" -- it is the shortest complete file that
       satisfies the AgentBackend contract.
CONCEPT: The adapter pattern with the bodies left blank.

--------------------------------------------------------------------------
ADDING A PROVIDER: THE WHOLE JOB IS THREE THINGS
--------------------------------------------------------------------------
  1. START or RESUME a conversation and get back an id you can store.
  2. SEND a prompt on that conversation and read the final text back,
     constrained to a JSON Schema (or, if the provider cannot constrain it,
     ask for JSON in the prompt and validate what comes back).
  3. REPORT token usage, if the provider tells you any.

Do those three inside run_structured(), set the three class attributes at the
top, delete the raise, and you are done.  agent/backends/codex.py is the same
file with the bodies filled in -- read them side by side.
"""

from __future__ import annotations

from agent.backends.base import Access, BackendUnavailable, StructuredRun, OutputT

_MESSAGE = """The 'antigravity' backend is not implemented.

Google Antigravity had no documented, stable programmatic API when this was
written, so there was nothing to adapt to. To implement it you need exactly
three things:

  1. a way to start a conversation and get back an id;
  2. a way to send a prompt on that id and read the final response;
  3. a way to report token usage (optional -- return None if unavailable).

Fill in run_structured() in agent/backends/antigravity.py, set max_access to
what the provider can actually do, and delete the raise. Use
agent/backends/codex.py as the reference implementation.

For now, configure this role to use 'codex' instead."""


class AntigravityBackend:
    name = "antigravity"
    supports_repo_access = False
    max_access = Access.NONE

    def __init__(self, *, model: str | None = None, **options):
        self.model = model
        self.options = options
        # Raising in __init__ means the failure happens in cli.py at startup,
        # before any tokens are spent -- not three nodes into a task.
        raise BackendUnavailable(_MESSAGE)

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
        # ---- 1. start or resume a conversation ---------------------------
        # ---- 2. send `prompt`, get text back matching output_model -------
        # ---- 3. build a Usage from whatever the provider reports ---------
        raise BackendUnavailable(_MESSAGE)

    def close(self) -> None:
        return None
