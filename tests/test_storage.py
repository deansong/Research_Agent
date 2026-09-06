"""
WHAT:  Tests for where a session's files live, and for reading the task out of
       a hand-written brief.txt.
WHY:   These are the two ways a run can quietly use the WRONG folder or the
       WRONG task -- and both fail silently rather than loudly, which is
       exactly the kind of bug worth pinning down with a test.
CONCEPT: No LangGraph, no model. Just paths and precedence.

Run with:   python tests/test_storage.py
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from agent import storage  # noqa: E402
from agent.cli import _resolve_session  # noqa: E402
from agent.config import AgentConfig, BackendConfig  # noqa: E402


def _cfg(session=None):
    return AgentConfig(default=BackendConfig(provider="fake"), roles={}, session=session)


def _args(**kw):
    """The handful of argparse attributes _resolve_session actually reads."""
    return SimpleNamespace(**{"task": None, "task_file": None, "session_dir": None, **kw})


def test_default_layout_is_under_dot_agent():
    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp)
        paths = storage.session_paths(repo, "demo")

        assert paths.session == repo / ".agent" / "sessions" / "demo", paths.session
        assert paths.external is False
        assert paths.input_brief == paths.session / "brief.txt"
        # brief.txt (yours) and brief.md (the designer's) are different files.
        assert paths.input_brief != paths.brief
        assert paths.artifacts.is_dir()
        print("PASS  a named session lives under <repo>/.agent/sessions/")


def test_session_dir_puts_everything_somewhere_else():
    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp) / "repo"
        elsewhere = pathlib.Path(tmp) / "experiments" / "run1"
        repo.mkdir()
        elsewhere.mkdir(parents=True)

        paths = storage.session_paths(repo, "ignored", session_dir=elsewhere)

        assert paths.session == elsewhere, paths.session
        assert paths.external is True
        assert paths.checkpoint == elsewhere / "checkpoint.sqlite"
        # ...but promoted agents stay with the REPO, because they outlive the
        # session and are meant to be reused by other ones.
        assert paths.agents_dir == repo / ".agent" / "agents", paths.agents_dir
        print("PASS  --session-dir relocates the session but not .agent/agents/")


def test_session_dir_typo_is_refused_rather_than_created():
    """The failure this prevents: a typo'd path silently starts an EMPTY
    session, and the brief.txt you wrote appears to have been ignored."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp)
        missing = repo / "no" / "such" / "tree"
        try:
            storage.session_paths(repo, "x", session_dir=missing)
            raise AssertionError("expected SystemExit for a missing parent")
        except SystemExit as exc:
            assert "typo" in str(exc), str(exc)

        # A NEW folder inside an existing parent is fine -- that is not a typo,
        # that is naming the next run.
        fresh = repo / "runs"
        (repo / "runs").parent.mkdir(parents=True, exist_ok=True)
        paths = storage.session_paths(repo, "x", session_dir=fresh)
        assert paths.session.is_dir()
        print("PASS  a missing parent is a typo; a missing leaf is a new run")


def test_brief_txt_is_read_as_the_task():
    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp) / "repo"
        folder = pathlib.Path(tmp) / "prepared"
        repo.mkdir()
        folder.mkdir()
        (folder / "brief.txt").write_text(
            "Survey the layout.\n\nList every top-level package.\n"
        )

        paths, task = _resolve_session(_args(session_dir=str(folder)), _cfg(), repo)

        assert paths.session == folder
        # Multi-line, which is the point: this is why it is a file and not a
        # --task argument or a single-line terminal prompt.
        assert task.startswith("Survey the layout."), task
        assert "every top-level package" in task, task
        # And it is copied into request.txt, which is what identifies a session.
        assert paths.request.read_text().strip() == task
        print("PASS  brief.txt is read as the initial idea, newlines and all")


def test_task_precedence():
    """--task beats brief.txt beats request.txt. Most explicit wins."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp) / "repo"
        folder = pathlib.Path(tmp) / "prepared"
        repo.mkdir()
        folder.mkdir()
        (folder / "request.txt").write_text("what we recorded last time")

        # request.txt alone -> resumes.
        _, task = _resolve_session(_args(session_dir=str(folder)), _cfg(), repo)
        assert task == "what we recorded last time", task

        # brief.txt outranks it: YOU wrote brief.txt, WE wrote request.txt.
        (folder / "brief.txt").write_text("the idea I typed up by hand")
        _, task = _resolve_session(_args(session_dir=str(folder)), _cfg(), repo)
        assert task == "the idea I typed up by hand", task

        # --task outranks everything.
        _, task = _resolve_session(
            _args(session_dir=str(folder), task="on the command line"), _cfg(), repo
        )
        assert task == "on the command line", task
        print("PASS  --task > brief.txt > request.txt")


def test_empty_brief_txt_does_not_count_as_a_task():
    """An empty file means "I meant to write this and forgot", so it must not
    shadow request.txt and quietly resume with no explanation."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp) / "repo"
        folder = pathlib.Path(tmp) / "prepared"
        repo.mkdir()
        folder.mkdir()
        (folder / "request.txt").write_text("the real task")
        (folder / "brief.txt").write_text("   \n\n")

        _, task = _resolve_session(_args(session_dir=str(folder)), _cfg(), repo)
        assert task == "the real task", task
        print("PASS  an empty brief.txt is reported and skipped")


def test_named_session_also_reads_brief_txt():
    """--session NAME knows the folder before it knows the task too, so the
    same file works there -- --session-dir is not special."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp)
        paths = storage.session_paths(repo, "prepared")
        paths.input_brief.write_text("do the thing")

        got, task = _resolve_session(_args(), _cfg(session="prepared"), repo)
        assert got.session == paths.session
        assert task == "do the thing", task
        print("PASS  brief.txt works for --session as well as --session-dir")


def test_infrastructure_rules_name_an_external_session_folder():
    """A session outside .agent/ is inside the executor's write sandbox and
    would otherwise look like ordinary project files."""
    from agent.work.compile import infrastructure_rules

    inside = infrastructure_rules("/repo/.agent/sessions/s/artifacts",
                                  "/repo/.agent/sessions/s")
    assert "this session's own folder" not in inside, inside

    outside = infrastructure_rules("/home/me/exp/run1/artifacts", "/home/me/exp/run1")
    assert "/home/me/exp/run1" in outside, outside
    print("PASS  working rules protect an external session folder, and only then")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("\nAll storage tests passed.")
