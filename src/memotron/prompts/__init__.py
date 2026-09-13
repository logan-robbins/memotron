"""
Memotron prompt library.

Each sub-module defines a tuple of DreamPromptProfile objects for a specific extraction
use case. Add a new .py file with a PROFILES tuple and register it in builtin_profiles()
to make it available to DreamConfig without modifying config.py.

Discovery order controls the profile key uniqueness check in DreamConfig — profiles
added later can shadow built-in profiles of the same name@version only if the caller
explicitly replaces the default_prompt_profiles tuple.
"""

from __future__ import annotations

from memotron.config import DreamPromptProfile
from memotron.prompts.research_temporal import PROFILES as _RESEARCH_TEMPORAL


def builtin_profiles() -> tuple[DreamPromptProfile, ...]:
    """Return all prompt profiles from the built-in library."""
    return _RESEARCH_TEMPORAL
