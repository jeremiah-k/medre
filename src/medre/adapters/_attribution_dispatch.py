"""Shared source-platform attribution projection dispatch.

Detects the source platform from the adapter identifier string and
native metadata keys, then delegates to the appropriate adapter
projection helper.  Each adapter package ships its own projection
helper (``project_*_attribution``); this module is the single point
that knows how to route between them.

This module lives in the adapters package (not core) so that core
attribution remains free of native transport key knowledge.  It
imports **no core rendering modules** — it only calls pure-data
projection functions from adapter attribution modules.

Internal API — not re-exported from ``medre.adapters``.
"""

from __future__ import annotations

from typing import Any

from medre.adapters.meshcore.attribution import (
    MESHCORE_NAMESPACED_KEYS,
    project_meshcore_attribution,
)

__all__ = [
    "detect_source_platform",
    "project_source_fields",
]

# ---------------------------------------------------------------------------
# Adapter-ID heuristic fragments
# ---------------------------------------------------------------------------

_ID_HEURISTICS: tuple[tuple[str, str], ...] = (
    ("matrix", "matrix"),
    ("meshtastic", "meshtastic"),
    ("meshcore", "meshcore"),
    ("lxmf", "lxmf"),
)

# Characteristic native-metadata keys per platform (ordered by priority).
# Matrix is checked before Meshtastic because Matrix native data may be
# enriched with Meshtastic-style bare keys.

_MATRIX_KEYS: frozenset[str] = frozenset({"sender", "event_id", "room_id"})
_MESHTASTIC_KEYS: frozenset[str] = frozenset(
    {"longname", "shortname", "from_id", "packet_id", "channel"}
)
_LXMF_KEYS: frozenset[str] = frozenset({"source_hash", "destination_hash"})


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------


def detect_source_platform(
    source_adapter: str,
    native_data: dict[str, Any],
) -> str | None:
    """Detect the source platform from adapter ID and native metadata keys.

    Priority order:

    1. Adapter-ID substring heuristic (e.g. ``"meshtastic-radio"``
       → ``"meshtastic"``).
    2. Native key shape inspection (namespaced ``meshcore.*`` keys,
       then Matrix, Meshtastic, LXMF characteristic keys).

    Returns ``None`` when the platform cannot be determined.
    """
    # 1. Adapter-ID heuristic.
    lowered = source_adapter.lower()
    for fragment, platform in _ID_HEURISTICS:
        if fragment in lowered:
            return platform

    if not native_data:
        return None

    # 2. Native key shape fallback.
    # MeshCore: namespaced keys (highest priority for native detection).
    if any(k in native_data for k in MESHCORE_NAMESPACED_KEYS):
        return "meshcore"

    # Matrix: checked before Meshtastic because Matrix native data may
    # carry Meshtastic-enriched bare keys.
    if any(k in native_data for k in _MATRIX_KEYS):
        return "matrix"

    if any(k in native_data for k in _MESHTASTIC_KEYS):
        return "meshtastic"

    if any(k in native_data for k in _LXMF_KEYS):
        return "lxmf"

    return None


# ---------------------------------------------------------------------------
# Internal raw-value coercion
# ---------------------------------------------------------------------------


def _str(value: Any) -> str | None:
    """Coerce *value* to ``str`` or return ``None`` for missing."""
    if value is None:
        return None
    s = str(value)
    return s if s else None


def _extract_localpart(mxid: str) -> str:
    """Extract the localpart from a Matrix MXID (``@user:domain``)."""
    if mxid.startswith("@"):
        rest = mxid[1:]
        colon = rest.find(":")
        if colon > 0:
            return rest[:colon]
        return rest
    return mxid


# ---------------------------------------------------------------------------
# Projection dispatch
# ---------------------------------------------------------------------------


def project_source_fields(
    native_data: dict[str, Any],
    *,
    source_adapter: str = "",
    source_transport_id: str | None = None,
) -> dict[str, str | None]:
    """Project source attribution fields by detecting and dispatching
    to platform-appropriate extraction logic.

    Returns a dict of generic attribution fields keyed by their
    ``RelayAttribution`` canonical names, suitable for passing to
    ``build_relay_attribution(projected_fields=...)``.

    The dict always includes ``source_platform`` when the platform is
    detected; ``None`` otherwise.

    **Flat-key fallback:** After platform-specific extraction, any
    still-empty sender fields are patched from Meshtastic-style flat
    keys (``longname``, ``shortname``, ``from_id``) in *native_data*.
    This supports cross-platform relay where the codec pipeline enriches
    native metadata with these keys regardless of source platform.
    """
    platform = detect_source_platform(source_adapter, native_data)

    fields: dict[str, str | None] = {"source_platform": platform}

    # ------------------------------------------------------------------
    # Platform-specific extraction (raw values, preserving old behaviour)
    # ------------------------------------------------------------------

    if platform == "matrix":
        sender = native_data.get("sender")
        sender_str = _str(sender)
        display_name = native_data.get("displayname") or native_data.get("display_name")
        display_str = _str(display_name)
        short_label: str | None = None
        if sender_str:
            short_label = _extract_localpart(sender_str)
        fields.update(
            {
                "source_sender_id": sender_str,
                "source_sender_label": display_str,
                "source_sender_short_label": short_label,
                "source_sender_handle": sender_str,
            }
        )

    elif platform == "meshtastic":
        # Raw extraction matching old _extract_meshtastic_fields:
        # sender_label = longname only (no shortname fallback).
        longname = _str(native_data.get("longname"))
        shortname = _str(native_data.get("shortname"))
        from_id = _str(native_data.get("from_id"))
        fields.update(
            {
                "source_sender_id": from_id,
                "source_sender_label": longname,
                "source_sender_short_label": shortname,
            }
        )

    elif platform == "meshcore":
        # Delegate to the adapter projection helper which handles
        # namespaced and bare keys correctly.
        fields.update(project_meshcore_attribution(native_data))

    elif platform == "lxmf":
        # Only extract sender_id; LXMF has no native display name.
        # Labels remain unset so prefix templates render empty.
        from medre.adapters.lxmf.attribution import normalize_source_hash

        sender_id = normalize_source_hash(native_data.get("source_hash"))
        fields["source_sender_id"] = sender_id

    # ------------------------------------------------------------------
    # Flat-key fallback for cross-platform enrichment
    # ------------------------------------------------------------------
    # The codec pipeline may store Meshtastic-style flat keys
    # (longname, shortname, from_id) in native_data regardless of
    # source platform.  Patch any sender fields that are still empty.

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

    return fields
