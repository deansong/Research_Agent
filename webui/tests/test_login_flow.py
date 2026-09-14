"""
WHAT:  Driving a login from the browser -- the pty, the extraction, the outcome.
WHY:   Two of these tests are regressions for bugs that only appeared when the
       REAL binaries were driven: a device code indented under its bullet, and
       a pty turning every newline into CRLF. Both made the code invisible
       while every string looked right in the transcript.
CONCEPT: A fake CLI on a real pty, not a mocked subprocess.

Run with:   python -m pytest webui/tests/test_login_flow.py -q
"""

from __future__ import annotations

import pathlib
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from agent.backends.auth import AuthStatus  # noqa: E402
from webui import auth as login  # noqa: E402


def flow(monkeypatch, body: str, *, expects_code: bool = False) -> None:
    """Register a fake provider whose login is `body`."""
    monkeypatch.setitem(
        login.FLOWS, "probe",
        login.Flow(argv=[sys.executable, "-u", "-c", body], scrub=False,
                   expects_code=expects_code, input_label="Paste it"),
    )


def outcome(monkeypatch, logged_in: bool, detail: str = "") -> None:
    """What the status probe will say once the child has gone."""
    monkeypatch.setattr(
        login, "status_for",
        lambda provider, **kw: AuthStatus(provider=provider, installed=True,
                                          logged_in=logged_in, detail=detail),
    )


def settle(session, seconds: float = 20.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and session.snapshot()["running"]:
        time.sleep(0.05)
    return session.snapshot()


def until(session, predicate, seconds: float = 20.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        snap = session.snapshot()
        if predicate(snap):
            return snap
        time.sleep(0.05)
    return session.snapshot()


@pytest.fixture(autouse=True)
def _no_leaks():
    yield
    login.shutdown()


# ---- the two bugs the real binaries found ---------------------------------


def test_an_indented_device_code_is_found(monkeypatch):
    """REGRESSION: codex indents the code under "2. Enter this one-time code".
    A pattern anchored at column 0 found nothing, and the page showed a
    sign-in link with no code beside it."""
    flow(monkeypatch, "print('2. Enter this code'); print('   CW4P-ZJ3G9')")
    outcome(monkeypatch, True)

    snap = settle(login.start("probe"))

    assert snap["code"] == "CW4P-ZJ3G9"


def test_carriage_returns_from_the_pty_do_not_hide_the_code(monkeypatch):
    """REGRESSION: a pty translates \\n to CRLF on output, so every line ends
    in \\r -- which `$` does not consume. Same symptom, different cause, and
    fixing only the indentation left this one live."""
    flow(monkeypatch,
         "import sys\n" r"sys.stdout.write('head\r\n   CW4P-ZJ3G9\r\n')")
    outcome(monkeypatch, True)

    snap = settle(login.start("probe"))

    assert snap["code"] == "CW4P-ZJ3G9"
    assert "\r" not in snap["output"]


def test_a_progress_line_shows_only_what_the_screen_would_show(monkeypatch):
    """A lone \\r is the tool rewriting the line it just printed."""
    flow(monkeypatch, r"import sys; sys.stdout.write('Loading 1%\rLoading 99%\rDone\n')")
    outcome(monkeypatch, True)

    snap = settle(login.start("probe"))

    assert "Done" in snap["output"]
    assert "Loading" not in snap["output"], "the tool's working out reached the page"


# ---- the outcome is the provider's answer, not the exit code --------------


def test_a_clean_exit_that_did_not_log_in_is_a_failure(monkeypatch):
    """The guarantee in the module docstring. Exit 0 has already been caught
    lying twice in this project; "we ran the command" is not "you are logged
    in". Replace the re-probe with `returncode == 0` and this test goes red."""
    flow(monkeypatch, "print('all done!')")
    outcome(monkeypatch, False, detail="Not logged in.")

    snap = settle(login.start("probe"))

    assert snap["state"] == "failed"
    assert snap["error"] == "Not logged in."


def test_a_login_the_provider_confirms_is_done(monkeypatch):
    flow(monkeypatch, "print('ok')")
    outcome(monkeypatch, True)

    snap = settle(login.start("probe"))

    assert snap["state"] == "done"
    assert snap["error"] == ""


def test_a_child_that_fails_is_still_judged_by_the_probe(monkeypatch):
    """The mirror: a non-zero exit does not get to say "logged out" either --
    the credentials might have been written just before it tripped over
    something on the way out."""
    flow(monkeypatch, "print('wrote the token'); sys.exit(3)")
    outcome(monkeypatch, True)

    assert settle(login.start("probe"))["state"] == "done"


# ---- input, and keeping it out of the transcript --------------------------


def test_what_you_type_reaches_the_child(monkeypatch):
    """Echoed back upper-cased, because echoing it back VERBATIM is the one
    thing the redaction below is there to prevent -- the first version of this
    test asserted on the plain string and failed against working code."""
    flow(monkeypatch, "line = input(); print('GOT:' + line.strip().upper())",
         expects_code=True)
    outcome(monkeypatch, True)

    session = login.start("probe")
    until(session, lambda s: s["accepts_input"])
    session.send("the-code")

    assert "GOT:THE-CODE" in settle(session)["output"]


def test_a_pasted_code_is_not_left_in_the_transcript(monkeypatch):
    """It is a single-use credential, and the transcript is readable by every
    later GET. ECHO is turned off on the pty AND the string is redacted --
    belt and braces, because a child that resets termios defeats the first."""
    flow(monkeypatch, "line = input(); print('read ' + str(len(line.strip())))",
         expects_code=True)
    outcome(monkeypatch, True)

    session = login.start("probe")
    until(session, lambda s: s["accepts_input"])
    session.send("sup3r-s3cr3t-code")

    snap = settle(session)
    assert "read 17" in snap["output"], "the child did receive it"
    assert "sup3r-s3cr3t-code" not in snap["output"]


def test_a_child_that_echoes_the_code_back_still_does_not_leak_it(monkeypatch):
    """Turning ECHO off on the pty stops the usual leak, but a child that sets
    its own terminal modes -- or simply prints what it read -- defeats that and
    nothing warns you. So the string is redacted as well, and this is the test
    that holds the second layer in place."""
    flow(monkeypatch, "line = input(); print('you typed ' + line.strip())",
         expects_code=True)
    outcome(monkeypatch, True)

    session = login.start("probe")
    until(session, lambda s: s["accepts_input"])
    session.send("sup3r-s3cr3t-code")

    snap = settle(session)
    assert "you typed" in snap["output"], "the child did print it back"
    assert "sup3r-s3cr3t-code" not in snap["output"]


def test_the_pty_does_not_echo_what_is_typed_at_it(monkeypatch):
    """The first layer, pinned on its own. Asserted against the RAW buffer,
    because the redaction above would hide the failure -- two defences that can
    only be tested through each other are one defence with extra steps."""
    flow(monkeypatch, "import time; time.sleep(5)", expects_code=True)
    outcome(monkeypatch, False)

    session = login.start("probe")
    until(session, lambda s: s["accepts_input"])
    session.send("echo-me-please")
    time.sleep(1.0)

    assert "echo-me-please" not in session._text, "the pty echoed it back"
    session.cancel()


def test_input_is_refused_once_the_flow_is_over(monkeypatch):
    flow(monkeypatch, "print('bye')", expects_code=True)
    outcome(monkeypatch, True)

    session = login.start("probe")
    settle(session)

    with pytest.raises(login.LoginError, match="no longer running"):
        session.send("too late")


# ---- cancelling -----------------------------------------------------------


def test_cancelling_says_cancelled_and_not_failed(monkeypatch):
    """The reader thread notices the broken pty right after cancel() sets the
    state; it must not report that as a failure of the login."""
    flow(monkeypatch, "import time; time.sleep(60)")
    outcome(monkeypatch, False, detail="Not logged in.")

    session = login.start("probe")
    until(session, lambda s: s["state"] == "running")
    session.cancel()
    time.sleep(1.0)                      # long enough for the reader to react

    snap = session.snapshot()
    assert snap["state"] == "cancelled"
    assert snap["error"] == ""


def test_an_outcome_already_reached_is_never_rewritten(monkeypatch):
    """Cancel pressed just as the login lands. Both are real events arriving
    in an order nobody controls, and the first one to settle is the true one --
    otherwise a login that WORKED is reported as cancelled and the person does
    it again for nothing. This is the terminal-state guard in _finish, and
    without it this test goes red while every other one stays green."""
    flow(monkeypatch, "print('ok')")
    outcome(monkeypatch, True)

    session = login.start("probe")
    assert settle(session)["state"] == "done"

    session.cancel()

    assert session.snapshot()["state"] == "done"


def test_cancelling_kills_the_child(monkeypatch):
    flow(monkeypatch, "import time; time.sleep(120)")
    outcome(monkeypatch, False)

    session = login.start("probe")
    until(session, lambda s: s["state"] == "running")
    proc = session._proc
    session.cancel()

    assert proc.wait(timeout=10) is not None


def test_a_flow_that_overruns_is_cancelled(monkeypatch):
    monkeypatch.setattr(login, "LOGIN_TIMEOUT", 0.5)
    flow(monkeypatch, "import time; time.sleep(60)")
    outcome(monkeypatch, False)

    snap = settle(login.start("probe"), seconds=15)

    assert snap["state"] == "timeout"
    assert "again" in snap["error"]


# ---- the registry ---------------------------------------------------------


def test_a_second_start_joins_the_one_already_running(monkeypatch):
    """Two concurrent `codex login` flows race to write the same auth.json,
    and the second device code invalidates the first."""
    flow(monkeypatch, "import time; time.sleep(30)")
    outcome(monkeypatch, False)

    first = login.start("probe")
    until(first, lambda s: s["state"] == "running")

    assert login.start("probe") is first
    assert login.start("probe", restart=True) is not first
    assert first.snapshot()["state"] == "cancelled"


def test_antigravity_has_no_browser_login(monkeypatch):
    """MEASURED: `agy` signs in through a full-screen TUI. The refusal has to
    point somewhere, not just say no."""
    with pytest.raises(login.LoginError) as caught:
        login.start("antigravity")

    assert "terminal" in str(caught.value)


def test_a_missing_binary_says_so_before_anything_spawns(monkeypatch):
    monkeypatch.setitem(login.FLOWS, "probe",
                        login.Flow(argv=["definitely-not-installed"], scrub=False,
                                   expects_code=False))

    with pytest.raises(login.LoginError, match="not on PATH"):
        login.start("probe")


def test_escape_sequences_never_reach_the_browser(monkeypatch):
    flow(monkeypatch, r"print('\x1b[94mhttps://example.org/go\x1b[39m')")
    outcome(monkeypatch, True)

    snap = settle(login.start("probe"))

    assert snap["url"] == "https://example.org/go"
    assert "\x1b" not in snap["output"]


def test_only_providers_with_a_flow_can_be_driven():
    """FLOWS is the one list. A provider added here without the status side
    knowing about it would offer a button that cannot work."""
    from agent.backends.auth import PROBES

    assert set(login.FLOWS) <= set(PROBES)
    for name, spec in login.FLOWS.items():
        status = PROBES[name]()
        assert status.browser_login is True, f"{name} offers a flow but says it cannot"
        assert spec.argv[0] == status.login_command.split()[0]
