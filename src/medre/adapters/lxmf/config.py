"""LXMF adapter configuration.

Re-export shim — definition moved to ``medre.config.adapters.lxmf`` in Tranche 2.
"""
from __future__ import annotations

from medre.config.adapters.lxmf import LxmfConfig, LxmfConfigError

__all__ = [
    "LxmfConfig",
    "LxmfConfigError",
]
