"""
WHAT:  "Is this provider logged in?", answered the same way for every provider.
WHY:   A logged-out provider fails in the middle of a turn, minutes in, with a
       message about the model rather than about the login.  Asking first turns
       that into one line before anything is spent.
CONCEPT: Not LangGraph.  A read-only probe per provider, plus what to run to
       fix it -- deliberately separate from the backends, because you need this
       answer BEFORE a backend can be constructed.

--------------------------------------------------------------------------
MEASURED, 2026-09-14, against codex 0.153.4, claude 2.1.252, agy 1.1.27
--------------------------------------------------------------------------
Each row cost a run of the real binary, logged in and (with HOME pointed at an
empty directory) logged out:

    provider      probe                      logged in        logged out
    codex         codex login status         exit 0           exit 1
                                             "Logged in using ChatGPT"
                                                              "Not logged in"
    claude_code   claude auth status --json  {"loggedIn":true, {"loggedIn":false,
                                              "authMethod":..} "authMethod":"none"}
    antigravity   agy models                 exit 0, the      exit 1, "Please
                                             model list       sign in to view
                                                              available models."

Three consequences, all load-bearing:

1. `claude auth status` EXITS 0 EITHER WAY.  Reading the exit code reports a
   logged-out install as logged in.  The JSON body is the signal -- the same
   lesson as `is_error` in _cli.py, from the same binary.

2. THE PROBE MUST SEE THE CREDENTIALS THE BACKEND WILL USE.  _cli.py scrubs
   CLAUDE_*/ANTHROPIC_*/CODEX_* from its children (measured note 2 there), so
   `claude` under a backend reads ~/.claude.  Probing WITHOUT that scrub reads
   $CLAUDE_CONFIG_DIR instead -- a different file, and a different answer.
   codex.py scrubs nothing, so its probe must not either.  Hence `scrub` is
   per provider and mirrors the backend rather than being one global rule.

3. ANTIGRAVITY HAS NO STATUS COMMAND.  `agy --help` lists no login, logout or
   auth subcommand; signing in happens by launching the bare TUI.  `agy models`
   is a network call that needs the token, so it answers the question -- at the
   cost of ~2s and a request, which is why nothing here polls.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass

#: Long enough for a network round trip (`agy models` took 2.1s), short enough
#: that a wedged binary does not hold a page open.
PROBE_TIMEOUT = 20.0


@dataclass(frozen=True)
class AuthStatus:
    """One provider's answer, in a shape the CLI and the web UI both render."""

    provider: str
    #: False when the command is not on PATH. Everything else is then unknown.
    installed: bool
    #: None means the probe ran but could not be read -- an unrecognised
    #: output shape, a timeout. Deliberately three-valued: reporting "logged
    #: out" for "could not tell" sends people to re-run a login that works.
    logged_in: bool | None
    #: One line, from the tool itself where possible.
    detail: str = ""
    #: Whom we are logged in AS, when the tool says. The reason this is worth
    #: showing: a shared machine logged into the wrong account looks identical
    #: to a working one until the bill arrives.
    account: str = ""
    method: str = ""
    #: What a human would type in a terminal to fix it.
    login_command: str = ""
    #: Whether webui/auth.py can drive that login from the browser. False for
    #: a full-screen TUI, which cannot be driven without emulating a terminal.
    browser_login: bool = False
    #: Said when browser_login is False, or when there is nothing to log into.
    note: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.installed and self.logged_in)


def _run(argv: list[str], *, scrub: bool) -> tuple[int, str, str]:
    """Run a probe. Never raises: every failure is an answer about the login.

    See measured note 2 for why `scrub` exists and why it is not a constant.
    """
    from agent.backends._cli import SCRUBBED_PREFIXES

    env = dict(os.environ)
    if scrub:
        env = {k: v for k, v in env.items() if not k.startswith(SCRUBBED_PREFIXES)}

    try:
        proc = subprocess.run(
            argv, env=env, capture_output=True, text=True,
            stdin=subprocess.DEVNULL, timeout=PROBE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return -1, "", f"{argv[0]} did not answer within {PROBE_TIMEOUT:.0f}s"
    except OSError as exc:
        return -1, "", str(exc)

    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


#: SGR colours, erase-line, and the OSC-8 hyperlinks `claude` wraps URLs in.
#: Stripped because these lines are shown in a browser and in a plain terminal,
#: neither of which should be rendering a raw escape.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\\\)")


def _verdict(*texts: str) -> str:
    """The LAST non-empty line of the first stream that has one.

    Last rather than first, and this is measured, not defensive: `codex login
    status` prints a PATH warning before "Not logged in", and `agy models`
    prints "Fetching available models..." before the error -- both on stderr,
    both ahead of the answer. Taking the first line reported the warning as
    the login state, which is how this function came to exist.
    """
    for text in texts:
        lines = [ln.strip() for ln in _ANSI.sub("", text).splitlines() if ln.strip()]
        if lines:
            return lines[-1]
    return ""


# ---- one function per provider --------------------------------------------


def _codex(executable: str = "codex") -> AuthStatus:
    common = dict(
        provider="codex",
        login_command=f"{executable} login",
        browser_login=True,
    )
    if not shutil.which(executable):
        return AuthStatus(installed=False, logged_in=None,
                          detail=f"`{executable}` is not on PATH.", **common)

    # Not scrubbed: codex.py passes the ambient environment through to the SDK,
    # so CODEX_HOME (if set) picks the auth.json the backend will really use.
    code, out, err = _run([executable, "login", "status"], scrub=False)
    line = _verdict(out, err)

    if code == 0:
        # "Logged in using ChatGPT" / "Logged in using an API key"
        method = line.split("using", 1)[1].strip() if "using" in line else ""
        return AuthStatus(installed=True, logged_in=True, detail=line or "Logged in.",
                          method=method, **common)
    if code == 1:
        return AuthStatus(installed=True, logged_in=False,
                          detail=line or "Not logged in.", **common)

    return AuthStatus(installed=True, logged_in=None,
                      detail=line or f"`{executable} login status` exited {code}.",
                      **common)


def _claude_code(executable: str = "claude") -> AuthStatus:
    common = dict(
        provider="claude_code",
        login_command=f"{executable} auth login",
        browser_login=True,
    )
    if not shutil.which(executable):
        return AuthStatus(installed=False, logged_in=None,
                          detail=f"`{executable}` is not on PATH.", **common)

    # Scrubbed, to match what _cli.py gives the child. See measured note 2.
    code, out, err = _run([executable, "auth", "status", "--json"], scrub=True)

    try:
        body = json.loads(out)
    except (json.JSONDecodeError, TypeError):
        # Measured note 1: the exit code cannot stand in here. A body we cannot
        # read means we do not know, and saying so is the honest answer.
        return AuthStatus(
            installed=True, logged_in=None,
            detail=_verdict(err, out)
            or f"`{executable} auth status` returned nothing readable (exit {code}).",
            **common)

    logged_in = bool(body.get("loggedIn"))
    method = str(body.get("authMethod") or "")
    account = str(body.get("email") or body.get("account") or
                  body.get("organizationName") or "")

    return AuthStatus(
        installed=True,
        logged_in=logged_in,
        detail="Logged in." if logged_in else "Not logged in.",
        account=account,
        method="" if method == "none" else method,
        **common)


def _antigravity(executable: str = "agy") -> AuthStatus:
    common = dict(
        provider="antigravity",
        login_command=executable,
        # Measured note 3: signing in means the bare TUI, which opens the
        # alternate screen buffer and the kitty keyboard protocol. Driving that
        # from a browser means emulating a terminal, which this project does
        # not do -- so the web UI shows the command instead of running it.
        browser_login=False,
        note=f"`{executable}` signs in through its full-screen interface, so "
             f"this one has to be done in a terminal.",
    )
    if not shutil.which(executable):
        return AuthStatus(installed=False, logged_in=None,
                          detail=f"`{executable}` is not on PATH.", **common)

    code, out, err = _run([executable, "models"], scrub=True)

    if code == 0:
        models = [ln for ln in out.splitlines() if "\t" in ln]
        return AuthStatus(installed=True, logged_in=True,
                          detail=f"Logged in; {len(models)} models available."
                                 if models else "Logged in.",
                          **common)

    line = _verdict(err, out)
    if code == 1:
        # "Error: Please sign in to view available models. Launch the CLI
        # without arguments to sign in."
        return AuthStatus(installed=True, logged_in=False,
                          detail=line.removeprefix("Error:").strip() or "Not signed in.",
                          **common)

    return AuthStatus(installed=True, logged_in=None,
                      detail=line or f"`{executable} models` exited {code}.", **common)


def _no_login(provider: str, why: str) -> AuthStatus:
    """A provider there is nothing to log into.

    logged_in=True rather than None on purpose: the question this module
    answers is "will a turn on this provider fail for want of a login", and
    for these the answer is no.
    """
    return AuthStatus(provider=provider, installed=True, logged_in=True,
                      detail=why, note=why)


#: provider name -> how to ask. Keyed by the names in backends.PROVIDERS, so a
#: provider added there without a probe here is caught by a test rather than by
#: silently never being checked.
PROBES = {
    "codex": _codex,
    "claude_code": _claude_code,
    "antigravity": _antigravity,
    "api": lambda: _no_login("api", "Uses an API key from the environment, not a login."),
    "fake": lambda: _no_login("fake", "Offline test double. No login needed."),
}


def status_for(provider: str, **kwargs) -> AuthStatus:
    """One provider. Unknown names answer rather than raise -- a config can
    name a provider this module has never heard of, and that is worth saying
    on the page instead of 500-ing the whole panel."""
    probe = PROBES.get(provider)
    if probe is None:
        return AuthStatus(provider=provider, installed=False, logged_in=None,
                          detail=f"No login check exists for '{provider}'.")
    return probe(**kwargs)


def status_all(providers) -> list[AuthStatus]:
    """Several providers, probed CONCURRENTLY.

    Serially this is ~2.5s (agy's network call dominates), which is long enough
    that a page would show an empty panel first. In parallel it is one agy call.
    """
    from concurrent.futures import ThreadPoolExecutor

    names = list(dict.fromkeys(providers))
    if not names:
        return []
    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        return list(pool.map(status_for, names))
