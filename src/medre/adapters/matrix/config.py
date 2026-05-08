"""Matrix adapter configuration.

:class:`MatrixConfig` is a frozen dataclass that holds all settings
required to connect to a Matrix homeserver.  Use :meth:`MatrixConfig.validate`
to verify the configuration before passing it to :class:`MatrixAdapter`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Self

from medre.adapters.matrix.errors import MatrixConfigError


@dataclass(frozen=True)
class MatrixConfig:
    """Immutable configuration for a :class:`~medre.adapters.matrix.adapter.MatrixAdapter`.

    Attributes
    ----------
    adapter_id:
        Unique identifier for this adapter instance.
    homeserver:
        Matrix homeserver URL (must start with ``"http://"`` or
        ``"https://"``).
    user_id:
        Fully-qualified Matrix user ID (must start with ``"@"``).
    device_id:
        Optional device ID for the client session.
    access_token:
        Access token for authentication.
    room_allowlist:
        Optional set of room IDs to accept messages from.  ``None``
        means all rooms are accepted.
    metadata_embedding_mode:
        How metadata is embedded in messages.  Defaults to ``"safe"``.
    store_path:
        Optional filesystem path for the nio store directory.
    sync_timeout_ms:
        Timeout in milliseconds for long-polling sync requests.
    """

    adapter_id: str
    homeserver: str
    user_id: str
    device_id: str | None = None
    access_token: str = ""
    room_allowlist: set[str] | None = None
    metadata_embedding_mode: str = "safe"
    store_path: str | None = None
    sync_timeout_ms: int = 30000

    def validate(self) -> Self:
        """Validate the configuration and return *self* for chaining.

        Raises
        ------
        MatrixConfigError
            If any required field is missing or malformed.
        """
        if not self.homeserver.startswith("http"):
            raise MatrixConfigError(
                f"homeserver must start with 'http', got {self.homeserver!r}"
            )
        if not self.user_id.startswith("@"):
            raise MatrixConfigError(
                f"user_id must start with '@', got {self.user_id!r}"
            )
        if not self.access_token:
            raise MatrixConfigError("access_token must be non-empty")
        return self
