"""Moved to research_backend/base.py. This name is kept so old imports work."""

import sys

import research_backend.base as _module

sys.modules[__name__] = _module
