# Step 1 probe results (measured 2026-09-08)

## claude 2.1.252 — BLOCKED
- `-p` **waits on stdin**: "no stdin data received in 3s". Must pass `< /dev/null`
  (or close the child's stdin) or every turn stalls 3s and risks hanging.
- Nested invocation inherits ~76 `CLAUDE_*`/`ANTHROPIC_*` env vars and reuses the
  PARENT's session_id. Backend must scrub the environment.
- **`is_error: true` arrives with exit code 0.** The exit code is not a failure
  signal; the envelope field is.
- Envelope (from the error case; same shape on success):
  `{type:"result", subtype, result:<text>, session_id, total_cost_usd,
    usage:{input_tokens, output_tokens, cache_read_input_tokens,
           cache_creation_input_tokens, output_tokens_details.thinking_tokens},
    is_error, num_turns, permission_denials, duration_ms, uuid}`
- **Auth expired**: "Failed to authenticate: OAuth session expired and could not
  be refreshed". ~/.claude/.credentials.json last written Aug 21. Needs an
  interactive `claude` login. Success-path envelope UNVERIFIED because of this.

## agy 1.1.27 — WORKS, fully probed
- `--json-schema` accepts a **file path** and the **strict dialect**
  (`required` listing every property, `additionalProperties:false`).
- Envelope: `{conversation_id, status, response, duration_seconds, num_turns,
  structured_output, json_schema, usage}`.
- **Use `structured_output`, not `response`.** `response` carried extra keys the
  model added (`toolAction`, `toolSummary`); `structured_output` is parsed and
  filtered to the schema.
- usage: `input_tokens, output_tokens, thinking_tokens, cache_read_tokens,
  total_tokens`. No cost field.
- **`status:"SUCCESS"` does NOT mean there is an answer.** A denied tool call
  gives SUCCESS, `response:""`, and no `structured_output`. Presence of
  `structured_output` is the real success test.
- Conversation ids are **read-back, not mintable**. `--conversation <fresh-uuid>`
  warns `conversation "..." not found` on stderr, then silently creates a
  DIFFERENT id. Resuming a real id works (recalled a codeword across turns).
- stream-json discriminator is **`event`**, not `type`: `init`, `step_update`,
  `result`. `step_update` carries `{step_index, state: ACTIVE|DONE|ERROR,
  step_type: user_input|agent_response|tool, tool_name, tool_info, usage}`.
  No text deltas — there is no `--include-partial-messages` equivalent, so the
  live-text tail will stay empty for this provider.

### The permission finding that changes the plan
In `-p` mode, measured with the same prompt each time:

| flags | run a command | read a file |
| --- | --- | --- |
| `--sandbox` | denied | denied |
| `--mode accept-edits` | denied | (not retested) |
| `--dangerously-skip-permissions` | **works** | works |

There is no middle setting. Any agy node that must touch the filesystem needs
`--dangerously-skip-permissions`. Denial is clean (no hang) and surfaces as a
step_update with `state:"ERROR"` and a readable `tool_info.error.message`.

---

# Login probes and flows (measured 2026-09-14)

Against codex 0.153.4, claude 2.1.252, agy 1.1.27. Each row was run twice: once
normally, and once with `HOME` pointed at an empty directory to get the
signed-out behaviour without disturbing a real login.

## Asking whether a provider is signed in

| provider | command | signed in | signed out |
| --- | --- | --- | --- |
| codex | `codex login status` | exit 0, `Logged in using ChatGPT` | exit 1, `Not logged in` |
| claude_code | `claude auth status --json` | `{"loggedIn":true,"authMethod":…}` | `{"loggedIn":false,"authMethod":"none"}` |
| antigravity | `agy models` | exit 0, the model list | exit 1, `Please sign in to view available models.` |

- **`claude auth status` exits 0 either way.** The JSON body is the only signal.
  It also prints `projectsDirectory`, which is a quick way to see *which*
  config directory it read.
- **codex and agy both print something ahead of the answer** — a PATH warning,
  `Fetching available models...` — and both put the whole thing on **stderr**
  with stdout empty. So the verdict is the *last* line, not the first.
- `agy` has no login, logout or auth subcommand at all. `agy models` is a
  network call that needs the token, which is what makes it a usable probe.
- The probe must run in the environment the *backend* will use: `_cli.py`
  scrubs `CLAUDE_*`, so a probe that keeps `CLAUDE_CONFIG_DIR` reads a
  different credentials file and answers about a different account. `codex.py`
  scrubs nothing, so its probe must not either.

## Driving a login

Both refuse to print **anything** when stdout is a pipe — they check `isatty()`
and render an interactive flow or nothing. On a pty both print immediately.
`pty.openpty()` handed to an ordinary `Popen` with `start_new_session=True` is
enough; `pty.fork()` is not needed.

| provider | flow | driveable from a browser |
| --- | --- | --- |
| codex | `codex login --device-auth` → prints a URL and a code like `CW4P-ZJ3G9`, then polls | yes |
| claude_code | `claude auth login` → prints a URL, then blocks on `Paste code here if prompted >` | yes |
| antigravity | bare `agy` → alternate screen buffer (`\e[?1049h`), cursor hide, kitty keyboard protocol | **no** |

- The default `codex login` (no `--device-auth`) opens a browser **on the
  server** and waits on a localhost callback there — wrong when the page is
  being viewed through an SSH tunnel. A device code travels.
- A pty translates every `\n` to CRLF on output. A tool that writes its own
  `\r\n` therefore arrives as `\r\r\n`.
- codex **indents** the device code under its numbered step.
- Those two together hid the code from a "code alone on its line" pattern
  twice, from a transcript that displayed it perfectly both times.
- `claude` echoes `Invalid code. Please make sure the full code was copied.`
  and **stays running**, so a bad paste can simply be retried.

## The one that can cost you a login

**`codex login --device-auth` deletes `~/.codex/auth.json` the moment it
starts**, not when it succeeds. Confirmed by checksum: present before, gone
during the flow, and the process had done nothing but print a code.

`claude auth login` does **not** — a dummy `.credentials.json` survived the
flow byte-identical.

So any UI offering "sign in again" has to snapshot the credential files first
and restore them on any outcome that is not a confirmed login. `webui/auth.py`
does; verified live, with `codex login status` agreeing afterwards.
