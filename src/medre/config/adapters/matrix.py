"""Matrix adapter configuration.

:class:`MatrixConfig` is a frozen dataclass that holds all settings
required to connect to a Matrix homeserver.  Use :meth:`MatrixConfig.validate`
to verify the configuration before passing it to :class:`MatrixAdapter`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Literal

from medre.config.adapters.errors import MatrixConfigError

__all__ = ["MatrixConfig"]


EncryptionMode = Literal["plaintext", "e2ee_required", "e2ee_optional"]
_VALID_ENCRYPTION_MODES: frozenset[str] = frozenset(
    {"plaintext", "e2ee_required", "e2ee_optional"}
)

MetadataEmbeddingMode = Literal["off", "minimal", "safe", "full"]
_VALID_METADATA_EMBEDDING_MODES: frozenset[str] = frozenset(
    {"off", "minimal", "safe", "full"}
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
    sync_stale_timeout_seconds:
        Maximum seconds without durable Classic Sync progress before the
        current sync loop is recycled. ``0`` disables active stale-sync
        recovery.
    megolm_key_request_rate_limit_per_minute:
        Maximum missing-room-key to-device request attempts admitted in a
        rolling minute, including retries.
    megolm_key_request_max_inflight:
        Maximum concurrent detached missing-room-key recovery tasks.
    encryption_mode:
        Encryption policy: ``"plaintext"`` (default), ``"e2ee_required"``,
        or ``"e2ee_optional"``.
    require_encrypted_rooms:
        When ``True``, the adapter should only operate in encrypted
        rooms.  Invalid with ``encryption_mode="plaintext"``.
    auto_join_rooms:
        Tuple of canonical Matrix room IDs (starting with ``"!"``)
        that the adapter should automatically join on startup.
        The runtime builder derives this from route configuration;
        it can also be set explicitly by the operator.
    """

    adapter_id: str
    homeserver: str
    user_id: str
    device_id: str | None = None
    access_token: str = ""
    room_allowlist: set[str] | None = None
    metadata_embedding_mode: MetadataEmbeddingMode = "safe"
    store_path: str | None = None
    sync_timeout_ms: int = 30000
    sync_stale_timeout_seconds: float = 300.0
    megolm_key_request_rate_limit_per_minute: int = 30
    megolm_key_request_max_inflight: int = 4
    encryption_mode: str = "plaintext"
    require_encrypted_rooms: bool = False
    auto_join_rooms: tuple[str, ...] = ()
    origin_label: str = ""
    relay_prefix: str = ""

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
        needs_homeserver = not (
            isinstance(self.homeserver, str) and self.homeserver.strip()
        )
        needs_user_id = not (isinstance(self.user_id, str) and self.user_id.strip())
        needs_access_token = not (
            isinstance(self.access_token, str) and self.access_token.strip()
        )

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

        # Validate auto_join_rooms entries.
        if not isinstance(self.auto_join_rooms, tuple):
            raise MatrixConfigError("auto_join_rooms must be a tuple")
        for entry in self.auto_join_rooms:
            if not isinstance(entry, str) or not entry.strip():
                raise MatrixConfigError(
                    "auto_join_rooms entries must be non-empty strings"
                )
            if not entry.startswith("!") or ":" not in entry:
                raise MatrixConfigError(
                    f"auto_join_rooms entries must be canonical room IDs "
                    f"in '!localpart:server' form, got {entry!r}",
                )

        # --- Encryption-mode validation ---
        if self.encryption_mode not in _VALID_ENCRYPTION_MODES:
            raise MatrixConfigError(
                f"encryption_mode must be one of "
                f"{sorted(_VALID_ENCRYPTION_MODES)}, "
                f"got {self.encryption_mode!r}"
            )

        # --- metadata_embedding_mode validation ---
        # Case-sensitive against the schema-declared enum so a typo
        # ("sfae") or different casing ("SAFE") fails loudly at load time
        # instead of silently passing through to renderer behaviour.
        if self.metadata_embedding_mode not in _VALID_METADATA_EMBEDDING_MODES:
            raise MatrixConfigError(
                f"metadata_embedding_mode must be one of "
                f"{sorted(_VALID_METADATA_EMBEDDING_MODES)}, "
                f"got {self.metadata_embedding_mode!r}"
            )

        if self.encryption_mode == "e2ee_required":
            pass  # device_id and store_path are derived internally
            # by the adapter session (whoami() + state dir convention).

        if self.require_encrypted_rooms and self.encryption_mode == "plaintext":
            raise MatrixConfigError(
                "require_encrypted_rooms=True is invalid with "
                "encryption_mode='plaintext'"
            )

        # --- Runtime supervision ---
        if (
            isinstance(self.sync_timeout_ms, bool)
            or not isinstance(self.sync_timeout_ms, int)
            or self.sync_timeout_ms < 0
        ):
            raise MatrixConfigError("sync_timeout_ms must be an int >= 0")

        if isinstance(self.sync_stale_timeout_seconds, bool) or not isinstance(
            self.sync_stale_timeout_seconds, (int, float)
        ):
            raise MatrixConfigError("sync_stale_timeout_seconds must be a number")
        stale_timeout = float(self.sync_stale_timeout_seconds)
        if not math.isfinite(stale_timeout) or stale_timeout < 0:
            raise MatrixConfigError(
                "sync_stale_timeout_seconds must be finite and >= 0"
            )
        if stale_timeout > 0 and self.sync_timeout_ms > 0:
            # mindroom-nio permits a sync request to run up to 15 seconds past
            # the requested long-poll timeout before its client-side timeout.
            minimum = (self.sync_timeout_ms / 1000.0) + 15.0
            if stale_timeout <= minimum:
                raise MatrixConfigError(
                    "sync_stale_timeout_seconds must exceed sync_timeout_ms/1000 "
                    "+ 15 seconds so healthy long-poll requests are not recycled"
                )

        for name, value in (
            (
                "megolm_key_request_rate_limit_per_minute",
                self.megolm_key_request_rate_limit_per_minute,
            ),
            ("megolm_key_request_max_inflight", self.megolm_key_request_max_inflight),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise MatrixConfigError(f"{name} must be a positive int")

        # --- origin_label ---
        if isinstance(self.origin_label, bool):
            raise MatrixConfigError("origin_label must be a str, got bool")
        if not isinstance(self.origin_label, str):
            raise MatrixConfigError(
                f"origin_label must be a str, "
                f"got {type(self.origin_label).__name__}"
            )

        # --- relay_prefix ---
        if isinstance(self.relay_prefix, bool):
            raise MatrixConfigError("relay_prefix must be a str, got bool")
        if not isinstance(self.relay_prefix, str):
            raise MatrixConfigError(
                f"relay_prefix must be a str, "
                f"got {type(self.relay_prefix).__name__}"
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
            f"room_allowlist={self.room_allowlist!r}, "
            f"auto_join_rooms={self.auto_join_rooms!r})"
        )
