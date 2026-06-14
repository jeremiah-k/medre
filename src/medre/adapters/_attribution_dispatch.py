"""Shared source-platform attribution projection dispatch.

Detects the source platform from an explicit platform hint, the adapter
identifier string, or native metadata keys, then delegates to the
appropriate adapter projection helper.  Each adapter package ships its
own projection helper (``project_*_attribution``); this module is the
single point that routes between them.

This module is **dispatch-only** — it contains no native key
interpretation logic.  All platform-specific extraction lives in the
adapter attribution modules.

This module lives in the adapters package (not core) so that core
attribution remains free of native transport key knowledge.  It imports
**no core rendering modules** — it only calls pure-data projection
functions from adapter attribution modules.

Internal API — not re-exported from ``medre.adapters``.
"""

from __future__ import annotations

from typing import Any

from medre.adapters.lxmf.attribution import project_lxmf_attribution
from medre.adapters.matrix.attribution import project_matrix_attribution
from medre.adapters.meshcore.attribution import (
    MESHCORE_NAMESPACED_KEYS,
    project_meshcore_attribution,
)
from medre.adapters.meshtastic.attribution import (
    apply_flat_key_fallback,
    project_meshtastic_attribution,
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
    *,
    platform_hint: str | None = None,
) -> str | None:
    """Detect the source platform from adapter ID and native metadata keys.

    Priority order:

    1. Explicit *platform_hint* (typically from
       :class:`SourceAttributionConfig.platform`).  When present, this
       takes precedence over all other detection methods — even if
       native metadata is sparse.
    2. Adapter-ID substring heuristic (e.g. ``"meshtastic-radio"``
       → ``"meshtastic"``).
    3. Native key shape inspection (namespaced ``meshcore.*`` keys,
       then Matrix, Meshtastic, LXMF characteristic keys).

    Returns ``None`` when the platform cannot be determined.

    Parameters
    ----------
    source_adapter:
        The adapter identifier string (e.g. ``"radio-a"``).
    native_data:
        Raw native metadata dict from the event.
    platform_hint:
        Optional explicit platform name from the runtime source
        attribution registry.  Highest priority when provided.
    """
    # 1. Explicit platform hint — highest priority.
    if platform_hint:
        return platform_hint

    # 2. Adapter-ID heuristic.
    lowered = source_adapter.lower()
    for fragment, platform in _ID_HEURISTICS:
        if fragment in lowered:
            return platform

    if not native_data:
        return None

    # 3. Native key shape fallback.
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
# Projection dispatch
# ---------------------------------------------------------------------------


def project_source_fields(
    native_data: dict[str, Any],
    *,
    source_adapter: str = "",
    source_transport_id: str | None = None,
    platform_hint: str | None = None,
) -> dict[str, str | None]:
    """Project source attribution fields by detecting and dispatching
    to platform-appropriate adapter projection helpers.

    Returns a dict of generic attribution fields keyed by their
    ``RelayAttribution`` canonical names, suitable for passing to
    ``build_relay_attribution(projected_fields=...)``.

    The dict always includes ``source_platform`` when the platform is
    detected; ``None`` otherwise.

    **Platform resolution** uses :func:`detect_source_platform` with the
    same priority: *platform_hint* > adapter-ID heuristic > native key
    shape > ``None``.

    **Flat-key fallback:** After platform-specific extraction, any
    still-empty sender fields are patched from Meshtastic-style flat
    keys (``longname``, ``shortname``, ``from_id``) in *native_data*.
    This supports cross-platform relay where the codec pipeline enriches
    native metadata with these keys regardless of source platform.  The
    fallback logic lives in :func:`meshtastic.attribution.apply_flat_key_fallback`.

    Parameters
    ----------
    native_data:
        Raw native metadata dict from the event.
    source_adapter:
        Adapter identifier string for platform detection.
    source_transport_id:
        Transport-level identifier (passed to Meshtastic projection as
        a sender_id fallback).
    platform_hint:
        Optional explicit platform name from the runtime source
        attribution registry.  Highest priority for platform resolution.
    """
    platform = detect_source_platform(
        source_adapter,
        native_data,
        platform_hint=platform_hint,
    )

    fields: dict[str, str | None] = {"source_platform": platform}

    # ------------------------------------------------------------------
    # Delegate to adapter-local projection helpers (no inline native
    # interpretation in this module).
    # ------------------------------------------------------------------

    if platform == "matrix":
        fields.update(project_matrix_attribution(native_data))

    elif platform == "meshtastic":
        fields.update(
            project_meshtastic_attribution(
                native_data,
                with_fallback=False,
            )
        )

    elif platform == "meshcore":
        fields.update(project_meshcore_attribution(native_data))

    elif platform == "lxmf":
        # Delegate source_hash normalisation to the adapter module.
        # Only sender_id is projected; LXMF has no native display name.
        # Labels remain unset so prefix templates render empty.
        lxmf = project_lxmf_attribution(native_data)
        fields["source_sender_id"] = lxmf.sender_id

    # ------------------------------------------------------------------
    # Flat-key fallback for cross-platform enrichment.
    # ------------------------------------------------------------------
    # The codec pipeline may store Meshtastic-style flat keys
    # (longname, shortname, from_id) in native_data regardless of
    # source platform.  Patch any sender fields that are still empty.
    # This logic lives in the Meshtastic attribution module.
    apply_flat_key_fallback(fields, native_data)

    return fields
