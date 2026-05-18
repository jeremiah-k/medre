"""Matrix adapter configuration.

Re-export shim — definition moved to ``medre.config.adapters.matrix`` in Tranche 2.
"""
from __future__ import annotations

from medre.config.adapters.matrix import (
    EncryptionMode,
    MatrixConfig,
    MatrixConfigError,
)

__all__ = [
    "EncryptionMode",
    "MatrixConfig",
    "MatrixConfigError",
]
