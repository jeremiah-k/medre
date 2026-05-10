"""Matrix adapter configuration.

:class:`MatrixConfig` is a frozen dataclass that holds all settings
required to connect to a Matrix homeserver.  Use :meth:`MatrixConfig.validate`
to verify the configuration before passing it to :class:`MatrixAdapter`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Self

from medre.adapters.matrix.errors import MatrixConfigError

EncryptionMode = Literal["plaintext", "e2ee_required", "e2ee_optional"]
_VALID_ENCRYPTION_MODES: frozenset[str] = frozenset(
    {"plaintext", "e2ee_required", "e2ee_optional"}
)


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
        **Internal** — not operator-facing.  The adapter session
        discovers the device ID via ``whoami()`` on login when needed.
        Only set this when the caller already knows the device ID
        (e.g. live test harnesses).
    access_token:
        Access token for authentication.
    room_allowlist:
        Optional set of room IDs to accept messages from.  ``None``
        means all rooms are accepted.
    metadata_embedding_mode:
        How metadata is embedded in messages.  Defaults to ``"safe"``.
    store_path:
        **Internal** — not operator-facing.  The runtime derives a
        default store path under the resolved state directory
        (``{state}/matrix/{adapter_id}/store``).  Only set this for
        test harnesses that need explicit control.
    sync_timeout_ms:
        Timeout in milliseconds for long-polling sync requests.
    encryption_mode:
        Encryption policy: ``"plaintext"`` (default), ``"e2ee_required"``,
        or ``"e2ee_optional"``.
    require_encrypted_rooms:
        When ``True``, the adapter should only operate in encrypted
        rooms.  Invalid with ``encryption_mode="plaintext"``.
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
    encryption_mode: str = "plaintext"
    require_encrypted_rooms: bool = False

    def validate(self) -> Self:
        """Validate the configuration and return *self* for chaining.

        Raises
        ------
        MatrixConfigError
            If any required field is missing or malformed.
        """
        if not isinstance(self.homeserver, str) or not self.homeserver.strip():
            raise MatrixConfigError("homeserver must be a non-empty string")
        if not (
            self.homeserver.startswith("http://")
            or self.homeserver.startswith("https://")
        ):
            raise MatrixConfigError(
                f"homeserver must start with 'http://' or 'https://', "
                f"got {self.homeserver!r}"
            )
        if not isinstance(self.user_id, str) or not self.user_id.strip():
            raise MatrixConfigError("user_id must be a non-empty string")
        if not self.user_id.startswith("@"):
            raise MatrixConfigError(
                f"user_id must start with '@', got {self.user_id!r}"
            )
        if not isinstance(self.access_token, str) or not self.access_token.strip():
            raise MatrixConfigError("access_token must be non-empty")
        # Validate room allowlist entries if provided.
        if self.room_allowlist is not None:
            for entry in self.room_allowlist:
                if not isinstance(entry, str) or not entry.strip():
                    raise MatrixConfigError(
                        "room_allowlist entries must be non-empty strings"
                    )

        # --- Encryption-mode validation ---
        if self.encryption_mode not in _VALID_ENCRYPTION_MODES:
            raise MatrixConfigError(
                f"encryption_mode must be one of "
                f"{sorted(_VALID_ENCRYPTION_MODES)}, "
                f"got {self.encryption_mode!r}"
            )

        if self.encryption_mode == "e2ee_required":
            pass  # device_id and store_path are derived internally
            # by the adapter session (whoami() + state dir convention).

        if self.require_encrypted_rooms and self.encryption_mode == "plaintext":
            raise MatrixConfigError(
                "require_encrypted_rooms=True is invalid with "
                "encryption_mode='plaintext'"
            )

        return self

    def __repr__(self) -> str:
        """Return a representation with access_token redacted."""
        token_preview = (
            self.access_token[:3] + "…" if len(self.access_token) > 3 else "***"
        )
        return (
            f"MatrixConfig(adapter_id={self.adapter_id!r}, "
            f"homeserver={self.homeserver!r}, "
            f"user_id={self.user_id!r}, "
            f"access_token={token_preview!r}, "
            f"encryption_mode={self.encryption_mode!r}, "
            f"room_allowlist={self.room_allowlist!r})"
        )
