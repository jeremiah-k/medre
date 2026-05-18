"""Optional MeshCore SDK dependency guard.

When the ``meshcore`` package is available, :data:`HAS_MESHCORE` is
``True`` and the adapter can create real SDK client instances.  When
absent, the adapter raises :class:`~medre.adapters.meshcore.errors.MeshCoreConnectionError`
on :meth:`start` for non-fake connection types.

This module is the **sole** import site for the ``meshcore`` package.
All other MeshCore adapter modules must not import ``meshcore`` directly.
"""

from __future__ import annotations

HAS_MESHCORE: bool

try:
    import meshcore  # noqa: F401

    HAS_MESHCORE = True
except ImportError:
    HAS_MESHCORE = False
