# research_backend

Talk to Codex, Claude Code and Antigravity the same way: which models they
offer, how much of their usage limits is left, and conversations you can
continue by id, from any script, at any time.

```python
import research_backend as RB

RB.list_providers()          # ['antigravity', 'claude_code', 'claude_strata', 'codex', 'codex_strata', 'fake', 'strata']
RB.list_models("codex")      # the models codex accepts, and each one's effort levels
RB.list_efforts("codex")     # ['low', 'medium', 'high', 'xhigh', 'max', 'ultra']
RB.list_skills("skills")     # the skills in a folder of <name>/SKILL.md
RB.usage()                   # every provider's limits, and when they reset
RB.status()                  # who is logged in, and how to log in if not
RB.list_sessions()           # every session so far

reader = RB(provider="codex", model="gpt-5.6-sol", effort="high")   # or RB(), then set_provider
reader.list_models()
reader.set_effort("xhigh")   # from the next message on
reader.new_session("lit-review")
reader.session_id            # 'lit-review'
reader.send("Which datasets exist for hotel review classification?")
reader.context()             # how full its context window is, and the last turns
reader.usage()               # codex's limits

writer = RB(provider="claude_code", model="opus", access="write")   # side by side
writer.send("Draft a related-work paragraph.")      # starts a session if none

later = RB(session="lit-review")                    # continue it, from any script
later.send("Which of those are free to use?")
```

Everything prints readably:

```
>>> print(reader.usage())
codex (team plan)
  5 hours    0% used, resets Fri 23 Oct 14:30 (in 4h 59m)
  weekly     5% used, resets Thu 29 Oct 17:17 (in 6d 8h)
  no credits; 2 free limit resets available
  [codex app-server (account/rateLimits/read), as of Fri 23 Oct 09:30]

>>> print(reader.context())
lit-review: codex/gpt-5.6-sol, 3 turn(s)
context: about 15,857 of 258,400 tokens (6.1%)

--- turn 1 (23 Oct 09:30) ---
you: Which datasets exist for hotel review classification?
codex: ...
```

## RB(...)

`import research_backend as RB`, then `RB(...)` makes a `ResearchBackend`: a
provider, a model and the current session. Make one for each conversation you
want open at once.

`RB(provider=None, model=None, *, effort=None, skills=None, skills_dir=None, session=None, access=None, instructions="", workdir=None, home=None, **options)`

| argument | meaning | default |
| --- | --- | --- |
| `provider` | `"codex"`, `"codex_strata"`, `"strata"`, `"claude_code"`, `"claude_strata"`, `"antigravity"` or `"fake"` | none yet: `set_provider()` later |
| `model` | an id from `list_models()` | the provider's default |
| `effort` | how hard the model thinks: a level from `list_efforts()` | the model's default |
| `skills` | which skills the model gets: `None`, `"auto"`, or a list of names (see Skills) | `None` |
| `skills_dir` | the folder of skills | `$RESEARCH_BACKEND_SKILLS_DIR` |
| `session` | continue this session; its provider and model come with it | none yet |
| `access` | `"none"`, `"read_only"`, `"write"` or `"full"` | `read_only` (`none` on antigravity, which cannot do read-only) |
| `instructions` | standing instructions, sent with every message | none |
| `workdir` | the folder the model works in | a new empty folder for each session |
| `home` | where sessions are kept | `~/.research_backend` |
| anything else | passed to the provider: `timeout`, `max_seconds`, `gpu`, ... | |

An unknown provider, or an access level a provider cannot do, fails right
away rather than at the first message.

| | |
| --- | --- |
| `.provider`, `.model`, `.session_id` | what is set now |
| `.list_providers()` | the providers there are |
| `.list_models(provider=None)` | the models a provider accepts (the current one unless named) |
| `.usage(provider=None)` | its limits: `.windows` (each with `used_percent`, `resets_at`), `.plan`, `.credits` |
| `.status(provider=None)` | whether it is installed and logged in |
| `.set_provider(name, model=None)` | talk to another provider; the current session is put aside, since a session belongs to one provider |
| `.set_model(model)`, `.set_effort(level)`, `.set_access(level)`, `.set_instructions(text)` | from the next message on; the current session carries on with them |
| `.set_schema(schema)` | the shape of every answer from now on: a JSON schema or a Pydantic class; `None` for plain text |
| `.list_efforts(provider=None, model=None)` | the effort levels a model has (the current one unless named) |
| `.set_skills(skills)`, `.set_skills_dir(folder)`, `.list_skills()` | choose skills from the next message on; list the folder's, the chosen ones marked |
| `.new_session(name=None, workdir=None, exist_ok=False)` | start a conversation and make it current; returns its id, made up if you give no name. A name already taken is refused, unless `exist_ok=True`, which continues that session |
| `.open_session(id)` | continue an earlier session; its provider and model become current |
| `.list_sessions(provider=None)` | every session, or one provider's, most recently used first; prints as a table |
| `.close_session()` | stop using the current session, and keep it; the next `send()` starts a new one |
| `.send(prompt, schema=None, progress=False)` | send a message and wait for the `Reply`; starts a session if there is none; `schema` shapes this answer only |
| `.context()` | how much of the model's context window the conversation fills, and its last turns |
| `.history(last=None)` | the conversation so far, as text |
| `.delete_session(id=None)` | delete a session for good, the current one unless named |
| `.session` | the current `Session` itself: `.turns`, `.folder`, `.thread_id`, `.rename()` |

The `set_...` methods return the object, so they chain:
`RB().set_provider("codex").set_model("gpt-5.6-sol")`.

## Listing, continuing, closing

```python
print(RB.list_sessions())            # or RB.list_sessions("codex")
```

```
id          provider     model        effort   turns  last used     last message
lit-review  codex        gpt-5.6-sol  high     3      23 Oct 10:06  Which of those are free to use?
draft       claude_code  opus         default  1      23 Oct 09:31  Draft the introduction.
```

```python
reader = RB(session="lit-review")    # continue one: its provider, model, effort come with it
reader.open_session("draft")         # or switch an existing RB to another one
reader.new_session("lit-review", exist_ok=True)   # continue it if it exists, else start it

reader.close_session()               # stop using it; it is kept, continue it any time
reader.delete_session("draft")       # gone for good
```

Nothing has to be closed before a script ends: every turn is saved as it
happens, so any session can be continued later. Closing only means this `RB`
stops sending to it.

## Skills

A skill is a folder with a `SKILL.md`, the format Claude Code and Antigravity
use. Point RB at a folder of them and choose which ones a session gets:

```
skills/
  statistical-power/
    SKILL.md          ---
                      name: statistical-power
                      description: When to use it, in a sentence or two.
                      ---
                      The instructions, in Markdown.
    power.py          anything else it needs
```

```python
RB.list_skills("skills")               # what is there: name and description

reader = RB(provider="codex", skills_dir="skills", skills="auto")
writer = RB(provider="claude_code", skills_dir="skills", skills=["acl-paper", "academic-plotting"])

reader.set_skills(["statistical-power"])   # from the next message on
reader.set_skills(None)                    # no skills
print(reader.list_skills())                # the folder's skills, chosen ones marked *
```

| `skills=` | the model gets |
| --- | --- |
| `None` (default) | no skills |
| `"auto"` | every skill's name, description and `SKILL.md` path; it reads the ones that fit |
| a list of names | those skills' full text |

- **How skills are sent:** with the first message of a session, in front of
  your text, and again only when you change the choice. They are not sent in
  the instructions, which go with every message (and Codex hangs on long
  instructions).
- **The folder is readable** on every turn that has skills. Claude Code is given
  `--add-dir` for it; the other providers can read it already. The model is
  told not to change it.
- **Placeholders** filled in a skill's text: `{skill_dir}` (its folder),
  `{skills_dir}`, `{python}` (the running Python) and `{workspace}` (the
  session's folder). Any other `{...}`, such as LaTeX, is left as written.
- **Kept with the session:** `RB(session=...)` continues with the same skills,
  and does not send them again.
- **Errors:** an unknown skill name is refused when you set it, listing what
  the folder has.
- **Where the folder comes from:** `skills_dir=`, else
  `$RESEARCH_BACKEND_SKILLS_DIR`.
- **What you see:** `history()` shows your messages as you wrote them; each
  turn records which skills went with it.

## Thinking effort

`effort` sets how hard the model reasons before it answers. The levels are
the provider's own, so they differ:

| provider | levels | where they come from |
| --- | --- | --- |
| codex | per model, e.g. `gpt-5.6-sol`: low, medium, high, xhigh, max, ultra (default medium) | the app-server's model list, so `RB.list_models("codex")` shows each model's |
| claude_code | low, medium, high, xhigh, max | `claude --effort` |
| antigravity | low, medium, high, xhigh, max | `agy --effort` |
| strata, codex_strata | none (thinking off), low, medium, high; default medium (the `strata` profile's `model_reasoning_effort`) | the Responses API's `reasoning.effort`; measured: low ~170, high ~300 reasoning tokens on the same problem |

A level the model does not have is refused when you set it, naming the ones
it has. Leaving it out, or `set_effort(None)`, uses the model's default. It is
kept with the session, so `RB(session=...)` continues at the same effort. A
set effort does not follow `set_provider()` to another provider, because the
levels differ.

## Replies

`.send()` returns a `Reply`; printing it prints the answer.

| field | |
| --- | --- |
| `text` | the answer |
| `data` | with `schema=`, the validated object |
| `tokens` | input, cached, output and reasoning tokens, and the context window |
| `seconds` | how long the turn took |
| `events` | what the model did on the way: commands, file changes, reasoning |

## Structured answers: a JSON schema

Give a JSON schema and the answer comes back as a dict that matches it. The
schema can be a dict, JSON text, or the path of a `.json` file:

```python
papers = {
    "type": "object",
    "properties": {
        "papers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "year": {"type": "integer"},
                    "kind": {"type": "string", "enum": ["method", "dataset", "survey"]},
                },
                "required": ["title", "year", "kind"],
            },
        }
    },
    "required": ["papers"],
}

reply = reader.send("Name two papers on LoRA.", schema=papers)    # this answer only
reply.data["papers"][0]["title"]          # 'LoRA: Low-Rank Adaptation of Large Language Models'

reader.set_schema("schemas/papers.json")  # every answer from now on
reader.set_schema(None)                   # plain text again
```

A Pydantic class works the same way, and then `reply.data` is an instance of it:

```python
from pydantic import BaseModel

class Pick(BaseModel):
    dataset: str
    why: str

reader.send("Pick the best one.", schema=Pick).data.dataset
```

The rules:

- **The top level must be an object** (`"type": "object"`): the providers answer with a JSON object. For a list, put it in a property, as `papers` is above.
- **Strict mode:** the schema sent is tightened the way Codex requires. Every property is required, and no extra properties are allowed. So an optional field comes back present, possibly empty, rather than missing.
- **Checked twice:** the provider enforces the schema, and the answer is checked again against your schema as written. A bad schema is refused when you set it, and an answer that does not match raises `BackendOutputError`.

`reply.text` is the answer as JSON text either way.

`send(..., progress=True)` prints what the model is doing while it works;
otherwise turns are quiet, and the same detail is in the reply's `events`.
A session answers one message at a time. Sending to a session that is already
answering raises `SessionBusy`, whichever process sent the first message.
A failed turn is recorded in the session and raised: `BackendBusy`,
`BackendOutOfCredits`, `BackendTimeout`, and the other errors.

`context()` counts the last turn's prompt: Codex and Antigravity report cached
tokens inside their input count, and Claude Code reports them separately, so
the two are added for it. The window is the one the provider reports (Codex),
else a per-model table in `catalog.py`.

## Where things are kept

```
~/.research_backend/            (or $RESEARCH_BACKEND_HOME, or RB(home=...))
    sessions/<id>/session.json  the session and every turn, as indented JSON
    sessions/<id>/workspace/    its working folder, unless you gave one
    limits/claude_code.json     the last limits a Claude Code turn reported
```

## How each provider answers

| | models | usage | continuing a conversation |
| --- | --- | --- | --- |
| codex | the app-server's `model/list` | the app-server's `account/rateLimits/read` | the Codex thread id |
| claude_code | a built-in list (`claude` cannot list them) | the last `rate_limit_event` seen during a turn | `claude --resume <id>` |
| antigravity | `agy models` | `agy -p /quota` | `agy --conversation <id>` |
| codex_strata | the model the `strata` profile names | none | the Codex thread id |
| strata | the server's `GET /models`, with `strata` first | none | the conversation is resent each turn, from `~/.research_backend/responses_threads/<thread>.json` |
| fake | one placeholder | made up | an echo that counts its turns |

### The Strata providers

All three run the Strata model, a Qwen model served at
`http://143.167.8.120:21435/v1` with the Responses API. Its key is read from
`STRATA_API_KEY` in the environment, and is never stored by research_backend.

- **`codex_strata`** is Codex with its `strata` profile,
  `$CODEX_HOME/strata.config.toml`. It has Codex's tools, sandbox and access
  levels, so it can serve any role, files and commands included. The profile
  is read here and passed as `--config` overrides (its model settings and its
  `[model_providers.*]` tables): `codex --profile` exists only in newer Codex
  binaries, and the app-server the SDK starts takes no profile. It gets a
  Codex client of its own.
- **`claude_strata`** is Claude Code pointed at the same server, which also
  answers Anthropic's `/v1/messages`: Claude Code's tools, permissions and
  sessions on the Strata model. The backend sets `ANTHROPIC_BASE_URL` (the
  server without `/v1`), `ANTHROPIC_AUTH_TOKEN` (from `STRATA_API_KEY`) and
  the model for every slot the CLI fills, after it scrubs the parent's
  `ANTHROPIC_*` -- nothing to set but `STRATA_API_KEY`. No cost is reported
  (the CLI prices the tokens as Anthropic's), and `claude` prints
  `unrecognized_model`, which is harmless.
- **`strata`** talks to the server directly: no agent, no files, no tools
  (`max_access` is `none`), so a call that asks for `read_only` or more is
  refused. It suits the roles that only think about text. A schema is sent as
  a strict `json_schema` format; the reply streams, so `on_progress` sees it
  being written. `STRATA_BASE_URL` points it at another server.

None of `list_models`, `usage` and `status` raise. A provider that cannot answer
comes back with `error` saying why.

## The code

```
research_backend/
    __init__.py       RB.list_providers() and the other every-provider functions;
                      makes the module callable
    client.py         ResearchBackend -- what RB(...) returns; Context
    conversation.py   Session, Reply: sending, continuing, saving
    catalog.py        list_models()
    limits.py         usage()
    skills.py         list_skills(); reading SKILL.md; what a session is sent
    auth.py           status()
    quota.py          waiting out a usage limit during a turn
    base.py           what every backend implements, Access, Usage, the errors
    home.py           ~/.research_backend
    backends/         one module per provider; make() builds one by name
        codex.py  claude_code.py  antigravity.py  responses.py (strata)
        api.py (not written yet)  fake.py
        _cli.py       what the two CLI-driven backends share
        _progress.py  turning provider events into readable lines
        _schema.py    the strict JSON schema the providers need
```

## Installing

From the repository root, `import research_backend` works as it is. To use it
from anywhere, run `pip install -e .`. Codex needs the `openai-codex` package
(`pip install -e '.[codex]'`). Claude Code and Antigravity need only their
commands, `claude` and `agy`, on PATH and logged in.
