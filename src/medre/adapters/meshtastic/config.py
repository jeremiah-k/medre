"""Meshtastic adapter configuration.

Re-export shim — definition moved to ``medre.config.adapters.meshtastic`` in Tranche 2.
"""
from __future__ import annotations

from medre.config.adapters.meshtastic import MeshtasticConfig, MeshtasticConfigError

__all__ = [
    "MeshtasticConfig",
    "MeshtasticConfigError",
]
