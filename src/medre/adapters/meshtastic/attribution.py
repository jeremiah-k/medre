"""Project versioned Meshtastic native metadata into generic attribution.

The codec persists transport-native identity under ``native.meshtastic``. This
module reads only the current versioned namespace. ``source_transport_id`` is
the transport-neutral fallback for sender identity when ``from_id`` is absent.
"""

from __future__ import annotations

from typing import Any

from medre.adapters.meshtastic.event_shape import meshtastic_namespace

__all__ = [
    "project_meshtastic_attribution",
]


def project_meshtastic_attribution(
    native_data: dict[str, Any],
    *,
    source_transport_id: str | None = None,
    compact: bool = False,
) -> dict[str, str | None]:
    """Project the current Meshtastic native namespace into generic fields.

    ``source_transport_id`` remains a canonical fallback for sender identity
    when the transport namespace does not carry ``from_id``. Bare adapter-native
    keys are not part of the supported metadata shape.
    """
    meshtastic = meshtastic_namespace(native_data)
    sender_id = _str(meshtastic.get("from_id")) or _str(source_transport_id)
    longname = _str(meshtastic.get("longname"))
    shortname = _str(meshtastic.get("shortname"))

    sender_label = longname or shortname or sender_id
    sender_short_label = shortname or _compact(longname) or _compact(sender_id)
    if compact:
        sender_label = _compact(sender_label)
        sender_short_label = _compact(sender_short_label)

    return {
        "source_sender_id": sender_id,
        "source_sender_label": sender_label,
        "source_sender_short_label": sender_short_label,
        "source_sender_handle": None,
    }


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _str(value: object) -> str | None:
    """Coerce *value* to ``str`` or return ``None`` for missing/empty."""
    if value is None:
        return None
    s = str(value)
    return s if s else None


def _compact(value: str | None) -> str | None:
    """Strip spaces from *value*, returning ``None`` when the result is empty."""
    if value is None:
        return None
    return value.replace(" ", "") or None
