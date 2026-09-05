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


def _git(repo_path: str, args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception as exc:
        return f"git unavailable: {exc}"

    return (result.stdout.strip() or result.stderr.strip())
