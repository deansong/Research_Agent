"""Moved to research_backend/auth.py. This name is kept so old imports work."""

import sys

import research_backend.auth as _module

sys.modules[__name__] = _module
