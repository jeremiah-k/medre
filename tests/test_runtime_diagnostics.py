"""Tests for runtime diagnostic snapshot (Track 1).

Covers:
- Deterministic output across two identical ``to_dict()`` calls.
- JSON serialisability with ``json.dumps(sort_keys=True)``.
- Fake adapter health inclusion via ``normalize_adapter_health``.
- Renderer registry and platform registry inclusion.
- Event bus status summary inclusion.
- Placeholder fields are sentinel-only (no real queue/task infra).
- No secret leakage when context/details include token-like keys.
- Snapshot is frozen (immutable).
- ``capture_runtime_snapshot`` pure function behaviour.
- Status accessors on EventBus and RenderingPipeline.
"""

from __future__ import annotations

import json
import importlib
from typing import Any

import pytest

from medre.core.contracts.adapter import (
    AdapterCapabilities,
    AdapterInfo,
    AdapterRole,
)
from medre.core.events.bus import EventBus
from medre.core.lifecycle.states import AdapterState
from medre.core.rendering.renderer import RenderingPipeline
from medre.core.routing.models import Route, RouteSource, RouteTarget
from medre.core.routing.router import Router

_diagnostics: Any = importlib.import_module("medre.core.runtime.diagnostics")
RuntimeSnapshot = _diagnostics.RuntimeSnapshot
_AdapterHealthInput = _diagnostics._AdapterHealthInput
_NOT_YET_IMPLEMENTED = _diagnostics._NOT_YET_IMPLEMENTED
capture_runtime_snapshot = _diagnostics.capture_runtime_snapshot
capture_route_topology = _diagnostics.capture_route_topology


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_info(
    adapter_id: str = "test-adapter",
    platform: str = "test_platform",
    role: AdapterRole = AdapterRole.TRANSPORT,
    health: str = "healthy",
) -> AdapterInfo:
    return AdapterInfo(
        adapter_id=adapter_id,
        platform=platform,
        role=role,
        version="0.1.0",
        capabilities=AdapterCapabilities(),
        health=health,
    )


def _make_health_input(
    adapter_id: str = "test-adapter",
    platform: str = "test_platform",
    role: AdapterRole = AdapterRole.TRANSPORT,
    health: str = "healthy",
    lifecycle_state: AdapterState | None = None,
    adapter: object | None = None,
    details: dict[str, object] | None = None,
) -> Any:
    info = _make_info(
        adapter_id=adapter_id,
        platform=platform,
        role=role,
        health=health,
    )
    return _AdapterHealthInput(
        info=info,
        lifecycle_state=lifecycle_state,
        adapter=adapter,
        details=details,
    )


# ===================================================================
# Deterministic output
# ===================================================================


class TestDeterminism:
    """Two ``to_dict()`` calls produce identical output."""

    def test_to_dict_is_deterministic(self) -> None:
        entries = [
            _make_health_input(adapter_id="b-adapter", platform="plat_b"),
            _make_health_input(adapter_id="a-adapter", platform="plat_a"),
        ]
        snap = capture_runtime_snapshot(adapter_healths=entries)
        d1 = snap.to_dict()
        d2 = snap.to_dict()
        assert d1 == d2
        assert json.dumps(d1, sort_keys=True) == json.dumps(d2, sort_keys=True)

    def test_adapter_ordering_is_deterministic(self) -> None:
        """Adapters are sorted by adapter_id regardless of input order."""
        entries = [
            _make_health_input(adapter_id="z-adapter"),
            _make_health_input(adapter_id="a-adapter"),
            _make_health_input(adapter_id="m-adapter"),
        ]
        snap = capture_runtime_snapshot(adapter_healths=entries)
        result = snap.to_dict()
        ids = [a["adapter_id"] for a in result["adapters"]]
        assert ids == sorted(ids)


# ===================================================================
# JSON serialisability
# ===================================================================


class TestJsonSerialisable:
    """Snapshot output is safe for ``json.dumps(sort_keys=True)``."""

    def test_json_dumps_succeeds(self) -> None:
        entries = [
            _make_health_input(
                adapter_id="json-test",
                details={"metric": 42, "flag": True},
            ),
        ]
        snap = capture_runtime_snapshot(adapter_healths=entries)
        text = json.dumps(snap.to_dict(), sort_keys=True)
        assert isinstance(text, str)
        parsed = json.loads(text)
        assert parsed["adapters"][0]["adapter_id"] == "json-test"

    def test_all_values_are_json_safe_types(self) -> None:
        entries = [_make_health_input()]
        snap = capture_runtime_snapshot(adapter_healths=entries)
        d = snap.to_dict()
        # Recursively check that no non-JSON-safe types exist
        _assert_json_safe(d)


def _assert_json_safe(obj: object) -> None:
    """Recursively assert that obj contains only JSON-safe types."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert isinstance(k, str), f"Non-string key: {k!r}"
            _assert_json_safe(v)
    elif isinstance(obj, list):
        for item in obj:
            _assert_json_safe(item)
    elif isinstance(obj, (str, int, float, bool)) or obj is None:
        pass
    else:
        raise AssertionError(f"Non-JSON-safe value: {obj!r} (type={type(obj).__name__})")


# ===================================================================
# Fake adapter health inclusion
# ===================================================================


class TestFakeAdapterHealth:
    """Fake adapter health is normalised and included in the snapshot."""

    def test_fake_adapter_health_included(self) -> None:
        class FakeTransport:
            pass

        info = _make_info(
            adapter_id="fake-t",
            platform="fake_transport",
            role=AdapterRole.TRANSPORT,
            health="healthy",
        )
        entry = _AdapterHealthInput(info=info, adapter=FakeTransport())
        snap = capture_runtime_snapshot(adapter_healths=[entry])
        result = snap.to_dict()
        assert len(result["adapters"]) == 1
        adapter = result["adapters"][0]
        assert adapter["adapter_id"] == "fake-t"
        assert adapter["platform"] == "fake_transport"
        assert adapter["role"] == "transport"
        assert adapter["health"] == "healthy"
        assert adapter["fake_or_live"] == "fake"

    def test_multiple_adapters_included(self) -> None:
        entries = [
            _make_health_input(adapter_id="a1", platform="p1"),
            _make_health_input(adapter_id="a2", platform="p2"),
            _make_health_input(adapter_id="a3", platform="p3"),
        ]
        snap = capture_runtime_snapshot(adapter_healths=entries)
        result = snap.to_dict()
        assert len(result["adapters"]) == 3

    def test_no_adapters_produces_empty_list(self) -> None:
        snap = capture_runtime_snapshot()
        result = snap.to_dict()
        assert result["adapters"] == []

    def test_adapter_health_uses_normalize_adapter_health(self) -> None:
        """Verify that unknown health values are normalised to 'unknown'."""
        info = _make_info(adapter_id="unhealthy", health="on-fire")
        entry = _AdapterHealthInput(info=info)
        snap = capture_runtime_snapshot(adapter_healths=[entry])
        result = snap.to_dict()
        assert result["adapters"][0]["health"] == "unknown"
        assert result["adapters"][0]["details"]["adapter_health_raw"] == "on-fire"


# ===================================================================
# Renderer registry / platform registry
# ===================================================================


class TestRendererRegistry:
    """Renderer pipeline status is captured."""

    def test_renderer_pipeline_summary_included(self) -> None:
        pipeline = RenderingPipeline()
        pipeline.register_adapter_platform("radio-a", "meshtastic")
        pipeline.register_adapter_platform("chat-b", "matrix")
        snap = capture_runtime_snapshot(renderer_pipeline=pipeline)
        result = snap.to_dict()
        reg = result["renderer_registry"]
        assert reg["renderer_count"] == 0
        assert reg["renderer_names"] == []
        assert reg["platform_registry"] == {
            "chat-b": "matrix",
            "radio-a": "meshtastic",
        }

    def test_no_renderer_pipeline_gives_placeholder(self) -> None:
        snap = capture_runtime_snapshot()
        result = snap.to_dict()
        assert result["renderer_registry"] == {"status": "not_yet_implemented"}


# ===================================================================
# Event bus status
# ===================================================================


class TestEventBusStatus:
    """Event bus status is captured."""

    def test_event_bus_summary_included(self) -> None:
        bus = EventBus()
        snap = capture_runtime_snapshot(event_bus=bus)
        result = snap.to_dict()
        assert result["event_bus_status"]["subscription_count"] == 0
        assert result["event_bus_status"]["middleware_count"] == 0

    def test_event_bus_with_subscriptions(self) -> None:
        bus = EventBus()
        bus.subscribe("message", lambda e: None)
        bus.subscribe("message.created", lambda e: None)
        snap = capture_runtime_snapshot(event_bus=bus)
        result = snap.to_dict()
        assert result["event_bus_status"]["subscription_count"] == 2

    def test_no_event_bus_gives_placeholder(self) -> None:
        snap = capture_runtime_snapshot()
        result = snap.to_dict()
        assert result["event_bus_status"] == {"status": "not_yet_implemented"}


# ===================================================================
# Placeholder fields
# ===================================================================


class TestPlaceholders:
    """Queue, backpressure, and task fields are sentinel-only."""

    def test_queue_status_placeholder(self) -> None:
        snap = capture_runtime_snapshot()
        result = snap.to_dict()
        assert result["queue_status"] == {"status": "not_yet_implemented"}

    def test_backpressure_status_placeholder(self) -> None:
        snap = capture_runtime_snapshot()
        result = snap.to_dict()
        assert result["backpressure_status"] == {"status": "not_yet_implemented"}

    def test_task_status_placeholder(self) -> None:
        snap = capture_runtime_snapshot()
        result = snap.to_dict()
        assert result["task_status"] == {"status": "not_yet_implemented"}

    def test_placeholders_are_plain_dicts(self) -> None:
        snap = capture_runtime_snapshot()
        result = snap.to_dict()
        for key in ("queue_status", "backpressure_status", "task_status"):
            val = result[key]
            assert isinstance(val, dict)
            assert set(val.keys()) == {"status"}

    def test_storage_backend_placeholder_when_not_provided(self) -> None:
        snap = capture_runtime_snapshot()
        result = snap.to_dict()
        assert result["storage_backend_status"] == {"status": "not_yet_implemented"}

    def test_replay_backend_placeholder_when_not_provided(self) -> None:
        snap = capture_runtime_snapshot()
        result = snap.to_dict()
        assert result["replay_backend_status"] == {"status": "not_yet_implemented"}

    def test_custom_storage_status(self) -> None:
        snap = capture_runtime_snapshot(
            storage_status={"backend": "sqlite", "initialized": True},
        )
        result = snap.to_dict()
        assert result["storage_backend_status"]["backend"] == "sqlite"
        assert result["storage_backend_status"]["initialized"] is True

    def test_custom_replay_status(self) -> None:
        snap = capture_runtime_snapshot(
            replay_status={"mode": "strict", "events_processed": 0},
        )
        result = snap.to_dict()
        assert result["replay_backend_status"]["mode"] == "strict"


# ===================================================================
# Secret leakage prevention
# ===================================================================


class TestNoSecretLeakage:
    """Snapshots must not include tokens or config secrets."""

    def test_token_like_keys_in_details_not_at_top_level(self) -> None:
        info = _make_info(adapter_id="secret-test")
        entry = _AdapterHealthInput(
            info=info,
            details={
                "access_token": "syt_super_secret_value",
                "api_key": "sk_live_abc123",
                "password": "hunter2",
                "connection_url": "https://user:pass@host",
                "normal_metric": 42,
            },
        )
        snap = capture_runtime_snapshot(adapter_healths=[entry])
        result = snap.to_dict()

        # Sensitive data should be in details, NOT at top level
        adapter = result["adapters"][0]
        assert "access_token" not in adapter or "access_token" in adapter.get("details", {})
        assert "access_token" in adapter["details"]
        assert adapter["details"]["access_token"] == "syt_super_secret_value"

        # Top-level keys must be the fixed set
        assert set(adapter.keys()) == {
            "adapter_id", "platform", "role", "health",
            "fake_or_live", "capabilities", "details",
        }

    def test_snapshot_top_level_has_no_secrets(self) -> None:
        """The top-level snapshot dict has no token-like keys."""
        info = _make_info()
        entry = _AdapterHealthInput(
            info=info,
            details={"token": "secret123"},
        )
        snap = capture_runtime_snapshot(adapter_healths=[entry])
        result = snap.to_dict()
        top_keys = set(result.keys())
        # None of these should be present at snapshot top level
        for secret_key in ("token", "access_token", "api_key", "password"):
            assert secret_key not in top_keys


# ===================================================================
# Immutability
# ===================================================================


class TestImmutability:
    """RuntimeSnapshot is frozen (immutable)."""

    def test_frozen_dataclass(self) -> None:
        snap = capture_runtime_snapshot()
        with pytest.raises(AttributeError):
            snap.adapters = ()  # type: ignore[misc]

    def test_tuple_adapters_immutable(self) -> None:
        snap = capture_runtime_snapshot(
            adapter_healths=[_make_health_input()],
        )
        assert isinstance(snap.adapters, tuple)


# ===================================================================
# Snapshot top-level structure
# ===================================================================


class TestSnapshotStructure:
    """to_dict() produces the required top-level keys."""

    def test_required_top_level_keys(self) -> None:
        snap = capture_runtime_snapshot()
        result = snap.to_dict()
        expected_keys = {
            "adapters",
            "renderer_registry",
            "event_bus_status",
            "storage_backend_status",
            "replay_backend_status",
            "route_topology",
            "queue_status",
            "backpressure_status",
            "task_status",
        }
        assert set(result.keys()) == expected_keys

    def test_to_dict_returns_new_object(self) -> None:
        """to_dict() returns a new dict each call (no shared mutability)."""
        snap = capture_runtime_snapshot()
        d1 = snap.to_dict()
        d2 = snap.to_dict()
        assert d1 is not d2
        assert d1 == d2


# ===================================================================
# EventBus status_summary accessor
# ===================================================================


class TestEventBusStatusSummary:
    """EventBus.status_summary() returns read-only state."""

    def test_returns_dict(self) -> None:
        bus = EventBus()
        summary = bus.status_summary()
        assert isinstance(summary, dict)

    def test_keys(self) -> None:
        bus = EventBus()
        summary = bus.status_summary()
        assert "subscription_count" in summary
        assert "middleware_count" in summary

    def test_reflects_subscriptions(self) -> None:
        bus = EventBus()
        bus.subscribe("test", lambda e: None)
        summary = bus.status_summary()
        assert summary["subscription_count"] == 1


# ===================================================================
# RenderingPipeline status_summary accessor
# ===================================================================


class TestRenderingPipelineStatusSummary:
    """RenderingPipeline.status_summary() returns read-only state."""

    def test_returns_dict(self) -> None:
        pipeline = RenderingPipeline()
        summary = getattr(pipeline, "status_summary")()
        assert isinstance(summary, dict)

    def test_keys(self) -> None:
        pipeline = RenderingPipeline()
        summary = getattr(pipeline, "status_summary")()
        assert "renderer_count" in summary
        assert "renderer_names" in summary
        assert "platform_registry" in summary

    def test_platform_registry_sorted(self) -> None:
        pipeline = RenderingPipeline()
        pipeline.register_adapter_platform("z-radio", "meshtastic")
        pipeline.register_adapter_platform("a-chat", "matrix")
        summary = getattr(pipeline, "status_summary")()
        keys = list(summary["platform_registry"].keys())
        assert keys == sorted(keys)


# ===================================================================
# _AdapterHealthInput
# ===================================================================


class TestAdapterHealthInput:
    """_AdapterHealthInput stores references without copying."""

    def test_stores_info(self) -> None:
        info = _make_info()
        entry = _AdapterHealthInput(info=info)
        assert entry.info is info
        assert entry.lifecycle_state is None
        assert entry.adapter is None
        assert entry.details is None

    def test_stores_all_fields(self) -> None:
        info = _make_info()
        state = AdapterState.INITIALIZING
        details = {"latency_ms": 100}

        class _FakeAdapter:
            pass

        adapter = _FakeAdapter()
        entry = _AdapterHealthInput(
            info=info,
            lifecycle_state=state,
            adapter=adapter,
            details=details,
        )
        assert entry.info is info
        assert entry.lifecycle_state is state
        assert entry.adapter is adapter
        assert entry.details is details


# ===================================================================
# End-to-end: capture with all subsystems
# ===================================================================


class TestEndToEndCapture:
    """Full capture with adapters, bus, renderer, and placeholders."""

    def test_full_snapshot(self) -> None:
        bus = EventBus()
        bus.subscribe("message", lambda e: None)

        pipeline = RenderingPipeline()
        pipeline.register_adapter_platform("radio-1", "meshtastic")
        pipeline.register_adapter_platform("chat-1", "matrix")

        entries = [
            _make_health_input(
                adapter_id="chat-1",
                platform="matrix",
                role=AdapterRole.PRESENTATION,
                health="healthy",
            ),
            _make_health_input(
                adapter_id="radio-1",
                platform="meshtastic",
                role=AdapterRole.TRANSPORT,
                health="degraded",
            ),
        ]

        snap = capture_runtime_snapshot(
            adapter_healths=entries,
            renderer_pipeline=pipeline,
            event_bus=bus,
            storage_status={"backend": "sqlite", "wal_mode": True},
        )
        result = snap.to_dict()

        # Adapters sorted by id
        assert result["adapters"][0]["adapter_id"] == "chat-1"
        assert result["adapters"][1]["adapter_id"] == "radio-1"
        assert result["adapters"][0]["health"] == "healthy"
        assert result["adapters"][1]["health"] == "degraded"

        # Renderer registry
        assert result["renderer_registry"]["renderer_count"] == 0
        assert result["renderer_registry"]["platform_registry"] == {
            "chat-1": "matrix",
            "radio-1": "meshtastic",
        }

        # Event bus
        assert result["event_bus_status"]["subscription_count"] == 1

        # Storage
        assert result["storage_backend_status"]["backend"] == "sqlite"

        # Placeholders
        assert result["queue_status"] == {"status": "not_yet_implemented"}
        assert result["backpressure_status"] == {"status": "not_yet_implemented"}
        assert result["task_status"] == {"status": "not_yet_implemented"}
        assert result["replay_backend_status"] == {"status": "not_yet_implemented"}

        # JSON round-trip
        text = json.dumps(result, sort_keys=True)
        parsed = json.loads(text)
        assert parsed == result


# ===================================================================
# Route topology — capture_route_topology
# ===================================================================


def _make_route(
    route_id: str = "r1",
    source_adapter: str | None = None,
    event_kinds: tuple[str, ...] = (),
    source_channel: str | None = None,
    target_adapter: str | None = "discord",
    target_channel: str | None = "general",
    enabled: bool = True,
    ownership: str = "shared",
    fanout_strategy: str = "broadcast",
) -> Route:
    """Build a Route with sensible defaults for testing."""
    return Route(
        id=route_id,
        source=RouteSource(
            adapter=source_adapter,
            event_kinds=event_kinds,
            channel=source_channel,
        ),
        targets=[
            RouteTarget(adapter=target_adapter, channel=target_channel),
        ],
        enabled=enabled,
        ownership=ownership,
        fanout_strategy=fanout_strategy,
    )


class TestCaptureRouteTopologyBasic:
    """Basic topology capture from a Router."""

    def test_empty_router(self) -> None:
        router = Router()
        topo = capture_route_topology(router)
        assert topo["routes"] == []
        assert topo["route_health_summary"] == {
            "enabled": 0,
            "disabled": 0,
            "total": 0,
        }
        assert topo["adapter_route_map"] == {}

    def test_single_enabled_route(self) -> None:
        route = _make_route(
            route_id="r1",
            source_adapter="matrix",
            event_kinds=("message.text",),
            target_adapter="discord",
        )
        router = Router(routes=[route])
        topo = capture_route_topology(router)

        assert len(topo["routes"]) == 1
        r = topo["routes"][0]
        assert r["route_id"] == "r1"
        assert r["enabled"] is True
        assert r["ownership"] == "shared"
        assert r["fanout_strategy"] == "broadcast"
        assert r["source"]["adapter"] == "matrix"
        assert r["source"]["event_kinds"] == ["message.text"]
        assert r["source"]["channel"] is None
        assert r["target_count"] == 1
        assert r["target_adapters"] == ["discord"]
        assert r["error_count"] == 0
        assert r["event_count"] == 0
        assert r["delivered"] == 0
        assert r["failed"] == 0
        assert r["skipped"] == 0
        assert r["loop_prevented"] == 0

    def test_disabled_route_counted(self) -> None:
        route = _make_route(route_id="r-disabled", enabled=False)
        router = Router(routes=[route])
        topo = capture_route_topology(router)

        assert topo["route_health_summary"]["enabled"] == 0
        assert topo["route_health_summary"]["disabled"] == 1
        assert topo["route_health_summary"]["total"] == 1
        assert topo["routes"][0]["enabled"] is False

    def test_routes_sorted_by_id(self) -> None:
        routes = [
            _make_route(route_id="z-route"),
            _make_route(route_id="a-route"),
            _make_route(route_id="m-route"),
        ]
        router = Router(routes=routes)
        topo = capture_route_topology(router)
        ids = [r["route_id"] for r in topo["routes"]]
        assert ids == sorted(ids)

    def test_multiple_routes_mixed_enabled(self) -> None:
        routes = [
            _make_route(route_id="r1", enabled=True),
            _make_route(route_id="r2", enabled=False),
            _make_route(route_id="r3", enabled=True),
        ]
        router = Router(routes=routes)
        topo = capture_route_topology(router)

        assert topo["route_health_summary"]["enabled"] == 2
        assert topo["route_health_summary"]["disabled"] == 1
        assert topo["route_health_summary"]["total"] == 3


class TestRouteTopologyAdapterMap:
    """Adapter-route relationship mapping."""

    def test_source_adapter_mapped(self) -> None:
        route = _make_route(
            route_id="r1",
            source_adapter="matrix",
            target_adapter="discord",
        )
        router = Router(routes=[route])
        topo = capture_route_topology(router)

        assert "matrix" in topo["adapter_route_map"]
        assert topo["adapter_route_map"]["matrix"]["source_of"] == ["r1"]
        assert topo["adapter_route_map"]["matrix"]["target_of"] == []

    def test_target_adapter_mapped(self) -> None:
        route = _make_route(
            route_id="r1",
            source_adapter="matrix",
            target_adapter="discord",
        )
        router = Router(routes=[route])
        topo = capture_route_topology(router)

        assert "discord" in topo["adapter_route_map"]
        assert topo["adapter_route_map"]["discord"]["target_of"] == ["r1"]
        assert topo["adapter_route_map"]["discord"]["source_of"] == []

    def test_adapter_both_source_and_target(self) -> None:
        route = _make_route(
            route_id="r1",
            source_adapter="matrix",
            target_adapter="matrix",
        )
        router = Router(routes=[route])
        topo = capture_route_topology(router)

        arm = topo["adapter_route_map"]["matrix"]
        assert arm["source_of"] == ["r1"]
        assert arm["target_of"] == ["r1"]

    def test_multiple_routes_same_adapter(self) -> None:
        routes = [
            _make_route(route_id="r1", source_adapter="matrix", target_adapter="discord"),
            _make_route(route_id="r2", source_adapter="matrix", target_adapter="meshtastic"),
        ]
        router = Router(routes=routes)
        topo = capture_route_topology(router)

        arm = topo["adapter_route_map"]["matrix"]
        assert sorted(arm["source_of"]) == ["r1", "r2"]

    def test_adapter_map_sorted(self) -> None:
        routes = [
            _make_route(route_id="r1", source_adapter="zebra", target_adapter="alpha"),
        ]
        router = Router(routes=routes)
        topo = capture_route_topology(router)
        keys = list(topo["adapter_route_map"].keys())
        assert keys == sorted(keys)

    def test_null_adapter_not_in_map(self) -> None:
        """Routes with None source/target adapter are not in adapter map."""
        route = _make_route(
            route_id="r1",
            source_adapter=None,
            target_adapter=None,
        )
        router = Router(routes=[route])
        topo = capture_route_topology(router)
        assert topo["adapter_route_map"] == {}


class TestRouteTopologyTargets:
    """Per-route target details in topology snapshot."""

    def test_target_adapters_sorted(self) -> None:
        route = Route(
            id="r1",
            source=RouteSource(adapter=None, event_kinds=(), channel=None),
            targets=[
                RouteTarget(adapter="zebra", channel="ch1"),
                RouteTarget(adapter="alpha", channel="ch2"),
            ],
        )
        router = Router(routes=[route])
        topo = capture_route_topology(router)

        assert topo["routes"][0]["target_adapters"] == ["alpha", "zebra"]

    def test_target_dicts_included(self) -> None:
        route = _make_route(
            route_id="r1",
            target_adapter="discord",
            target_channel="general",
        )
        router = Router(routes=[route])
        topo = capture_route_topology(router)

        targets = topo["routes"][0]["targets"]
        assert len(targets) == 1
        assert targets[0]["adapter"] == "discord"
        assert targets[0]["channel"] == "general"

    def test_empty_event_kinds(self) -> None:
        route = _make_route(route_id="r1", event_kinds=())
        router = Router(routes=[route])
        topo = capture_route_topology(router)
        assert topo["routes"][0]["source"]["event_kinds"] == []


class TestRouteTopologyDeterminism:
    """Topology output is deterministic and JSON-safe."""

    def test_deterministic_across_calls(self) -> None:
        routes = [
            _make_route(route_id="b-route", source_adapter="m2", target_adapter="d2"),
            _make_route(route_id="a-route", source_adapter="m1", target_adapter="d1"),
        ]
        router = Router(routes=routes)
        topo1 = capture_route_topology(router)
        topo2 = capture_route_topology(router)
        assert topo1 == topo2

    def test_json_serialisable(self) -> None:
        route = _make_route(
            route_id="r1",
            source_adapter="matrix",
            event_kinds=("message.text",),
            target_adapter="discord",
        )
        router = Router(routes=[route])
        topo = capture_route_topology(router)
        text = json.dumps(topo, sort_keys=True)
        parsed = json.loads(text)
        assert parsed == topo

    def test_all_values_json_safe(self) -> None:
        route = _make_route(route_id="r1", source_adapter="m", target_adapter="d")
        router = Router(routes=[route])
        topo = capture_route_topology(router)
        _assert_json_safe(topo)

    def test_no_raw_sdk_objects(self) -> None:
        """Snapshot contains no Route/RouteSource/RouteTarget objects."""
        route = _make_route(route_id="r1", source_adapter="m", target_adapter="d")
        router = Router(routes=[route])
        topo = capture_route_topology(router)
        text = json.dumps(topo)
        assert "RouteSource" not in text
        assert "RouteTarget" not in text
        assert "<medre" not in text


class TestRouteTopologyViaRuntimeSnapshot:
    """Route topology integration with capture_runtime_snapshot."""

    def test_no_router_gives_placeholder(self) -> None:
        snap = capture_runtime_snapshot()
        result = snap.to_dict()
        assert result["route_topology"] == {"status": "not_yet_implemented"}

    def test_with_router_includes_topology(self) -> None:
        route = _make_route(
            route_id="r1",
            source_adapter="matrix",
            target_adapter="discord",
        )
        router = Router(routes=[route])
        snap = capture_runtime_snapshot(router=router)
        result = snap.to_dict()

        assert "route_topology" in result
        assert len(result["route_topology"]["routes"]) == 1
        assert result["route_topology"]["routes"][0]["route_id"] == "r1"

    def test_full_snapshot_includes_route_topology(self) -> None:
        bus = EventBus()
        pipeline = RenderingPipeline()
        route = _make_route(route_id="r1")
        router = Router(routes=[route])

        snap = capture_runtime_snapshot(
            adapter_healths=[_make_health_input()],
            renderer_pipeline=pipeline,
            event_bus=bus,
            router=router,
        )
        result = snap.to_dict()

        assert "route_topology" in result
        assert result["route_topology"]["route_health_summary"]["total"] == 1
        assert result["route_topology"]["routes"][0]["route_id"] == "r1"

    def test_zeroed_counters_present(self) -> None:
        """Per-route error_count and event_count are zeroed placeholders."""
        route = _make_route(route_id="r1")
        router = Router(routes=[route])
        topo = capture_route_topology(router)

        r = topo["routes"][0]
        assert r["error_count"] == 0
        assert r["event_count"] == 0
        assert r["delivered"] == 0
        assert r["failed"] == 0
        assert r["skipped"] == 0
        assert r["loop_prevented"] == 0
        assert "last_error" not in r


# ===================================================================
# Route topology with live RouteStats
# ===================================================================


class TestCaptureRouteTopologyWithStats:
    """Route topology enriched with live RouteStats counters."""

    def _make_stats(self) -> Any:
        from medre.core.routing.stats import RouteStats

        return RouteStats()

    def test_enriched_counters_with_route_stats(self) -> None:
        route = _make_route(route_id="r1")
        router = Router(routes=[route])
        stats = self._make_stats()
        stats.record_delivered("r1")
        stats.record_delivered("r1")
        stats.record_failed("r1", "timeout")

        topo = capture_route_topology(router, route_stats=stats)
        r = topo["routes"][0]

        assert r["delivered"] == 2
        assert r["failed"] == 1
        assert r["skipped"] == 0
        assert r["loop_prevented"] == 0
        assert r["error_count"] == 1
        assert r["event_count"] == 2
        assert r["last_error"] == "timeout"

    def test_no_route_stats_gives_zeroed_counters(self) -> None:
        route = _make_route(route_id="r1")
        router = Router(routes=[route])
        topo = capture_route_topology(router, route_stats=None)

        r = topo["routes"][0]
        assert r["delivered"] == 0
        assert r["failed"] == 0
        assert r["skipped"] == 0
        assert r["loop_prevented"] == 0
        assert "last_error" not in r

    def test_unknown_route_in_stats_ignored(self) -> None:
        """Stats for a route not in the router are silently ignored."""
        route = _make_route(route_id="r1")
        router = Router(routes=[route])
        stats = self._make_stats()
        stats.record_delivered("ghost-route")

        topo = capture_route_topology(router, route_stats=stats)
        r = topo["routes"][0]

        assert r["delivered"] == 0
        assert len(topo["routes"]) == 1

    def test_multiple_routes_with_mixed_stats(self) -> None:
        routes = [
            _make_route(route_id="r1", source_adapter="m", target_adapter="d"),
            _make_route(route_id="r2", source_adapter="m", target_adapter="d2"),
        ]
        router = Router(routes=routes)
        stats = self._make_stats()
        stats.record_delivered("r1")
        stats.record_loop_prevented("r2")
        stats.record_skipped("r2")

        topo = capture_route_topology(router, route_stats=stats)
        r1 = topo["routes"][0]
        r2 = topo["routes"][1]

        assert r1["delivered"] == 1
        assert r1["failed"] == 0
        assert r1["loop_prevented"] == 0
        assert "last_error" not in r1

        assert r2["delivered"] == 0
        assert r2["loop_prevented"] == 1
        assert r2["skipped"] == 1

    def test_deterministic_with_route_stats(self) -> None:
        route = _make_route(route_id="r1")
        router = Router(routes=[route])
        stats = self._make_stats()
        stats.record_delivered("r1")
        stats.record_failed("r1", "err")

        topo1 = capture_route_topology(router, route_stats=stats)
        topo2 = capture_route_topology(router, route_stats=stats)
        assert topo1 == topo2

    def test_json_safe_with_route_stats(self) -> None:
        route = _make_route(route_id="r1")
        router = Router(routes=[route])
        stats = self._make_stats()
        stats.record_delivered("r1")
        stats.record_failed("r1", "timeout")

        topo = capture_route_topology(router, route_stats=stats)
        text = json.dumps(topo, sort_keys=True)
        parsed = json.loads(text)
        assert parsed == topo

    def test_last_error_absent_when_no_failure(self) -> None:
        """last_error key is absent when route has no failures."""
        route = _make_route(route_id="r1")
        router = Router(routes=[route])
        stats = self._make_stats()
        stats.record_delivered("r1")

        topo = capture_route_topology(router, route_stats=stats)
        assert "last_error" not in topo["routes"][0]


class TestRuntimeSnapshotWithRouteStats:
    """capture_runtime_snapshot passes route_stats through."""

    def _make_stats(self) -> Any:
        from medre.core.routing.stats import RouteStats

        return RouteStats()

    def test_snapshot_with_router_and_route_stats(self) -> None:
        route = _make_route(route_id="r1")
        router = Router(routes=[route])
        stats = self._make_stats()
        stats.record_delivered("r1")
        stats.record_failed("r1", "err")

        snap = capture_runtime_snapshot(router=router, route_stats=stats)
        result = snap.to_dict()
        r = result["route_topology"]["routes"][0]

        assert r["delivered"] == 1
        assert r["failed"] == 1
        assert r["last_error"] == "err"

    def test_snapshot_with_router_no_stats_zeroed(self) -> None:
        route = _make_route(route_id="r1")
        router = Router(routes=[route])

        snap = capture_runtime_snapshot(router=router)
        result = snap.to_dict()
        r = result["route_topology"]["routes"][0]

        assert r["delivered"] == 0
        assert r["failed"] == 0
        assert "last_error" not in r

    def test_snapshot_deterministic_with_route_stats(self) -> None:
        route = _make_route(route_id="r1")
        router = Router(routes=[route])
        stats = self._make_stats()
        stats.record_delivered("r1")

        snap = capture_runtime_snapshot(router=router, route_stats=stats)
        d1 = snap.to_dict()
        d2 = snap.to_dict()
        assert d1 == d2
