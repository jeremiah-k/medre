"""Compatibility guard for optional mindroom-nio dependency."""
from __future__ import annotations

HAS_NIO: bool
try:
    import nio  # noqa: F401

    HAS_NIO = True
except ImportError:
    HAS_NIO = False
