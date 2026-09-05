"""
WHAT:  A stub backend for talking to a plain LLM API (Anthropic or OpenAI).
WHY:   The cheapest option for the roles that only think about text.  Also the
       clearest illustration of why `Access` exists: this backend has NO
       filesystem access at all, so it can never be the executor.
CONCEPT: Adapter stub.  See antigravity.py for the general shape.

--------------------------------------------------------------------------
WHAT AN IMPLEMENTATION LOOKS LIKE
--------------------------------------------------------------------------
Structured output, per provider:

  Anthropic -- define ONE tool named "emit" whose input_schema is
      output_model.model_json_schema(), then force it:
          tool_choice={"type": "tool", "name": "emit"}
      and read the result from content[0].input. Reliable, no parsing.

  OpenAI -- response_format={"type": "json_schema", "json_schema":
      {"name": ..., "schema": ..., "strict": True}}.
      GOTCHA: strict mode requires EVERY property to be listed in "required"
      and "additionalProperties": false at every level. Our StrictModel gives
      the second, but fields with defaults (most of ours) are omitted from
      "required", so you must post-process the schema to add them all and
      inline any $defs. Skip that and every one of our schemas is rejected.

Conversation continuity -- THE INTERESTING PROBLEM.  These APIs are
stateless: there is no thread id, you resend the message list every turn. So
where do the messages live? Three options:

  (a) in LangGraph state -- balloons every checkpoint and breaks the
      separation this project teaches (workflow state != conversation state);
  (b) an in-process dict -- lost on restart, so --session stops meaning
      anything;
  (c) a sidecar table in the SAME sqlite file the checkpointer uses, keyed by
      a uuid4 you mint exactly like Claude Code does.

(c) is the recommended one: thread handling becomes identical across all
providers, nodes need no changes, and nothing extra lands in the checkpoint.

Import anthropic/openai lazily inside __init__ and raise BackendUnavailable
with "pip install anthropic" on ImportError.
"""

from __future__ import annotations

from agent.backends.base import Access, BackendUnavailable, StructuredRun, OutputT

_MESSAGE = """The 'api' backend is not implemented yet.

The design is written out in full at the top of agent/backends/api.py: forced
tool-use for Anthropic, json_schema strict mode for OpenAI (including the
'required' gotcha), and how to keep conversation history for a stateless API.

Fill in run_structured() there and delete the raise.

Note that this backend can never serve the executor: it has no filesystem
access, so it cannot edit your repository. Use 'codex' for that role."""


class ApiBackend:
    name = "api"
    # These two attributes are the whole reason the executor check works.
    supports_repo_access = False
    max_access = Access.NONE

    def __init__(self, *, model: str | None = None, provider: str = "anthropic", **options):
        self.model = model
        self.provider = provider
        self.options = options
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
        raise BackendUnavailable(_MESSAGE)

    def close(self) -> None:
        return None
