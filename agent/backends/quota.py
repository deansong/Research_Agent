"""Moved to research_backend/quota.py. This name is kept so old imports work."""

import sys

import research_backend.quota as _module

sys.modules[__name__] = _module
