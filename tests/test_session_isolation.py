"""
WHAT:  Sessions do not see each other: every new session gets its own id and
       folder, the folders go under a sessions root you choose, and every
       model is told to keep out of the other sessions.
WHY:   A new hotel-study session reopened, or read, an earlier one's folder
       and planned around its files as if they were this task's earlier work.
       The id was a hash of the task, so the same words even shared a folder.

Run with:   python -m pytest tests/test_session_isolation.py -q
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
from types import SimpleNamespace

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from agent import activity, storage  # noqa: E402
from agent.cli import _resolve_session  # noqa: E402
from agent.config import AgentConfig, BackendConfig, load_config  # noqa: E402


def _cfg(**kw):
    return AgentConfig(default=BackendConfig(provider="fake"), roles={}, **kw)


def _args(**kw):
    return SimpleNamespace(**{"task": None, "task_file": None, "session_dir": None, **kw})


def test_the_same_task_twice_is_two_sessions(tmp_path):
    first, _ = _resolve_session(_args(task="study hotel skills"), _cfg(), tmp_path)
    second, _ = _resolve_session(_args(task="study hotel skills"), _cfg(), tmp_path)

    assert first.session != second.session, "a new task never reopens an old folder"
    assert first.session.name.startswith("study-hotel-skills-")
    assert first.session.parent == second.session.parent == tmp_path / ".agent" / "sessions"


def test_sessions_go_under_the_configured_root(tmp_path):
    repo, root = tmp_path / "repo", tmp_path / "all-sessions"
    repo.mkdir()
    paths, _ = _resolve_session(_args(task="t"), _cfg(sessions_root=str(root)), repo)

    assert paths.session.parent == root
    assert paths.sessions_root == root
    assert paths.external, "outside .agent/, so the working rules name it"
    assert not (repo / ".agent" / "sessions").exists() or not any(
        (repo / ".agent" / "sessions").iterdir())
    assert [name for name, _, _ in storage.list_sessions(repo, root)] == [paths.session.name]

    # Resuming by id finds it under the same root.
    again, task = _resolve_session(_args(), _cfg(session=paths.session.name,
                                                 sessions_root=str(root)), repo)
    assert again.session == paths.session and task == "t"


def test_a_relative_root_is_relative_to_the_repository(tmp_path):
    assert storage.sessions_root(tmp_path, "runs") == tmp_path / "runs"
    assert storage.sessions_root(tmp_path, None) == tmp_path / ".agent" / "sessions"


def test_the_root_comes_from_the_file_the_environment_or_the_flag(tmp_path):
    (tmp_path / ".agent").mkdir()
    (tmp_path / ".agent" / "config.json").write_text(json.dumps({"sessions_root": "from-file"}))
    none = SimpleNamespace(backend=None, model=None, backend_role=None, session=None,
                           config=None, sessions_root=None)

    assert load_config(repo_path=tmp_path, cli=none, env={}).sessions_root == "from-file"
    assert load_config(repo_path=tmp_path, cli=none,
                       env={"AGENT_SESSIONS_ROOT": "from-env"}).sessions_root == "from-env"
    flag = SimpleNamespace(**{**vars(none), "sessions_root": "from-flag"})
    assert load_config(repo_path=tmp_path, cli=flag,
                       env={"AGENT_SESSIONS_ROOT": "from-env"}).sessions_root == "from-flag"


def test_the_rule_names_this_session_and_the_others(tmp_path):
    paths = storage.session_paths(tmp_path, "mine", root=tmp_path / "root")
    rule = storage.isolation_rules(paths)
    assert str(paths.session) in rule and str(tmp_path / "root") in rule


def test_over_http_each_session_is_new_and_every_model_is_told(tmp_path):
    """Two sessions with the same task, through the web API, under a chosen
    root: two folders, both listed, and every model turn -- design roles and
    the generated agent's nodes -- carried the isolation rule."""
    from tests.test_supervisor import _answer, _client, _until
    from webui.tests.conftest import temp_repo

    root = tmp_path / "sessions"
    with temp_repo() as tmp:
        repo = pathlib.Path(tmp)
        client = _client(repo, "off")

        def new(start):
            response = client.post("/api/sessions", json={
                "repo": str(repo), "task": "survey hotel jobs", "backend": "fake",
                "sessions_root": str(root), "start": start})
            assert response.status_code == 200, response.text
            return response.json()

        idle = new(False)
        sid = new(True)["id"]
        assert idle["id"] != sid
        listed = client.get("/api/sessions", params={"repo": str(repo),
                                                     "root": str(root)}).json()
        assert {s["id"] for s in listed} == {idle["id"], sid}
        assert all(pathlib.Path(s["folder"]).parent == root for s in listed)

        _until(client, sid, purpose="discussion")
        _answer(client, sid, "/plan")
        _until(client, sid, purpose="plan_review")
        _answer(client, sid, "/approve")
        _until(client, sid, purpose="design_review")
        _answer(client, sid, "/approve")
        _until(client, sid, purpose="findings")
        _answer(client, sid, "/approve")
        _until(client, sid, done=True)

        session = root / sid
        nodes = [d.name for d in (session / "activity").iterdir() if d.is_dir()]
        work = [n for n in nodes if n not in ("discussor", "planner", "designer")]
        assert work, nodes
        for node in nodes:
            sent = activity.latest(session, node).sent
            assert "SESSION ISOLATION" in sent["instructions"], node
            assert str(session) in sent["instructions"], node


def test_a_session_open_from_another_folder_is_listed_and_reopened_as_is(tmp_path):
    """A server started with a new sessions folder still resumes the sessions
    that were running in the old one. Open must show them, and clicking one
    must reattach it -- not make an empty folder of the same name in the new
    sessions folder."""
    from tests.test_supervisor import _client, _until
    from webui.tests.conftest import temp_repo

    with temp_repo() as tmp:
        repo = pathlib.Path(tmp)
        client = _client(repo, "off")
        sid = client.post("/api/sessions", json={
            "repo": str(repo), "task": "old work", "backend": "fake"}).json()["id"]
        _until(client, sid, purpose="discussion")

        elsewhere = tmp_path / "new-root"
        listed = client.get("/api/sessions", params={"repo": str(repo),
                                                     "root": str(elsewhere)}).json()
        assert [(s["id"], s["open"]) for s in listed] == [(sid, True)]
        assert listed[0]["folder"].startswith(str(repo)), "its real folder"

        again = client.post("/api/sessions", json={
            "repo": str(repo), "session": sid, "sessions_root": str(elsewhere),
            "start": True})
        assert again.status_code == 200, again.text
        assert again.json()["folder"] == listed[0]["folder"]
        assert not (elsewhere / sid).exists()
