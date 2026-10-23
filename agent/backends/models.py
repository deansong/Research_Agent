"""Moved to research_backend/catalog.py. This name is kept so old imports work."""

import sys

import research_backend.catalog as _module

sys.modules[__name__] = _module
