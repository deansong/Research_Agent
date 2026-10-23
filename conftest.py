"""
Keeps the test suite away from the real machine's sessions.

Sessions default to a folder in the home directory, and resume marks to the
user's state folder (agent/storage.py). A test must touch neither: its
sessions go inside its own temporary repository, as they always did, and its
marks into a temporary folder -- or a test run would leave sessions in the
user's list and have the next real server try to resume them. The same for
research_backend's own folder (~/.research_backend).
"""

import pytest


@pytest.fixture(autouse=True)
def _contained(tmp_path_factory, monkeypatch):
    from agent import storage

    monkeypatch.setattr(storage, "DEFAULT_SESSIONS_ROOT", None)
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path_factory.mktemp("agent-state")))
    monkeypatch.delenv("AGENT_SESSIONS_ROOT", raising=False)
    monkeypatch.setenv("RESEARCH_BACKEND_HOME",
                       str(tmp_path_factory.mktemp("research-backend")))
