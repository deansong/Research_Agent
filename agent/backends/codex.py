"""Moved to research_backend/backends/codex.py. This name is kept so old imports work."""

import sys

import research_backend.backends.codex as _module

sys.modules[__name__] = _module
