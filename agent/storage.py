"""
WHAT:  Where a session's files live on disk.
WHY:   A session is now a DIRECTORY, not just a row in a database: it holds the
       checkpoint, the agent that was designed for it, and the task brief. One
       place decides those paths.
CONCEPT: Checkpointer storage -- but note the measured trap below.

--------------------------------------------------------------------------
LAYOUT
--------------------------------------------------------------------------
    <repo>/.agent/
        .gitignore              written by us: "sessions/", "config.json"
        config.json             per-repo backend config (unchanged)
        sessions/<session>/     scratch, gitignored
            brief.txt           OPTIONAL, WRITTEN BY YOU -- see below
            checkpoint.sqlite   both phases live here, under two thread ids
            meta.json           schema version, so a stale session refuses
            request.txt         what YOU asked for -- identifies the session
            brief.md            the designer's rewrite of it, for the agent
            plan.json           four stages, work in substeps -- EDIT before approving
            artifacts/          where a run puts its OUTPUT -- see below
            agent.tmp/          staging; renamed to agent/ only once valid
            agent/              the agent designed for this session
        agents/<name>/          promoted, committed, reusable

--------------------------------------------------------------------------
ONE FOLDER PER SESSION, AND WHERE THEY ALL GO
--------------------------------------------------------------------------
Every new session gets a NEW id -- a few words of the task, the time, and a
random suffix -- so two sessions never share a folder, a checkpoint or an
agent, however alike their tasks are. (An id used to be a hash of the task, so
the same words reopened an old session and its files.) A session is resumed
only by naming it: --session ID, or opening it in the web UI.

The sessions root is the folder those session folders are made in:
<repo>/.agent/sessions by default, or `sessions_root` in config.json,
AGENT_SESSIONS_ROOT, --sessions-root, or the web UI's New session dialog.
Outside the repository is the better place for it: then a model working in
the repository cannot come across other sessions at all.

--------------------------------------------------------------------------
EACH SESSION IS ITS OWN PROJECT
--------------------------------------------------------------------------
A new session gets <session>/workspace: a NEW, EMPTY git repository of its
own. Every model turn runs there; the code the run writes is that project.
Nothing is copied in from the repository the server runs from -- that is the
agent's own code, and copying it put the agent's source, and whatever earlier
experiments had left beside it, in front of every model. A session reads and
writes only its own folder: its workspace, its artifacts, its plan and agent.

Sessions made before this, with a git worktree of the repository (their
workspace.json names a `branch`), keep working as they were.
--------------------------------------------------------------------------
A SESSION FOLDER SOMEWHERE ELSE
--------------------------------------------------------------------------
`--session-dir /path/to/anywhere` uses that directory AS the session folder,
with the same contents. The repo still supplies `.agent/agents/` (so `promote`
keeps working) and `.agent/config.json`, but nothing else lives under it.

Use it when the session is the thing you care about: an experiment you want
beside its data, a folder you want to keep after the repo is gone, a shared
directory two people look at. `--session NAME` stays the right choice for
ordinary throwaway work.

--------------------------------------------------------------------------
brief.txt -- THE INPUT, versus brief.md -- AN OUTPUT
--------------------------------------------------------------------------
These two are easy to confuse, so:

    brief.txt   YOU write it, by hand, before the first run. It is the
                initial idea -- exactly what you would otherwise have typed
                at the "What do you want to build/change?" prompt. Prepare a
                folder, drop a brief.txt in it, point --session-dir at it,
                and the run starts without asking you anything.

    brief.md    THE DESIGNER writes it, during the design phase. It is a
                rewrite of your idea addressed to the generated agent, and
                it appears only after an agent has been designed.

`brief.md` sits beside `agent/` deliberately: promoting a good agent is
`cp -r sessions/<s>/agent .agent/agents/<name>`, and a task-specific brief
must not ride along.

`artifacts/` exists because a write-access node otherwise scatters its output
across the repository root. A run that produced data files, reports and caches
invented its own top-level `artifacts/`, `configs/` and `docs/` directories in
someone's project. Source changes belong in the repository -- that is the job --
but everything a run PRODUCES belongs to the run, and lands here where it is
already gitignored and thrown away with the session.

The agent.tmp/ -> agent/ rename is the durable signal that design succeeded.
Without it, a session that crashed between "written" and "validated" would be
ambiguous on resume.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

DOT_AGENT = ".agent"

#: Optional, hand-written: the initial idea, read instead of prompting for it.
INPUT_BRIEF = "brief.txt"

#: The session's own worktree, and the record of how it was made.
WORKSPACE = "workspace"
WORKSPACE_RECORD = "workspace.json"
LEGACY_ROOT = Path.home() / ".codex-langgraph-agent"

GITIGNORE_LINES = (
    "# Written by the agent. Sessions are scratch; agents/ is meant to be kept.",
    "sessions/",
    "config.json",
)


@dataclass(frozen=True)
class SessionPaths:
    repo: Path
    dot_agent: Path
    session: Path
    checkpoint: Path
    staging: Path
    agent_dir: Path
    brief: Path
    input_brief: Path
    request: Path
    artifacts: Path
    plan: Path
    approved: Path
    meta: Path
    agents_dir: Path
    sessions_root: Path
    """Where this session's siblings live -- see isolation_rules()."""
    external: bool = False
    """True when this session's folder is outside <repo>/.agent/ -- because of
    --session-dir or a sessions root somewhere else.
    Two things care: the gitignore we write does not cover it, and the working
    rules handed to write-access nodes must name it explicitly so the agent
    does not treat its own checkpoint as fair game."""

    @property
    def workspace(self) -> Path:
        """Where this session's models work: its own worktree when it has one
        (see create_workspace), else the repository itself."""
        own = self.session / WORKSPACE
        return own if (self.session / WORKSPACE_RECORD).exists() else self.repo

    @property
    def own_workspace(self) -> bool:
        return self.workspace != self.repo

    def has_agent(self) -> bool:
        """True once a validated agent has been written for this session."""
        return (self.agent_dir / "graph.json").exists()

    def is_approved(self) -> bool:
        """True once you have looked at that agent and said yes.

        Separate from has_agent() on purpose. The folder exists as soon as the
        validator renames it -- which is what makes it editable, and what lets
        a resumed session find it -- but existing is not the same as being
        agreed to. Only this decides whether the work phase may start.
        """
        return self.approved.exists()

    def approve(self, note: str = "") -> None:
        self.approved.write_text(note or "approved\n")

    def unapprove(self) -> None:
        """Send an agent back for review -- after editing it, say."""
        self.approved.unlink(missing_ok=True)


#: Where session folders go when nobody says: OUTSIDE the repository, so a
#: run never adds anything to it. AGENT_SESSIONS_ROOT, `sessions_root` in
#: config.json, --sessions-root or the New session dialog choose another.
#: None means <repo>/.agent/sessions, as it used to be -- the test suite
#: uses that, so a test's sessions stay inside its temporary repository.
DEFAULT_SESSIONS_ROOT: Path | None = Path.home() / "research_agent_sessions"


def sessions_root(repo: Path, configured: str | Path | None = None) -> Path:
    """The folder session folders are made in: `configured` (relative to the
    repository when relative), else DEFAULT_SESSIONS_ROOT."""
    repo = Path(repo).resolve()
    if not configured:
        if DEFAULT_SESSIONS_ROOT is None:
            return repo / DOT_AGENT / "sessions"
        return Path(DEFAULT_SESSIONS_ROOT).expanduser().resolve()
    root = Path(configured).expanduser()
    return (root if root.is_absolute() else repo / root).resolve()


def session_paths(
    repo: Path,
    session: str,
    *,
    session_dir: Path | str | None = None,
    root: Path | str | None = None,
) -> SessionPaths:
    """Compute (and create) every path for one session.

    `session` names a folder under the sessions root (`root`, see
    sessions_root()). `session_dir`
    overrides that with a directory of your choosing, anywhere -- the contents
    are identical either way, so everything downstream reads the same fields
    and does not care which route was taken.

    Creating the directories here is not tidiness: SqliteSaver.from_conn_string
    does NOT create parent directories, it raises OperationalError. Measured.
    """
    repo = Path(repo).resolve()
    dot_agent = repo / DOT_AGENT

    siblings = sessions_root(repo, root)
    if session_dir is not None:
        root = Path(session_dir).expanduser().resolve()
        # An unhelpful failure mode to guard: a typo'd --session-dir would
        # otherwise happily mkdir -p a whole new tree and start an empty
        # session, and you would not find out until the brief.txt you wrote
        # "disappeared". Requiring the PARENT to exist catches the typo while
        # still letting you name a fresh subdirectory.
        if not root.exists() and not root.parent.is_dir():
            raise SystemExit(
                f"--session-dir {root} does not exist, and neither does its "
                f"parent {root.parent}.\nCreate the directory first, or check "
                f"the path for a typo."
            )
        if root.exists() and not root.is_dir():
            raise SystemExit(f"--session-dir {root} exists but is not a directory.")
    else:
        root = siblings / _safe(session)
    external = dot_agent not in root.parents

    paths = SessionPaths(
        repo=repo,
        dot_agent=dot_agent,
        session=root,
        checkpoint=root / "checkpoint.sqlite",
        staging=root / "agent.tmp",
        agent_dir=root / "agent",
        brief=root / "brief.md",
        # Written by hand, by you, BEFORE the first run -- the initial idea,
        # in place of typing it at the prompt. Read by cli._resolve_session.
        input_brief=root / INPUT_BRIEF,
        request=root / "request.txt",
        artifacts=root / "artifacts",
        plan=root / "plan.json",
        # Written when you approve the designed agent. A marker FILE rather
        # than a state flag because it has to survive the process: the review
        # gate lives in the bootstrap graph, and without a durable record a
        # resumed run sees a folder on disk, concludes the session has an
        # agent, and skips straight past the gate to execution.
        approved=root / "approved",
        meta=root / "meta.json",
        # Promoted agents stay with the REPOSITORY even for an external
        # session: they are reusable across sessions, which is the point of
        # promoting one, and an external folder is usually one experiment.
        agents_dir=dot_agent / "agents",
        sessions_root=root.parent if session_dir is not None else siblings,
        external=external,
    )

    root.mkdir(parents=True, exist_ok=True)
    paths.artifacts.mkdir(parents=True, exist_ok=True)
    if not external:
        # Only a session that lives in <repo>/.agent/ needs it gitignored.
        # A session anywhere else adds nothing to the repository at all.
        _ensure_gitignore(dot_agent)
    if external:
        _warn_if_committable(paths)
    _notice_legacy(repo)

    return paths


def _warn_if_committable(paths: SessionPaths) -> None:
    """An external session inside the repo is not covered by our gitignore.

    Worth one line: a checkpoint database and a run's artifacts turning up in
    `git status` is surprising, and people usually did not mean it.
    """
    try:
        relative = paths.session.relative_to(paths.repo)
    except ValueError:
        return  # outside the repo entirely -- git will never see it
    print(
        f"Note: session folder {relative}/ is inside the repository and is NOT "
        f"gitignored.\n"
        f"      Add it to .gitignore if you do not want the checkpoint and "
        f"artifacts committed."
    )


def new_session_id(task: str, root: Path | None = None) -> str:
    """A fresh id for a new session: a few words of the task, the time, and a
    random suffix -- e.g. `compare-hotel-staff-skill-20261018-1405-a3f9`.

    Never derived from the task alone. That was the old rule, so the same
    words reopened the old session, its agent and the files it left, which is
    the interference this replaces. Readable words so a folder listing still
    says what each one is; the time so they sort; the suffix so two started
    in the same minute differ.
    """
    import re
    import secrets
    import time

    words = re.findall(r"[a-z0-9]+", task.lower())[:5]
    slug = "-".join(words)[:40].strip("-") or "task"
    stamp = time.strftime("%Y%m%d-%H%M")
    while True:
        name = f"{slug}-{stamp}-{secrets.token_hex(2)}"
        if root is None or not (Path(root) / name).exists():
            return name


def create_workspace(paths: SessionPaths) -> str:
    """Give a NEW session its own, empty project: <session>/workspace, a git
    repository with one empty commit to compare against. Returns a line
    saying what was done ("" when nothing was). See "EACH SESSION IS ITS OWN
    PROJECT".

    Only for a session that has not started: one that already ran keeps the
    workspace it had.
    """
    if (paths.session / WORKSPACE_RECORD).exists():
        return ""
    if paths.checkpoint.exists():
        return ""
    import subprocess
    import time

    workspace = paths.session / WORKSPACE

    def git(*argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(workspace), *argv],
                              capture_output=True, text=True, timeout=60)

    workspace.mkdir(parents=True, exist_ok=True)
    try:
        made = git("init", "-q")
        # An empty first commit: what "the files this session changed" is
        # measured against, and somewhere for the run to commit on top of.
        committed = git("-c", "user.name=research-agent", "-c",
                        "user.email=research-agent@localhost", "commit", "-q",
                        "--allow-empty", "-m", f"Start of session {paths.session.name}")
        base = git("rev-parse", "HEAD").stdout.strip()
        if made.returncode != 0 or committed.returncode != 0 or not base:
            raise OSError((made.stderr or committed.stderr).strip()[:300])
    except (OSError, subprocess.SubprocessError) as exc:
        base = ""
        note = f" (git could not set it up: {exc}; it is a plain folder)"
    else:
        note = ""
    (paths.session / WORKSPACE_RECORD).write_text(json.dumps({
        "kind": "new_project", "base": base,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }, indent=2) + "\n")
    return f"Workspace: {workspace} -- a new, empty project of its own{note}."


def workspace_record(paths: SessionPaths) -> dict:
    try:
        return json.loads((paths.session / WORKSPACE_RECORD).read_text())
    except (OSError, ValueError):
        return {}


class DeleteRefused(Exception):
    """Not deleted, and why -- the folder is not a session's, or is the project."""


def delete_session(paths: SessionPaths, *, drop_branch: bool = False) -> list[str]:
    """Delete a session: its folder (conversation, agent, results -- for good),
    its worktree, its resume mark, and with `drop_branch` its branch too.
    Returns what was done, one line each.

    Refuses anything that does not look like a session folder, or that holds
    the project: `--session-dir` can point a session anywhere, and an
    rmtree aimed by an HTTP request must not be able to reach the repository.
    """
    folder = paths.session.resolve()
    repo = paths.repo.resolve()
    if repo.is_relative_to(folder) or folder in (Path(folder.anchor), Path.home().resolve()):
        raise DeleteRefused(f"{folder} contains the project or is a top-level folder; "
                            f"not deleting it.")
    if not any((folder / name).exists() for name in
               ("request.txt", "checkpoint.sqlite", INPUT_BRIEF, WORKSPACE_RECORD)):
        raise DeleteRefused(f"{folder} does not look like a session folder; not deleting it.")

    import subprocess

    def git(*argv: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(repo), *argv],
                              capture_output=True, text=True, timeout=120)

    done = [f"Deleted {folder}."]
    record = workspace_record(paths)
    branch = record.get("branch", "")
    # Only an older session's workspace is a worktree of the repository;
    # a new one is its own repository and goes with the folder.
    if not branch:
        record = {}
    if record:
        # Before the folder goes: git refuses to delete a branch a worktree
        # still has checked out, and removing it properly leaves no record.
        git("worktree", "remove", "--force", str(folder / WORKSPACE))
    shutil.rmtree(folder, ignore_errors=True)
    if folder.exists():
        raise DeleteRefused(f"Could not delete everything in {folder}.")
    if record:
        git("worktree", "prune")
        if drop_branch:
            gone = git("branch", "-D", branch)
            done.append(f"Deleted branch {branch}." if gone.returncode == 0 else
                        f"Could not delete branch {branch}: {gone.stderr.strip()[:200]}")
        else:
            done.append(f"Its code is kept on branch {branch} "
                        f"(git branch -D {branch} to remove it).")
    unmark_running(paths)
    return done


def _skills_dir() -> Path:
    from agent import skills

    return skills.skills_dir()


def isolation_rules(paths: SessionPaths) -> str:
    """What every model is told about where it may work -- appended to the
    design roles' and every generated node's instructions.

    Measured: a session's discussor and planner opened earlier sessions'
    folders and planned around what they found there ("reuse the existing
    preregistration") -- another task's work, as if it were this one's. And
    sessions that worked in the agent's own repository found its code and
    old experiments' leftovers, and treated them as the project. Short on
    purpose: it rides in developer instructions, where size has a cost.
    """
    text = (
        f"\n\nTHIS SESSION IS SELF-CONTAINED. Its folder is {paths.session}; "
        f"work only inside it. "
    )
    if paths.own_workspace and not workspace_record(paths).get("branch"):
        text += (
            f"The project is {paths.workspace}: a new git repository of this "
            f"session's own, which started empty -- write the code there. "
            f"Nothing outside the session folder is part of this task: never "
            f"read, use or write the agent's own code at {paths.repo}, or "
            f"other sessions' folders under {paths.sessions_root}. Outside it "
            f"you may only read the shared skills in {_skills_dir()} (never "
            f"change them), use installed tools, install packages, and "
            f"download data, models and papers into this session.")
    else:
        text += (
            f"Other sessions' folders (under {paths.sessions_root}) belong to "
            f"other tasks: never open, use or cite anything in them.")
        if paths.own_workspace:
            text += (f" You work in {paths.workspace}, this session's own copy of "
                     f"the project; never change anything in {paths.repo}.")
    return text


# ---- what survives a server restart ---------------------------------------
#
# A session that was RUNNING when the web server stopped -- killed, crashed,
# restarted to pick up new code -- carries on when it starts again. The mark is
# a small file per session, pointing at the session folder wherever its
# sessions root is. Written when a run starts; removed when it ends for any
# reason except the server stopping under it.
#
# The marks live in the user's state folder, not in the repository: nothing
# a run does adds files to the repository. AGENT_STATE_DIR moves it (the
# tests point it at a temporary folder).

def state_dir() -> Path:
    import os

    return Path(os.environ.get("AGENT_STATE_DIR")
                or Path.home() / ".codex-langgraph-agent").expanduser()


def _running_dir(repo: Path) -> Path:
    return state_dir() / "running"


def mark_running(paths: SessionPaths) -> None:
    folder = _running_dir(paths.repo)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{paths.session.name}.json").write_text(
        json.dumps({"session_dir": str(paths.session),
                    "repo": str(Path(paths.repo).resolve())}) + "\n")


def unmark_running(paths: SessionPaths) -> None:
    forget_running(paths.session.name)


def forget_running(session_name: str) -> None:
    (state_dir() / "running" / f"{session_name}.json").unlink(missing_ok=True)


def _adopt_legacy_marks(repo: Path) -> None:
    """Marks used to be written to <repo>/.agent/running/. Move any there to
    the state folder, so a session that was running under the old layout
    still resumes -- and the repository is left without them."""
    legacy = Path(repo).resolve() / DOT_AGENT / "running"
    if not legacy.is_dir():
        return
    folder = _running_dir(repo)
    for mark in legacy.glob("*.json"):
        try:
            record = json.loads(mark.read_text())
            record.setdefault("repo", str(Path(repo).resolve()))
            folder.mkdir(parents=True, exist_ok=True)
            (folder / mark.name).write_text(json.dumps(record) + "\n")
            mark.unlink()
        except (OSError, ValueError):
            continue
    try:
        legacy.rmdir()
    except OSError:
        pass


def marked_running(repo: Path) -> list[Path]:
    """The session folders that were running when the server for `repo` last
    stopped."""
    _adopt_legacy_marks(repo)
    folder = _running_dir(repo)
    repo = str(Path(repo).resolve())
    out = []
    for mark in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            record = json.loads(mark.read_text())
            if record.get("repo", repo) == repo:
                out.append(Path(record["session_dir"]))
        except (ValueError, KeyError, OSError):
            mark.unlink(missing_ok=True)
    return out


#: The choices a session was started with -- models, supervisor, a pre-built
#: agent -- so reopening it, or resuming it after a restart, runs it the same
#: way. They used to live only in the request that created it.
OPTIONS_FILE = "options.json"


def save_options(paths: SessionPaths, options: dict) -> None:
    (paths.session / OPTIONS_FILE).write_text(json.dumps(options, indent=2) + "\n")


def load_options(paths: SessionPaths) -> dict:
    try:
        value = json.loads((paths.session / OPTIONS_FILE).read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def list_sessions(repo: Path, root: Path | str | None = None) -> list[tuple[str, str, bool]]:
    """(name, first line of brief, has an agent) for every session under the
    sessions root, newest first."""
    root = sessions_root(repo, root)
    if not root.exists():
        return []

    out = []
    for entry in sorted(root.iterdir(), key=lambda e: e.stat().st_mtime, reverse=True):
        if not entry.is_dir() or not (entry / "request.txt").exists() and not (
                entry / INPUT_BRIEF).exists() and not (entry / "checkpoint.sqlite").exists():
            continue
        # The REQUEST identifies the session. Fall back to a hand-written
        # brief.txt for a folder prepared but never yet run, then to brief.md
        # -- the designer's rewrite, which is not the same thing as either.
        text = ""
        for name in ("request.txt", INPUT_BRIEF, "brief.md"):
            source = entry / name
            if source.exists() and source.read_text().strip():
                text = source.read_text().strip().splitlines()[0]
                break
        out.append((entry.name, text, (entry / "agent" / "graph.json").exists()))
    return out


#: How much of a transcript to hand back. A long design conversation is a few
#: dozen entries; this is a bound against a pathological one, not a budget.
MAX_TRANSCRIPT = 400


def read_transcript(paths: SessionPaths) -> list[dict]:
    """The session's conversation, read straight out of the checkpoint.

    Why this exists: the browser's chat history lived ONLY in an in-memory
    event buffer, so restarting the server emptied it -- and the conversation
    that produced an agent is not a log, it is the thing somebody came back to
    read. The checkpoint has had it all along.

    Read-only, on its own connection, so opening a page can never disturb a
    run that is writing to the same database. Both phases are returned in
    order: the design conversation first, then the work one, because that is
    the order they happened in and they are one session to the person reading.

    Never raises. A session with no checkpoint yet, a database locked by
    something else, a schema from an older version -- all mean "no history to
    show", which is a fact about the page and not an error worth a traceback.
    """
    if not paths.checkpoint.exists():
        return []

    try:
        import sqlite3

        from langgraph.checkpoint.sqlite import SqliteSaver

        conn = sqlite3.connect(f"file:{paths.checkpoint}?mode=ro", uri=True,
                               check_same_thread=False)
        try:
            saver = SqliteSaver(conn)
            # Newest checkpoint per thread. list() yields newest first, so the
            # first sighting of a thread is the one to keep.
            newest: dict[str, list] = {}
            for item in saver.list(None, limit=1000):
                thread = str(item.config.get("configurable", {}).get("thread_id", ""))
                if thread in newest:
                    continue
                entries = item.checkpoint.get("channel_values", {}).get("transcript")
                newest[thread] = list(entries) if isinstance(entries, list) else []
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 -- see the docstring
        return []

    out: list[dict] = []
    # The design conversation first, then the work one -- the order they
    # happened in, and one session to the person reading it.
    #
    # A plain sort is enough and is all that is here: every thread this
    # project creates is "<session>:bootstrap", "<session>:work" or
    # "<session>:work2", which sort into exactly that order. An earlier
    # version had a bespoke key to force it; no test could tell the two
    # apart, which is the definition of cleverness not worth keeping. A phase
    # named so that this breaks would need a real ordering, and a test.
    for thread in sorted(newest):
        for entry in newest[thread]:
            if isinstance(entry, dict) and entry.get("text"):
                out.append({"role": str(entry.get("role", "?")),
                            "text": str(entry["text"])})
    return out[-MAX_TRANSCRIPT:]


def builtin_agents_dir() -> Path:
    """Agents shipped with the tool itself."""
    return Path(__file__).resolve().parent / "builtin_agents"


def resolve_agent(spec: str, paths: SessionPaths | None = None) -> Path:
    """Turn --pre-build-agent's argument into a folder path.

    Accepts, in order: an explicit path, a name under <repo>/.agent/agents/,
    or the name of an agent shipped with the tool.
    """
    candidate = Path(spec).expanduser()
    if candidate.is_dir():
        return candidate.resolve()

    if paths is not None:
        promoted = paths.agents_dir / spec
        if promoted.is_dir():
            return promoted

    builtin = builtin_agents_dir() / spec
    if builtin.is_dir():
        return builtin

    known = ", ".join(sorted(p.name for p in builtin_agents_dir().iterdir() if p.is_dir()))
    extra = ""
    if paths is not None and paths.agents_dir.exists():
        promoted_names = sorted(p.name for p in paths.agents_dir.iterdir() if p.is_dir())
        if promoted_names:
            extra = f"\nIn this repo: {', '.join(promoted_names)}"
    raise SystemExit(
        f"No agent named {spec!r}, and it is not a directory.\n"
        f"Built in: {known}{extra}"
    )


def promote(paths: SessionPaths, name: str, *, overwrite: bool = False) -> Path:
    """Copy this session's agent into <repo>/.agent/agents/<name>."""
    if not paths.has_agent():
        raise SystemExit(f"Session {paths.session.name!r} has no agent to promote yet.")

    target = paths.agents_dir / _safe(name)
    if target.exists() and not overwrite:
        raise SystemExit(f"{target} already exists. Pass --force to replace it.")
    if target.exists():
        shutil.rmtree(target)

    shutil.copytree(paths.agent_dir, target)
    return target


def _ensure_gitignore(dot_agent: Path) -> None:
    """Keep sessions out of git, but leave agents/ committable -- that is the
    whole point of promoting one."""
    path = dot_agent / ".gitignore"
    if path.exists():
        return
    dot_agent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(GITIGNORE_LINES) + "\n")


_NOTICED: set[Path] = set()


def _notice_legacy(repo: Path) -> None:
    """Tell the human where their pre-folder sessions went. Nothing is migrated:
    the schema changed completely, and a half-read checkpoint is the worst
    thing to debug."""
    if not LEGACY_ROOT.exists() or repo in _NOTICED:
        return
    # Once per repository per process: session_paths runs for every session
    # opened, and a server resuming two sessions printed it twice.
    _NOTICED.add(repo)
    import hashlib

    digest = hashlib.sha256(str(repo).encode()).hexdigest()[:16]
    for legacy in (LEGACY_ROOT / f"{digest}-v2.sqlite", LEGACY_ROOT / f"{digest}.sqlite"):
        if legacy.exists():
            print(
                f"Note: an older session database exists at {legacy}.\n"
                f"      Sessions now live in {repo / DOT_AGENT / 'sessions'}/.\n"
                f"      The old file is left untouched."
            )
            return


def _safe(name: str) -> str:
    """Keep a session or agent name usable as a single directory component."""
    cleaned = "".join(c if c.isalnum() or c in "-_." else "-" for c in name).strip("-.")
    if not cleaned:
        raise SystemExit(f"Unusable name: {name!r}")
    return cleaned
