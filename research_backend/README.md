# research_backend

Talk to Codex, Claude Code and Antigravity the same way: which models they
offer, how much of their usage limits is left, and conversations you can
continue by id, from any script, at any time.

```python
import research_backend as rb

rb.providers()              # ['antigravity', 'claude_code', 'codex', 'fake']
print(rb.models("codex"))   # the models codex will accept
print(rb.usage())           # every provider's limits, and when they reset
print(rb.status())          # who is logged in, and how to log in if not
```

```
codex (team plan)
  5 hours    0% used, resets Fri 23 Oct 14:30 (in 4h 59m)
  weekly     5% used, resets Thu 29 Oct 17:17 (in 6d 8h)
  no credits; 2 free limit resets available
  [codex app-server (account/rateLimits/read), as of Fri 23 Oct 09:30]
```

## Sessions

A session is one conversation with one model. It is saved after every turn, so
you can continue it by its id from another script, days later.

```python
s = rb.new_session("codex", model="gpt-5.6-sol", name="lit-review",
                   instructions="You are a careful research assistant.")
print(s.send("Which datasets exist for hotel review classification?"))

# later, anywhere:
print(rb.send("lit-review", "Which of those are free to use?"))
print(rb.session("lit-review").history())
print(rb.sessions())        # every session, most recently used first
```

`new_session` takes:

| argument | meaning | default |
| --- | --- | --- |
| `provider` | `"codex"`, `"claude_code"`, `"antigravity"` or `"fake"` | required |
| `model` | an id from `rb.models(provider)` | the provider's default |
| `name` | the session's id | provider + time, e.g. `codex-20261023-093047-1f2e` |
| `workdir` | the folder the model works in | a new empty folder in the session's |
| `access` | `"none"`, `"read_only"`, `"write"` or `"full"` | `read_only` (`none` on antigravity, which cannot do read-only) |
| `instructions` | standing instructions, sent with every message | none |
| anything else | passed to the provider: `timeout`, `max_seconds`, `gpu`, ... | |

`send(prompt)` returns a `Reply`:

| field | |
| --- | --- |
| `text` | the answer (printing the reply prints this) |
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

reply = s.send("Pick the best one.", schema=Pick)
reply.data.dataset
```

`send(..., progress=True)` prints what the model is doing while it works.
A session answers one message at a time. Sending to a session that is already
answering raises `SessionBusy`, whichever process sent the first message.
A failed turn is recorded in the session and raised: `BackendBusy`,
`BackendOutOfCredits`, `BackendTimeout`, and the other errors.

Other methods: `s.turns` (every turn, as dicts), `s.rename(new_id)`,
`s.delete()`. Deleting removes the session's own folder; a `workdir` you chose
is left alone.

## Where things are kept

```
~/.research_backend/            (or $RESEARCH_BACKEND_HOME)
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

None of `models`, `usage` and `status` raise. A provider that cannot answer
comes back with `error` saying why.

## The code

```
research_backend/
    __init__.py       the functions above
    conversation.py   Session, Reply: sending, continuing, saving
    catalog.py        models()
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
