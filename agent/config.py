from __future__ import annotations

import os


CODEX_MODEL = os.getenv("CODEX_MODEL") or None
DEFAULT_SESSION = "main"
DEFAULT_RECURSION_LIMIT = 1000
