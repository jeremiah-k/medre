"""Meshtastic-native to generic attribution projection helper.

Projects Meshtastic-specific native fields (``longname``, ``shortname``,
``from_id``) into the generic attribution schema used by the relay
rendering pipeline.  This keeps the Meshtastic-to-generic mapping in
adapter code rather than in the platform-neutral core extractors.

The primary consumer is :class:`~medre.adapters.meshtastic.renderer.MeshtasticRenderer`
which uses this helper for the flat-key fallback and compact-prefix
behaviour.

Generic fields produced
-----------------------
* ``source_sender_id`` — native sender identifier.
* ``source_sender_label`` — primary human-readable label.
* ``source_sender_short_label`` — abbreviated label.

Resolution order
----------------
``source_sender_id``
    ``from_id`` → ``source_transport_id`` → ``None``.

``source_sender_label``
    ``longname`` → ``shortname`` → ``source_sender_id``.

    Each candidate is checked for a non-empty value before falling
    through.  When *compact* is ``True``, spaces are stripped.

``source_sender_short_label``
    ``shortname`` → compact ``longname`` → compact ``source_sender_id``.

    "Compact" means ``str.replace(" ", "")``.  The fallback chain
    ensures a non-empty short label is always available when any
    identifying field is present.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "apply_flat_key_fallback",
    "project_meshtastic_attribution",
]

# Type alias for the generic field map returned by the projection helper.
ProjectionMap = dict[str, str | None]


def project_meshtastic_attribution(
    native_data: dict[str, Any],
    *,
    source_transport_id: str | None = None,
    compact: bool = False,
    with_fallback: bool = True,
) -> ProjectionMap:
    """Project Meshtastic-native fields into generic attribution fields.

    Parameters
    ----------
    native_data:
        Raw Meshtastic native metadata dict.  Expected keys:
        ``longname``, ``shortname``, ``from_id``.  Missing keys are
        treated as absent (not an error).
    source_transport_id:
        Fallback sender identifier (typically from
        ``event.source_transport_id``).  Used when ``from_id`` is not
        present in *native_data*.  Only used when *with_fallback* is
        ``True``.
    compact:
        When ``True``, strip spaces from display-name tokens in the
        projected labels.  This supports the Meshtastic renderer's
        compact-prefix mode for cross-platform reaction text.
    with_fallback:
        When ``True`` (default), use full fallback chains for sender
        identity:

        * ``sender_id`` ← ``from_id`` → ``source_transport_id``.
        * ``sender_label`` ← ``longname`` → ``shortname`` →
          ``sender_id``.
        * ``sender_short_label`` ← ``shortname`` → compact
          ``longname`` → compact ``sender_id``.

        When ``False``, each field uses only its primary native key
        with no fallback chain:

        * ``sender_id`` ← ``from_id`` only.
        * ``sender_label`` ← ``longname`` only.
        * ``sender_short_label`` ← ``shortname`` only.

        The ``False`` mode is used by the attribution dispatch for
        simple field extraction; callers that need richer identity
        resolution (e.g. the Meshtastic renderer) apply their own
        fallbacks after the dispatch returns.

    Returns
    -------
    dict[str, str | None]
        Generic attribution fields: ``source_sender_id``,
        ``source_sender_label``, ``source_sender_short_label``.
        Fields are ``None`` when no value could be resolved.
    """
    from_id = _str(native_data.get("from_id"))
    longname = _str(native_data.get("longname"))
    shortname = _str(native_data.get("shortname"))

    if not with_fallback:
        # Simple extraction — each field from its primary key only.
        return {
            "source_sender_id": from_id,
            "source_sender_label": longname,
            "source_sender_short_label": shortname,
        }

    # --- Full fallback chains ---------------------------------------
    sender_id: str | None = from_id or source_transport_id

    # --- sender_label: longname > shortname > sender_id --------------
    sender_label: str | None = longname or shortname or sender_id

    # --- sender_short_label: shortname > compact longname > compact sender_id
    sender_short_label: str | None = (
        shortname or _compact(longname) or _compact(sender_id)
    )

    # --- Compact mode: strip spaces from labels ----------------------
    if compact:
        sender_label = _compact(sender_label)
        sender_short_label = _compact(sender_short_label)

    return {
        "source_sender_id": sender_id,
        "source_sender_label": sender_label,
        "source_sender_short_label": sender_short_label,
    }


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _str(value: Any) -> str | None:
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


# ---------------------------------------------------------------------------
# Flat-key fallback (cross-platform enrichment)
# ---------------------------------------------------------------------------


def apply_flat_key_fallback(
    fields: dict[str, str | None],
    native_data: dict[str, Any],
) -> None:
    """Patch empty attribution fields from Meshtastic-style flat keys.

    The codec pipeline may store Meshtastic-style flat keys
    (``longname``, ``shortname``, ``from_id``) in *native_data*
    regardless of source platform.  This function patches any sender
    fields in *fields* that are still empty (``None`` or falsy) from
    those keys.

    Modifies *fields* in place.  This is a cross-platform enrichment
    step applied after platform-specific projection.

    Parameters
    ----------
    fields:
        Generic attribution fields dict (mutated in place).  Keys are
        ``RelayAttribution`` canonical names like ``source_sender_label``.
    native_data:
        Raw native metadata dict that may carry Meshtastic-style flat
        keys.
    """
    if not fields.get("source_sender_label"):
        ln = native_data.get("longname")
        if ln is not None:
            ln_str = str(ln)
            fields["source_sender_label"] = ln_str if ln_str else None

    if not fields.get("source_sender_short_label"):
        sn = native_data.get("shortname")
        if sn is not None:
            sn_str = str(sn)
            fields["source_sender_short_label"] = sn_str if sn_str else None

    if not fields.get("source_sender_id"):
        fid = native_data.get("from_id")
        if fid is not None:
            fid_str = str(fid)
            fields["source_sender_id"] = fid_str if fid_str else None
