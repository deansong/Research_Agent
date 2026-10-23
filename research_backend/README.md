# research_backend

Talk to Codex, Claude Code and Antigravity the same way: which models they
offer, how much of their usage limits is left, and conversations you can
continue by id, from any script, at any time.

Each provider is a `Provider` object, set up once. Each conversation is a
`Session` it opens. Use as many of both as you like, side by side, in threads
if you want them at the same time.

```python
from research_backend import Provider

codex  = Provider("codex", model="gpt-5.6-sol")
claude = Provider("claude_code", model="opus", access="write")
agy    = Provider("antigravity")

print(codex.models())     # the models codex accepts
print(codex.usage())      # its limits, and when they reset
print(claude.status())    # logged in? how to log in if not

reader = codex.new_session("lit-review")
writer = claude.new_session("draft", instructions="You write LaTeX.")
quick  = codex.new_session("scratch", model="gpt-5.6-luna")   # another codex session

print(reader.send("Which datasets exist for hotel review classification?"))
print(writer.send("Draft a related-work paragraph."))

# later, from any script:
codex.session("lit-review").send("Which of those are free to use?")
print(codex.sessions())   # codex's sessions
```

```
codex (team plan)
  5 hours    0% used, resets Fri 23 Oct 14:30 (in 4h 59m)
  weekly     5% used, resets Thu 29 Oct 17:17 (in 6d 8h)
  no credits; 2 free limit resets available
  [codex app-server (account/rateLimits/read), as of Fri 23 Oct 09:30]
```

For every provider at once:

```python
import research_backend as rb

rb.providers()            # ['antigravity', 'claude_code', 'codex', 'fake']
print(rb.usage())         # every provider's limits
print(rb.status())        # every provider's login
print(rb.sessions())      # every session, whichever provider
rb.session("lit-review")  # one, by id
```

## Provider

`Provider(name, model=None, access=None, instructions="", home=None, **options)`

| argument | meaning | default |
| --- | --- | --- |
| `name` | `"codex"`, `"claude_code"`, `"antigravity"` or `"fake"` | required |
| `model` | an id from `.models()` | the provider's default |
| `access` | `"none"`, `"read_only"`, `"write"` or `"full"` | `read_only` (`none` on antigravity, which cannot do read-only) |
| `instructions` | standing instructions, sent with every message | none |
| `home` | where its sessions are kept | `~/.research_backend` |
| anything else | passed to the provider: `timeout`, `max_seconds`, `gpu`, ... | |

An unknown provider, or an access level it cannot do, fails here rather than
at the first message.

| method | |
| --- | --- |
| `.models()` | the models it accepts: loop over them, or print |
| `.usage()` | its limits: `.windows` (each with `used_percent`, `resets_at`), `.plan`, `.credits` |
| `.status()` | whether it is installed and logged in |
| `.new_session(name=None, model=, workdir=, access=, instructions=, **options)` | a new conversation; anything not given comes from the Provider |
| `.session(id)` | one of its sessions, to continue it |
| `.sessions()` | its sessions, the most recently used first |

`workdir` is the folder the model works in. Without one, each session gets a
new empty folder of its own. Without a `name`, the id is made up from the
provider and the time, e.g. `codex-20261023-093047-1f2e`.

## Session

One conversation with one model, saved after every turn. Its id is what you
continue it by. It has `.id`, `.provider`, `.model`, `.access`, `.workdir`,
`.instructions` and `.thread_id`, the provider's own id for the conversation.

`.send(prompt)` returns a `Reply`:

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

reply = reader.send("Pick the best one.", schema=Pick)
reply.data.dataset
```

`send(..., progress=True)` prints what the model is doing while it works;
otherwise turns are quiet, and the same detail is in the reply's `events`.
A session answers one message at a time. Sending to a session that is already
answering raises `SessionBusy`, whichever process sent the first message.
A failed turn is recorded in the session and raised: `BackendBusy`,
`BackendOutOfCredits`, `BackendTimeout`, and the other errors.

Other methods: `.history()` (the conversation as text), `.turns` (every turn,
as dicts), `.rename(new_id)` and `.delete()`. Deleting removes the session's own folder; a `workdir` you chose
is left alone.

## Where things are kept

```
~/.research_backend/            (or $RESEARCH_BACKEND_HOME, or a Provider's home=)
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
    __init__.py       the every-provider functions
    provider.py       Provider
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
