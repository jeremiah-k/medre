"""LXMF native-to-generic attribution projection.

Projects LXMF-native identity fields (``source_hash``, announce-derived
display name) into generic sender attribution fields used by the MEDRE
rendering pipeline.

The attribution dispatch (``_attribution_dispatch.project_source_fields``)
delegates to this helper when the source platform is detected as LXMF.
Core rendering has no LXMF-specific key knowledge.

**Projection rules**

* ``source_hash`` (bytes, bytearray, or hex str) -> ``source_sender_id``
  (canonical hex string).  The opaque hash is exposed via
  ``{sender_id}`` and is never used as a human-readable label.
* ``lxmf.display_name`` (str) -> ``source_sender_label`` when non-empty.
* ``lxmf.short_name`` (str) -> ``source_sender_short_label`` when
  non-empty, falling back to a compact form of the display name.
* When no display name is present both label fields are ``None`` --
  the opaque ``source_hash`` does not become ``{sender}``.  Operators
  who want the hash in a prefix use ``{sender_id}``.

Public symbols
--------------
* :func:`project_lxmf_attribution` -- main entry point, returns a
  ``dict[str, str | None]`` keyed by ``RelayAttribution`` canonical
  names.
* :func:`normalize_source_hash` -- bytes/str normalisation for
  ``source_hash``.
"""

from __future__ import annotations

from typing import Any

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
    """Project LXMF native metadata to generic sender attribution.

    Inspects *native_data* for the ``source_hash`` key (sender identity)
    and the optional ``lxmf.display_name`` / ``lxmf.short_name`` keys
    (announce-derived display labels).

    Recognised keys:

    * ``source_hash`` -- primary sender identity (bytes, bytearray, or
      hex str).  Projected to ``source_sender_id``.
    * ``lxmf.display_name`` -- human-readable sender label captured at
      ingress from announce metadata or message identity.  Projected to
      ``source_sender_label`` when non-empty.
    * ``lxmf.short_name`` -- abbreviated sender label.  Projected to
      ``source_sender_short_label`` when non-empty, falling back to a
      compact form of ``lxmf.display_name``.

    The opaque ``source_hash`` is **never** projected to a label field.
    When no display name is available both label fields are ``None`` so
    that ``{sender}`` renders empty rather than a truncated hash.

    Parameters
    ----------
    native_data:
        Native metadata dict produced by the LXMF codec.

    Returns
    -------
    dict[str, str | None]
        Generic attribution fields keyed by their ``RelayAttribution``
        canonical names: ``source_sender_id``,
        ``source_sender_label``, ``source_sender_short_label``.
    """
    sender_id = normalize_source_hash(native_data.get("source_hash"))

    display_name = _str(native_data.get("lxmf.display_name"))
    short_name = _str(native_data.get("lxmf.short_name"))

    # Label fields come from real display names only -- never from the
    # opaque source_hash.  When no display name is present both fields
    # stay None so {sender} renders empty.
    sender_label: str | None = display_name
    sender_short_label: str | None = short_name or _compact(display_name)

    return {
        "source_sender_id": sender_id,
        "source_sender_label": sender_label,
        "source_sender_short_label": sender_short_label,
    }


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _str(value: object) -> str | None:
    """Coerce *value* to ``str`` or return ``None`` for missing/empty.

    ``bytes`` and ``bytearray`` are decoded as UTF-8 (matching the
    session's content/title normalisation).  Other non-``None`` types
    are coerced via ``str()`` -- this never raises, though the result
    may be semantically meaningless for exotic types.  An empty result
    after coercion returns ``None``.
    """
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        try:
            s = bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            s = bytes(value).decode("utf-8", errors="replace")
    else:
        s = str(value)
    return s if s else None


def _compact(value: str | None) -> str | None:
    """Strip spaces from *value*, returning ``None`` when the result is empty."""
    if value is None:
        return None
    return value.replace(" ", "") or None
