"""Project versioned LXMF native metadata into generic attribution.

The codec persists source identity and announce-derived labels under
``native.lxmf``. Opaque source hashes remain sender identifiers and never become
human-readable labels unless a real display name was captured at ingress.
"""

from __future__ import annotations

from typing import Any

from medre.adapters.lxmf.event_shape import lxmf_namespace

__all__ = [
    "normalize_source_hash",
    "project_lxmf_attribution",
]


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------


def normalize_source_hash(source_hash: Any) -> str | None:
    """Normalise a ``source_hash`` value to a canonical hex string.

    Accepts ``bytes``, ``bytearray``, or ``str``.  Returns ``None`` for
    other types or ``None`` input.  Empty bytes / empty strings return
    ``None`` (absent, not malformed).

    This mirrors the normalisation performed by
    :class:`~medre.adapters.lxmf.packet_classifier.LxmfPacketClassifier`
    and ensures consistent representation across the adapter boundary.

    Parameters
    ----------
    source_hash:
        Raw source hash value from native LXMF metadata.

    Returns
    -------
    str | None
        Canonical hex string, or ``None`` when absent / empty.
    """
    if source_hash is None:
        return None
    if isinstance(source_hash, (bytes, bytearray)):
        return source_hash.hex() if source_hash else None
    if isinstance(source_hash, str):
        return source_hash if source_hash else None
    return None


# ---------------------------------------------------------------------------
# Main projection
# ---------------------------------------------------------------------------


def project_lxmf_attribution(
    native_data: dict[str, Any],
) -> dict[str, str | None]:
    """Project the current LXMF native namespace into generic attribution."""
    lxmf = lxmf_namespace(native_data)
    sender_id = normalize_source_hash(lxmf.get("source_hash"))
    display_name = _label_str(lxmf.get("display_name"))
    short_name = _label_str(lxmf.get("short_name"))
    sender_label = display_name
    sender_short_label = short_name or _compact(display_name)

    return {
        "source_sender_id": sender_id,
        "source_sender_label": sender_label,
        "source_sender_short_label": sender_short_label,
    }


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _label_str(value: object) -> str | None:
    """Project *value* to a human-readable label string, strictly.

    Only text-bearing values are accepted:

    * :class:`str` -> returned as-is when non-whitespace-only,
      otherwise ``None``.  Leading/trailing whitespace on valid labels
      is preserved (e.g. ``"  Alice  "`` passes through unchanged).
    * :class:`bytes` / :class:`bytearray` -> decoded as UTF-8 with
      ``errors="replace"`` (matching the session's content/title
      normalisation); returned when non-whitespace-only, otherwise
      ``None``.

    All other types (``int``, ``float``, ``bool``, ``dict``, ``list``,
    ``None``, custom objects, ...) return ``None``.  This prevents
    arbitrary object coercion (e.g. ``str(123) == "123"`` or
    ``str({}) == "{}"``) from polluting display label fields such as
    ``source_sender_label``.  Display labels must originate from real
    text captured at ingress, not from runtime ``str()`` coercion.
    """
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, (bytes, bytearray)):
        s = bytes(value).decode("utf-8", errors="replace")
        return s if s.strip() else None
    return None


def _compact(value: str | None) -> str | None:
    """Strip spaces from *value*, returning ``None`` when the result is empty."""
    if value is None:
        return None
    return value.replace(" ", "") or None
