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

from medre.adapters.base import (
    AdapterCapabilities,
    AdapterInfo,
    AdapterRole,
)
from medre.core.events.bus import EventBus
from medre.core.lifecycle.states import AdapterState
from medre.core.rendering.renderer import RenderingPipeline

_diagnostics: Any = importlib.import_module("medre.core.runtime.diagnostics")
RuntimeSnapshot = _diagnostics.RuntimeSnapshot
_AdapterHealthInput = _diagnostics._AdapterHealthInput
_NOT_YET_IMPLEMENTED = _diagnostics._NOT_YET_IMPLEMENTED
capture_runtime_snapshot = _diagnostics.capture_runtime_snapshot


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
