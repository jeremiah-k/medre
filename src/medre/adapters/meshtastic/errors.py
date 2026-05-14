"""Meshtastic adapter exception hierarchy.

All Meshtastic-specific errors inherit from :class:`MeshtasticError` so that
callers can catch the entire family with a single ``except MeshtasticError``
clause.

Hierarchy::

    MeshtasticError
    ├── MeshtasticConnectionError — connection failures
    ├── MeshtasticSendError       — message send failures
    ├── MeshtasticConfigError     — invalid configuration (also ValueError)
    ├── MeshtasticCodecError      — encode / decode failures
    └── MeshtasticPacketError     — malformed or unparseable packets
"""
from __future__ import annotations


class MeshtasticError(Exception):
    """Base exception for all Meshtastic adapter errors."""


class MeshtasticConnectionError(MeshtasticError):
    """Raised when the adapter cannot connect to a Meshtastic node."""


class MeshtasticSendError(MeshtasticError):
    """Raised when a message send operation fails.

    Parameters
    ----------
    transient:
        ``True`` (default) if the error may succeed on retry;
        ``False`` for permanent failures (e.g. payload encoding,
        adapter not started).
    """

    transient: bool

    def __init__(self, *args: object, transient: bool = True) -> None:
        self.transient = transient
        super().__init__(*args)


class MeshtasticConfigError(MeshtasticError, ValueError):
    """Raised when the Meshtastic configuration is invalid.

    Inherits from both :class:`MeshtasticError` and :class:`ValueError` so
    that it is caught by either ``except MeshtasticError`` or
    ``except ValueError``.
    """


class MeshtasticCodecError(MeshtasticError):
    """Raised when encode or decode operations fail."""


class MeshtasticPacketError(MeshtasticError):
    """Raised when a packet is malformed or cannot be parsed."""
