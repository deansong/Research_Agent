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
