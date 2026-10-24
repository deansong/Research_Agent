"""
Keeps the test suite away from the real machine's sessions: research_backend
keeps sessions in ~/.research_backend unless told otherwise.
"""

import pytest


@pytest.fixture(autouse=True)
def _contained(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("RESEARCH_BACKEND_HOME",
                       str(tmp_path_factory.mktemp("research-backend")))
    monkeypatch.delenv("RESEARCH_BACKEND_SKILLS_DIR", raising=False)
