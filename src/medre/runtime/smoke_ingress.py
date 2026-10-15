"""Shared route-aware injection for smoke, run sessions, and failure drills."""

from __future__ import annotations

from typing import Any

from medre.config.model import RuntimeConfig
from medre.core.events.canonical import CanonicalEvent
from medre.core.events.kinds import EventKind
from medre.core.routing import Router
from medre.runtime.app import MedreApp
from medre.runtime.route_engine import build_runtime_routes


def expected_smoke_deliveries(
    config: RuntimeConfig, event: CanonicalEvent
) -> set[tuple[str, str]]:
    """Resolve configured fanout before startup can prune unavailable targets."""
    router = Router(build_runtime_routes(config.routes) if config.routes else [])
    return {
        (route.id, target.adapter)
        for route in router.match(event)
        for target in router.resolve_targets(event, route)
        if target.adapter is not None
    }


def make_smoke_event(
    adapter: Any,
    text: str,
) -> CanonicalEvent:
    """Preserve native payload fields and ensure canonical text is present.

    Adapter event factories may use native payload keys. Ensure the canonical
    text renderer receives the injected text while retaining native fields.
    """
    base = adapter.make_event(text=text, event_kind=EventKind.MESSAGE_TEXT)
    merged = dict(base.payload)
    merged["text"] = text
    return CanonicalEvent(
        event_id=base.event_id,
        event_kind=base.event_kind,
        schema_version=base.schema_version,
        timestamp=base.timestamp,
        source_adapter=base.source_adapter,
        source_transport_id=base.source_transport_id,
        source_channel_id=base.source_channel_id,
        parent_event_id=base.parent_event_id,
        lineage=base.lineage,
        relations=base.relations,
        payload=merged,
        metadata=base.metadata,
        source_native_ref=base.source_native_ref,
    )


# ---------------------------------------------------------------------------
# Source adapter selection
# ---------------------------------------------------------------------------


def eligible_smoke_sources(app: MedreApp) -> list[str]:
    """List route sources that can be exercised by smoke injection."""
    return [
        adapter_id
        for adapter_id in sorted(app.adapters)
        if adapter_id in app.started_adapter_ids
        and callable(getattr(app.adapters[adapter_id], "make_event", None))
        and any(
            route.enabled and route.source.adapter in (None, adapter_id)
            for route in app._registered_routes
        )
    ]


def pick_source_adapter(
    app: MedreApp, requested_source: str | None = None
) -> tuple[str, Any]:
    """Choose a route source, optionally selected explicitly by the operator."""
    candidates = eligible_smoke_sources(app)
    if requested_source is not None:
        if requested_source not in candidates:
            raise ValueError(
                f"Smoke source {requested_source!r} is not an enabled route source; "
                f"available: {candidates}"
            )
        return requested_source, app.adapters[requested_source]
    if candidates:
        adapter_id = candidates[0]
        return adapter_id, app.adapters[adapter_id]
    # A route-less smoke still reports its normal no-delivery evidence.
    sources = sorted(
        adapter_id
        for adapter_id in app.started_adapter_ids
        if callable(getattr(app.adapters[adapter_id], "make_event", None))
    )
    if not sources:
        raise ValueError("No started adapter provides the smoke make_event helper")
    adapter_id = sources[0]
    return adapter_id, app.adapters[adapter_id]
