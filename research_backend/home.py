"""
WHAT:  Where research_backend keeps what must outlive a process: sessions,
       and the last usage limits a provider reported.
WHY:   One folder, so it is obvious what to back up or delete.

    ~/.research_backend/            (or $RESEARCH_BACKEND_HOME)
        sessions/<id>/session.json  a session and every turn of it
        sessions/<id>/workspace/    its working folder, unless you chose one
        limits/<provider>.json      limits seen during turns (claude_code)
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

ENV = "RESEARCH_BACKEND_HOME"


def home() -> Path:
    return Path(os.environ.get(ENV) or Path.home() / ".research_backend").expanduser()


def write_json(path: Path, data) -> None:
    """Write all of it or none of it: a half-written session file is a lost
    conversation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(data, out, indent=2, ensure_ascii=False, default=str)
            out.write("\n")
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))
