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
            plan.json           the numbered steps -- EDIT THIS before approving
            artifacts/          where a run puts its OUTPUT -- see below
            agent.tmp/          staging; renamed to agent/ only once valid
            agent/              the agent designed for this session
        agents/<name>/          promoted, committed, reusable

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

import shutil
from dataclasses import dataclass
from pathlib import Path

DOT_AGENT = ".agent"

#: Optional, hand-written: the initial idea, read instead of prompting for it.
INPUT_BRIEF = "brief.txt"
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
    meta: Path
    agents_dir: Path
    external: bool = False
    """True when --session-dir put this session outside <repo>/.agent/sessions/.
    Two things care: the gitignore we write does not cover it, and the working
    rules handed to write-access nodes must name it explicitly so the agent
    does not treat its own checkpoint as fair game."""

    def has_agent(self) -> bool:
        """True once a validated agent has been written for this session."""
        return (self.agent_dir / "graph.json").exists()


def session_paths(
    repo: Path,
    session: str,
    *,
    session_dir: Path | str | None = None,
) -> SessionPaths:
    """Compute (and create) every path for one session.

    `session` names a folder under <repo>/.agent/sessions/. `session_dir`
    overrides that with a directory of your choosing, anywhere -- the contents
    are identical either way, so everything downstream reads the same fields
    and does not care which route was taken.

    Creating the directories here is not tidiness: SqliteSaver.from_conn_string
    does NOT create parent directories, it raises OperationalError. Measured.
    """
    repo = Path(repo).resolve()
    dot_agent = repo / DOT_AGENT

    external = session_dir is not None
    if external:
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
        root = dot_agent / "sessions" / _safe(session)

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
        meta=root / "meta.json",
        # Promoted agents stay with the REPOSITORY even for an external
        # session: they are reusable across sessions, which is the point of
        # promoting one, and an external folder is usually one experiment.
        agents_dir=dot_agent / "agents",
        external=external,
    )

    root.mkdir(parents=True, exist_ok=True)
    paths.artifacts.mkdir(parents=True, exist_ok=True)
    paths.agents_dir.mkdir(parents=True, exist_ok=True)
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


def session_name_for(task: str) -> str:
    """A stable, readable session name derived from the task itself.

    Why derive it rather than default to "main": a session owns ONE task. It
    holds the conversation that shaped the agent, the agent designed from that
    conversation, and the brief that agent runs against. Pointing a second,
    unrelated task at the same session silently reuses an agent built for the
    first one -- which is exactly the bug this replaces.

    Deriving from the task gives both properties at once: the same task resumes
    where it left off, and a different task gets its own session without
    anyone having to remember --session.

    The hash suffix matters. Two tasks can easily share their first few words
    ("add tests for the parser" / "add tests for the lexer"), and colliding
    those would resurrect the very bug we are fixing.
    """
    import hashlib
    import re

    words = re.findall(r"[a-z0-9]+", task.lower())[:5]
    slug = "-".join(words)[:40].strip("-") or "task"
    digest = hashlib.sha256(task.strip().encode()).hexdigest()[:6]
    return f"{slug}-{digest}"


def list_sessions(repo: Path) -> list[tuple[str, str, bool]]:
    """(name, first line of brief, has an agent) for every session in a repo."""
    root = Path(repo).resolve() / DOT_AGENT / "sessions"
    if not root.exists():
        return []

    out = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
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


def _notice_legacy(repo: Path) -> None:
    """Tell the human where their pre-folder sessions went. Nothing is migrated:
    the schema changed completely, and a half-read checkpoint is the worst
    thing to debug."""
    if not LEGACY_ROOT.exists():
        return
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
