from __future__ import annotations

import hashlib
from pathlib import Path



def database_path(repo_path: Path) -> Path:
    repo_hash = hashlib.sha256(str(repo_path.resolve()).encode("utf-8")).hexdigest()[:16]
    state_dir = Path.home() / ".codex-langgraph-agent"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / f"{repo_hash}.sqlite"
