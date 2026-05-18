"""Matrix adapter configuration.

:class:`MatrixConfig` is a frozen dataclass that holds all settings
required to connect to a Matrix homeserver.  Use :meth:`MatrixConfig.validate`
to verify the configuration before passing it to :class:`MatrixAdapter`.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Literal, Self

from medre.config.adapters.errors import MatrixConfigError


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
        (``{state}/adapters/{adapter_id}/matrix/store``).  Only set this for
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

    def validate(self) -> MatrixConfig:
        """Validate the configuration and return it for chaining.

        If ``homeserver``, ``user_id``, or ``access_token`` are empty in
        this config, the sidecar credentials file
        (``~/.config/medre/credentials/matrix.json``) is consulted as a
        fallback before raising a validation error.

        Raises
        ------
        MatrixConfigError
            If any required field is missing or malformed and no
            sidecar fallback is available.
        """
        resolved = self._apply_sidecar_fallback()
        return resolved._validate_fields()

    def _apply_sidecar_fallback(self) -> MatrixConfig:
        """Return a new config with empty credential fields filled from sidecar.

        If none of ``homeserver``, ``user_id``, or ``access_token`` are
        empty, returns *self* unchanged.  Otherwise loads the sidecar
        JSON and applies any missing values it contains.
        """
        needs_homeserver = not (isinstance(self.homeserver, str) and self.homeserver.strip())
        needs_user_id = not (isinstance(self.user_id, str) and self.user_id.strip())
        needs_access_token = not (isinstance(self.access_token, str) and self.access_token.strip())

        if not (needs_homeserver or needs_user_id or needs_access_token):
            return self

        # Import from config-owned credential helpers (not from adapters).
        from medre.config.adapters.matrix_credentials import load_credentials_json

        creds = load_credentials_json()
        if creds is None:
            return self

        overrides: dict[str, str] = {}
        if needs_homeserver and creds.get("homeserver"):
            overrides["homeserver"] = creds["homeserver"]
        if needs_user_id and creds.get("user_id"):
            overrides["user_id"] = creds["user_id"]
        if needs_access_token and creds.get("access_token"):
            overrides["access_token"] = creds["access_token"]

        if self.device_id is None:
            device_id_val = creds.get("device_id")
            if device_id_val:
                overrides["device_id"] = device_id_val

        if not overrides:
            return self

        return replace(self, **overrides)

    def _validate_fields(self) -> MatrixConfig:
        """Pure validation of already-resolved field values."""
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
