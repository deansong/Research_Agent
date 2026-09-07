"""
WHAT:  Stops every session runner a test left behind.
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
"""

from __future__ import annotations

import pytest

from webui import server


@pytest.fixture(autouse=True)
def _shut_down_runners():
    yield
    with server._RUNNERS_LOCK:
        runners = list(server._RUNNERS.values())
        server._RUNNERS.clear()
    for runner in runners:
        try:
            runner.shutdown()
        except Exception:  # noqa: BLE001 -- teardown must not mask a real failure
            pass
