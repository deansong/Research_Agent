"""
WHAT:  The files in this session's own folder, and what is in them.
WHY:   A run produces a plan, an agent, results and reports, and the browser
       should show them -- but only them. This tab used to list every changed
       file in the whole repository and read any file inside it: the tool's
       own source (agent/, webui/), other sessions' work, anything. On a server
       started with --host 0.0.0.0 that is the repository, readable by anyone
       who can reach the port.
CONCEPT: One root, the session folder; one listing; one reader; and a
       containment check that is the whole security story.

--------------------------------------------------------------------------
WHAT IS LISTED
--------------------------------------------------------------------------
Everything in the session folder -- the designed agent, the plan, the brief,
the supervisor's iterations, and artifacts/, where a run puts what it
produces -- except the checkpoint database and the per-turn records under
activity/, which are not text to read and have their own panels.

And the session's CODE: what it changed in its own workspace (a git worktree
of the project, see agent/storage.py) since it was made -- not the whole
worktree, which is a copy of the project, tool source and all. Only those
files can be read from it.

Code first, then the session's files, then results, each capped separately. Measured on a real
session: 1,985 artifacts, one JSON per GPU allocation. Sorted into one list by
time, the dozen files somebody wants to read sit past row nine hundred, and the
cap can drop them entirely.

--------------------------------------------------------------------------
THE CONTAINMENT CHECK
--------------------------------------------------------------------------
`read()` takes a path from the browser. Everything else here is listing;
this is the one function an attacker would aim at, and `../../.ssh/id_rsa`
is the whole attack. So the path is resolved and then checked to be inside
the session folder with Path.is_relative_to, AFTER resolution -- before it, a
symlink pointing out of the tree passes a string prefix test and then reads
whatever it points at.

The root is resolved too. On this machine /home/xingyi/ssd is a symlink into
/export/ssd, so a root that is not resolved and a path that is would never
compare equal and every read would be refused -- the failure is closed, but
it is still a bug and it took a real path to notice.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: Never listed, at any depth: noise nobody has ever wanted to read.
SKIP_DIRS = frozenset({
    ".agent", ".git", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "node_modules", ".venv", "venv", ".ipynb_checkpoints",
})

#: Not listed at the top of the session folder: the per-turn records and a
#: design being written, which have their own panels. (Only there -- a run is
#: free to make an artifacts/activity/ of its own.)
SKIP_TOP = frozenset({"activity", "agent.tmp", "workspace"})

#: The one root, as it appears at the front of every listed path.
ROOT = "session"

#: Read as text. Anything else is listed with its size and refused a body,
#: because a 400 MB checkpoint rendered as mojibake helps nobody.
TEXT_SUFFIXES = frozenset({
    ".py", ".js", ".ts", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".cfg",
    ".ini", ".md", ".txt", ".csv", ".tsv", ".sh", ".bash", ".sql", ".r", ".R",
    ".ipynb", ".lock", ".xml", ".html", ".css", ".log", ".env-example", "",
})

#: A file larger than this is listed but not sent whole. Training logs reach
#: hundreds of megabytes and the browser is not the tool for those.
MAX_BYTES = 2_000_000

#: Enough to see everything a run produces; bounded so a stray output
#: directory with 200,000 files cannot hang the request.
MAX_FILES = 2000


@dataclass(frozen=True)
class FileEntry:
    path: str          #: relative to the session folder, "session/" in front
    root: str          #: always "session"
    size: int
    modified: float
    origin: str        #: "code" (changed in the workspace), "session" or "artifact"
    readable: bool     #: text, and small enough to send

    def as_dict(self) -> dict:
        return {"path": self.path, "root": self.root, "size": self.size,
                "modified": self.modified, "origin": self.origin,
                "readable": self.readable}


def listing(session: Path) -> list[dict]:
    """Every file in the session folder: its own files first, then results."""
    code = [entry for entry in (_entry(Path(session) / "workspace" / name, Path(session))
                                for name in sorted(_workspace_changes(Path(session))))
            if entry is not None]
    own, results = [], []
    for target in _walk(Path(session), skip_top=SKIP_TOP):
        if target.name.startswith("checkpoint.sqlite"):
            continue
        entry = _entry(target, Path(session))
        if entry is not None:
            (results if entry.origin == "artifact" else own).append(entry)
    for group in (code, own, results):
        group.sort(key=lambda e: -e.modified)
    return [entry.as_dict()
            for entry in code[:MAX_FILES] + own[:MAX_FILES] + results[:MAX_FILES]]


def read(session: Path, path: str) -> dict:
    """One file's text, or a plain refusal saying which limit it hit."""
    root = Path(session).resolve()
    root_name, _, relative = path.partition("/")
    if root_name != ROOT or not relative:
        raise FileNotFoundError(f"{path!r} is not a file in this session.")

    target = (root / relative).resolve()
    # AFTER resolve, on both sides. See the note at the top of this file.
    if not target.is_relative_to(root):
        raise PermissionError(f"{path!r} resolves outside this session's folder.")
    if not target.is_file():
        raise FileNotFoundError(f"{path!r} is not a file.")
    # The workspace is a whole copy of the project: only what the session
    # changed in it is this session's to show.
    workspace = root / "workspace"
    if target.is_relative_to(workspace.resolve()):
        if target.relative_to(workspace.resolve()).as_posix() not in _workspace_changes(root):
            raise PermissionError(f"{path!r} is part of the project, not something "
                                  f"this session changed.")

    size = target.stat().st_size
    if not _is_text(target):
        return {"path": path, "size": size, "text": "",
                "skipped": "not a text file"}
    if size > MAX_BYTES:
        return {"path": path, "size": size, "text": "",
                "skipped": f"{size:,} bytes is over the {MAX_BYTES:,} limit"}

    # errors="replace" rather than a raise: a file with one bad byte in it is
    # still worth reading, and a traceback in its place is not.
    return {"path": path, "size": size,
            "text": target.read_text(encoding="utf-8", errors="replace"),
            "skipped": ""}


def _walk(root: Path, skip_top: frozenset = frozenset()):
    """Every file under `root`, skipping the directories nobody reads.

    rglob("*") would descend into .git and __pycache__ and then filter, which
    on this project is about 40,000 wasted stat calls. This prunes instead.
    """
    stack = [root]
    seen = 0
    while stack and seen < MAX_FILES * 4:
        current = stack.pop()
        try:
            children = list(current.iterdir())
        except OSError:
            continue
        for child in children:
            seen += 1
            if child.name in SKIP_DIRS or child.name.startswith("."):
                continue
            if current == root and child.name in skip_top:
                continue
            if child.is_symlink():
                # Listed files must be real files under the root. A symlink is
                # the same escape read() guards against, arriving by a
                # different door.
                continue
            if child.is_dir():
                stack.append(child)
            elif child.is_file():
                yield child


def _workspace_changes(session: Path) -> set[str]:
    """Files the session changed in its workspace since it was made: tracked
    files that differ from the base (committed on its branch or not), plus
    new files git does not ignore. Empty without a workspace."""
    workspace = session / "workspace"
    try:
        base = json.loads((session / "workspace.json").read_text())["base"]
    except (OSError, ValueError, KeyError):
        return set()
    names: set[str] = set()
    for argv in (["diff", "--name-only", base],
                 ["ls-files", "--others", "--exclude-standard"]):
        try:
            done = subprocess.run(["git", "-C", str(workspace), *argv],
                                  capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode == 0:
            names.update(line.strip() for line in done.stdout.splitlines() if line.strip())
    return {name for name in names if (workspace / name).is_file()
            and not (workspace / name).is_symlink()}


def _entry(target: Path, session: Path) -> FileEntry | None:
    try:
        stat = target.stat()
        relative = target.relative_to(session)
    except (OSError, ValueError):
        return None
    return FileEntry(
        path=f"{ROOT}/{relative.as_posix()}", root=ROOT,
        size=stat.st_size, modified=stat.st_mtime,
        origin={"artifacts": "artifact", "workspace": "code"}.get(relative.parts[0], "session"),
        readable=_is_text(target) and stat.st_size <= MAX_BYTES,
    )


def _is_text(target: Path) -> bool:
    return target.suffix.lower() in TEXT_SUFFIXES
