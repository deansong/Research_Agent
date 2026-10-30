"""
WHAT:  The Claude Code backend -- drives the `claude` CLI, one subprocess a turn.
WHY:   It reaches Opus, Sonnet and Fable directly, and a verifier from a
       different model family is a genuinely independent check -- which is the
       rule this project already enforces structurally for nodes.
CONCEPT: An adapter. All the machinery is in _cli.py; this file is vocabulary.

--------------------------------------------------------------------------
WHAT CHANGED SINCE THIS FILE WAS A STUB
--------------------------------------------------------------------------
The stub said structured output was "THE ONE REAL DIFFICULTY" because there
was no schema flag, and proposed appending the schema to the prompt, asking
for "ONLY a single JSON object", and stripping ```json fences. That research
is superseded: `claude --json-schema <schema>` exists as of 2.1.252. The
workaround is deleted rather than implemented.

The stub's OTHER idea was right and is implemented as written: mint a uuid4
and pass --session-id on turn 1, --resume after, so the handle semantics are
identical to Codex's thread id and nothing in the graph changes.

--------------------------------------------------------------------------
MEASURED, 2026-09-08, claude 2.1.252
--------------------------------------------------------------------------
The envelope from `-p --output-format json`, captured from a failing turn
(the success path is the same shape with is_error false):

    {"type": "result", "subtype": "success", "result": "<the answer text>",
     "session_id": "...", "total_cost_usd": 0, "is_error": false,
     "num_turns": 1, "permission_denials": [], "duration_ms": 56,
     "usage": {"input_tokens", "output_tokens", "cache_read_input_tokens",
               "cache_creation_input_tokens",
               "output_tokens_details": {"thinking_tokens"}}}

`is_error` IS THE SIGNAL, NOT THE EXIT CODE. The measured failure came back
with is_error true and exit status 0.

`total_cost_usd` is the first thing in this project to populate
Usage.cost_usd -- telemetry.py has printed it since it was written and it has
been None every time, because Codex does not report cost.

TWO HAZARDS, both handled in _cli.py because they are not specific to this
CLI: `claude -p` waits on stdin (so stdin is closed), and run from inside
another Claude Code session it inherits ~76 CLAUDE_*/ANTHROPIC_* variables
and reuses the PARENT's session_id (so the environment is scrubbed).

--------------------------------------------------------------------------
ISOLATING THE WORKSPACE
--------------------------------------------------------------------------
With cwd=repo_path this CLI loads that repository's CLAUDE.md, its
.claude/settings.json, its hooks and its plugins. For a node turn that is
instruction injection from the directory under study: a repository being
audited could carry a CLAUDE.md that redirects the node, and hooks would run
commands nobody asked for. So --setting-sources is empty and
--strict-mcp-config is set: a turn is shaped by the persona we send and
nothing the workspace happens to contain.

Codex has no equivalent exposure, because it takes its instructions as an API
parameter rather than discovering them on disk.
"""

from __future__ import annotations

import uuid

from research_backend.backends._cli import CliBackend, usage_from_keys
from research_backend.base import Access, Usage

#: What a read_only node may use. Unlike agy, this CLI can express "look but
#: do not touch", so READ_ONLY is a genuinely useful level here.
_READ_ONLY_TOOLS = "Read,Glob,Grep,Bash(git status:*),Bash(git diff:*),Bash(git log:*)"
_WRITING_TOOLS = "Write,Edit,MultiEdit,NotebookEdit"

#: An is_error envelope whose text says the SERVICE refused, not the request.
#: Retried a few times with backoff by _cli.py; everything else is final.
_TRANSIENT = ("overloaded", "at capacity", "529", "503", "502", "rate limit", "rate_limit",
              "service unavailable", "temporarily unavailable", "try again",
              "api error: 5", "internal server error", "connection error",
              "econnreset", "timed out")


class ClaudeCodeBackend(CliBackend):
    INSTRUCTIONS_ROUTE = "appended to Claude Code's system prompt (--append-system-prompt), every turn"
    PROVIDER_ADDS = ("Claude Code's own system prompt, its tool definitions and any CLAUDE.md "
                     "in the work folder or above it")
    name = "claude_code"
    label = "Claude Code"
    executable_default = "claude"

    #: MEASURED 2026-09-24, claude 2.1.281: `-p` with no positional prompt
    #: reads it from stdin. Sent that way because an argv entry is capped at
    #: 128 KB by the kernel, and a designer prompt is already ~45 KB.
    prompt_via_stdin = True

    supports_repo_access = True
    max_access = Access.FULL

    #: `claude --effort`, measured from `claude --help` (2.1.x).
    efforts: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
    effort: str | None = None

    def __init__(self, *, max_turns: int | None = None,
                 max_budget_usd: float | None = None, effort: str | None = None,
                 **options):
        from research_backend.base import checked_effort

        self.effort = checked_effort("claude_code", effort, self.efforts)
        self.max_turns = max_turns
        self.max_budget_usd = max_budget_usd
        super().__init__(**options)

    def run_structured(self, *, thread_id, repo_path, access, developer_instructions,
                       prompt, output_model):
        run = super().run_structured(
            thread_id=thread_id, repo_path=repo_path, access=access,
            developer_instructions=developer_instructions, prompt=prompt,
            output_model=output_model)
        # Kept for research_backend.limits("claude_code"): `claude` has no other
        # way to say how much of its limits is used.
        try:
            from research_backend.limits import remember_claude_limits

            remember_claude_limits(run.events)
        except OSError:
            pass
        return run

    # ---- vocabulary ------------------------------------------------------

    def argv(self, *, prompt, instructions, access, schema_path, session):
        import json
        import pathlib

        # No prompt here: it goes on stdin (prompt_via_stdin), which has no
        # size limit. `prompt` stays in the signature for the other CLIs.
        argv = [self.executable, "-p",
                "--output-format", "stream-json", "--include-partial-messages",
                "--verbose",
                "--json-schema", pathlib.Path(schema_path).read_text(),
                # See the docstring: keep the workspace out of the turn.
                "--setting-sources", "", "--strict-mcp-config"]
        if instructions.strip():
            # Append rather than replace: the CLI's own prompt carries the
            # tool scaffolding it needs to actually read files, and replacing
            # it wholesale costs that. If turns come back chatty or
            # tool-happy, --system-prompt is the one-word change.
            argv += ["--append-system-prompt", instructions]
        if self.model:
            argv += ["--model", self.model]
        if self.effort:
            argv += ["--effort", self.effort]
        if self.max_turns:
            argv += ["--max-turns", str(self.max_turns)]
        if self.max_budget_usd:
            argv += ["--max-budget-usd", str(self.max_budget_usd)]
        argv += self.access_flags(access)
        return argv + session

    def access_flags(self, access: Access) -> list[str]:
        """Belt and braces on read_only, deliberately.

        The allow-list is the intent and the deny-list is the guarantee, and
        they fail in different ways -- a tool gets renamed, or a new writing
        tool appears. `acceptEdits` alongside them is not a contradiction:
        with the writing tools denied there is nothing to accept, and its job
        is to stop the process blocking on a permission question nobody is
        there to answer.
        """
        # Folders outside the working directory it may use: the session's
        # results folder (written by a write turn) and the shared skills
        # (read by any turn -- a read-only discussor or validator included).
        roots = [*(getattr(self, "writable_roots", None) or []),
                 *(getattr(self, "readable_roots", None) or [])]
        extra = [flag for root in dict.fromkeys(roots) for flag in ("--add-dir", root)]
        if access in (Access.NONE, Access.READ_ONLY):
            return ["--allowed-tools", _READ_ONLY_TOOLS,
                    "--disallowed-tools", _WRITING_TOOLS,
                    "--permission-mode", "acceptEdits", *extra]
        if access is Access.WRITE:
            return ["--permission-mode", "acceptEdits", *extra]
        return ["--permission-mode", "bypassPermissions", *extra]

    def session_args(self, thread_id):
        """Mint the id rather than scraping it back.

        The neat part of the original design: --session-id takes a uuid we
        choose, so turn 1 names the conversation and every later turn resumes
        it -- identical handle semantics to Codex's thread id, so nothing in
        agent/work/state.py has to know a second shape.
        """
        if thread_id:
            return (["--resume", thread_id], thread_id)
        minted = str(uuid.uuid4())
        return (["--session-id", minted], minted)

    def is_envelope(self, event):
        return event.get("type") == "result"

    def record_for(self, event):
        kind = event.get("type")

        if kind == "stream_event":
            delta = ((event.get("event") or {}).get("delta") or {})
            text = delta.get("text") or delta.get("thinking") or ""
            if not text:
                return None
            stream = "reasoning" if delta.get("thinking") else "message"
            return {"kind": f"{stream}_delta", "transient": True,
                    "stream": stream, "text": text}

        if kind == "assistant":
            return self._from_content(event, "started")
        if kind == "user":
            return self._from_content(event, "completed")
        if kind == "result":
            return None
        if kind in ("system", "init"):
            return None
        if kind == "rate_limit_event":
            # The only place `claude` says how much of its limits are used;
            # research_backend.limits keeps the last one it saw.
            return {"kind": kind, "phase": "other",
                    "info": event.get("rate_limit_info") or {}}
        return {"kind": str(kind or "unknown"), "phase": "other"}

    def _from_content(self, event: dict, phase: str) -> dict | None:
        """Tool use and tool results, mapped onto the shared vocabulary."""
        blocks = ((event.get("message") or {}).get("content") or [])
        if not isinstance(blocks, list):
            return None

        for block in blocks:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")

            if btype == "tool_use":
                name = block.get("name", "")
                args = block.get("input") or {}
                if name == "Bash":
                    return {"kind": "command", "phase": phase,
                            "command": args.get("command", "")}
                if name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
                    return {"kind": "file_change", "phase": phase,
                            "changes": [{"path": args.get("file_path", "")}]}
                if name in ("WebSearch", "WebFetch"):
                    return {"kind": "web_search", "phase": phase,
                            "query": args.get("query") or args.get("url", "")}
                return {"kind": "tool", "phase": phase, "command": name}

            if btype == "tool_result":
                from research_backend.backends._progress import _clip

                content = block.get("content")
                text = content if isinstance(content, str) else str(content)
                entry = {"kind": "command", "phase": "completed",
                         "command": "", "output": _clip(text)}
                if block.get("is_error"):
                    entry["exit_code"] = 1
                return entry

            if btype == "thinking":
                return {"kind": "reasoning", "phase": "completed",
                        "summary": [str(block.get("thinking", ""))[:400]]}
        return None

    def final_text(self, envelope):
        if envelope.get("is_error"):
            return ""
        return str(envelope.get("result") or "")

    def refusal_text(self, envelope):
        if not envelope.get("is_error"):
            return ""
        return str(envelope.get("result") or envelope.get("subtype") or "")

    def transient_error(self, envelope):
        text = self.refusal_text(envelope)
        return text if any(m in text.lower() for m in _TRANSIENT) else ""

    def _why_empty(self, envelope, access=None):
        # `access` is accepted and unused: this provider's envelope already
        # names its own denials, so it never had to infer them from the level.
        # The parameter exists because the base class passes it.
        denials = envelope.get("permission_denials") or []
        detail = (f" It was denied {len(denials)} tool call(s); a node "
                  f"declared read_only cannot write." if denials else "")
        return (
            f"claude reported is_error={envelope.get('is_error')} and "
            f"returned: {str(envelope.get('result'))[:400]!r}.{detail}\n\n"
            f"If that is an authentication error, run `claude` once "
            f"interactively to refresh the login."
        )

    def usage_from(self, envelope) -> Usage | None:
        usage = envelope.get("usage")
        if not usage:
            return None
        built = usage_from_keys(
            usage,
            input_tokens="input_tokens",
            output_tokens="output_tokens",
            cached_input_tokens="cache_read_input_tokens",
        )
        thinking = (usage.get("output_tokens_details") or {}).get("thinking_tokens")
        cost = envelope.get("total_cost_usd")
        # The first backend here to report cost at all -- telemetry.py has
        # printed this field since it was written and it has always been None.
        return Usage(
            input_tokens=built.input_tokens,
            cached_input_tokens=built.cached_input_tokens,
            output_tokens=built.output_tokens,
            reasoning_tokens=int(thinking or 0),
            cost_usd=float(cost) if cost else None,
        )

    def session_id_from(self, envelope):
        return envelope.get("session_id")


class ClaudeStrataBackend(ClaudeCodeBackend):
    """Claude Code -- its tools, permissions and sessions -- on the Strata
    model.

    MEASURED 2026-10-30, claude 2.1.302: the Strata server answers Anthropic's
    POST /v1/messages as well as the Responses API, so the CLI is pointed at it
    directly -- no proxy: ANTHROPIC_BASE_URL (the server, without /v1: the CLI
    adds /v1/messages), ANTHROPIC_AUTH_TOKEN (the Strata key), and the model
    for every slot the CLI fills on its own (its "haiku" background calls
    included). A plain answer and a tool call (Read a file) both came back.

    Set after the scrub of CLAUDE_*/ANTHROPIC_* (_cli.py), so a parent Claude
    session cannot leak in and nothing in the user's shell is needed but
    STRATA_API_KEY (STRATA_BASE_URL moves the server, as for `strata`). Cost
    is not reported: the CLI prices these tokens as Anthropic's.
    """

    name = "claude_strata"
    label = "Claude Code on Strata"
    default_model = "strata"

    def __init__(self, **options):
        super().__init__(**options)
        # Always named: without --model the CLI would ask for a Claude model.
        self.model = self.model or self.default_model

    def child_env(self) -> dict[str, str]:
        import os

        from research_backend.backends.responses import StrataBackend
        from research_backend.base import BackendUnavailable

        key = os.environ.get(StrataBackend.key_env, "")
        if not key:
            raise BackendUnavailable(f"claude_strata needs {StrataBackend.key_env} in the "
                                     f"environment (the Strata server's key).")
        base = (os.environ.get(StrataBackend.base_url_env) or StrataBackend.default_base_url)
        base = base.rstrip("/").removesuffix("/v1")
        return {"ANTHROPIC_BASE_URL": base, "ANTHROPIC_AUTH_TOKEN": key,
                "ANTHROPIC_MODEL": self.model, "ANTHROPIC_SMALL_FAST_MODEL": self.model,
                **{f"ANTHROPIC_DEFAULT_{tier}_MODEL": self.model
                   for tier in ("HAIKU", "SONNET", "OPUS")},
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}

    def run_structured(self, **kwargs):
        # Not ClaudeCodeBackend's: there are no Claude limits to remember.
        return CliBackend.run_structured(self, **kwargs)

    def usage_from(self, envelope) -> Usage | None:
        import dataclasses

        usage = super().usage_from(envelope)
        return dataclasses.replace(usage, cost_usd=None) if usage is not None else None
