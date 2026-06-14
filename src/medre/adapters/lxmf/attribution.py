"""LXMF native-to-generic attribution projection.

Projects LXMF-native identity fields (``source_hash``, identity hash)
into the generic sender attribution fields used by the MEDRE rendering
pipeline.

This module is the **adapter-adjacent authority** for LXMF sender
projection.  After the next migration wave the core attribution code
will delegate LXMF projection here instead of embedding LXMF-specific
key knowledge directly.

**Projection rules**

* ``source_hash`` (bytes, bytearray, or hex str) → ``sender_id``
  (canonical hex string).
* ``sender_id`` → ``label`` (full hash, or first 16 hex chars with ``…``
  when longer).
* ``sender_id`` → ``short_label`` (first 8 hex characters).
* No ``source_display_name`` is projected — LXMF prefix default remains
  off and only generic sender fields are used.

Public symbols
--------------
* :class:`LxmfAttribution` — immutable projection result.
* :func:`project_lxmf_attribution` — main entry point.
* :func:`normalize_source_hash` — bytes/str normalisation.
* :func:`derive_label` — long human-readable label from hash.
* :func:`derive_short_label` — abbreviated label from hash.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "LxmfAttribution",
    "derive_label",
    "derive_short_label",
    "normalize_source_hash",
    "project_lxmf_attribution",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SHORT_LABEL_LEN: int = 8
"""Number of hex characters for the short label."""

_LABEL_TRUNCATE_LEN: int = 16
"""Max hex characters before truncating the long label."""

_ELLIPSIS: str = "\u2026"
"""Unicode ellipsis character appended to truncated labels."""


# ---------------------------------------------------------------------------
# LxmfAttribution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LxmfAttribution:
    """Immutable projection of LXMF-native fields to generic sender
    attribution.

    Every field is ``None`` when the source data is absent.

    Attributes
    ----------
    sender_id:
        Canonical hex string of the ``source_hash``, or ``None`` when
        absent.
    label:
        Human-readable label derived from the hash (truncated with ``…``
        when the hash exceeds 16 hex characters).  ``None`` when
        ``sender_id`` is absent.
    short_label:
        Abbreviated label (first 8 hex characters).  ``None`` when
        ``sender_id`` is absent.
    """

    sender_id: str | None = None
    label: str | None = None
    short_label: str | None = None


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
# Label derivation
# ---------------------------------------------------------------------------


def derive_label(sender_id: str) -> str:
    """Derive a human-readable label from a sender ID hash.

    Returns the full hash if it is short enough, otherwise truncates to
    ``_LABEL_TRUNCATE_LEN`` hex characters with a trailing ellipsis.

    Parameters
    ----------
    sender_id:
        Canonical hex string of the sender identity.

    Returns
    -------
    str
        Human-readable label (never empty for non-empty input).
    """
    if len(sender_id) <= _LABEL_TRUNCATE_LEN:
        return sender_id
    return sender_id[:_LABEL_TRUNCATE_LEN] + _ELLIPSIS


def derive_short_label(sender_id: str) -> str:
    """Derive a short label (abbreviated hash prefix).

    Returns the first ``_SHORT_LABEL_LEN`` hex characters of the sender
    ID.

    Parameters
    ----------
    sender_id:
        Canonical hex string of the sender identity.

    Returns
    -------
    str
        Short label (may be shorter than ``_SHORT_LABEL_LEN`` if the
        input is shorter).
    """
    return sender_id[:_SHORT_LABEL_LEN]


# ---------------------------------------------------------------------------
# Main projection
# ---------------------------------------------------------------------------


def project_lxmf_attribution(native_data: dict[str, Any]) -> LxmfAttribution:
    """Project LXMF native metadata to generic sender attribution.

    Inspects the ``native_data`` dict (typically
    ``event.metadata.native.data``) for the ``source_hash`` key and
    derives generic sender attribution fields suitable for the MEDRE
    rendering pipeline.

    Recognised keys:
    * ``source_hash`` — primary sender identity (bytes or hex str).

    Parameters
    ----------
    native_data:
        Native metadata dict produced by the LXMF codec.

    Returns
    -------
    LxmfAttribution
        Frozen projection with ``sender_id``, ``label``, and
        ``short_label``.  All fields are ``None`` when ``source_hash``
        is absent or empty.
    """
    sender_id = normalize_source_hash(native_data.get("source_hash"))

    if sender_id is None:
        return LxmfAttribution()

    return LxmfAttribution(
        sender_id=sender_id,
        label=derive_label(sender_id),
        short_label=derive_short_label(sender_id),
    )
