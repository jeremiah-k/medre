"""Adapter configuration validation errors.

All adapter config validation errors inherit from :class:`AdapterConfigError`
which itself inherits from :class:`ValueError`.  This allows callers to
catch config validation failures uniformly.

These errors are **config-time** errors, not runtime adapter errors.
They live in the config layer, not in the adapter error modules.

Hierarchy::

    ValueError
    └── AdapterConfigError
        ├── MatrixConfigError
        ├── MeshtasticConfigError
        ├── MeshCoreConfigError
        └── LxmfConfigError
"""

from __future__ import annotations


class AdapterConfigError(ValueError):
    """Base error raised when an adapter configuration is invalid."""


class MatrixConfigError(AdapterConfigError):
    """Raised when the Matrix configuration is invalid."""


class MeshtasticConfigError(AdapterConfigError):
    """Raised when the Meshtastic configuration is invalid."""


class MeshCoreConfigError(AdapterConfigError):
    """Raised when the MeshCore configuration is invalid."""


class LxmfConfigError(AdapterConfigError):
    """Raised when the LXMF configuration is invalid."""


__all__ = [
    "AdapterConfigError",
    "LxmfConfigError",
    "MatrixConfigError",
    "MeshCoreConfigError",
    "MeshtasticConfigError",
]
