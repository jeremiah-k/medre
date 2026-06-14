"""MeshCore-native to generic attribution projection helper.

Projects MeshCore-specific native metadata (the ``meshcore.*`` namespaced
keys produced by :class:`~medre.adapters.meshcore.codec.MeshCoreCodec`,
plus the bare fixture keys used for backward compatibility) into generic
attribution fields used by the relay rendering pipeline.

The attribution dispatch (``_attribution_dispatch.project_source_fields``)
delegates to this helper when the source platform is detected as
MeshCore.  Core rendering has no MeshCore-specific key knowledge.

The module imports **no adapter packages** and never touches SDK objects;
all data arrives as plain dicts.

Generic fields produced
-----------------------
* ``source_sender_id`` — native sender identifier (pubkey prefix).
* ``source_native_channel_id`` — MeshCore channel index.
* ``source_native_message_id`` — MeshCore packet ID (sender timestamp).
* ``source_sender_label`` — known-contact advertised name
  (``meshcore.contact_label``), or ``None`` when the sender is not a
  locally-known contact.  Opaque pubkey prefixes never populate this
  field; use ``{sender_id}`` in templates to expose the pubkey.
* ``source_sender_short_label`` — explicit ``meshcore.contact_short_label``
  when present, otherwise the first whitespace-delimited token of the
  contact label.  ``None`` when no contact label is available.

Resolution order
----------------
``source_sender_id``
    ``meshcore.pubkey_prefix`` → ``meshcore.sender_id`` → bare
    ``pubkey_prefix``.  Each candidate is checked for a non-empty value
    before falling through.

``source_native_channel_id``
    ``meshcore.channel`` → bare ``channel_idx``.

``source_native_message_id``
    ``meshcore.packet_id``.

``source_sender_label``
    ``meshcore.contact_label``.  Only non-empty real human labels
    populate this field.  The adapter injects this key at ingress when
    the session's local contacts store recognises the sender pubkey.

``source_sender_short_label``
    ``meshcore.contact_short_label`` → first whitespace-delimited token
    of ``meshcore.contact_label``.  ``None`` when no contact label is
    available.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "MESHCORE_NAMESPACED_KEYS",
    "ProjectionMap",
    "is_meshcore_native",
    "project_meshcore_attribution",
]

# Type alias for the generic field map returned by the projection helper.
ProjectionMap = dict[str, str | None]

#: Characteristic MeshCore native-metadata keys (namespaced).  Presence of
#: any of these identifies a native dict as MeshCore-shaped.  Mirrors the
#: set used by the core platform-detection fallback.
#:
#: Contact-label keys (``meshcore.contact_label``,
#: ``meshcore.contact_short_label``) are intentionally excluded because a
#: dict carrying only those keys lacks the core identity signals
#: (pubkey_prefix, sender_id, channel, packet_id) that make a dict
#: unambiguously MeshCore-native.  Detection relies on those identity
#: keys; contact labels are enrichment layered on top.
MESHCORE_NAMESPACED_KEYS: frozenset[str] = frozenset(
    {
        "meshcore.pubkey_prefix",
        "meshcore.sender_id",
        "meshcore.channel",
        "meshcore.packet_id",
    }
)


def is_meshcore_native(native_data: dict[str, Any]) -> bool:
    """Return ``True`` when *native_data* carries MeshCore namespaced keys.

    Useful for platform detection without importing adapter internals.
    A bare non-namespaced ``pubkey_prefix`` is intentionally **not**
    treated as a MeshCore signal here — it is ambiguous across test
    fixtures.

    Parameters
    ----------
    native_data:
        Raw native metadata dict to inspect.

    Returns
    -------
    bool
        Whether any ``meshcore.*`` key is present.
    """
    return any(k in native_data for k in MESHCORE_NAMESPACED_KEYS)


def project_meshcore_attribution(
    native_data: dict[str, Any],
) -> ProjectionMap:
    """Project MeshCore-native fields into generic attribution fields.

    Parameters
    ----------
    native_data:
        Raw MeshCore native metadata dict.  Expected namespaced keys
        (as produced by the codec): ``meshcore.pubkey_prefix``,
        ``meshcore.sender_id``, ``meshcore.channel``,
        ``meshcore.packet_id``.  Bare fallback keys ``pubkey_prefix``
        and ``channel_idx`` are tolerated for backward compatibility
        with older data and test fixtures.  Missing keys are treated as
        absent (not an error).

    Returns
    -------
    dict[str, str | None]
        Generic attribution fields keyed by their ``RelayAttribution``
        canonical names: ``source_sender_id``,
        ``source_native_channel_id``, ``source_native_message_id``,
        ``source_sender_label``, ``source_sender_short_label``.
        Resolved values are coerced to ``str``; fields are ``None`` when
        no value could be resolved.  Label fields are ``None`` when no
        known-contact label is available; opaque pubkey prefixes never
        populate the label fields.
    """
    # --- sender_id: pubkey_prefix > sender_id > bare pubkey_prefix --
    sender_id: str | None = (
        _str(native_data.get("meshcore.pubkey_prefix"))
        or _str(native_data.get("meshcore.sender_id"))
        or _str(native_data.get("pubkey_prefix"))
    )

    # --- channel: meshcore.channel > bare channel_idx ---------------
    channel: str | None = _str(native_data.get("meshcore.channel")) or _str(
        native_data.get("channel_idx")
    )

    # --- packet_id: meshcore.packet_id ------------------------------
    packet_id: str | None = _str(native_data.get("meshcore.packet_id"))

    # --- sender_label: contact_label only (human labels, never pubkeys)
    contact_label: str | None = _str(native_data.get("meshcore.contact_label"))
    contact_short_label: str | None = _str(
        native_data.get("meshcore.contact_short_label")
    )

    sender_label: str | None = contact_label
    # Short label: explicit short label, else first token of contact label.
    sender_short_label: str | None = contact_short_label or _first_token(contact_label)

    return {
        "source_sender_id": sender_id,
        "source_native_channel_id": channel,
        "source_native_message_id": packet_id,
        "source_sender_label": sender_label,
        "source_sender_short_label": sender_short_label,
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
