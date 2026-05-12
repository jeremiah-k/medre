"""LXMF adapter configuration.

:class:`LxmfConfig` is a frozen dataclass that holds all settings
required to connect to an LXMF router or node.  Use
:meth:`LxmfConfig.validate` to verify the configuration before
passing it to :class:`LxmfAdapter`.

Connection types
----------------
``"fake"``
    No real LXMF/Reticulum connectivity.  Used for testing without
    the ``lxmf`` / ``RNS`` packages installed.

``"reticulum"``
    Connect to a locally-running Reticulum instance via the ``RNS``
    and ``lxmf`` packages.  Accepted as a valid shape by
    :meth:`validate`; runtime availability is checked by
    :class:`~medre.adapters.lxmf.adapter.LxmfAdapter.start`.

All non-fake modes require the ``lxmf`` optional dependency at runtime.
:meth:`validate` checks shape only; :class:`~medre.adapters.lxmf.adapter.LxmfAdapter.start`
raises :class:`~medre.adapters.lxmf.errors.LxmfConnectionError` when the
SDK is unavailable or production connectivity is not yet implemented.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Self

from medre.adapters.lxmf.errors import LxmfConfigError

# Allowed connection_type values.
_ALLOWED_CONNECTION_TYPES: frozenset[str] = frozenset({"fake", "reticulum"})

# Allowed default_delivery_method values.
_ALLOWED_DELIVERY_METHODS: frozenset[str] = frozenset({
    "direct", "opportunistic", "propagated", "paper",
})

# Fields that must never contain secrets or private keys.
_NO_SECRET_FIELDS: frozenset[str] = frozenset({
    "display_name", "meshnet_name",
})


@dataclass(frozen=True)
class LxmfConfig:
    """Immutable configuration for a
    :class:`~medre.adapters.lxmf.adapter.LxmfAdapter`.

    Attributes
    ----------
    adapter_id:
        Unique identifier for this adapter instance.
    connection_type:
        Connection mode.  ``"fake"`` for testing (default).
        ``"reticulum"`` for real LXMF connectivity (requires ``lxmf``
        and ``RNS`` packages).
    display_name:
        Optional display name for LXMF announces.
    stamp_cost:
        Default stamp cost.  ``0`` means no stamp required.
        If non-zero, must be a positive integer.
    default_delivery_method:
        Default LXMF delivery method: ``"direct"``, ``"opportunistic"``,
        ``"propagated"``, or ``"paper"``.  Defaults to ``"direct"``.
    meshnet_name:
        Human-readable meshnet name (informational).
    default_channel:
        Default radio channel index for outbound messages.
    message_delay_seconds:
        Minimum delay between outbound messages (pacing).
    metadata_embedding:
        Whether to embed MEDRE metadata envelopes in LXMF fields.
        Envelopes contain provenance data only (event IDs, adapter
        names, relation metadata).  No private keys or secrets are
        ever embedded.
    identity_path:
        Path to a Reticulum identity file.  Required for non-fake
        connection types if the identity is not auto-generated.
        Must be a non-empty string when provided.
    storage_path:
        Path to a directory used by ``LXMF.LXMRouter`` for persistent
        message and peer storage.  **Required** when
        ``connection_type="reticulum"`` — LXMF 0.9.7 raises
        ``ValueError`` if ``storagepath`` is ``None``.  Ignored in
        fake mode.
    """

    adapter_id: str
    connection_type: str = "fake"
    display_name: str = ""
    stamp_cost: int = 8
    default_delivery_method: str = "direct"
    meshnet_name: str = ""
    default_channel: int = 0
    message_delay_seconds: float = 0.5
    metadata_embedding: bool = True
    identity_path: str | None = None
    storage_path: str | None = None

    def validate(self) -> Self:
        """Validate the configuration and return *self* for chaining.

        Raises
        ------
        LxmfConfigError
            If any required field is missing or malformed.
        """
        if not self.adapter_id:
            raise LxmfConfigError("adapter_id must be non-empty")

        # --- connection_type ---
        if self.connection_type not in _ALLOWED_CONNECTION_TYPES:
            raise LxmfConfigError(
                f"connection_type must be one of "
                f"{sorted(_ALLOWED_CONNECTION_TYPES)}, "
                f"got {self.connection_type!r}"
            )

        # Non-fake connection types are valid shapes.  Runtime availability
        # (whether lxmf/RNS are installed) is checked by LxmfAdapter.start(),
        # not by config validation.  Config only validates that the value
        # is a known connection_type.

        # --- default_delivery_method ---
        if self.default_delivery_method not in _ALLOWED_DELIVERY_METHODS:
            raise LxmfConfigError(
                f"default_delivery_method must be one of "
                f"direct/opportunistic/propagated/paper, "
                f"got {self.default_delivery_method!r}"
            )

        # --- numeric fields ---
        if self.message_delay_seconds < 0:
            raise LxmfConfigError(
                f"message_delay_seconds must be >= 0, "
                f"got {self.message_delay_seconds}"
            )
        if self.default_channel < 0:
            raise LxmfConfigError(
                f"default_channel must be >= 0, got {self.default_channel}"
            )
        if self.stamp_cost < 0:
            raise LxmfConfigError(
                f"stamp_cost must be >= 0, got {self.stamp_cost}"
            )
        if self.stamp_cost != 0 and not isinstance(self.stamp_cost, int):
            raise LxmfConfigError(
                f"stamp_cost must be an integer, "
                f"got {type(self.stamp_cost).__name__}"
            )

        # --- identity_path ---
        if self.identity_path is not None:
            if not isinstance(self.identity_path, str):
                raise LxmfConfigError(
                    f"identity_path must be a string or None, "
                    f"got {type(self.identity_path).__name__}"
                )
            if not self.identity_path.strip():
                raise LxmfConfigError(
                    "identity_path must be a non-empty string when provided"
                )

        # --- storage_path ---
        if self.storage_path is not None:
            if not isinstance(self.storage_path, str):
                raise LxmfConfigError(
                    f"storage_path must be a string or None, "
                    f"got {type(self.storage_path).__name__}"
                )
            if not self.storage_path.strip():
                raise LxmfConfigError(
                    "storage_path must be a non-empty string when provided"
                )
        if self.connection_type == "reticulum" and not self.storage_path:
            raise LxmfConfigError(
                "storage_path is required when connection_type='reticulum' "
                "(LXMF 0.9.7 LXMRouter raises ValueError without it)"
            )

        # --- metadata_embedding safety ---
        # metadata_embedding is a bool — no secrets can be embedded.
        # LxmfFieldsHelper.embed_envelope explicitly documents that
        # no private keys or secrets are embedded in envelopes.

        return self
