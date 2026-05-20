"""Canonical logging-level definitions shared across the codebase.

Centralises the set of valid level names and their mapping to
:mod:`logging` constants so that ``logging.py`` and ``loader.py`` do not
duplicate the definition.
"""

from __future__ import annotations

import logging

VALID_LEVEL_NAMES: frozenset[str] = frozenset(
    {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
)

LEVEL_NAME_TO_CONSTANT: dict[str, int] = {
    name: getattr(logging, name) for name in VALID_LEVEL_NAMES
}
