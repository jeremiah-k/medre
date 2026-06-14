"""Meshtastic-native to generic attribution projection helper.

Projects Meshtastic-specific native fields (``meshtastic.longname``,
``meshtastic.shortname``, ``meshtastic.from_id``) into the generic
attribution schema used by the relay rendering pipeline.  This keeps the
Meshtastic-to-generic mapping in adapter code rather than in the
platform-neutral core extractors.

The primary consumer is the attribution dispatch
(:func:`~medre.adapters._attribution_dispatch.project_source_fields`)
which delegates to this helper when the source platform is detected as
Meshtastic.

Key reading
-----------
Namespaced keys (``meshtastic.from_id``, ``meshtastic.longname``,
``meshtastic.shortname``) are the primary source and the shape emitted by
:class:`~medre.adapters.meshtastic.codec.MeshtasticCodec`.  Bare
``from_id``/``longname``/``shortname`` keys are accepted as legacy input
tolerance (e.g. test fixtures and stored events produced before
namespacing); they are not present in newly produced native metadata.
Identity label keys (``longname``/``shortname``) are read namespaced-only
with bare-key legacy fallback.

Generic fields produced
-----------------------
* ``source_sender_id`` — native sender identifier.
* ``source_sender_label`` — primary human-readable label.
* ``source_sender_short_label`` — abbreviated label.

Resolution order
----------------
``source_sender_id``
    ``meshtastic.from_id`` → bare ``from_id`` → ``source_transport_id``
    → ``None``.

``source_sender_label``
    ``meshtastic.longname`` → ``meshtastic.shortname`` → bare ``longname``
    → bare ``shortname`` → ``source_sender_id``.

    Each candidate is checked for a non-empty value before falling
    through.  When *compact* is ``True``, spaces are stripped.

``source_sender_short_label``
    ``meshtastic.shortname`` → compact ``meshtastic.longname`` → bare
    ``shortname`` → compact bare ``longname`` → compact ``source_sender_id``.

    "Compact" means ``str.replace(" ", "")``.  The fallback chain
    ensures a non-empty short label is always available when any
    identifying field is present.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "project_meshtastic_attribution",
]

# Type alias for the generic field map returned by the projection helper.
ProjectionMap = dict[str, str | None]


def project_meshtastic_attribution(
    native_data: dict[str, Any],
    *,
    source_transport_id: str | None = None,
    compact: bool = False,
) -> ProjectionMap:
    """Project Meshtastic-native fields into generic attribution fields.

    Uses full fallback chains to produce the best available generic
    sender identity from Meshtastic-native keys.

    Parameters
    ----------
    native_data:
        Raw Meshtastic native metadata dict.  Primary keys are the
        namespaced ``meshtastic.from_id``, ``meshtastic.longname``,
        ``meshtastic.shortname``; bare ``from_id``/``longname``/
        ``shortname`` are accepted as legacy input tolerance.  Missing
        keys are treated as absent (not an error).
    source_transport_id:
        Fallback sender identifier (typically from
        ``event.source_transport_id``).  Used when ``from_id`` is not
        present in *native_data*.
    compact:
        When ``True``, strip spaces from display-name tokens in the
        projected labels.  This supports the Meshtastic renderer's
        compact-prefix mode for cross-platform reaction text.

    Returns
    -------
    dict[str, str | None]
        Generic attribution fields: ``source_sender_id``,
        ``source_sender_label``, ``source_sender_short_label``.
        Fields are ``None`` when no value could be resolved.
    """
    # Namespaced keys are the primary shape emitted by the codec; bare
    # keys are legacy input tolerance only.  Within each fallback chain,
    # all namespaced candidates are tried before any bare candidate so
    # the namespaced (current) shape wins over bare (legacy) shape when
    # both are present.
    m_from_id = _str(native_data.get("meshtastic.from_id"))
    b_from_id = _str(native_data.get("from_id"))
    m_longname = _str(native_data.get("meshtastic.longname"))
    b_longname = _str(native_data.get("longname"))
    m_shortname = _str(native_data.get("meshtastic.shortname"))
    b_shortname = _str(native_data.get("shortname"))

    # --- sender_id: meshtastic.from_id > bare from_id > transport ----
    sender_id: str | None = m_from_id or b_from_id or source_transport_id

    # --- sender_label: meshtastic.longname > meshtastic.shortname >
    #     bare longname > bare shortname > sender_id -------------------
    sender_label: str | None = (
        m_longname or m_shortname or b_longname or b_shortname or sender_id
    )

    # --- sender_short_label: meshtastic.shortname > compact meshtastic.longname
    #     > bare shortname > compact bare longname > compact sender_id --
    sender_short_label: str | None = (
        m_shortname
        or _compact(m_longname)
        or b_shortname
        or _compact(b_longname)
        or _compact(sender_id)
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
