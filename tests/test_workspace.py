"""
WHAT:  Each new session works in its own git worktree, not the shared checkout.
WHY:   Measured: a running session's coder "restored" five files somebody was
       editing in the checkout it shared with them -- its checker had flagged
       them because they changed during its turn. Rules in a prompt reduce
       that; a separate copy of the project removes it.

Run with:   python -m pytest tests/test_workspace.py -q
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
from unittest.mock import MagicMock

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from agent import storage  # noqa: E402
from agent.backends.base import Access  # noqa: E402

_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def _git(repo, *argv):
    return subprocess.run(["git", "-C", str(repo), *argv], capture_output=True,
                          text=True, env=_ENV, check=True).stdout


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    try:
        _git(repo, "init", "-q")
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git is unavailable here")
    (repo / "tool.py").write_text("committed\n")
    (repo / ".gitignore").write_text(".agent/sessions/\ndata/\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base", "--no-gpg-sign")
    (repo / "tool.py").write_text("committed\nmy uncommitted edit\n")   # your WIP
    (repo / "data").mkdir()
    (repo / "data" / "big.csv").write_text("1,2\n")                     # untracked data
    return repo


def test_a_new_session_gets_its_own_worktree_of_the_project_as_it_is(repo):
    paths = storage.session_paths(repo, "s1")
    note = storage.create_workspace(paths)

    workspace = paths.session / "workspace"
    assert "branch agent/s1" in note and "uncommitted" in note, note
    assert paths.workspace == workspace and paths.own_workspace
    # The project as it is now -- your uncommitted edit included...
    assert (workspace / "tool.py").read_text() == "committed\nmy uncommitted edit\n"
    # ...but not untracked data, which the models are told to read from the checkout.
    assert not (workspace / "data").exists()
    assert str(repo) in storage.isolation_rules(paths)
    assert "agent/s1" in _git(repo, "branch", "--list", "agent/s1")

    # Work in it never reaches your checkout, nor yours it.
    (workspace / "tool.py").write_text("the agent's version\n")
    (workspace / "new.py").write_text("x = 1\n")
    assert (repo / "tool.py").read_text() == "committed\nmy uncommitted edit\n"
    assert not (repo / "new.py").exists()
    assert _git(repo, "stash", "list") == "", "the stash list is not touched"

    # Two sessions, two copies.
    other = storage.session_paths(repo, "s2")
    storage.create_workspace(other)
    assert not (other.workspace / "new.py").exists()


def test_a_session_that_already_ran_or_a_plain_directory_works_in_place(repo, tmp_path):
    started = storage.session_paths(repo, "old")
    started.checkpoint.write_text("")              # it ran before worktrees existed
    assert storage.create_workspace(started) == ""
    assert started.workspace == repo and not started.own_workspace

    plain = tmp_path / "not-git"
    plain.mkdir()
    paths = storage.session_paths(plain, "s")
    assert "not a git repository" in storage.create_workspace(paths)
    assert paths.workspace == plain
    assert "own copy of the project" not in storage.isolation_rules(paths)


def test_the_files_tab_shows_what_the_session_changed_and_nothing_else(repo):
    from webui import files

    paths = storage.session_paths(repo, "s1")
    storage.create_workspace(paths)
    (paths.workspace / "tool.py").write_text("changed by the run\n")
    (paths.workspace / "train.py").write_text("import torch\n")

    rows = {row["path"]: row for row in files.listing(paths.session)}
    code = {p for p, row in rows.items() if row["origin"] == "code"}
    assert code == {"session/workspace/tool.py", "session/workspace/train.py"}, rows
    assert files.read(paths.session, "session/workspace/train.py")["text"] == "import torch\n"

    # The rest of the worktree is the project -- not this session's to show.
    _git(paths.workspace, "add", "-A")
    _git(paths.workspace, "commit", "-qm", "run", "--no-gpg-sign")
    (paths.workspace / "untouched.py").write_text("")
    _git(paths.workspace, "add", "untouched.py")
    _git(paths.workspace, "commit", "-qm", "u", "--no-gpg-sign")
    with pytest.raises(PermissionError):
        files.read(paths.session, "session/workspace/.gitignore")
    assert "session/workspace/train.py" in {
        r["path"] for r in files.listing(paths.session)}, "committed on its branch: still its work"


def test_model_turns_run_in_the_workspace_and_may_write_the_results_folder(repo):
    """Every turn's working directory is the worktree, and the sandbox is
    told the results folder beside it is writable too."""
    from agent.backends.codex import CodexBackend
    from agent.backends.fake import FakeBackend
    from agent.config import AgentConfig, BackendConfig
    from agent.runtime import open_runtime

    paths = storage.session_paths(repo, "s1")
    storage.create_workspace(paths)
    cfg = AgentConfig(default=BackendConfig(provider="fake"), roles={})
    seen = []
    original = FakeBackend.run_structured

    def spy(self, **kw):
        seen.append(kw["repo_path"])
        return original(self, **kw)

    FakeBackend.run_structured = spy
    try:
        with open_runtime(cfg, paths) as runtime:
            session = runtime.bootstrap_session("survey it")
            session.graph.invoke(session.initial_state, config=session.config)
            backend = next(iter(runtime._built.values()))
            assert backend.writable_roots == [str(paths.artifacts)]
    finally:
        FakeBackend.run_structured = original
    assert seen and set(seen) == {str(paths.workspace)}, seen

    client = MagicMock()
    codex = CodexBackend(client)
    codex.writable_roots = [str(paths.artifacts)]
    codex._get_thread(thread_id=None, repo_path=str(paths.workspace),
                      access=Access.WRITE, developer_instructions="")
    sent = client.thread_start.call_args.kwargs
    assert sent["cwd"] == str(paths.workspace)
    assert sent["config"] == {"sandbox_workspace_write": {
        "writable_roots": [str(paths.artifacts)]}}


def test_claude_code_is_given_the_results_folder_too():
    from agent.backends.claude_code import ClaudeCodeBackend

    backend = ClaudeCodeBackend.__new__(ClaudeCodeBackend)
    backend.writable_roots = ["/s/artifacts"]
    flags = backend.access_flags(Access.WRITE)
    assert flags[-2:] == ["--add-dir", "/s/artifacts"], flags


def test_a_new_web_session_gets_a_workspace_and_says_so(repo):
    from tests.test_supervisor import _client, _until

    client = _client(repo, "off")
    sid = client.post("/api/sessions", json={
        "repo": str(repo), "task": "survey it", "backend": "fake"}).json()["id"]
    _until(client, sid, purpose="discussion")
    session = repo / ".agent" / "sessions" / sid
    record = json.loads((session / "workspace.json").read_text())
    assert record["branch"] == f"agent/{sid}" and record["uncommitted_included"]
    assert (session / "workspace" / "tool.py").exists()
