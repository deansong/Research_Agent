"""
WHAT:  Each new session is its own project: an empty git repository in its
       own folder. Nothing is copied in from the repository the server runs
       from, and nothing is written back to it.
WHY:   Sessions used to work in -- and then in a worktree of -- the agent's
       own repository: every model saw the agent's source and whatever
       earlier experiments had left there, and runs wrote their output into
       it. Each session is now fully independent.

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


def test_a_new_session_is_an_empty_project_of_its_own(repo):
    paths = storage.session_paths(repo, "s1")
    note = storage.create_workspace(paths)

    workspace = paths.session / "workspace"
    assert "new, empty project" in note, note
    assert paths.workspace == workspace and paths.own_workspace
    # Nothing of the repository is copied in -- tracked, edited or untracked.
    assert sorted(p.name for p in workspace.iterdir()) == [".git"]
    assert _git(workspace, "log", "--format=%s").startswith("Start of session s1")
    # ...and nothing is made in the repository: no branch, no worktree.
    assert _git(repo, "branch", "--list", "agent/*") == ""
    assert _git(repo, "worktree", "list").count("\n") == 1
    rules = storage.isolation_rules(paths)
    assert "SELF-CONTAINED" in rules and "started empty" in rules
    assert f"never read, use or write the agent's own code at {repo}" in rules

    # Two sessions, two projects.
    (workspace / "train.py").write_text("x = 1\n")
    other = storage.session_paths(repo, "s2")
    storage.create_workspace(other)
    assert not (other.workspace / "train.py").exists()
    assert not (repo / "train.py").exists()


def test_it_needs_no_git_repository_to_run_from(tmp_path):
    plain = tmp_path / "not-git"
    plain.mkdir()
    paths = storage.session_paths(plain, "s")
    assert "new, empty project" in storage.create_workspace(paths)
    assert paths.workspace == paths.session / "workspace"


def test_a_session_that_already_ran_keeps_what_it_had(repo):
    started = storage.session_paths(repo, "old")
    started.checkpoint.write_text("")              # it ran before workspaces existed
    assert storage.create_workspace(started) == ""
    assert started.workspace == repo and not started.own_workspace


def test_an_older_session_with_a_worktree_keeps_it(repo):
    """Sessions made with a worktree of the repository go on as they were."""
    paths = storage.session_paths(repo, "legacy")
    _git(repo, "worktree", "add", "-q", "-b", "agent/legacy",
         str(paths.session / "workspace"), "HEAD")
    (paths.session / "workspace.json").write_text(json.dumps(
        {"branch": "agent/legacy", "base": _git(repo, "rev-parse", "HEAD").strip()}))
    assert storage.create_workspace(paths) == ""
    assert (paths.workspace / "tool.py").exists()
    assert "own copy of the project" in storage.isolation_rules(paths)


def test_the_files_tab_shows_everything_the_session_wrote(repo):
    from webui import files

    paths = storage.session_paths(repo, "s1")
    storage.create_workspace(paths)
    (paths.workspace / "train.py").write_text("import torch\n")
    (paths.workspace / "src").mkdir()
    (paths.workspace / "src" / "data.py").write_text("rows = []\n")

    rows = {row["path"]: row for row in files.listing(paths.session)}
    code = {p for p, row in rows.items() if row["origin"] == "code"}
    assert code == {"session/workspace/train.py", "session/workspace/src/data.py"}, rows
    assert files.read(paths.session, "session/workspace/train.py")["text"] == "import torch\n"

    # Committed by the run: still its work.
    _git(paths.workspace, "add", "-A")
    _git(paths.workspace, "commit", "-qm", "run", "--no-gpg-sign")
    assert "session/workspace/train.py" in {
        r["path"] for r in files.listing(paths.session)}
    with pytest.raises(PermissionError):
        files.read(paths.session, "session/workspace/.git/HEAD")


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


def test_a_new_web_session_is_its_own_project(repo):
    from tests.test_supervisor import _client, _until

    client = _client(repo, "off")
    root = repo.parent / "sessions"
    sid = client.post("/api/sessions", json={
        "repo": str(repo), "task": "survey it", "backend": "fake",
        "sessions_root": str(root)}).json()["id"]
    _until(client, sid, purpose="discussion")
    session = root / sid
    record = json.loads((session / "workspace.json").read_text())
    assert record["kind"] == "new_project" and "branch" not in record
    assert not (session / "workspace" / "tool.py").exists()
    assert _git(repo, "status", "--short", "--ignored") == " M tool.py\n!! data/\n", \
        "the repository is exactly as it was: nothing added, not even ignored"
