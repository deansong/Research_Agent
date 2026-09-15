"""
WHAT:  Stops every session runner a test left behind, and gives tests a temp
       repo that stops them BEFORE it is deleted.
WHY:   A runner is a THREAD, and the repository it writes into is usually a
       tempfile.TemporaryDirectory owned by the test. When the test returns
       first, cleanup deletes the tree from under a live thread -- so rmtree
       raises, and the failure is reported against a test whose own assertions
       all passed. That is what the intermittent
       `test_an_absolute_repo_is_honoured_as_typed` failure was: its "PASS"
       line is in the captured output of the run that failed.
CONCEPT: pytest teardown, doing what the app's lifespan hook does on shutdown.

Not a fix for a bug in the app: `_lifespan` already shuts runners down when a
real server stops. TestClient does not enter the lifespan for every test here,
so the tests have to do it themselves.

--------------------------------------------------------------------------
WHY THE AUTOUSE FIXTURE BELOW IS NOT ENOUGH
--------------------------------------------------------------------------
It runs when the test FUNCTION returns -- and a test written as

    with tempfile.TemporaryDirectory() as tmp:
        ...

deletes its tree while still inside the function, so the fixture is already
too late. That is why the flake this file was written for never went away.

MEASURED: after `POST /api/sessions` returns, the runner thread goes on to
write `<repo>/.agent/sessions/<name>/activity/discussor/0001.json`. Whether
rmtree sees it depends on which of the two wins, which is exactly the shape
of an intermittent failure -- and the report lands on a test whose own
assertions all passed.

`temp_repo()` is the fix: same thing, with the runners stopped first. Use it
instead of tempfile.TemporaryDirectory in any test that creates a session.
"""

from __future__ import annotations

import contextlib
import tempfile

import pytest

from webui import server


def _stop_runners() -> None:
    with server._RUNNERS_LOCK:
        runners = list(server._RUNNERS.values())
        server._RUNNERS.clear()
    for runner in runners:
        try:
            runner.shutdown()
        except Exception:  # noqa: BLE001 -- teardown must not mask a real failure
            pass


@contextlib.contextmanager
def temp_repo():
    """A temporary directory, emptied of live threads before it is removed."""
    with tempfile.TemporaryDirectory() as tmp:
        try:
            yield tmp
        finally:
            _stop_runners()


@pytest.fixture(autouse=True)
def _shut_down_runners():
    """The backstop, for tests that use pytest's own tmp_path."""
    yield
    _stop_runners()
