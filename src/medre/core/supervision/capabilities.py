"""Runtime transport capability metadata.

Capabilities are declarative runtime metadata used by diagnostics,
planning, routing suppression, rendering constraints, and replay
filtering.  They describe adapter behaviour; they do not enable
transport features by themselves.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

from medre.core.contracts.adapter import AdapterCapabilities


@dataclass(frozen=True)
class TransportCapabilities:
    """Deterministic, log-safe summary of adapter capabilities.

    The fields intentionally describe broad runtime semantics rather than
    adapter implementation details.  A ``False`` value means MEDRE must not
    assume the capability exists for this adapter instance.

    Relation-level fields (``replies_level``, ``reactions_level``,
    ``edits_level``, ``deletes_level``) expose the raw three-level string
    from :class:`AdapterCapabilities` for diagnostics and planning.
    Threads are explicitly not included.
    """

    supports_direct_messages: bool = False
    supports_channels: bool = False
    supports_reactions: bool = False
    supports_edits: bool = False
    supports_binary_payloads: bool = False
    supports_delivery_receipts: bool = False
    supports_ack_tracking: bool = False
    supports_store_and_forward: bool = False
    supports_async_delivery: bool = False
    supports_identity_encryption: bool = False
    supports_presence: bool = False
    supports_topic_rooms: bool = False
    supports_mesh_routing: bool = False
    supports_priority_delivery: bool = False
    max_text_bytes: int | None = None
    max_text_chars: int | None = None

    # Relation-level capability strings from AdapterCapabilities.
    replies_level: str = "unsupported"
    reactions_level: str = "unsupported"
    edits_level: str = "unsupported"
    deletes_level: str = "unsupported"

    def to_dict(self) -> dict[str, bool | int | str | None]:
        """Return deterministic JSON-safe capability metadata."""
        return {field.name: getattr(self, field.name) for field in fields(self)}


def summarize_adapter_capabilities(
    capabilities: AdapterCapabilities,
) -> TransportCapabilities:
    """Project adapter capability flags into runtime capability metadata.

    The projection is deliberately conservative: relation strings only
    become boolean support when they are not ``"unsupported"``.  Newer
    operational capability fields are copied directly from
    :class:`~medre.core.contracts.adapter.AdapterCapabilities`.
    """
    return TransportCapabilities(
        supports_direct_messages=capabilities.direct_messages,
        supports_channels=capabilities.channels,
        supports_reactions=capabilities.reactions != "unsupported",
        supports_edits=capabilities.edits != "unsupported",
        supports_binary_payloads=capabilities.attachments,
        supports_delivery_receipts=capabilities.delivery_receipts,
        supports_ack_tracking=capabilities.ack_tracking,
        supports_store_and_forward=capabilities.store_and_forward,
        supports_async_delivery=capabilities.async_delivery,
        supports_identity_encryption=capabilities.identity_encryption,
        supports_presence=capabilities.presence,
        supports_topic_rooms=capabilities.topic_rooms,
        supports_mesh_routing=capabilities.mesh_routing,
        supports_priority_delivery=capabilities.priority_delivery,
        max_text_bytes=capabilities.max_text_bytes,
        max_text_chars=capabilities.max_text_chars,
        replies_level=capabilities.replies,
        reactions_level=capabilities.reactions,
        edits_level=capabilities.edits,
        deletes_level=capabilities.deletes,
    )


def serialize_adapter_capabilities(
    capabilities: AdapterCapabilities,
) -> dict[str, bool | int | str | None]:
    """Serialize adapter capabilities for diagnostics and logging."""
    return summarize_adapter_capabilities(capabilities).to_dict()


def is_capability_summary(value: Any) -> bool:
    """Return whether *value* looks like a serialized capability summary.

    This small helper is used by tests to assert that diagnostics expose
    metadata rather than live transport objects or private state.
    """
    if not isinstance(value, dict):
        return False
    expected = {field.name for field in fields(TransportCapabilities)}
    return set(value) == expected
