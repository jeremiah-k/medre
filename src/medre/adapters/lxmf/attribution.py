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
* ``lxmf.display_name`` (str, or bytes/bytearray decoded as UTF-8) ->
  ``source_sender_label`` when non-empty.  Only text-bearing values are
  accepted; non-text types (int, dict, list, ...) yield ``None`` so that
  ``str()`` coercion never produces a misleading label.
* ``lxmf.short_name`` (str, or bytes/bytearray decoded as UTF-8) ->
  ``source_sender_short_label`` when non-empty, falling back to a
  compact form of the display name.  The same strict typing applies.
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
      ``source_sender_label`` when non-empty.  Only text-bearing values
      (``str``, ``bytes``, ``bytearray``) are accepted; non-text types
      yield ``None`` rather than being coerced via ``str()``.
    * ``lxmf.short_name`` -- abbreviated sender label.  Projected to
      ``source_sender_short_label`` when non-empty, falling back to a
      compact form of ``lxmf.display_name``.  The same strict typing
      applies.

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

    display_name = _label_str(native_data.get("lxmf.display_name"))
    short_name = _label_str(native_data.get("lxmf.short_name"))

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
