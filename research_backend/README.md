# research_backend

Talk to Codex, Claude Code and Antigravity the same way: which models they
offer, how much of their usage limits is left, and conversations you can
continue by id, from any script, at any time.

```python
import research_backend as RB

RB.list_providers()          # ['antigravity', 'claude_code', 'codex', 'fake']
RB.list_models("codex")      # the models codex accepts, and each one's effort levels
RB.list_efforts("codex")     # ['low', 'medium', 'high', 'xhigh', 'max', 'ultra']
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

`RB(provider=None, model=None, *, effort=None, session=None, access=None, instructions="", workdir=None, home=None, **options)`

| argument | meaning | default |
| --- | --- | --- |
| `provider` | `"codex"`, `"claude_code"`, `"antigravity"` or `"fake"` | none yet: `set_provider()` later |
| `model` | an id from `list_models()` | the provider's default |
| `effort` | how hard the model thinks: a level from `list_efforts()` | the model's default |
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
| `.list_efforts(provider=None, model=None)` | the effort levels a model has (the current one unless named) |
| `.new_session(name=None, workdir=None)` | start a conversation and make it current; returns its id, made up if you give no name |
| `.open_session(id)` | continue an earlier session; its provider and model become current |
| `.list_sessions(provider=None)` | every session, or one provider's, most recently used first |
| `.send(prompt, schema=None, progress=False)` | send a message and wait for the `Reply`; starts a session if there is none |
| `.context()` | how much of the model's context window the conversation fills, and its last turns |
| `.history(last=None)` | the conversation so far, as text |
| `.delete_session(id=None)` | forget a session, the current one unless named |
| `.session` | the current `Session` itself: `.turns`, `.folder`, `.thread_id`, `.rename()` |

The `set_...` methods return the object, so they chain:
`RB().set_provider("codex").set_model("gpt-5.6-sol")`.

## Thinking effort

`effort` sets how hard the model reasons before it answers. The levels are
the provider's own, so they differ:

| provider | levels | where they come from |
| --- | --- | --- |
| codex | per model, e.g. `gpt-5.6-sol`: low, medium, high, xhigh, max, ultra (default medium) | the app-server's model list, so `RB.list_models("codex")` shows each model's |
| claude_code | low, medium, high, xhigh, max | `claude --effort` |
| antigravity | low, medium, high, xhigh, max | `agy --effort` |

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

Ask for a structured answer with a Pydantic class:

```python
from pydantic import BaseModel

class Pick(BaseModel):
    dataset: str
    why: str

reply = reader.send("Pick the best one.", schema=Pick)
reply.data.dataset
```

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
| fake | one placeholder | made up | an echo that counts its turns |

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
    auth.py           status()
    quota.py          waiting out a usage limit during a turn
    base.py           what every backend implements, Access, Usage, the errors
    home.py           ~/.research_backend
    backends/         one module per provider; make() builds one by name
        codex.py  claude_code.py  antigravity.py  api.py (not written yet)  fake.py
        _cli.py       what the two CLI-driven backends share
        _progress.py  turning provider events into readable lines
        _schema.py    the strict JSON schema the providers need
```

The research agent in `agent/` uses these backends too. Its old import paths
(`agent.backends.codex`, ...) still work: they are now these modules.

## Installing

From the repository root, `import research_backend` works as it is. To use it
from anywhere, run `pip install -e .`. Codex needs the `openai-codex` package
(`pip install -e '.[codex]'`). Claude Code and Antigravity need only their
commands, `claude` and `agy`, on PATH and logged in.
