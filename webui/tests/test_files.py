"""
WHAT:  The Files tab's server half -- which files are listed, and what may be
       read: the session's own folder, and nothing else.
WHY:   One function here takes a path straight from the browser. Everything
       else is listing; `read` is the attack surface, and a containment check
       that is subtly wrong looks exactly like one that is right. The tab used
       to list and read the whole repository -- the tool's own source, other
       sessions' work -- which is why the session folder is now the only root.
CONCEPT: Real directories in a tmpdir, real symlinks.

Run with:   python -m pytest webui/tests/test_files.py -q
"""

from __future__ import annotations

import os
import pathlib
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from webui import files  # noqa: E402


@pytest.fixture
def session(tmp_path):
    """A repository with a session folder inside it, laid out as a real one is."""
    repo = tmp_path / "repo"
    folder = repo / ".agent" / "sessions" / "s"
    (folder / "artifacts").mkdir(parents=True)
    (folder / "agent").mkdir()
    (folder / "plan.json").write_text('{"steps": []}\n')
    (folder / "agent" / "graph.json").write_text("{}\n")
    (folder / "artifacts" / "metrics.json").write_text('{"acc": 0.92}\n')
    (repo / "agent").mkdir()
    (repo / "agent" / "config.py").write_text("SECRET_SETTING = 1\n")
    return folder


# ---- the part that has to be right ---------------------------------------


def test_nothing_outside_the_session_folder_can_be_read(session):
    """`../../.ssh/id_rsa` is the whole attack -- and so is the repository the
    session sits in: the tool's own source, other sessions, the user's code.
    The server can be started with --host 0.0.0.0, so whatever this reads,
    anyone who can reach the port can read."""
    secret = session.parent / "id_rsa"
    secret.write_text("PRIVATE KEY")

    for attempt in ("session/../id_rsa",
                    "session/artifacts/../../id_rsa",
                    "session/./../id_rsa",
                    "session/../../../agent/config.py"):
        with pytest.raises(PermissionError):
            files.read(session, attempt)

    # Not a path in this session at all: a different refusal -- both are
    # refusals, and asserting only one lets the other regress.
    for attempt in ("/etc/passwd", "repo/agent/config.py", "artifacts/metrics.json",
                    "session"):
        with pytest.raises(FileNotFoundError):
            files.read(session, attempt)


def test_a_symlink_out_of_the_folder_is_refused_after_resolution(session):
    """The one a string-prefix check passes: "session/artifacts/escape" starts
    with the root as TEXT and points outside it. Only resolving BOTH sides
    first catches it."""
    secret = session.parent / "outside.txt"
    secret.write_text("not yours")
    try:
        (session / "artifacts" / "escape").symlink_to(secret)
    except OSError:
        pytest.skip("this filesystem does not do symlinks")

    with pytest.raises(PermissionError):
        files.read(session, "session/artifacts/escape")
    listed = [row["path"] for row in files.listing(session)]
    assert "session/artifacts/escape" not in listed, listed


def test_a_file_inside_the_session_is_read(session):
    """The refusals above are only worth anything if the normal case works."""
    body = files.read(session, "session/plan.json")
    assert body["text"] == '{"steps": []}\n' and body["skipped"] == "", body
    assert files.read(session, "session/artifacts/metrics.json")["text"].strip() \
        == '{"acc": 0.92}'


# ---- what gets listed ------------------------------------------------------


def test_the_listing_is_the_session_folder_and_nothing_else(session):
    repo = session.parents[2]
    (repo / "train.py").write_text("import torch\n")            # the repository's
    other = repo / ".agent" / "sessions" / "other"               # another session's
    other.mkdir()
    (other / "plan.json").write_text("{}")
    # Bookkeeping with its own panels, and noise.
    (session / "checkpoint.sqlite").write_text("x")
    (session / "activity" / "s1").mkdir(parents=True)
    (session / "activity" / "s1" / "0001.json").write_text("{}")
    (session / "agent.tmp").mkdir()
    (session / "agent.tmp" / "graph.json").write_text("{}")
    (session / "artifacts" / "__pycache__").mkdir()
    (session / "artifacts" / "__pycache__" / "x.pyc").write_text("x")
    # ...but a run's own folder that happens to be called activity is output.
    (session / "artifacts" / "activity").mkdir()
    (session / "artifacts" / "activity" / "log.txt").write_text("ran\n")

    rows = {row["path"]: row for row in files.listing(session)}
    assert set(rows) == {"session/plan.json", "session/agent/graph.json",
                         "session/artifacts/metrics.json",
                         "session/artifacts/activity/log.txt"}, sorted(rows)
    assert rows["session/plan.json"]["origin"] == "session"
    assert rows["session/artifacts/metrics.json"]["origin"] == "artifact"


def test_artifacts_never_push_the_session_files_off_the_end(session):
    """MEASURED on a real session: 1,985 artifacts, one JSON per GPU
    allocation. In one time-ordered list the files somebody opened this tab to
    read sit a thousand rows down -- and past MAX_FILES are not there at all."""
    later = time.time() + 60
    for i in range(files.MAX_FILES + 50):
        target = session / "artifacts" / f"alloc-{i:05d}.json"
        target.write_text("{}")
        os.utime(target, (later, later))

    rows = files.listing(session)
    assert rows[0]["origin"] == "session", "the session's own files come first"
    assert {"session/plan.json", "session/agent/graph.json"} <= {
        row["path"] for row in rows if row["origin"] == "session"}


def test_a_file_too_big_or_not_text_says_which(session):
    """"Nothing appeared" is not a diagnosis. Too big and not text are
    different problems with different answers."""
    (session / "artifacts" / "huge.log").write_text("x" * (files.MAX_BYTES + 10))
    (session / "artifacts" / "model.bin").write_bytes(b"\x00\x01\x02")

    over = files.read(session, "session/artifacts/huge.log")
    assert over["text"] == "" and "over the" in over["skipped"], over
    blob = files.read(session, "session/artifacts/model.bin")
    assert blob["text"] == "" and "not a text file" in blob["skipped"], blob


# ---- over HTTP -------------------------------------------------------------


def test_the_endpoints_serve_the_session_folder_and_refuse_the_rest(tmp_path):
    """The wiring, end to end: the route calls `files.read`, turns a refusal
    into a 403 rather than a traceback or, worse, a 200 -- and the repository
    around the session is neither listed nor readable."""
    from fastapi.testclient import TestClient

    from webui import server
    from webui.tests.conftest import temp_repo

    server._RUNNERS.clear()

    class Args:
        backend = "fake"
        model = None
        backend_role = None
        config = None
        session = None
        supervisor = "off"

    with temp_repo() as tmp:
        repo = pathlib.Path(tmp)
        client = TestClient(server.create_app(repo, Args()))
        created = client.post("/api/sessions", json={
            "repo": str(repo), "task": "t", "backend": "fake", "start": False})
        assert created.status_code == 200, created.text
        folder = pathlib.Path(created.json()["folder"])
        sid = created.json()["id"]

        (repo / "train.py").write_text("import torch\n")          # the repository's
        (folder / "artifacts" / "report.md").write_text("# done\n")  # the session's

        listed = client.get(f"/api/sessions/{sid}/files")
        assert listed.status_code == 200, listed.text
        paths = [row["path"] for row in listed.json()["files"]]
        assert "session/artifacts/report.md" in paths, paths
        assert not [p for p in paths if "train.py" in p], paths

        body = client.get(f"/api/sessions/{sid}/files/content",
                          params={"path": "session/artifacts/report.md"})
        assert body.status_code == 200 and body.json()["text"] == "# done\n", body.text

        # The repository is out of reach, by its old name or by climbing.
        refused = client.get(f"/api/sessions/{sid}/files/content",
                             params={"path": "session/../../../train.py"})
        assert refused.status_code == 403, refused.text
        assert "import torch" not in refused.text
        assert refused.json()["detail"]["code"] == "outside_session", refused.text
        old_root = client.get(f"/api/sessions/{sid}/files/content",
                              params={"path": "repo/train.py"})
        assert old_root.status_code == 404, old_root.text
