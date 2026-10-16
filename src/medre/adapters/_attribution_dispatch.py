"""Registry-driven source-platform attribution projection dispatch.

This shared adapter-infrastructure module performs dispatch only.  Native field
interpretation remains adapter-owned and is reached through lazy registry refs,
so adding a transport does not require another shared if/elif chain.
"""

from __future__ import annotations

import re
from typing import Any

from medre.adapter_registry import (
    get_adapter_spec,
    iter_adapter_specs,
    native_detection_specs,
)
from medre.adapters._native_metadata_dispatch import versioned_native_namespace

__all__ = ["detect_source_platform", "project_source_fields"]


def detect_source_platform(
    source_adapter: str,
    native_data: dict[str, Any],
    *,
    platform_hint: str | None = None,
) -> str | None:
    """Detect source platform from an explicit hint, adapter ID, or metadata."""
    if platform_hint:
        return platform_hint

    # Native evidence is authoritative; instance IDs are arbitrary operator labels.
    for spec in native_detection_specs():
        if versioned_native_namespace(native_data, spec.transport):
            return spec.transport

    lowered = source_adapter.lower()
    specs = sorted(
        iter_adapter_specs(), key=lambda spec: (-len(spec.transport), spec.transport)
    )
    for spec in specs:
        if lowered == spec.transport:
            return spec.transport
    for spec in specs:
        if re.search(
            r"(?<![a-z0-9])" + re.escape(spec.transport) + r"(?![a-z0-9])", lowered
        ):
            return spec.transport
    return None


def project_source_fields(
    native_data: dict[str, Any],
    *,
    source_adapter: str = "",
    source_transport_id: str | None = None,
    platform_hint: str | None = None,
) -> dict[str, str | None]:
    """Project source attribution through the registered adapter projector."""
    platform = detect_source_platform(
        source_adapter, native_data, platform_hint=platform_hint
    )
    fields: dict[str, str | None] = {"source_platform": platform}
    if platform is None:
        return fields

    spec = get_adapter_spec(platform)
    if spec is None:
        return fields
    projector = spec.attribution_projector.load()
    fields.update(projector(native_data, source_transport_id=source_transport_id))
    return fields
