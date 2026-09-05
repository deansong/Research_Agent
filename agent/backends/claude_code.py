"""
WHAT:  A stub backend for Claude Code (the `claude` CLI).
WHY:   Lets you configure a role to Claude Code today and get a clear message,
       and records the research so implementing it is filling in blanks rather
       than starting over.
CONCEPT: Adapter stub.  See antigravity.py for the "adding a provider" walk
       through; this file adds the Claude-Code-specific details.

--------------------------------------------------------------------------
WHAT AN IMPLEMENTATION LOOKS LIKE (researched, not guessed)
--------------------------------------------------------------------------
Invocation -- one subprocess per turn, no long-lived process:

    subprocess.run(
        ["claude", "-p", prompt, "--output-format", "json",
         "--append-system-prompt", developer_instructions,
         *session_args, *access_args],
        cwd=repo_path, capture_output=True, text=True, timeout=...,
    )

Conversation continuity.  The neat trick: rather than scraping `session_id`
out of the first response, MINT the id yourself --

    first turn : --session-id <uuid4 you generated>
    later turns: --resume <that same uuid>

-- which makes the handle semantics identical to Codex's thread id, so
nothing in agent/state.py needs to change.  (--session-id requires a valid
UUID, which is exactly what uuid.uuid4() gives you.)

Developer instructions -> --append-system-prompt, sent on EVERY turn.
Harmless on resume, and it means the persona survives a dropped session.

Access mapping:
    READ_ONLY -> --allowedTools "Read Glob Grep Bash(git *)"
    WRITE     -> --permission-mode acceptEdits
    FULL      -> --permission-mode bypassPermissions
    NONE      -> clamp to READ_ONLY (the CLI always has a working directory)

Structured output -- THE ONE REAL DIFFICULTY.  There is no --output-schema
flag. So append to the prompt:

    output_model.model_json_schema()  +  "Reply with ONLY a single JSON
    object matching this schema. No prose, no code fences."

then parse `payload["result"]`, tolerating ```json fences. On ValidationError,
send ONE repair turn on the same session quoting the pydantic errors; if that
also fails, raise BackendOutputError.

Usage: payload["usage"] gives input_tokens, output_tokens and
cache_read_input_tokens (-> cached_input_tokens); payload["total_cost_usd"]
maps to Usage.cost_usd.

Why the CLI and not the claude-agent-sdk: the SDK's query() is async and this
whole graph is synchronous, so it would need asyncio.run() inside a node --
workable, but a trap if the graph ever goes async. The CLI adds no
dependencies and the exact command can be printed and pasted into a shell
when something misbehaves, which matters a lot while you are learning.
"""

from __future__ import annotations

from agent.backends.base import Access, BackendUnavailable, StructuredRun, OutputT

_MESSAGE = """The 'claude_code' backend is not implemented yet.

The design is written out in full at the top of agent/backends/claude_code.py:
the exact `claude -p --output-format json` invocation, how to mint a session
id with --session-id / --resume, how to map Access onto --allowedTools and
--permission-mode, and how to get structured output when the CLI has no
--output-schema flag.

Fill in run_structured() there and delete the raise.

For now, configure this role to use 'codex' instead."""


class ClaudeCodeBackend:
    name = "claude_code"
    supports_repo_access = True
    # The CLI genuinely can do all of these; the value stays honest so that
    # when you implement it, role/access validation already works.
    max_access = Access.FULL

    def __init__(
        self,
        *,
        model: str | None = None,
        executable: str = "claude",
        timeout: float = 900.0,
        **options,
    ):
        self.model = model
        self.executable = executable
        self.timeout = timeout
        self.options = options
        # Fail at startup, not mid-task.
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
