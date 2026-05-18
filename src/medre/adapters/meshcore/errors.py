"""MeshCore adapter exception hierarchy.

All MeshCore-specific errors inherit from :class:`MeshCoreError` so that
callers can catch the entire family with a single ``except MeshCoreError``
clause.

Hierarchy::

    MeshCoreError
    ├── MeshCoreConnectionError — connection failures
    ├── MeshCoreSendError       — message send failures
    ├── MeshCoreCodecError      — encode / decode failures
    └── MeshCorePacketError     — malformed or unparseable packets
"""

from __future__ import annotations


class MeshCoreError(Exception):
    """Base exception for all MeshCore adapter errors."""


class MeshCoreConnectionError(MeshCoreError):
    """Raised when the adapter cannot connect to a MeshCore node."""


class MeshCoreSendError(MeshCoreError):
    """Raised when a message send operation fails.

    Parameters
    ----------
    transient:
        ``True`` (default) if the error may succeed on retry;
        ``False`` for permanent failures (e.g. not initialised,
        invalid address, SDK-level rejection).
    """

    transient: bool

    def __init__(self, *args: object, transient: bool = True) -> None:
        self.transient = transient
        super().__init__(*args)


class MeshCoreCodecError(MeshCoreError):
    """Raised when encode or decode operations fail."""


class MeshCorePacketError(MeshCoreError):
    """Raised when a packet is malformed or cannot be parsed."""
