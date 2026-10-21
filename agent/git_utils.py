"""
WHAT:  A read-only snapshot of the repository's git state.
WHY:   After the executor edits files, the orchestrator needs to see what
       actually changed -- the executor's own summary of its work is not
       evidence. `git status` is.
CONCEPT: Not LangGraph. Note this is the only subprocess in the project; the
       model providers are all in-process or handled in agent/backends/.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class GitSnapshot:
    status: str
    diff_stat: str


def snapshot(repo_path: str) -> GitSnapshot:
    return GitSnapshot(
        status=_git(repo_path, ["status", "--short"]),
        diff_stat=_git(repo_path, ["diff", "--stat"]),
    )


# git's own error output can be enormous -- `git diff` in a non-repository
# prints its whole usage message. Whatever comes back here ends up in graph
# state AND in the orchestrator's prompt, so it has to stay small.
_MAX_OUTPUT_CHARS = 4000


def _git(repo_path: str, args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError:
        return "git is not installed"
    except subprocess.TimeoutExpired:
        return "git timed out after 30s"
    except OSError as exc:
        return f"git unavailable: {exc}"

    if result.returncode != 0:
        # Return the FIRST line only. The old code returned all of stderr,
        # which for a non-repository is ~200 lines of `git diff --help`
        # output -- and every one of those lines was being pasted into the
        # orchestrator's prompt.
        first_line = (result.stderr.strip().splitlines() or [""])[0]
        return f"git error: {first_line}" if first_line else "git error"

    output = result.stdout.strip()
    if len(output) > _MAX_OUTPUT_CHARS:
        output = output[:_MAX_OUTPUT_CHARS] + "\n... (truncated)"
    return output


# ---------------------------------------------------------------------------
# what ONE turn changed
# ---------------------------------------------------------------------------
# `git diff --stat` after a node shows everything every node has changed so
# far, so a checker reading it cannot tell this worker's edits from the last
# one's. A fingerprint before and after the turn can: path -> blob hash, for
# every file git would report as changed or untracked. `.agent/` is excluded
# -- it is the checkpoint and the run's own output, and it is huge.

#: Beyond this many changed paths the fingerprint stops; a turn that touches
#: more than this is reported as such rather than listed.
_MAX_FINGERPRINT_PATHS = 3000


def fingerprint(repo_path: str) -> dict[str, str] | None:
    """path -> content hash for every changed or untracked file, or None."""
    try:
        listed = subprocess.run(
            ["git", "-C", repo_path, "status", "--porcelain", "-z", "-uall",
             "--", ".", ":(exclude).agent"],
            capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if listed.returncode != 0:
        return None

    paths = []
    entries = listed.stdout.split("\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        if code[0] in "RC":
            index += 1                      # the rename's source path follows
        paths.append(path)
        if len(paths) > _MAX_FINGERPRINT_PATHS:
            return {"__too_many__": str(len(paths))}

    if not paths:
        return {}
    hashed = subprocess.run(
        ["git", "-C", repo_path, "hash-object", "--stdin-paths"],
        input="\n".join(paths), capture_output=True, text=True, timeout=60,
        check=False)
    hashes = hashed.stdout.split() if hashed.returncode == 0 else []
    if len(hashes) != len(paths):
        # A path that vanished between the two calls (deleted) cannot be
        # hashed; fall back to one at a time so the rest still count.
        hashes = []
        for path in paths:
            one = subprocess.run(["git", "-C", repo_path, "hash-object", path],
                                 capture_output=True, text=True, check=False)
            hashes.append(one.stdout.strip() or "deleted")
    return dict(zip(paths, hashes))


def turn_changes(repo_path: str, before: dict[str, str] | None) -> str:
    """The files changed since `before` was taken, one per line.

    "M path" for a changed file, "A path" for a new one, "D path" for one
    that is gone. Empty means the turn changed nothing in the repository.
    """
    after = fingerprint(repo_path)
    if before is None or after is None:
        return "(could not tell -- git is unavailable here)"
    if "__too_many__" in before or "__too_many__" in after:
        return f"(more than {_MAX_FINGERPRINT_PATHS} changed files; not listed)"

    lines = []
    for path in sorted(set(before) | set(after)):
        old, new = before.get(path), after.get(path)
        if old == new:
            continue
        if old is None:
            # Not in `before` but in `after`: new, or a tracked file that was
            # clean before -- which is a modification.
            lines.append(f"{'A' if _untracked(repo_path, path) else 'M'} {path}")
        elif new is None:
            lines.append(f"R {path}  (reverted to the committed version)")
        elif new == "deleted":
            lines.append(f"D {path}")
        else:
            lines.append(f"M {path}")
    text = "\n".join(lines)
    if len(text) > _MAX_OUTPUT_CHARS:
        text = text[:_MAX_OUTPUT_CHARS] + "\n... (truncated)"
    return text


def _untracked(repo_path: str, path: str) -> bool:
    result = subprocess.run(["git", "-C", repo_path, "ls-files", "--error-unmatch", path],
                            capture_output=True, text=True, check=False)
    return result.returncode != 0
