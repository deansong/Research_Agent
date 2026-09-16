"""
WHAT:  Which files the agent produced, and what is in them.
WHY:   A run writes code -- a dataloader, a training script, a baseline -- and
       until now the only way to read any of it was to leave the browser and
       go find it on disk. The node inspector can show you the prompt that
       asked for a file and the command that ran it, and not the file.
CONCEPT: Two roots, one listing, one reader, and a containment check that is
       the whole security story.

--------------------------------------------------------------------------
WHICH FILES COUNT AS "GENERATED"
--------------------------------------------------------------------------
The repository is the user's own project and mostly predates the run, so
listing all of it would bury the eight files that matter under four hundred
that do not. The question is really "what changed because of this agent", and
git already answers it exactly: untracked plus modified, which is how the
agent's output was identified by hand when it first became a problem.

Git is asked, not assumed. A session can point at a directory that is not a
repository at all, and there the fallback is mtime: files touched since the
session folder was created. Cruder -- a `pip install` inside the repo would
show up -- but never wrong in the direction that hides something.

The artifacts directory is listed whole. It exists only because of this run,
so every file in it is by definition produced by it.

--------------------------------------------------------------------------
THE CONTAINMENT CHECK
--------------------------------------------------------------------------
`read()` takes a path from the browser. Everything else here is listing;
this is the one function an attacker would aim at, and `../../.ssh/id_rsa`
is the whole attack. So the path is resolved and then checked to be inside a
root with Path.is_relative_to, AFTER resolution -- before it, a symlink
pointing out of the tree passes a string prefix test and then reads whatever
it points at.

Roots are resolved too. On this machine /home/xingyi/ssd is a symlink into
/export/ssd, so a root that is not resolved and a path that is would never
compare equal and every read would be refused -- the failure is closed, but
it is still a bug and it took a real path to notice.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

#: Never listed. Checkpoints and activity are shown elsewhere in their own
#: panels, and __pycache__ is noise nobody has ever wanted to read.
SKIP_DIRS = frozenset({
    ".agent", ".git", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "node_modules", ".venv", "venv", ".ipynb_checkpoints",
})

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
    path: str          #: relative to its root, with the root name in front
    root: str          #: "repo" or "artifacts"
    size: int
    modified: float
    origin: str        #: "new", "modified", or "artifact"
    readable: bool     #: text, and small enough to send

    def as_dict(self) -> dict:
        return {"path": self.path, "root": self.root, "size": self.size,
                "modified": self.modified, "origin": self.origin,
                "readable": self.readable}


def listing(repo: Path, artifacts: Path, since: float | None = None) -> list[dict]:
    """Every file this run appears to have produced: code first, then results.

    CODE FIRST IS NOT COSMETIC, and the real data is why. This session had 14
    changed source files and 1,985 artifacts -- per-run JSON, one file per GPU
    allocation. Sorted by time into one list, the eight files somebody wants
    to read sit somewhere past the nine hundredth row, and the MAX_FILES cap
    can drop them off the end entirely: an artifact written one second later
    outranks the training script, so a busy run would show a listing with no
    code in it at all.

    So the two roots are capped and sorted separately. Newest first inside
    each, because within one kind recency is the right order.
    """
    code = sorted(_repo_changes(Path(repo), since), key=lambda e: -e.modified)
    results = sorted(_tree(Path(artifacts), "artifacts"), key=lambda e: -e.modified)
    found = code[:MAX_FILES] + results[:MAX_FILES]
    return [entry.as_dict() for entry in found]


def read(repo: Path, artifacts: Path, path: str) -> dict:
    """One file's text, or a plain refusal saying which limit it hit."""
    roots = {"repo": Path(repo).resolve(), "artifacts": Path(artifacts).resolve()}
    root_name, _, relative = path.partition("/")
    root = roots.get(root_name)
    if root is None or not relative:
        raise FileNotFoundError(f"{path!r} does not name a listed root.")

    target = (root / relative).resolve()
    # AFTER resolve, on both sides. See the note at the top of this file.
    if not target.is_relative_to(root):
        raise PermissionError(f"{path!r} resolves outside {root_name}.")
    if not target.is_file():
        raise FileNotFoundError(f"{path!r} is not a file.")

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


# ---- how the two roots are found -----------------------------------------


def _repo_changes(repo: Path, since: float | None) -> list[FileEntry]:
    changed = _git_changes(repo)
    if changed is None:
        changed = _touched_since(repo, since)
    return changed


def _git_changes(repo: Path) -> list[FileEntry] | None:
    """Untracked and modified files, or None if this is not a git repository.

    `--porcelain` is the machine-readable format and is explicitly stable
    across versions; the human one is not, and has changed.
    """
    try:
        done = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain", "-uall"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None

    entries = []
    for line in done.stdout.splitlines():
        if len(line) < 4:
            continue
        code, name = line[:2], line[3:].strip()
        # A rename prints "old -> new"; the new name is the one that exists.
        if " -> " in name:
            name = name.split(" -> ", 1)[1]
        name = name.strip('"')
        if _skipped(name):
            continue
        target = repo / name
        if not target.is_file():
            continue
        entry = _entry(target, repo, "repo",
                       "new" if code.strip() == "??" else "modified")
        if entry is not None:
            entries.append(entry)
    return entries


def _touched_since(repo: Path, since: float | None) -> list[FileEntry]:
    """The fallback for a directory git does not manage."""
    if since is None:
        return []
    entries = []
    for target in _walk(repo):
        try:
            if target.stat().st_mtime >= since:
                entry = _entry(target, repo, "repo", "new")
                if entry is not None:
                    entries.append(entry)
        except OSError:
            continue
    return entries


def _tree(root: Path, name: str) -> list[FileEntry]:
    if not root.is_dir():
        return []
    entries = []
    for target in _walk(root):
        entry = _entry(target, root, name, "artifact")
        if entry is not None:
            entries.append(entry)
    return entries


def _walk(root: Path):
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
            if child.is_symlink():
                # Listed files must be real files under the root. A symlink is
                # the same escape read() guards against, arriving by a
                # different door.
                continue
            if child.is_dir():
                stack.append(child)
            elif child.is_file():
                yield child


def _entry(target: Path, root: Path, name: str, origin: str) -> FileEntry | None:
    try:
        stat = target.stat()
        relative = target.relative_to(root)
    except (OSError, ValueError):
        return None
    return FileEntry(
        path=f"{name}/{relative.as_posix()}",
        root=name, size=stat.st_size, modified=stat.st_mtime, origin=origin,
        readable=_is_text(target) and stat.st_size <= MAX_BYTES,
    )


def _skipped(name: str) -> bool:
    parts = Path(name).parts
    return any(part in SKIP_DIRS or part.startswith(".") for part in parts[:-1]) \
        or Path(name).name.startswith(".")


def _is_text(target: Path) -> bool:
    return target.suffix.lower() in TEXT_SUFFIXES
