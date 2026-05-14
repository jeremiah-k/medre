"""LXMF adapter exception hierarchy.

All LXMF-specific errors inherit from :class:`LxmfError` so that
callers can catch the entire family with a single ``except LxmfError``
clause.

Hierarchy::

    LxmfError
    ├── LxmfConnectionError — connection failures
    ├── LxmfSendError       — message send failures
    ├── LxmfConfigError     — invalid configuration (also ValueError)
    ├── LxmfCodecError      — encode / decode failures
    └── LxmfPacketError     — malformed or unparseable packets
"""
from __future__ import annotations


class LxmfError(Exception):
    """Base exception for all LXMF adapter errors."""


class LxmfConnectionError(LxmfError):
    """Raised when the adapter cannot connect to an LXMF router/node."""


class LxmfSendError(LxmfError):
    """Raised when a message send operation fails.

    Parameters
    ----------
    transient:
        ``True`` (default) if the error may succeed on retry;
        ``False`` for permanent failures (e.g. invalid destination,
        not initialised).
    """

    transient: bool

    def __init__(self, *args: object, transient: bool = True) -> None:
        self.transient = transient
        super().__init__(*args)


class LxmfConfigError(LxmfError, ValueError):
    """Raised when the LXMF configuration is invalid.

    Inherits from both :class:`LxmfError` and :class:`ValueError` so
    that it is caught by either ``except LxmfError`` or
    ``except ValueError``.
    """


class LxmfCodecError(LxmfError):
    """Raised when encode or decode operations fail."""


class LxmfPacketError(LxmfError):
    """Raised when a packet is malformed or cannot be parsed."""


class LxmfSessionError(LxmfError):
    """Raised when the LXMF session lifecycle encounters an error."""
