"""
WHAT:  The Files tab's server half -- which files are listed, and what may be
       read.
WHY:   One function here takes a path straight from the browser. Everything
       else is listing; `read` is the attack surface, and a containment check
       that is subtly wrong looks exactly like one that is right.
CONCEPT: Real directories in a tmpdir, real symlinks, a real git repo.

Run with:   python -m pytest webui/tests/test_files.py -q
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from webui import files  # noqa: E402


@pytest.fixture
def roots(tmp_path):
    """A repo and an artifacts directory, laid out as a session's are."""
    repo = tmp_path / "repo"
    art = tmp_path / "artifacts"
    (repo / "classifier_experiment").mkdir(parents=True)
    art.mkdir()
    (repo / "classifier_experiment" / "train.py").write_text("import torch\n")
    (art / "metrics.json").write_text('{"acc": 0.92}\n')
    return repo, art


def _git_repo(repo: pathlib.Path) -> bool:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    try:
        for argv in (["init", "-q"], ["add", "-A"],
                     ["commit", "-qm", "base", "--no-gpg-sign"]):
            done = subprocess.run(["git", "-C", str(repo), *argv],
                                  capture_output=True, env=env)
            if done.returncode != 0:
                return False
    except OSError:
        return False
    return True


# ---- the part that has to be right ---------------------------------------


def test_a_path_that_climbs_out_of_the_roots_is_refused(roots):
    """`../../.ssh/id_rsa`, which is the whole attack.

    This server binds to loopback, which bounds who can try it -- it does not
    make trying it harmless, because anything running on the same machine can
    reach it, and a listing endpoint that will read any file on disk is a
    different thing from one that reads a session's output.
    """
    repo, art = roots
    secret = repo.parent / "id_rsa"
    secret.write_text("PRIVATE KEY")

    for attempt in ("repo/../id_rsa",
                    "repo/classifier_experiment/../../id_rsa",
                    "repo/./../id_rsa"):
        with pytest.raises(PermissionError):
            files.read(repo, art, attempt)

    # An absolute path names no root at all, so it is a different refusal --
    # both are refusals, and asserting only one lets the other regress.
    with pytest.raises(FileNotFoundError):
        files.read(repo, art, "/etc/passwd")
    print("PASS  a path climbing out of the roots is refused")


def test_a_symlink_out_of_the_tree_is_refused_after_resolution(roots):
    """The one a string-prefix check passes.

    "repo/escape" starts with the root as TEXT and points outside it. Only
    resolving BOTH sides first catches this, which is why `read` resolves the
    root as well as the target -- and why the note in files.py says so.
    """
    repo, art = roots
    secret = repo.parent / "outside.txt"
    secret.write_text("not yours")
    try:
        (repo / "escape").symlink_to(secret)
    except OSError:
        pytest.skip("this filesystem does not do symlinks")

    with pytest.raises(PermissionError):
        files.read(repo, art, "repo/escape")

    # And it must not be offered in the first place.
    listed = [row["path"] for row in files.listing(repo, art, since=0)]
    assert "repo/escape" not in listed, listed
    print("PASS  a symlink out of the tree is refused after resolution")


def test_a_file_inside_a_root_is_read(roots):
    """The refusals above are only worth anything if the normal case works."""
    repo, art = roots
    body = files.read(repo, art, "repo/classifier_experiment/train.py")
    assert body["text"] == "import torch\n", body
    assert body["skipped"] == "", body

    assert files.read(repo, art, "artifacts/metrics.json")["text"].strip() \
        == '{"acc": 0.92}'
    print("PASS  a file inside a root is read")


# ---- what gets listed ------------------------------------------------------


def test_the_listing_is_what_changed_not_the_whole_repository(roots):
    """A repository is mostly not this run's work.

    git already knows the difference, and it is the same question that had to
    be answered by hand when the agent's output first needed separating from
    the project's own files.
    """
    repo, art = roots
    if not _git_repo(repo):
        pytest.skip("git is unavailable here")

    (repo / "classifier_experiment" / "baseline.py").write_text("# new\n")
    (repo / "classifier_experiment" / "train.py").write_text("import torch\n# edited\n")

    rows = {row["path"]: row for row in files.listing(repo, art)}
    assert rows["repo/classifier_experiment/baseline.py"]["origin"] == "new"
    assert rows["repo/classifier_experiment/train.py"]["origin"] == "modified"
    # An untouched committed file is not this run's output and must not appear.
    (repo / "untouched.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "untouched.py"],
                   capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "u", "--no-gpg-sign"],
                   capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t",
                        "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t"})
    after = {row["path"] for row in files.listing(repo, art)}
    assert "repo/untouched.py" not in after, after
    print("PASS  the listing is what changed, not the whole repository")


def test_artifacts_never_push_the_code_off_the_end(roots):
    """MEASURED on a real session: 32 changed source files against 1,985
    artifacts, one JSON per GPU allocation.

    Sorted into one list by time, the files somebody opened this tab to read
    sit a thousand rows down -- and past MAX_FILES they are not in the
    response at all. So a busy run would return a file listing containing no
    code, which is the one outcome that makes the tab pointless.
    """
    repo, art = roots
    # Artifacts NEWER than the code, which is the case that breaks a single
    # time-ordered list: every one of them outranks the training script.
    later = time.time() + 60
    for i in range(files.MAX_FILES + 50):
        target = art / f"alloc-{i:05d}.json"
        target.write_text("{}")
        os.utime(target, (later, later))

    rows = files.listing(repo, art, since=0)
    code = [row for row in rows if row["root"] == "repo"]
    assert code, "the code was pushed off the end by artifacts"
    assert code[0]["path"] == "repo/classifier_experiment/train.py", code[0]
    # And code comes first, so it is on screen without scrolling past 2,000.
    assert rows[0]["root"] == "repo", rows[0]
    print("PASS  artifacts never push the code off the end")


def test_a_file_too_big_or_not_text_says_which(roots):
    """"Nothing appeared" is not a diagnosis. Too big and not text are
    different problems with different answers."""
    repo, art = roots
    big = repo / "huge.log"
    big.write_text("x" * (files.MAX_BYTES + 10))
    binary = repo / "model.bin"
    binary.write_bytes(b"\x00\x01\x02")

    over = files.read(repo, art, "repo/huge.log")
    assert over["text"] == ""
    assert "over the" in over["skipped"], over

    blob = files.read(repo, art, "repo/model.bin")
    assert blob["text"] == ""
    assert "not a text file" in blob["skipped"], blob
    print("PASS  a file that is not shown says which limit it hit")


def test_the_session_s_own_bookkeeping_is_not_listed_as_output(roots):
    """.agent holds checkpoints and activity, shown in their own panels. In
    this list they would be several thousand rows of noise between somebody
    and their training script."""
    repo, art = roots
    (repo / ".agent" / "sessions" / "s").mkdir(parents=True)
    (repo / ".agent" / "sessions" / "s" / "checkpoint.sqlite").write_text("x")
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "train.cpython-311.pyc").write_text("x")

    listed = [row["path"] for row in files.listing(repo, art, since=0)]
    assert not [p for p in listed if ".agent" in p], listed
    assert not [p for p in listed if "__pycache__" in p], listed
    print("PASS  session bookkeeping is not listed as the run's output")


# ---- over HTTP -------------------------------------------------------------


def test_the_endpoints_serve_a_session_s_files_and_refuse_the_rest(tmp_path):
    """The wiring, end to end. The unit tests above prove `files.read` refuses
    an escape; this proves the ROUTE calls it and turns the refusal into a 403
    rather than a traceback or, worse, a 200.
    """
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

    with temp_repo() as tmp:
        repo = pathlib.Path(tmp)
        client = TestClient(server.create_app(repo, Args()))
        created = client.post("/api/sessions", json={
            "repo": str(repo), "task": "t", "backend": "fake"})
        assert created.status_code == 200, created.text
        sid = created.json()["id"]

        # Something for the run to have "produced".
        (repo / "train.py").write_text("import torch\n")

        listed = client.get(f"/api/sessions/{sid}/files")
        assert listed.status_code == 200, listed.text
        paths = [row["path"] for row in listed.json()["files"]]
        assert "repo/train.py" in paths, paths

        body = client.get(f"/api/sessions/{sid}/files/content",
                          params={"path": "repo/train.py"})
        assert body.status_code == 200, body.text
        assert body.json()["text"] == "import torch\n"

        # The escape, over HTTP. A 403 with our envelope, not a 500.
        secret = repo.parent / "id_rsa"
        secret.write_text("PRIVATE KEY")
        refused = client.get(f"/api/sessions/{sid}/files/content",
                             params={"path": "repo/../id_rsa"})
        assert refused.status_code == 403, refused.text
        assert "PRIVATE KEY" not in refused.text
        assert refused.json()["detail"]["code"] == "outside_session", refused.text

        missing = client.get(f"/api/sessions/{sid}/files/content",
                             params={"path": "repo/nope.py"})
        assert missing.status_code == 404, missing.text
    print("PASS  the endpoints serve a session's files and refuse the rest")
