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
            checkpoint.sqlite   both phases live here, under two thread ids
            meta.json           schema version, so a stale session refuses
            request.txt         what YOU asked for -- identifies the session
            brief.md            the designer's rewrite of it, for the agent
            agent.tmp/          staging; renamed to agent/ only once valid
            agent/              the agent designed for this session
        agents/<name>/          promoted, committed, reusable

`brief.md` sits beside `agent/` deliberately: promoting a good agent is
`cp -r sessions/<s>/agent .agent/agents/<name>`, and a task-specific brief
must not ride along.

The agent.tmp/ -> agent/ rename is the durable signal that design succeeded.
Without it, a session that crashed between "written" and "validated" would be
ambiguous on resume.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

DOT_AGENT = ".agent"
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
    request: Path
    meta: Path
    agents_dir: Path

    def has_agent(self) -> bool:
        """True once a validated agent has been written for this session."""
        return (self.agent_dir / "graph.json").exists()


def session_paths(repo: Path, session: str) -> SessionPaths:
    """Compute (and create) every path for one session.

    Creating the directories here is not tidiness: SqliteSaver.from_conn_string
    does NOT create parent directories, it raises OperationalError. Measured.
    """
    repo = Path(repo).resolve()
    dot_agent = repo / DOT_AGENT
    session_dir = dot_agent / "sessions" / _safe(session)

    paths = SessionPaths(
        repo=repo,
        dot_agent=dot_agent,
        session=session_dir,
        checkpoint=session_dir / "checkpoint.sqlite",
        staging=session_dir / "agent.tmp",
        agent_dir=session_dir / "agent",
        brief=session_dir / "brief.md",
        request=session_dir / "request.txt",
        meta=session_dir / "meta.json",
        agents_dir=dot_agent / "agents",
    )

    session_dir.mkdir(parents=True, exist_ok=True)
    paths.agents_dir.mkdir(parents=True, exist_ok=True)
    _ensure_gitignore(dot_agent)
    _notice_legacy(repo)

    return paths


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
        # The REQUEST identifies the session; brief.md is the designer's
        # rewrite of it for the generated agent, and is not the same thing.
        source = entry / "request.txt"
        if not source.exists():
            source = entry / "brief.md"
        text = source.read_text().strip().splitlines()[0] if source.exists() else ""
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
