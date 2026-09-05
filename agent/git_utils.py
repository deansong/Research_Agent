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
