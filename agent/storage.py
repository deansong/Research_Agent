"""
WHAT:  Decides where a repository's LangGraph checkpoint database lives.
WHY:   One sqlite file per repository, many named sessions (threads) inside
       it.  Hashing the path keeps filenames short and avoids collisions.
CONCEPT: Checkpointer storage.  The file this returns is what makes ctrl-C
       recoverable.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from agent.state import STATE_VERSION

ROOT = Path.home() / ".codex-langgraph-agent"


def database_path(repo_path: Path) -> Path:
    """Return the sqlite path for `repo_path`, creating the parent directory.

    The state schema version is part of the filename.  When the schema changes
    incompatibly we simply start a new file rather than shipping migration
    code: a half-migrated checkpoint is the single most confusing thing to
    debug, and old files stay on disk and readable.
    """
    ROOT.mkdir(parents=True, exist_ok=True)

    digest = hashlib.sha256(str(repo_path.resolve()).encode()).hexdigest()[:16]
    current = ROOT / f"{digest}-v{STATE_VERSION}.sqlite"

    # Tell the human their old sessions still exist but are not being used.
    legacy = ROOT / f"{digest}.sqlite"
    if legacy.exists() and not current.exists():
        print(
            f"Note: found an older session database ({legacy.name}).\n"
            f"      The state schema changed, so this run starts a fresh one\n"
            f"      ({current.name}). The old file is left untouched."
        )

    return current
