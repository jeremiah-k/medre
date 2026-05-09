"""LXMF adapter configuration.

:class:`LxmfConfig` is a frozen dataclass that holds all settings
required to connect to an LXMF router or node.  Use
:meth:`LxmfConfig.validate` to verify the configuration before
passing it to :class:`LxmfAdapter`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Self

from medre.adapters.lxmf.errors import LxmfConfigError


@dataclass(frozen=True)
class LxmfConfig:
    """Immutable configuration for a
    :class:`~medre.adapters.lxmf.adapter.LxmfAdapter`.

    Attributes
    ----------
    adapter_id:
        Unique identifier for this adapter instance.
    connection_type:
        Connection mode.  Only ``"fake"`` is supported in tranche 1.
        Defaults to ``"fake"`` for testing without hardware.
    display_name:
        Optional display name for LXMF announces.
    stamp_cost:
        Default stamp cost (0 = no stamp required).
    default_delivery_method:
        Default LXMF delivery method: ``"direct"``, ``"opportunistic"``,
        ``"propagated"``, or ``"paper"``.  Defaults to ``"direct"``.
        This is a configuration hint for future real connectivity; the
        fake adapter ignores it.
    meshnet_name:
        Human-readable meshnet name (informational).
    default_channel:
        Default radio channel index for outbound messages.
    message_delay_seconds:
        Minimum delay between outbound messages (pacing).
    metadata_embedding:
        Whether to embed MEDRE metadata envelopes in LXMF fields.
    identity_path:
        Path to identity file (placeholder for future use).
    """

    adapter_id: str
    connection_type: Literal["fake"] = "fake"
    display_name: str = ""
    stamp_cost: int = 8
    default_delivery_method: Literal["direct", "opportunistic", "propagated", "paper"] = "direct"
    meshnet_name: str = ""
    default_channel: int = 0
    message_delay_seconds: float = 0.5
    metadata_embedding: bool = True
    identity_path: str | None = None

    def validate(self) -> Self:
        """Validate the configuration and return *self* for chaining.

        Raises
        ------
        LxmfConfigError
            If any required field is missing or malformed.
        """
        if not self.adapter_id:
            raise LxmfConfigError("adapter_id must be non-empty")
        if self.connection_type != "fake":
            raise LxmfConfigError(
                "only fake connection_type is supported in tranche 1, "
                f"got {self.connection_type!r}"
            )
        if self.default_delivery_method not in (
            "direct", "opportunistic", "propagated", "paper",
        ):
            raise LxmfConfigError(
                f"default_delivery_method must be one of "
                f"direct/opportunistic/propagated/paper, "
                f"got {self.default_delivery_method!r}"
            )
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
        return self
