# Prompts

Every prompt a model is given lives here, as Markdown, not in the code:

- `roles/bootstrap/<role>.md` and `roles/execute/<role>.md`: each role's
  standing instructions, sent with every message.
- `roles/autopilot.md`: the autopilot's.
- `notes/*.md`: the longer notes the code sends a role, such as "a new
  design is ready", or "a retry answer must be one JSON object".

**To change one, copy it, don't edit it here.** The first file found wins:

| where | for |
| --- | --- |
| `<run>/prompts/<same path>` | that run only (read when the run opens; restart the web server to pick up a change) |
| `$RESEARCH_AGENT_PROMPTS/<same path>` | every run of yours |
| here | the defaults |

`$name` placeholders in a note are filled in by the code that sends it;
each file's `<!-- ... -->` comment says what they are and is never sent.
Write `$$` for a dollar sign.

Keep a role's instructions short: they go with every message, and Codex
has been measured to hang past about 6 KB. Things about the study belong
in the brief the code builds (`views.py`), not here.

Short validation messages ("R7 is not a rubric id") stay in the code,
next to the checks they belong to.
