"""Runtime diagnostic snapshot for deterministic introspection.

Provides a pure function :func:`capture_runtime_snapshot` that aggregates
existing runtime state into a JSON-safe, deterministic dictionary.  The
snapshot makes the current system behaviour **visible** without adding
new infrastructure.

The snapshot contains:

* **Adapters** – registered adapter health via
  :func:`~medre.core.supervision.health.normalize_adapter_health`.
* **Renderer registry / platform registry** – from
  :class:`~medre.core.rendering.renderer.RenderingPipeline.status_summary`.
* **Storage / replay backend status** – placeholder summaries.
* **Event bus status** – from
  :class:`~medre.core.events.bus.EventBus.status_summary`.
* **Route topology** – topology-aware route diagnostics from
  :class:`~medre.core.routing.router.Router`, including per-route
  identity, source/target topology, enabled/disabled counts,
  adapter-route relationships, and live delivery counters when
  :class:`~medre.core.routing.stats.RouteStats` is provided (or
  zeroed defaults when absent).
* **Queue / backpressure / task status** – ``{"status": "unavailable"}``
  sentinels when no real data is provided.

Public symbols
--------------
* :class:`RuntimeSnapshot` – frozen snapshot with :meth:`to_dict`.
* :func:`capture_runtime_snapshot` – pure function that builds a snapshot.
* :func:`capture_route_topology` – pure function that builds route topology.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

from medre.core.contracts.adapter import AdapterCapabilities
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.planning.capabilities import capability_unsupported
from medre.core.supervision.health import normalize_adapter_health

# Forward-reference type alias for Router; imported lazily inside
# capture_route_topology to avoid hard coupling at module load.
_RouterLike = Any


# ---------------------------------------------------------------------------
# Sentinel for unimplemented subsystems
# ---------------------------------------------------------------------------

_UNAVAILABLE_SENTINEL: dict[str, str] = {"status": "unavailable"}
"""Deterministic placeholder for subsystems that are not yet available."""


# ---------------------------------------------------------------------------
# Adapter health input protocol (structural typing)
# ---------------------------------------------------------------------------


class _AdapterHealthInput:
    """Minimal structural type accepted for adapter health entries.

    Attributes
    ----------
    info:
        An :class:`~medre.core.contracts.adapter.AdapterInfo` instance.
    lifecycle_state:
        Optional :class:`~medre.core.lifecycle.states.AdapterState`.
    adapter:
        Optional adapter instance for fake/live detection.
    details:
        Optional protocol-specific details dict.
    """

    __slots__ = ("info", "lifecycle_state", "adapter", "details")

    def __init__(
        self,
        info: Any,
        lifecycle_state: Any | None = None,
        adapter: Any | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        self.info = info
        self.lifecycle_state = lifecycle_state
        self.adapter = adapter
        self.details = details


# ---------------------------------------------------------------------------
# RuntimeSnapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeSnapshot:
    """Immutable, JSON-safe diagnostic snapshot of the runtime.

    Use :func:`capture_runtime_snapshot` to construct.  Call
    :meth:`to_dict` for deterministic serialisation.

    Attributes
    ----------
    adapters:
        Sorted list of normalised adapter health dicts.
    renderer_registry:
        Status summary from the rendering pipeline.
    event_bus_status:
        Status summary from the event bus.
    storage_backend_status:
        Placeholder or summary for the storage backend.
    replay_backend_status:
        Placeholder or summary for the replay backend.
    route_topology:
        Topology-aware route diagnostics from the Router, including
        live per-route counters when RouteStats is provided.
    queue_status:
        Queue status dict.  Defaults to ``{"status": "unavailable"}``.
    backpressure_status:
        Backpressure status dict.  Defaults to ``{"status": "unavailable"}``.
    task_status:
        Task status dict.  Defaults to ``{"status": "unavailable"}``.
    """

    adapters: tuple[dict[str, Any], ...]
    renderer_registry: dict[str, Any]
    event_bus_status: dict[str, Any]
    storage_backend_status: dict[str, Any]
    replay_backend_status: dict[str, Any]
    route_topology: dict[str, Any] = field(
        default_factory=lambda: dict(_UNAVAILABLE_SENTINEL),
    )
    queue_status: dict[str, str] = field(
        default_factory=lambda: dict(_UNAVAILABLE_SENTINEL),
    )
    backpressure_status: dict[str, str] = field(
        default_factory=lambda: dict(_UNAVAILABLE_SENTINEL),
    )
    task_status: dict[str, str] = field(
        default_factory=lambda: dict(_UNAVAILABLE_SENTINEL),
    )

    def to_dict(self) -> dict[str, Any]:
        """Return a deterministic, JSON-safe dictionary.

        All list and sub-dict outputs are sorted by key for stable
        serialisation with ``json.dumps(sort_keys=True)``.
        """
        return _sorted_dict(
            {
                "adapters": [self._normalised_adapter(a) for a in self.adapters],
                "renderer_registry": _sorted_dict(self.renderer_registry),
                "event_bus_status": _sorted_dict(self.event_bus_status),
                "storage_backend_status": _sorted_dict(self.storage_backend_status),
                "replay_backend_status": _sorted_dict(self.replay_backend_status),
                "route_topology": _sorted_dict(self.route_topology),
                "queue_status": _sorted_dict(self.queue_status),
                "backpressure_status": _sorted_dict(self.backpressure_status),
                "task_status": _sorted_dict(self.task_status),
            }
        )

    @staticmethod
    def _normalised_adapter(adapter_dict: dict[str, Any]) -> dict[str, Any]:
        """Sort an adapter health dict and its nested dicts."""
        result: dict[str, Any] = {}
        for key in sorted(adapter_dict):
            val = adapter_dict[key]
            if isinstance(val, dict):
                result[key] = _sorted_dict(val)
            else:
                result[key] = val
        return result


# ---------------------------------------------------------------------------
# Pure snapshot builder
# ---------------------------------------------------------------------------


def capture_runtime_snapshot(
    *,
    adapter_healths: Sequence[_AdapterHealthInput] | None = None,
    renderer_pipeline: Any | None = None,
    event_bus: Any | None = None,
    storage_status: dict[str, Any] | None = None,
    replay_status: dict[str, Any] | None = None,
    router: _RouterLike | None = None,
    route_stats: Any | None = None,
    capabilities: dict[str, AdapterCapabilities] | None = None,
    queue_status: dict[str, Any] | None = None,
    backpressure_status: dict[str, Any] | None = None,
    task_status: dict[str, Any] | None = None,
) -> RuntimeSnapshot:
    """Build a deterministic runtime diagnostic snapshot.

    This is a pure function: it reads state from the supplied objects and
    returns an immutable snapshot.  It does **not** start polls, trigger
    health checks, or modify any supplied object.

    Parameters
    ----------
    adapter_healths:
        Sequence of :class:`_AdapterHealthInput` entries describing each
        registered adapter.  Each entry's ``info`` is passed through
        :func:`~medre.core.supervision.health.normalize_adapter_health`.
    renderer_pipeline:
        Optional :class:`~medre.core.rendering.renderer.RenderingPipeline`.
        When provided, its :meth:`status_summary` output is captured.
    event_bus:
        Optional :class:`~medre.core.events.bus.EventBus`.  When provided,
        its :meth:`status_summary` output is captured.
    storage_status:
        Optional dict summarising the storage backend.  Defaults to a
        placeholder when not provided.
    replay_status:
        Optional dict summarising the replay backend.  Defaults to a
        placeholder when not provided.
    router:
        Optional :class:`~medre.core.routing.router.Router`.  When
        provided, :func:`capture_route_topology` builds a topology-aware
        snapshot of all registered routes.
    route_stats:
        Optional :class:`~medre.core.routing.stats.RouteStats`.  When
        provided alongside *router*, live per-route counters are
         included in the route topology snapshot.
    capabilities:
        Optional mapping of adapter IDs to their declared
        :class:`~medre.core.contracts.adapter.AdapterCapabilities`.
        When provided alongside *router*, each route entry is enriched
        with ``capability_warnings`` for event kinds the target adapter
        does not support.
    queue_status:
        Optional dict summarising queue state.  Defaults to
        ``{"status": "unavailable"}`` when not provided.
    backpressure_status:
        Optional dict summarising backpressure state.  Defaults to
        ``{"status": "unavailable"}`` when not provided.
    task_status:
        Optional dict summarising task state.  Defaults to
        ``{"status": "unavailable"}`` when not provided.

    Returns
    -------
    RuntimeSnapshot
        Frozen snapshot with :meth:`RuntimeSnapshot.to_dict`.
    """
    # -- Adapter health entries (sorted by adapter_id for determinism) ------
    adapter_entries: list[dict[str, Any]] = []
    if adapter_healths is not None:
        for entry in adapter_healths:
            normalised = normalize_adapter_health(
                entry.info,
                lifecycle_state=entry.lifecycle_state,
                adapter=entry.adapter,
                details=entry.details,
            )
            adapter_entries.append(normalised)
    adapter_entries.sort(key=lambda d: d.get("adapter_id", ""))

    # -- Renderer registry --------------------------------------------------
    if renderer_pipeline is not None and hasattr(renderer_pipeline, "status_summary"):
        renderer_summary = renderer_pipeline.status_summary()
    else:
        renderer_summary = dict(_UNAVAILABLE_SENTINEL)

    # -- Event bus status ---------------------------------------------------
    if event_bus is not None and hasattr(event_bus, "status_summary"):
        bus_summary = event_bus.status_summary()
    else:
        bus_summary = dict(_UNAVAILABLE_SENTINEL)

    # -- Storage / replay placeholders --------------------------------------
    storage_summary = (
        storage_status if storage_status is not None else dict(_UNAVAILABLE_SENTINEL)
    )
    replay_summary = (
        replay_status if replay_status is not None else dict(_UNAVAILABLE_SENTINEL)
    )

    # -- Route topology -----------------------------------------------------
    if router is not None:
        route_topology_dict = capture_route_topology(
            router,
            route_stats=route_stats,
            capabilities=capabilities,
        )
    else:
        route_topology_dict = dict(_UNAVAILABLE_SENTINEL)

    # -- Queue / backpressure / task status ----------------------------------
    queue_summary = (
        queue_status if queue_status is not None else dict(_UNAVAILABLE_SENTINEL)
    )
    backpressure_summary = (
        backpressure_status
        if backpressure_status is not None
        else dict(_UNAVAILABLE_SENTINEL)
    )
    task_summary = (
        task_status if task_status is not None else dict(_UNAVAILABLE_SENTINEL)
    )

    return RuntimeSnapshot(
        adapters=tuple(adapter_entries),
        renderer_registry=renderer_summary,
        event_bus_status=bus_summary,
        storage_backend_status=storage_summary,
        replay_backend_status=replay_summary,
        route_topology=route_topology_dict,
        queue_status=queue_summary,
        backpressure_status=backpressure_summary,
        task_status=task_summary,
    )


# ---------------------------------------------------------------------------
# Route topology snapshot
# ---------------------------------------------------------------------------


def _route_source_to_dict(source: Any) -> dict[str, Any]:
    """Convert a :class:`RouteSource` to a JSON-safe dict."""
    return {
        "adapter": getattr(source, "adapter", None),
        "event_kinds": list(getattr(source, "event_kinds", ()) or ()),
        "channel": getattr(source, "channel", None),
    }


def _route_target_to_dict(target: Any) -> dict[str, Any]:
    """Convert a :class:`RouteTarget` to a JSON-safe dict."""
    result: dict[str, Any] = {
        "adapter": getattr(target, "adapter", None),
        "channel": getattr(target, "channel", None),
    }
    dest = getattr(target, "destination", None)
    if dest is not None:
        result["destination"] = {
            "kind": getattr(dest, "kind", None),
            "destination_hash": getattr(dest, "destination_hash", None),
            "destination_name": getattr(dest, "destination_name", None),
        }
    return result


def _check_capability_warning(
    event_kind: str,
    caps: AdapterCapabilities,
    adapter_id: str,
) -> str | None:
    """Return a warning string if *event_kind* is unsupported by *caps*.

    Delegates to the shared :func:`capability_unsupported` and formats
    the result with adapter identity for diagnostics.

    Also checks reply support explicitly, because the shared
    ``capability_unsupported`` only checks replies when ``relations``
    is non-empty, but the synthetic event used here has ``relations=()``.
    """
    # Construct a minimal event for the shared check (only event_kind and
    # relations matter for capability checking).
    event = CanonicalEvent(
        event_id="diag-00000000-0000-0000-0000-000000000000",
        event_kind=event_kind,
        schema_version=1,
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        source_adapter="diag",
        source_transport_id="diag",
        source_channel_id=None,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={},
        metadata=EventMetadata(),
    )
    reason = capability_unsupported(event, caps)
    if reason is None:
        # Explicit check for reply support: the shared capability_unsupported
        # only detects unsupported replies when relations are non-empty, but
        # the diagnostics synthetic event has no relations.  Check directly.
        if caps.replies == "unsupported":
            return (
                f"event_kind '{event_kind}' not supported by target adapter "
                f"'{adapter_id}': replies unsupported by adapter"
            )
        return None
    return f"event_kind '{event_kind}' not supported by target adapter '{adapter_id}': {reason}"


def capture_route_topology(
    router: _RouterLike,
    route_stats: Any | None = None,
    capabilities: dict[str, AdapterCapabilities] | None = None,
) -> dict[str, Any]:
    """Build a deterministic, JSON-safe route topology snapshot.

    This is a **pure, observational** function: it reads the current
    route registration state from *router* and returns a frozen-style
    dictionary.  It does **not** modify the router, perform I/O, or
    create new mutable global state.

    This function exposes:

    * **Static topology** – route identities, source specs, target
      adapters, enabled/disabled state, ownership, and fanout strategy.
    * **Derived health summary** – counts of enabled, disabled, and
      total routes.
    * **Adapter-route map** – which adapters appear as sources or
      targets of which routes.
    * **Live counters** – when *route_stats* (a
      :class:`~medre.core.routing.stats.RouteStats`) is provided, each
        per-route entry is enriched with ``delivered``, ``failed``,
        ``skipped``, ``loop_prevented``, ``policy_suppressed``,
        ``capability_suppressed``, and ``last_error`` from the live
      counters.  When *route_stats* is ``None``, counters remain zeroed
      and ``last_error`` is omitted.

    Parameters
    ----------
    router:
        A :class:`~medre.core.routing.router.Router` instance.  Must
        have a ``_routes`` attribute mapping route IDs to
        :class:`~medre.core.routing.models.Route` objects.
    route_stats:
        Optional :class:`~medre.core.routing.stats.RouteStats` instance.
        When provided, live counter values replace the zeroed defaults.
    capabilities:
        Optional mapping of adapter IDs to their declared
        :class:`~medre.core.contracts.adapter.AdapterCapabilities`.
        When provided, each route entry is enriched with a
        ``capability_warnings`` list identifying event kinds that the
        target adapter does not support.

    Returns
    -------
    dict[str, Any]
        Deterministic topology snapshot suitable for JSON serialisation.
    """
    raw_routes: dict[str, Any] = getattr(router, "_routes", {})

    # Build live stats lookup when route_stats is provided.
    stats_snapshot: dict[str, dict] = {}
    if route_stats is not None and hasattr(route_stats, "snapshot"):
        stats_snapshot = route_stats.snapshot()

    per_route: list[dict[str, Any]] = []
    enabled_count = 0
    disabled_count = 0
    # adapter_name -> {"source_of": [...], "target_of": [...]}
    adapter_map: dict[str, dict[str, list[str]]] = {}

    for route_id in sorted(raw_routes):
        route = raw_routes[route_id]
        rid = getattr(route, "id", route_id)
        enabled = bool(getattr(route, "enabled", True))
        ownership = getattr(route, "ownership", "shared")
        fanout = getattr(route, "fanout_strategy", "broadcast")

        if enabled:
            enabled_count += 1
        else:
            disabled_count += 1

        source = getattr(route, "source", None)
        source_dict = _route_source_to_dict(source) if source is not None else {}
        targets = getattr(route, "targets", [])
        target_dicts = [_route_target_to_dict(t) for t in targets]

        # Collect target adapter names
        target_adapters: list[str | None] = [
            getattr(t, "adapter", None) for t in targets
        ]

        # Determine per-route counters from RouteStats or zeroed defaults.
        live = stats_snapshot.get(rid)
        if live is not None:
            delivered = live.get("delivered", 0)
            failed = live.get("failed", 0)
            skipped = live.get("skipped", 0)
            loop_prevented = live.get("loop_prevented", 0)
            policy_suppressed = live.get("policy_suppressed", 0)
            capability_suppressed = live.get("capability_suppressed", 0)
            last_error = live.get("last_error")
        else:
            delivered = 0
            failed = 0
            skipped = 0
            loop_prevented = 0
            policy_suppressed = 0
            capability_suppressed = 0
            last_error = None

        route_entry: dict[str, Any] = {
            "route_id": rid,
            "enabled": enabled,
            "ownership": ownership,
            "fanout_strategy": fanout,
            "source": source_dict,
            "targets": target_dicts,
            "target_count": len(targets),
            "target_adapters": sorted(a for a in target_adapters if a is not None),
            "delivered": delivered,
            "failed": failed,
            "skipped": skipped,
            "loop_prevented": loop_prevented,
            "policy_suppressed": policy_suppressed,
            "capability_suppressed": capability_suppressed,
            "error_count": failed,
            "event_count": delivered,
        }
        if last_error is not None:
            route_entry["last_error"] = last_error

        # -- Capability mismatch warnings ---------------------------------
        capability_warnings: list[str] = []
        if capabilities is not None:
            source_event_kinds = (
                list(getattr(source, "event_kinds", ()) or ())
                if source is not None
                else []
            )
            for ta in target_adapters:
                if ta is None:
                    continue
                caps = capabilities.get(ta)
                if caps is None:
                    continue
                for ek in source_event_kinds:
                    warning = _check_capability_warning(ek, caps, ta)
                    if warning is not None:
                        capability_warnings.append(warning)
        route_entry["capability_warnings"] = sorted(set(capability_warnings))

        per_route.append(route_entry)

        # Build adapter-route relationships
        src_adapter = getattr(source, "adapter", None) if source is not None else None
        if src_adapter is not None:
            adapter_map.setdefault(src_adapter, {"source_of": [], "target_of": []})
            adapter_map[src_adapter]["source_of"].append(rid)

        for ta in target_adapters:
            if ta is not None:
                adapter_map.setdefault(ta, {"source_of": [], "target_of": []})
                adapter_map[ta]["target_of"].append(rid)

    # Sort adapter map deterministically
    sorted_adapter_map: dict[str, dict[str, list[str]]] = {}
    for adapter_name in sorted(adapter_map):
        entry = adapter_map[adapter_name]
        sorted_adapter_map[adapter_name] = {
            "source_of": sorted(entry["source_of"]),
            "target_of": sorted(entry["target_of"]),
        }

    return _sorted_dict(
        {
            "routes": per_route,
            "route_health_summary": {
                "enabled": enabled_count,
                "disabled": disabled_count,
                "total": enabled_count + disabled_count,
            },
            "adapter_route_map": sorted_adapter_map,
        }
    )


# ---------------------------------------------------------------------------
# Deterministic sorting helper
# ---------------------------------------------------------------------------


def _sorted_dict(d: dict[str, Any]) -> dict[str, Any]:
    """Return a new dict with keys sorted recursively."""
    result: dict[str, Any] = {}
    for key in sorted(d):
        val = d[key]
        if isinstance(val, dict):
            result[key] = _sorted_dict(val)
        elif isinstance(val, list):
            result[key] = [_sorted_dict(v) if isinstance(v, dict) else v for v in val]
        else:
            result[key] = val
    return result
