"""Runtime diagnostic snapshot for deterministic introspection.

Provides a pure function :func:`capture_runtime_snapshot` that aggregates
existing runtime state into a JSON-safe, deterministic dictionary.  The
snapshot makes the current system behaviour **visible** without adding
new infrastructure.

The snapshot contains:

* **Adapters** – registered adapter health via
  :func:`~medre.core.runtime.health.normalize_adapter_health`.
* **Renderer registry / platform registry** – from
  :class:`~medre.core.rendering.renderer.RenderingPipeline.status_summary`.
* **Storage / replay backend status** – placeholder summaries.
* **Event bus status** – from
  :class:`~medre.core.events.bus.EventBus.status_summary`.
* **Queue / backpressure / task placeholders** – sentinel-only
  ``{"status": "not_yet_implemented"}`` dicts (no real infrastructure).

Public symbols
--------------
* :class:`RuntimeSnapshot` – frozen snapshot with :meth:`to_dict`.
* :func:`capture_runtime_snapshot` – pure function that builds a snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from medre.core.runtime.health import normalize_adapter_health


# ---------------------------------------------------------------------------
# Sentinel for unimplemented subsystems
# ---------------------------------------------------------------------------

_NOT_YET_IMPLEMENTED: dict[str, str] = {"status": "not_yet_implemented"}
"""Deterministic placeholder for subsystems that are not yet built."""


# ---------------------------------------------------------------------------
# Adapter health input protocol (structural typing)
# ---------------------------------------------------------------------------


class _AdapterHealthInput:
    """Minimal structural type accepted for adapter health entries.

    Attributes
    ----------
    info:
        An :class:`~medre.adapters.base.AdapterInfo` instance.
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
    queue_status:
        Placeholder (not yet implemented).
    backpressure_status:
        Placeholder (not yet implemented).
    task_status:
        Placeholder (not yet implemented).
    """

    adapters: tuple[dict[str, Any], ...]
    renderer_registry: dict[str, Any]
    event_bus_status: dict[str, Any]
    storage_backend_status: dict[str, Any]
    replay_backend_status: dict[str, Any]
    queue_status: dict[str, str] = field(
        default_factory=lambda: dict(_NOT_YET_IMPLEMENTED),
    )
    backpressure_status: dict[str, str] = field(
        default_factory=lambda: dict(_NOT_YET_IMPLEMENTED),
    )
    task_status: dict[str, str] = field(
        default_factory=lambda: dict(_NOT_YET_IMPLEMENTED),
    )

    def to_dict(self) -> dict[str, Any]:
        """Return a deterministic, JSON-safe dictionary.

        All list and sub-dict outputs are sorted by key for stable
        serialisation with ``json.dumps(sort_keys=True)``.
        """
        return _sorted_dict({
            "adapters": [self._normalised_adapter(a) for a in self.adapters],
            "renderer_registry": _sorted_dict(self.renderer_registry),
            "event_bus_status": _sorted_dict(self.event_bus_status),
            "storage_backend_status": _sorted_dict(self.storage_backend_status),
            "replay_backend_status": _sorted_dict(self.replay_backend_status),
            "queue_status": _sorted_dict(self.queue_status),
            "backpressure_status": _sorted_dict(self.backpressure_status),
            "task_status": _sorted_dict(self.task_status),
        })

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
        :func:`~medre.core.runtime.health.normalize_adapter_health`.
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
        renderer_summary = dict(_NOT_YET_IMPLEMENTED)

    # -- Event bus status ---------------------------------------------------
    if event_bus is not None and hasattr(event_bus, "status_summary"):
        bus_summary = event_bus.status_summary()
    else:
        bus_summary = dict(_NOT_YET_IMPLEMENTED)

    # -- Storage / replay placeholders --------------------------------------
    storage_summary = storage_status if storage_status is not None else dict(_NOT_YET_IMPLEMENTED)
    replay_summary = replay_status if replay_status is not None else dict(_NOT_YET_IMPLEMENTED)

    return RuntimeSnapshot(
        adapters=tuple(adapter_entries),
        renderer_registry=renderer_summary,
        event_bus_status=bus_summary,
        storage_backend_status=storage_summary,
        replay_backend_status=replay_summary,
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
