"""
WHAT:  The Antigravity backend -- drives the `agy` CLI, one subprocess a turn.
WHY:   It reaches models nothing else here does (Gemini 3.8 Flash and 3.1 Pro,
       GPT-OSS 120B), and the cheap tiers are a good fit for the roles that
       run experiments rather than design them.
CONCEPT: An adapter. All the machinery is in _cli.py; this file is vocabulary.

--------------------------------------------------------------------------
MEASURED, 2026-09-08, agy 1.1.27
--------------------------------------------------------------------------
The envelope, from `-p --output-format json --json-schema <path>`:

    {"conversation_id": "...", "status": "SUCCESS",
     "response": "{\\"answer\\":\\"ok\\",\\"toolAction\\":\\"...\\"}\\n",
     "structured_output": {"answer": "ok"},
     "json_schema": {...echoed...},
     "usage": {"input_tokens", "output_tokens", "thinking_tokens",
               "cache_read_tokens", "total_tokens"}}

USE `structured_output`, NOT `response`. They are not the same: `response`
carried two keys the model invented (toolAction, toolSummary) that the schema
did not ask for, and `structured_output` is parsed and filtered to the schema.
Reading `response` would hand extra="forbid" models a validation error that
has nothing to do with the design.

`status: "SUCCESS"` IS NOT A SUCCESS TEST. A denied tool call gives SUCCESS,
`response: ""`, and no `structured_output` at all. The presence of a parseable
answer is the only test worth running -- _cli.py does that.

CONVERSATION IDS ARE READ BACK, NOT MINTED. `--conversation <fresh-uuid>`
prints `warning: conversation "..." not found` on stderr and then silently
creates a DIFFERENT conversation, so a minted id would leave every turn
starting fresh while we believed it was resuming. Resuming a real id works --
a second turn recalled a codeword from the first.

DO NOT USE `--continue`. It means "the most recent conversation in this
directory": two agy roles in one repository would continue each OTHER's
conversation, and the symptom -- a checker that already knows what the runner
was told -- would take a long time to trace back here.

--------------------------------------------------------------------------
ACCESS, AND WHY THERE IS NO MIDDLE SETTING
--------------------------------------------------------------------------
Measured with one prompt and every flag set agy offers, in `-p` mode. The
last row was added after a real run failed on it:

    --sandbox                       run a command: DENIED   read a file: DENIED
    --mode accept-edits             run a command: DENIED
    --dangerously-skip-permissions  run a command: works    read a file: works
    --mode plan                     read a file: DENIED
                                    (denied_actions: read_file / ListDir)

`--mode plan` was the last candidate for a genuine read-only setting, and it
is not one: it refuses reads as firmly as --sandbox refuses commands. So this
CLI has a HOLE IN THE MIDDLE of the access range -- it can do FULL and it
cannot do READ_ONLY -- which `max_access` cannot express, because that field
assumes a backend able to do more can also do less.

`unsupported_access` below is what expresses it, and build_backends refuses
the combination at startup. Before that, every check_* verifier pointed here
spent about 90 seconds and 45,000 tokens to return SUCCESS with no answer,
and the shipped default put `checker` on this provider -- so every generated
research agent had four broken verifiers in it.

The node's declared `access` in nodes.json stays the policy boundary for the
levels this backend CAN serve, and the backend prints a line naming the flag
every time it uses --dangerously-skip-permissions, because a silent one is
the one thing here that can damage a machine.

Denial is at least clean: it refuses rather than blocking, arrives as a
step_update with state "ERROR" and a readable tool_info.error.message, and
the final envelope lists what was refused in `denied_actions`.

--------------------------------------------------------------------------
THE STREAM
--------------------------------------------------------------------------
`--output-format stream-json` discriminates on `event`, not `type`:

    {"event": "init",        "init": {model, cwd, tools}}
    {"event": "step_update", "step_update": {step_index, state, step_type,
                                             tool_name, tool_info, usage}}
    {"event": "result",      "result": {...the envelope above...}}

There are no text deltas -- no --include-partial-messages equivalent -- so the
live-text tail stays empty for this provider and the heartbeat falls back to
the last step it saw. Not a defect to fix here; the CLI does not send them.
"""

from __future__ import annotations

from agent.backends._cli import CliBackend, usage_from_keys
from agent.backends.base import Access, Usage

#: state -> the phase vocabulary _progress.py already uses, so headline(),
#: Turn.counts() and the web UI's activity panel work with no changes.
_PHASE = {"ACTIVE": "started", "DONE": "completed", "ERROR": "error"}


class AntigravityBackend(CliBackend):
    name = "antigravity"
    label = "Antigravity"
    executable_default = "agy"

    supports_repo_access = True
    #: FULL, raised from NONE. The CLI genuinely can do this, and max_access is
    #: a capability statement -- policy is the node's declared `access`, which
    #: _check_access enforces against this. Left at NONE, the research
    #: skeleton's run_exp_* nodes (which declare write) could not use it at all.
    max_access = Access.FULL
    #: The hole in the middle. MEASURED 2026-09-15, agy 1.1.27, all three
    #: settings, with a prompt that asks for one file to be read:
    #:
    #:   --sandbox                        denied_actions: RunCommand
    #:   --mode plan                      denied_actions: read_file (ListDir)
    #:   --dangerously-skip-permissions   works
    #:
    #: So there is no read-only mode: a node declared read_only would get a
    #: turn that can do nothing, return SUCCESS with no structured_output, and
    #: fail 90 seconds in. Declared here so build_backends refuses at startup
    #: and names the role instead.
    unsupported_access = frozenset({Access.READ_ONLY})

    def __init__(self, *, effort: str | None = None, **options):
        self.effort = effort
        self._warned_dangerous = False
        super().__init__(**options)

    # ---- vocabulary ------------------------------------------------------

    def argv(self, *, prompt, instructions, access, schema_path, session):
        # agy has no system-prompt flag, so the persona rides in the prompt.
        # Separated loudly, because the model has to be able to tell the
        # standing instructions from this turn's request.
        body = prompt
        if instructions.strip():
            body = (f"{instructions.strip()}\n\n"
                    f"{'=' * 70}\n\n{prompt}")

        argv = [self.executable, "-p", body,
                "--output-format", "stream-json",
                "--json-schema", schema_path,
                # Above our own idle timeout, so the CLI never gives up first:
                # its default is 5m and ours is 300s, which would race.
                "--print-timeout", f"{int(self.max_seconds)}s"]
        if self.model:
            argv += ["--model", self.model]
        if self.effort:
            argv += ["--effort", self.effort]
        argv += self.access_flags(access)
        return argv + session

    def access_flags(self, access: Access) -> list[str]:
        """See the module docstring: there is no middle setting.

        READ_ONLY never reaches here -- `unsupported_access` refuses it at
        startup. NONE still maps to --sandbox, and for NONE that IS honest: a
        node declared `none` is asking for a turn that touches nothing, and
        refusing every tool call is exactly that.
        """
        if access is Access.NONE:
            return ["--sandbox"]

        if not self._warned_dangerous:
            self._warned_dangerous = True
            print(f"[antigravity] {access.value} access runs `agy` with "
                  f"--dangerously-skip-permissions; it has no setting between "
                  f"that and refusing every tool call.", flush=True)
        return ["--dangerously-skip-permissions"]

    def session_args(self, thread_id):
        # minted=None: ids come BACK from agy, they are not accepted.
        return (["--conversation", thread_id] if thread_id else [], None)

    def is_envelope(self, event):
        return event.get("event") == "result"

    def record_for(self, event):
        kind = event.get("event")
        if kind == "init":
            return None
        if kind == "result":
            return None
        if kind != "step_update":
            return {"kind": str(kind or "unknown"), "phase": "other"}

        step = event.get("step_update") or {}
        phase = _PHASE.get(str(step.get("state")), "other")
        step_type = step.get("step_type")

        if step_type == "tool":
            info = step.get("tool_info") or {}
            params = info.get("parameters") or {}
            entry = {
                "kind": "command" if step.get("tool_name") == "run_command"
                        else "tool",
                "phase": phase,
                "command": params.get("CommandLine") or step.get("tool_name", ""),
            }
            error = info.get("error") or {}
            if error:
                entry["output"] = str(error.get("message", ""))[:4000]
                entry["exit_code"] = 1
            return entry

        if step_type == "agent_response":
            return {"kind": "reasoning", "phase": phase,
                    "summary": [f"step {step.get('step_index')}"]}
        return None

    def final_text(self, envelope):
        import json

        result = envelope.get("result") or envelope
        parsed = result.get("structured_output")
        return json.dumps(parsed) if parsed is not None else ""

    def _why_empty(self, envelope):
        """Say what was actually refused, and do NOT suggest widening a verifier.

        The envelope carries `denied_actions` -- measured:
        [{"action": "command", "display_name": "RunCommand"}] -- so the turn
        can be named instead of guessed at.

        The advice this replaced was "give the node write access". For the
        node that hits this most, a `check_*` verifier, that is the one change
        it must not make: a verifier is read_only BY DESIGN, and
        _verification_problems identifies one structurally as read_only with
        no steps that is the source of a branch. Widening it stops it counting
        as a verifier at all, and hands the judge the power to fix the work it
        is judging -- which is the single rule this project's graphs are built
        around.
        """
        result = envelope.get("result") or envelope
        denied = result.get("denied_actions") or []
        names = sorted({str(d.get("display_name") or d.get("action"))
                        for d in denied if isinstance(d, dict)})
        refused = (f"agy refused {', '.join(names)}"
                   if names else
                   f"agy reported status={result.get('status')!r} and sent no "
                   f"structured_output, which is what a refused tool call "
                   f"looks like")

        return (
            f"{refused}.\n\n"
            f"This is agy's permission model, not the model failing. It has no "
            f"setting between --dangerously-skip-permissions and refusing "
            f"every tool call, so a node that must read files or run commands "
            f"cannot run on this provider under read_only.\n\n"
            f"Point the role at a provider that can, which for a verifier is "
            f"the right fix and costs one line:\n"
            f'    {{"roles": {{"checker": {{"provider": "codex"}}}}}}\n'
            f"in <repo>/.agent/config.json.\n\n"
            f"Do NOT give a check_* node write access to get past this. A "
            f"verifier is read_only by design -- that is what stops the judge "
            f"fixing the work it is judging, and it is how the validator "
            f"recognises a verifier at all."
        )

    def usage_from(self, envelope) -> Usage | None:
        result = envelope.get("result") or envelope
        usage = result.get("usage")
        if not usage:
            return None
        return usage_from_keys(
            usage,
            input_tokens="input_tokens",
            output_tokens="output_tokens",
            reasoning_tokens="thinking_tokens",
            cached_input_tokens="cache_read_tokens",
        )

    def session_id_from(self, envelope):
        result = envelope.get("result") or envelope
        return result.get("conversation_id")
