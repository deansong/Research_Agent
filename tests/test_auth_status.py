"""
WHAT:  The login probes, driven by fake executables that behave like the real
       binaries did when measured.
WHY:   Every one of these tests is a transcription of something the real CLI
       actually does -- exiting 0 while logged out, printing a warning ahead of
       the answer -- so the suite fails if the parsing drifts back to the
       obvious-but-wrong reading.
CONCEPT: A script on disk passed as `executable`, not a mocked subprocess.

Run with:   python -m pytest tests/test_auth_status.py -q
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from agent.backends import PROVIDERS  # noqa: E402
from agent.backends import auth  # noqa: E402
from agent.backends.auth import AuthStatus  # noqa: E402


def fake(tmp_path, name: str, body: str) -> str:
    """A stand-in binary. Real file, real exec, real exit code."""
    path = tmp_path / name
    path.write_text(f"#!{sys.executable}\nimport os, sys\n{body}\n")
    path.chmod(0o755)
    return str(path)


# ---- claude: the exit code lies -------------------------------------------


def test_claude_logged_out_is_read_from_the_body_not_the_exit_code(tmp_path):
    """MEASURED: `claude auth status` exits 0 whether or not you are logged in.

    This is the whole reason the claude probe parses JSON. Swap the body check
    for `returncode == 0` and this test reports a logged-out install as ready.
    """
    body = json.dumps({"loggedIn": False, "authMethod": "none"})
    exe = fake(tmp_path, "claude", f"print({body!r}); sys.exit(0)")

    status = auth.status_for("claude_code", executable=exe)

    assert status.installed is True
    assert status.logged_in is False
    assert status.ok is False


def test_claude_logged_in_reports_the_account_and_method(tmp_path):
    body = json.dumps({"loggedIn": True, "authMethod": "claudeai",
                       "email": "someone@example.org"})
    exe = fake(tmp_path, "claude", f"print({body!r}); sys.exit(0)")

    status = auth.status_for("claude_code", executable=exe)

    assert status.logged_in is True and status.ok is True
    assert status.account == "someone@example.org"
    assert status.method == "claudeai"


def test_claude_authmethod_none_is_not_shown_as_a_method(tmp_path):
    """"none" is the absence of a method, and printing it as one reads as a
    provider called "none"."""
    body = json.dumps({"loggedIn": False, "authMethod": "none"})
    exe = fake(tmp_path, "claude", f"print({body!r}); sys.exit(0)")

    assert auth.status_for("claude_code", executable=exe).method == ""


def test_an_unreadable_body_is_unknown_rather_than_logged_out(tmp_path):
    """Three-valued on purpose: "could not tell" must not send somebody to
    re-run a login that is working."""
    exe = fake(tmp_path, "claude", "print('not json at all'); sys.exit(0)")

    status = auth.status_for("claude_code", executable=exe)

    assert status.logged_in is None
    assert status.ok is False


# ---- codex and agy: the verdict is the last line, not the first -----------


def test_codex_reads_the_verdict_past_a_leading_warning(tmp_path):
    """MEASURED: codex prints a PATH warning to stderr ABOVE "Not logged in".

    Taking the first line reported the warning as the login state.
    """
    exe = fake(tmp_path, "codex", (
        "print('WARNING: proceeding, even though we could not create PATH "
        "aliases: ...', file=sys.stderr)\n"
        "print('Not logged in', file=sys.stderr)\n"
        "sys.exit(1)"
    ))

    status = auth.status_for("codex", executable=exe)

    assert status.logged_in is False
    assert status.detail == "Not logged in"


def test_agy_reads_the_verdict_past_the_progress_line(tmp_path):
    """MEASURED: agy prints "Fetching available models..." then erases it with
    an escape, then prints the error -- all on stderr."""
    exe = fake(tmp_path, "agy", (
        r"print('Fetching available models...', file=sys.stderr)" "\n"
        r"print('\x1b[KError: Please sign in to view available models.', file=sys.stderr)" "\n"
        "sys.exit(1)"
    ))

    status = auth.status_for("antigravity", executable=exe)

    assert status.logged_in is False
    assert status.detail == "Please sign in to view available models."
    assert "\x1b" not in status.detail, "an escape sequence reached the browser"


def test_codex_logged_in_names_the_method(tmp_path):
    exe = fake(tmp_path, "codex", "print('Logged in using ChatGPT'); sys.exit(0)")

    status = auth.status_for("codex", executable=exe)

    assert status.logged_in is True
    assert status.method == "ChatGPT"


def test_agy_logged_in_counts_the_models(tmp_path):
    exe = fake(tmp_path, "agy", (
        "print('Fetching available models...', file=sys.stderr)\n"
        r"print('a-model\tA Model')" "\n"
        r"print('b-model\tB Model')" "\n"
        "sys.exit(0)"
    ))

    status = auth.status_for("antigravity", executable=exe)

    assert status.logged_in is True
    assert "2 models" in status.detail


def test_an_unexpected_exit_code_is_unknown_not_logged_out(tmp_path):
    exe = fake(tmp_path, "codex", "print('kaboom', file=sys.stderr); sys.exit(17)")

    assert auth.status_for("codex", executable=exe).logged_in is None


# ---- the environment the probe runs in ------------------------------------


def test_the_claude_probe_is_scrubbed_like_the_backend_it_speaks_for(tmp_path, monkeypatch):
    """MEASURED (_cli.py note 2): a backend gives its child a scrubbed
    environment, so `claude` reads ~/.claude. A probe that kept CLAUDE_CONFIG_DIR
    would read a DIFFERENT credentials file and confidently report its state --
    an answer about the wrong account.
    """
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/somewhere/else")
    exe = fake(tmp_path, "claude", (
        "import json\n"
        "print(json.dumps({'loggedIn': 'CLAUDE_CONFIG_DIR' not in os.environ,\n"
        "                  'authMethod': 'claudeai'}))"
    ))

    assert auth.status_for("claude_code", executable=exe).logged_in is True


def test_the_codex_probe_is_not_scrubbed_because_codex_py_does_not_scrub(tmp_path, monkeypatch):
    """The mirror of the test above, and the reason `scrub` is per provider.

    codex.py hands the ambient environment to the SDK, so CODEX_HOME picks the
    auth.json a turn will really use. Scrubbing it here would check a file the
    backend never opens.
    """
    monkeypatch.setenv("CODEX_HOME", "/somewhere/else")
    exe = fake(tmp_path, "codex", (
        "ok = os.environ.get('CODEX_HOME') == '/somewhere/else'\n"
        "print('Logged in using ChatGPT' if ok else 'Not logged in')\n"
        "sys.exit(0 if ok else 1)"
    ))

    assert auth.status_for("codex", executable=exe).logged_in is True


def test_a_wedged_binary_times_out_as_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "PROBE_TIMEOUT", 0.5)
    exe = fake(tmp_path, "codex", "import time; time.sleep(30)")

    status = auth.status_for("codex", executable=exe)

    assert status.logged_in is None
    assert "did not answer" in status.detail


def test_a_missing_command_is_not_installed(tmp_path):
    status = auth.status_for("codex", executable=str(tmp_path / "nope"))

    assert status.installed is False
    assert status.logged_in is None
    assert status.ok is False


# ---- the registry ---------------------------------------------------------


def test_every_provider_has_a_login_check():
    """A provider added to PROVIDERS without a probe here would silently never
    be checked, which is the failure this whole module exists to prevent."""
    assert set(auth.PROBES) == set(PROVIDERS)


def test_a_provider_with_nothing_to_log_into_is_ready():
    for name in ("fake", "api"):
        status = auth.status_for(name)
        assert status.ok is True, name
        assert status.note, f"{name} should say WHY there is no login"


def test_an_unknown_provider_answers_rather_than_raising():
    status = auth.status_for("does_not_exist")

    assert status.logged_in is None
    assert "does_not_exist" in status.detail


def test_antigravity_is_the_one_that_cannot_be_driven_from_a_browser():
    """MEASURED: `agy` signs in through a full-screen TUI. The web UI must show
    the command instead of pretending it can run it."""
    # Asserted on the status objects, because those are what the page reads --
    # and browser_login has to be right even when the binary is absent, which
    # is exactly when somebody is looking for the instructions.
    agy = auth.status_for("antigravity", executable="/nonexistent")
    assert agy.browser_login is False
    assert "terminal" in agy.note, "it must say WHERE to do it instead"

    for name, exe in (("codex", "/nonexistent"), ("claude_code", "/nonexistent")):
        status = auth.status_for(name, executable=exe)
        assert status.browser_login is True, name
        assert status.login_command, name


def test_status_all_answers_for_every_provider_asked_for():
    names = ["fake", "api", "fake"]

    result = auth.status_all(names)

    assert [s.provider for s in result] == ["fake", "api"], "duplicates collapsed"
    assert auth.status_all([]) == []


# ---- saying so before anything is spent -----------------------------------


def test_a_signed_out_provider_is_named_before_a_run_starts(monkeypatch, capsys):
    """The failure the whole module exists to prevent: a role pointed at a
    signed-out provider fails INSIDE a turn, minutes in, with a message about
    the model. build_backends is the last place that can say so for free."""
    from agent import backends

    monkeypatch.setattr(auth, "status_all", lambda names: [
        AuthStatus(provider="claude_code", installed=True, logged_in=False,
                   detail="Not logged in.", login_command="claude auth login")])

    class Backend:
        name = "claude_code"

    backends._warn_signed_out({"designer": Backend(), "planner": Backend()})

    printed = capsys.readouterr().out
    assert "claude_code is not signed in" in printed
    assert "designer, planner" in printed, "did not say WHICH roles are affected"
    assert "claude auth login" in printed, "did not say how to fix it"


def test_nothing_is_said_when_every_provider_is_fine(monkeypatch, capsys):
    """A warning that appears on a healthy run is a warning people stop
    reading."""
    from agent import backends

    monkeypatch.setattr(auth, "status_all", lambda names: [
        AuthStatus(provider="codex", installed=True, logged_in=True, detail="ok")])

    class Backend:
        name = "codex"

    backends._warn_signed_out({"coder": Backend()})

    assert capsys.readouterr().out == ""


def test_a_provider_that_cannot_be_checked_is_not_reported_as_signed_out(monkeypatch, capsys):
    """logged_in is None. Warning about it would send somebody to re-run a
    login that works, which is the exact mistake the three-valued answer
    exists to avoid."""
    from agent import backends

    monkeypatch.setattr(auth, "status_all", lambda names: [
        AuthStatus(provider="codex", installed=True, logged_in=None,
                   detail="timed out", login_command="codex login")])

    class Backend:
        name = "codex"

    backends._warn_signed_out({"coder": Backend()})

    assert capsys.readouterr().out == ""


def test_the_login_check_is_wired_into_build_backends(monkeypatch, capsys):
    """Through build_backends, not straight at the helper.

    The helper's own tests all pass with the call site deleted -- a function
    can be perfect and never run. This project has been caught by exactly that
    once before (a validator check nothing invoked), so the wiring gets its own
    test that goes in the front door.
    """
    from agent.backends import Access, build_backends
    from agent.config import AgentConfig, BackendConfig

    monkeypatch.setattr(auth, "status_all", lambda names: [
        AuthStatus(provider="fake", installed=True, logged_in=False,
                   detail="pretend it is signed out",
                   login_command="fake login")])

    cfg = AgentConfig(default=BackendConfig(provider="fake", model="m-1"), roles={})
    build_backends(cfg, {"coder": Access.WRITE})

    printed = capsys.readouterr().out
    assert "fake is not signed in" in printed, printed
    assert "coder" in printed, printed
