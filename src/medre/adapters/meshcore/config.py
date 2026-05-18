"""MeshCore adapter configuration.

Re-export shim — definition moved to ``medre.config.adapters.meshcore`` in Tranche 2.
"""
from __future__ import annotations

from medre.config.adapters.meshcore import MeshCoreConfig, MeshCoreConfigError

__all__ = [
    "MeshCoreConfig",
    "MeshCoreConfigError",
]
