"""Moved to research_backend/backends/_progress.py. This name is kept so old imports work."""

import sys

import research_backend.backends._progress as _module

sys.modules[__name__] = _module
