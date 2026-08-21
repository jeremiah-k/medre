"""Project versioned MeshCore native metadata into generic attribution.

The codec persists transport identity, channel, packet, and contact labels under
``native.meshcore``. Platform detection accepts a positively versioned MeshCore
namespace; field projection reads only the current schema version.
"""

from __future__ import annotations

from typing import Any

from medre.adapters.meshcore.event_shape import (
    meshcore_namespace,
    meshcore_versioned_namespace,
)

__all__ = [
    "ProjectionMap",
    "is_meshcore_native",
    "project_meshcore_attribution",
]

# Type alias for the generic field map returned by the projection helper.
ProjectionMap = dict[str, str | None]


def is_meshcore_native(native_data: dict[str, Any]) -> bool:
    """Return whether *native_data* carries a versioned MeshCore namespace."""
    return bool(meshcore_versioned_namespace(native_data))


def project_meshcore_attribution(
    native_data: dict[str, Any],
) -> ProjectionMap:
    """Project the current MeshCore native namespace into generic fields."""
    meshcore = meshcore_namespace(native_data)
    sender_id = _str(meshcore.get("pubkey_prefix")) or _str(meshcore.get("sender_id"))
    channel = _str(meshcore.get("channel"))
    packet_id = _str(meshcore.get("packet_id"))
    contact_label = _contact_label_str(meshcore.get("contact_label"))
    contact_short_label = _contact_label_str(meshcore.get("contact_short_label"))
    sender_label = contact_label
    sender_short_label = contact_short_label or _first_token(contact_label)

    return {
        "source_sender_id": sender_id,
        "source_native_channel_id": channel,
        "source_native_message_id": packet_id,
        "source_sender_label": sender_label,
        "source_sender_short_label": sender_short_label,
        "source_sender_handle": None,
    }


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _str(value: object) -> str | None:
    """Coerce *value* to ``str`` or return ``None`` for missing/empty.

    Used for non-label native fields (sender_id, channel, packet_id) where
    coercion of integer-typed codec outputs (e.g. channel=3 -> "3") is the
    intended behaviour.  Human contact labels must use
    :func:`_contact_label_str` instead.
    """
    if value is None:
        return None
    s = str(value)
    return s if s else None


def _contact_label_str(value: object) -> str | None:
    """Strict contact-label coercion.

    Accept only genuine ``str`` values, trim surrounding whitespace, and
    return ``None`` for any non-string input (int, dict, list, etc.) or
    for empty/whitespace-only strings.  This prevents accidental
    ``str(123)``-style rendering of non-human data into ``source_sender_label``
    or ``source_sender_short_label``.
    """
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed or None


def _first_token(value: str | None) -> str | None:
    """Return the first whitespace-delimited token of *value*.

    Splits *value* on whitespace and returns the first token (stripped),
    or ``None`` when *value* is ``None`` or contains no non-whitespace
    content.  This differs from the space-stripping ``_compact`` helpers
    used by the Meshtastic and LXMF transports: those remove all spaces
    while preserving the full string, whereas this helper keeps only the
    leading token.  The distinction keeps ``{sender_short}`` useful for
    short MeshCore advertised names (typically callsigns) and multi-word
    names alike.
    """
    if value is None:
        return None
    parts = value.strip().split(None, 1)
    return parts[0] if parts else None
