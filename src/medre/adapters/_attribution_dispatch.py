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
from medre.adapters._native_metadata_dispatch import versioned_native_namespace
from medre.adapters.meshcore.attribution import project_meshcore_attribution
from medre.adapters.meshtastic.attribution import project_meshtastic_attribution

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
    3. Native key shape inspection, in this order:

       a. Versioned MeshCore namespace.
       b. Versioned Matrix namespace.
       c. Versioned Meshtastic namespace.
       d. Versioned LXMF namespace.

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

    # 3. Versioned native namespace fallback.
    if versioned_native_namespace(native_data, "meshcore"):
        return "meshcore"

    if versioned_native_namespace(native_data, "matrix"):
        return "matrix"

    if versioned_native_namespace(native_data, "meshtastic"):
        return "meshtastic"

    if versioned_native_namespace(native_data, "lxmf"):
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

    This function is **dispatch-only**.  It detects the platform and
    delegates to the appropriate adapter projection helper.  It does not
    perform cross-platform identity enrichment — each adapter's projection
    helper is responsible for its own native key interpretation.

    Parameters
    ----------
    native_data:
        Raw native metadata dict from the event.
    source_adapter:
        Adapter identifier string for platform detection.
    source_transport_id:
        Transport-level identifier.  Passed to the Meshtastic projection
        helper as a ``sender_id`` fallback when ``from_id`` is absent.
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
                source_transport_id=source_transport_id,
            )
        )

    elif platform == "meshcore":
        fields.update(project_meshcore_attribution(native_data))

    elif platform == "lxmf":
        # Delegate to the adapter projection helper.  The returned dict
        # carries source_sender_id (from source_hash) and, when a real
        # display name is captured at ingress, source_sender_label /
        # source_sender_short_label.  The opaque source_hash never
        # becomes {sender} -- operators use {sender_id} for the hash.
        fields.update(project_lxmf_attribution(native_data))

    return fields
