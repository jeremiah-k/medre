"""MeshCore-native to generic attribution projection helper.

Projects MeshCore-specific native metadata (the ``meshcore.*`` namespaced
keys produced by :class:`~medre.adapters.meshcore.codec.MeshCoreCodec`,
plus the bare fixture keys used for backward compatibility) into generic
attribution fields used by the relay rendering pipeline.

This keeps the MeshCore-to-generic mapping in adapter code rather than in
the platform-neutral core extractors.  After a follow-up wave, the core
extractors in :mod:`medre.core.rendering.attribution` will delegate to
this helper instead of knowing MeshCore key shapes directly.

The module imports **no adapter packages** and never touches SDK objects;
all data arrives as plain dicts.

Generic fields produced
-----------------------
* ``source_sender_id`` — native sender identifier (pubkey prefix).
* ``source_native_channel_id`` — MeshCore channel index.
* ``source_native_message_id`` — MeshCore packet ID (sender timestamp).
* ``source_sender_label`` — always ``None`` (MeshCore carries no display
  name; see the relay-attribution tests ``test_meshcore_no_display_name``
  and ``test_meshcore_sender_short_template``).
* ``source_sender_short_label`` — always ``None`` (no abbreviated label).

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
        no value could be resolved.  The two label fields are always
        ``None`` because MeshCore carries no display name.
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

    return {
        "source_sender_id": sender_id,
        "source_native_channel_id": channel,
        "source_native_message_id": packet_id,
        # MeshCore carries no display name or short label.
        "source_sender_label": None,
        "source_sender_short_label": None,
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
