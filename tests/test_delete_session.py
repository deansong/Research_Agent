"""
WHAT:  Deleting a session from the web UI: its folder, worktree and resume
       mark go; its branch only when asked; nothing that is not a session.
WHY:   An rmtree aimed by an HTTP request. The refusals are the point.

Run with:   python -m pytest tests/test_delete_session.py -q
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from agent import storage  # noqa: E402
from tests.test_workspace import _git, repo  # noqa: E402,F401 -- the fixture


def _branches(repo):
    return _git(repo, "branch", "--list", "agent/*")


def test_delete_removes_folder_worktree_and_mark_and_keeps_the_branch(repo):  # noqa: F811
    paths = storage.session_paths(repo, "s1")
    storage.create_workspace(paths)
    paths.request.write_text("t")
    storage.mark_running(paths)

    done = storage.delete_session(paths)
    assert not paths.session.exists()
    assert storage.marked_running(repo) == []
    assert "agent/s1" in _branches(repo) and "kept on branch agent/s1" in " ".join(done)
    assert "s1" not in _git(repo, "worktree", "list"), "no stale worktree record"


def test_delete_can_take_the_branch_too(repo):  # noqa: F811
    paths = storage.session_paths(repo, "s2")
    storage.create_workspace(paths)
    storage.delete_session(paths, drop_branch=True)
    assert "agent/s2" not in _branches(repo)


def test_it_refuses_what_is_not_a_session_or_holds_the_project(repo, tmp_path):  # noqa: F811
    empty = storage.session_paths(repo, "nothing-here")
    with pytest.raises(storage.DeleteRefused, match="does not look like"):
        storage.delete_session(empty)

    # --session-dir pointed at the project's parent: never.
    above = storage.session_paths(repo, "x", session_dir=repo.parent)
    (repo.parent / "request.txt").write_text("t")
    with pytest.raises(storage.DeleteRefused, match="contains the project"):
        storage.delete_session(above)
    assert (repo / "tool.py").exists()


def test_over_http_a_running_session_is_stopped_then_deleted(repo):  # noqa: F811
    from tests.test_supervisor import _client, _until

    client = _client(repo, "off")
    sid = client.post("/api/sessions", json={
        "repo": str(repo), "task": "survey it", "backend": "fake"}).json()["id"]
    _until(client, sid, purpose="discussion")
    folder = repo / ".agent" / "sessions" / sid

    gone = client.post(f"/api/sessions/{sid}/delete",
                       json={"repo": str(repo), "drop_branch": True})
    assert gone.status_code == 200, gone.text
    assert not folder.exists() and f"agent/{sid}" not in _branches(repo)
    assert client.get(f"/api/sessions/{sid}").status_code == 404, "no longer open"

    again = client.post(f"/api/sessions/{sid}/delete", json={"repo": str(repo)})
    assert again.status_code == 404
    for bad in ("..", "x..y/../.."):
        refused = client.post(f"/api/sessions/{bad}/delete", json={"repo": str(repo)})
        assert refused.status_code in (404, 405), refused.text
    assert (repo / "tool.py").exists()
