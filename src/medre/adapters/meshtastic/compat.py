"""Compatibility guard for optional meshtastic dependency."""
from __future__ import annotations

HAS_MESHTASTIC: bool
try:
    import meshtastic  # noqa: F401

    HAS_MESHTASTIC = True
except ImportError:
    HAS_MESHTASTIC = False
