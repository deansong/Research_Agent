"""
WHAT:  The /api/auth routes -- status, and starting a login from the page.
WHY:   The status half is read-only and safe anywhere; the login half signs
       THIS MACHINE in to something, and the two must not be confused.
CONCEPT: TestClient, with the real probes stubbed so nothing touches a
       credentials file.

Run with:   python -m pytest webui/tests/test_auth_api.py -q
"""

from __future__ import annotations

import pathlib
import sys
import time

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from agent.backends.auth import AuthStatus  # noqa: E402
from webui import auth as login  # noqa: E402
from webui import server  # noqa: E402


def poll(web, path: str, ready, seconds: float = 20.0) -> dict:
    """Read `path` until `ready` says so, or give up.

    Deadline-based, not a fixed number of iterations: a count that is ample on
    an idle laptop is a flaky test on a loaded CI box, and the failure looks
    like a bug in the flow rather than in the test.
    """
    deadline = time.monotonic() + seconds
    body = web.get(path).json()
    while time.monotonic() < deadline and not ready(body):
        time.sleep(0.02)
        body = web.get(path).json()
    return body


class Args:
    backend = "fake"
    model = None
    backend_role = None
    config = None
    session = None


def client(tmp_path, host: str = "127.0.0.1") -> TestClient:
    server._RUNNERS.clear()
    return TestClient(server.create_app(tmp_path, Args(), host=host))


@pytest.fixture(autouse=True)
def _stub_probes(monkeypatch):
    """No real binary is run by these tests. The probes have their own suite."""
    def fake(provider, **kwargs):
        return AuthStatus(
            provider=provider, installed=True,
            logged_in=(provider != "claude_code"),
            detail="stubbed",
            login_command=f"{provider} login",
            browser_login=provider in ("codex", "claude_code"),
            note="terminal only" if provider == "antigravity" else "",
        )

    monkeypatch.setattr("agent.backends.auth.status_for", fake)
    monkeypatch.setattr("webui.auth.status_for", fake)
    yield
    login.shutdown()


# ---- status ---------------------------------------------------------------


def test_status_reports_every_provider_and_which_are_in_use(tmp_path):
    body = client(tmp_path).get("/api/auth").json()

    by_name = {p["provider"]: p for p in body["providers"]}
    assert {"codex", "claude_code", "antigravity"} <= set(by_name)
    assert by_name["claude_code"]["ok"] is False
    assert by_name["codex"]["ok"] is True

    # --backend fake puts every role on fake, so that is the one in use -- and
    # the others are still listed, because signing in before configuring a
    # provider is the order people actually do it in.
    assert by_name["fake"]["in_use"] is True
    assert by_name["antigravity"]["in_use"] is False


def test_the_answer_says_whether_a_login_can_be_started_here(tmp_path):
    assert client(tmp_path).get("/api/auth").json()["can_login_here"] is True


# ---- the loopback rule ----------------------------------------------------


def test_a_login_is_refused_on_a_server_anyone_can_reach(tmp_path):
    """The UI has no token. On 0.0.0.0 a login route would let anyone who can
    reach the port sign this machine into THEIR account -- after which every
    turn runs on their credentials, and nothing on the page says so."""
    web = client(tmp_path, host="0.0.0.0")

    body = web.get("/api/auth").json()
    assert body["can_login_here"] is False
    assert "0.0.0.0" in body["why_not"]

    response = web.post("/api/auth/codex/login")
    assert response.status_code == 403
    assert "127.0.0.1" in response.json()["detail"]["message"], "say what to do"


def test_reading_the_status_is_allowed_from_anywhere(tmp_path):
    """It says whether a login exists, never what it is -- and refusing it
    would leave a remote page unable to explain why nothing runs."""
    response = client(tmp_path, host="0.0.0.0").get("/api/auth")

    assert response.status_code == 200
    assert response.json()["providers"]


# ---- starting, feeding and stopping a flow --------------------------------


def test_starting_a_login_returns_the_link_to_open(tmp_path, monkeypatch):
    monkeypatch.setitem(
        login.FLOWS, "codex",
        login.Flow(argv=[sys.executable, "-u", "-c",
                         "print('go to https://example.org/device')\n"
                         "print('   ABCD-1234')\nimport time; time.sleep(30)"],
                   scrub=False, expects_code=False),
    )
    web = client(tmp_path)

    web.post("/api/auth/codex/login")
    body = poll(web, "/api/auth/codex/login", lambda b: b["url"] and b["code"])

    assert body["url"] == "https://example.org/device"
    assert body["code"] == "ABCD-1234"
    assert body["running"] is True

    assert web.post("/api/auth/codex/login/cancel").json() == {"ok": True}
    assert web.get("/api/auth/codex/login").json()["state"] == "cancelled"


def test_a_provider_with_no_browser_login_says_where_to_do_it(tmp_path):
    response = client(tmp_path).post("/api/auth/antigravity/login")

    assert response.status_code == 400
    assert "terminal" in response.json()["detail"]["message"]


def test_asking_about_a_login_nobody_started(tmp_path):
    response = client(tmp_path).get("/api/auth/codex/login")

    assert response.status_code == 404


def test_typing_into_a_login_nobody_started(tmp_path):
    response = client(tmp_path).post("/api/auth/codex/login/input",
                                     json={"text": "ABCD"})

    assert response.status_code == 404


def test_cancelling_a_login_nobody_started_is_not_an_error(tmp_path):
    """The page cancels on unload, and a 500 in the console for tidying up
    after nothing is noise that trains people to ignore the console."""
    assert client(tmp_path).post("/api/auth/codex/login/cancel").status_code == 200


def test_a_pasted_code_reaches_the_flow(tmp_path, monkeypatch):
    monkeypatch.setitem(
        login.FLOWS, "claude_code",
        login.Flow(argv=[sys.executable, "-u", "-c",
                         "line = input(); print('SAW ' + line.strip().upper())"],
                   scrub=False, expects_code=True, input_label="Paste it"),
    )
    web = client(tmp_path)

    web.post("/api/auth/claude_code/login")
    poll(web, "/api/auth/claude_code/login", lambda b: b["accepts_input"])

    web.post("/api/auth/claude_code/login/input", json={"text": "the-code"})

    body = poll(web, "/api/auth/claude_code/login", lambda b: not b["running"])

    assert "SAW THE-CODE" in body["output"]
    # The stub probe says claude_code is logged out, so the honest outcome of
    # a child that exited cleanly is still "failed".
    assert body["state"] == "failed"
