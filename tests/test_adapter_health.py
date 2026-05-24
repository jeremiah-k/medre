"""Tests for adapter health normalization (Track 2).

Covers:
- VALID_HEALTH_STRINGS contains the required six values.
- normalize_adapter_health produces the required top-level keys with
  correct types.
- All normalized health values are valid vocabulary members.
- Fake and Matrix adapters return AdapterInfo from health_check().
- Protocol-specific details do not leak into core top-level fields.
- Lifecycle transitional states override adapter self-reported health.
- Fake/live mode detection works from class name, config, and platform
  heuristics.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import pytest

from medre.adapters.fakes.lxmf import FakeLxmfAdapter
from medre.adapters.fakes.matrix import FakeMatrixAdapter
from medre.adapters.fakes.meshcore import FakeMeshCoreAdapter
from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.adapters.fakes.transport import FakeTransportAdapter
from medre.core.contracts.adapter import (
    AdapterCapabilities,
    AdapterContext,
    AdapterInfo,
    AdapterRole,
)
from medre.core.lifecycle.states import AdapterState
from medre.core.supervision.health import (
    VALID_HEALTH_STRINGS,
    normalize_adapter_health,
)

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


def _make_context(adapter_id: str = "test") -> AdapterContext:
    return AdapterContext(
        adapter_id=adapter_id,
        event_bus=None,
        publish_inbound=_async_noop,
        logger=logging.getLogger(f"test.{adapter_id}"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


async def _async_noop(event: object) -> None:
    pass


# ===================================================================
# Health vocabulary
# ===================================================================


class TestHealthVocabulary:
    """VALID_HEALTH_STRINGS covers the required six health strings."""

    def test_contains_all_six_required_strings(self) -> None:
        expected = {"healthy", "degraded", "failed", "unknown", "starting", "stopping"}
        assert VALID_HEALTH_STRINGS == expected

    def test_is_frozenset(self) -> None:
        assert isinstance(VALID_HEALTH_STRINGS, frozenset)


# ===================================================================
# normalize_adapter_health structure
# ===================================================================


class TestNormalizeStructure:
    """normalize_adapter_health produces the required key structure."""

    def test_required_top_level_keys(self) -> None:
        info = _make_info()
        result = normalize_adapter_health(info)
        assert set(result.keys()) == {
            "adapter_id",
            "platform",
            "role",
            "health",
            "fake_or_live",
            "capabilities",
            "details",
        }

    def test_adapter_id_from_info(self) -> None:
        info = _make_info(adapter_id="my-adapter")
        result = normalize_adapter_health(info)
        assert result["adapter_id"] == "my-adapter"

    def test_platform_from_info(self) -> None:
        info = _make_info(platform="meshtastic")
        result = normalize_adapter_health(info)
        assert result["platform"] == "meshtastic"

    def test_role_serialized_as_string(self) -> None:
        info = _make_info(role=AdapterRole.PRESENTATION)
        result = normalize_adapter_health(info)
        assert result["role"] == "presentation"
        assert isinstance(result["role"], str)

    def test_health_is_valid_string(self) -> None:
        for h in ("healthy", "degraded", "failed", "unknown"):
            info = _make_info(health=h)
            result = normalize_adapter_health(info)
            assert result["health"] == h
            assert result["health"] in VALID_HEALTH_STRINGS

    def test_details_is_dict(self) -> None:
        info = _make_info()
        result = normalize_adapter_health(info)
        assert isinstance(result["details"], dict)

    def test_details_contains_version(self) -> None:
        info = _make_info()
        result = normalize_adapter_health(info)
        assert result["details"]["version"] == "0.1.0"

    def test_details_contains_raw_adapter_health(self) -> None:
        info = _make_info(health="degraded")
        result = normalize_adapter_health(info)
        assert result["details"]["adapter_health_raw"] == "degraded"


# ===================================================================
# Unknown / invalid health normalization
# ===================================================================


class TestUnknownHealth:
    """Invalid or unrecognised health strings normalise to 'unknown'."""

    def test_empty_string_becomes_unknown(self) -> None:
        info = _make_info(health="")
        result = normalize_adapter_health(info)
        assert result["health"] == "unknown"

    def test_unrecognised_string_becomes_unknown(self) -> None:
        info = _make_info(health="on-fire")
        result = normalize_adapter_health(info)
        assert result["health"] == "unknown"

    def test_raw_health_preserved_in_details(self) -> None:
        info = _make_info(health="on-fire")
        result = normalize_adapter_health(info)
        assert result["details"]["adapter_health_raw"] == "on-fire"
        assert result["health"] == "unknown"


# ===================================================================
# Lifecycle state integration
# ===================================================================


class TestLifecycleStateOverride:
    """Lifecycle transitional states override adapter self-reported health."""

    def test_initializing_maps_to_starting(self) -> None:
        info = _make_info(health="unknown")
        result = normalize_adapter_health(
            info,
            lifecycle_state=AdapterState.INITIALIZING,
        )
        assert result["health"] == "starting"

    def test_stopping_maps_to_stopping(self) -> None:
        info = _make_info(health="healthy")
        result = normalize_adapter_health(
            info,
            lifecycle_state=AdapterState.STOPPING,
        )
        assert result["health"] == "stopping"

    def test_ready_does_not_override_adapter_health(self) -> None:
        """Non-transitional states let adapter self-report through."""
        info = _make_info(health="degraded")
        result = normalize_adapter_health(
            info,
            lifecycle_state=AdapterState.READY,
        )
        assert result["health"] == "degraded"

    def test_failed_does_not_override_adapter_health(self) -> None:
        info = _make_info(health="unknown")
        result = normalize_adapter_health(
            info,
            lifecycle_state=AdapterState.FAILED,
        )
        # FAILED is not transitional, so adapter self-report is used
        assert result["health"] == "unknown"

    def test_lifecycle_state_raw_in_details(self) -> None:
        info = _make_info()
        result = normalize_adapter_health(
            info,
            lifecycle_state=AdapterState.INITIALIZING,
        )
        assert result["details"]["lifecycle_state_raw"] == "initializing"

    def test_no_lifecycle_state_omits_raw(self) -> None:
        info = _make_info()
        result = normalize_adapter_health(info)
        assert "lifecycle_state_raw" not in result["details"]


# ===================================================================
# Fake / live mode detection
# ===================================================================


class TestFakeLiveDetection:
    """Fake/live mode is inferred from class name, config, and platform."""

    def test_fake_class_name_detected(self) -> None:
        adapter = FakeTransportAdapter("test")
        info = _make_info(platform="fake_transport")
        result = normalize_adapter_health(info, adapter=adapter)
        assert result["fake_or_live"] == "fake"

    def test_fake_matrix_detected(self) -> None:
        adapter = FakeMatrixAdapter("test")
        info = _make_info(platform="matrix")
        result = normalize_adapter_health(info, adapter=adapter)
        assert result["fake_or_live"] == "fake"

    def test_faulty_class_name_detected(self) -> None:
        from medre.adapters.fakes.presentation import FaultyPresentationAdapter

        adapter = FaultyPresentationAdapter("test")
        info = _make_info(platform="faulty_presentation")
        result = normalize_adapter_health(info, adapter=adapter)
        assert result["fake_or_live"] == "fake"

    def test_config_connection_type_fake(self) -> None:
        """Real adapter with config.connection_type='fake' detected as fake."""

        class _FakeConfig:
            connection_type = "fake"

        class _RealishAdapter:
            _config = _FakeConfig()

        adapter = _RealishAdapter()
        info = _make_info(platform="meshtastic")
        result = normalize_adapter_health(info, adapter=adapter)
        assert result["fake_or_live"] == "fake"

    def test_config_connection_type_tcp_is_live(self) -> None:
        """Real adapter with config.connection_type='tcp' detected as live."""

        class _TcpConfig:
            connection_type = "tcp"

        class _TcpAdapter:
            _config = _TcpConfig()

        adapter = _TcpAdapter()
        info = _make_info(platform="meshtastic")
        result = normalize_adapter_health(info, adapter=adapter)
        assert result["fake_or_live"] == "live"

    def test_platform_fake_prefix_heuristic(self) -> None:
        """Platform starting with 'fake_' detected as fake without adapter."""
        info = _make_info(platform="fake_meshcore")
        result = normalize_adapter_health(info)
        assert result["fake_or_live"] == "fake"

    def test_no_signal_defaults_unknown(self) -> None:
        """No adapter, non-fake platform → conservative 'unknown'."""
        info = _make_info(platform="meshtastic")
        result = normalize_adapter_health(info)
        assert result["fake_or_live"] == "unknown"


# ===================================================================
# Protocol-specific details isolation
# ===================================================================


class TestDetailsIsolation:
    """Protocol-specific details stay in the 'details' dict."""

    def test_extra_details_in_details_dict(self) -> None:
        info = _make_info()
        result = normalize_adapter_health(
            info,
            details={
                "matrix_sync_latency_ms": 150,
                "rooms_joined": 3,
            },
        )
        assert result["details"]["matrix_sync_latency_ms"] == 150
        assert result["details"]["rooms_joined"] == 3

    def test_details_do_not_leak_to_top_level(self) -> None:
        info = _make_info()
        result = normalize_adapter_health(
            info,
            details={"native_event_id": "$abc123"},
        )
        top_keys = set(result.keys())
        assert "native_event_id" not in top_keys
        assert "native_event_id" in result["details"]

    def test_top_level_keys_are_fixed(self) -> None:
        """Top-level key set is always the same seven keys."""
        expected_keys = {
            "adapter_id",
            "platform",
            "role",
            "health",
            "fake_or_live",
            "capabilities",
            "details",
        }
        for health_val in ("healthy", "failed", "unknown"):
            info = _make_info(health=health_val)
            result = normalize_adapter_health(
                info,
                details={"extra_protocol_field": True},
            )
            assert set(result.keys()) == expected_keys


# ===================================================================
# AdapterInfo contract compliance
# ===================================================================


class TestAdapterInfoContract:
    """All adapters return AdapterInfo from health_check()."""

    @pytest.mark.parametrize(
        "adapter_cls,adapter_kwargs",
        [
            (
                FakeTransportAdapter,
                {"adapter_id": "ft"},
            ),
            (
                FakePresentationAdapter,
                {"adapter_id": "fp"},
            ),
            (
                FakeMatrixAdapter,
                {"adapter_id": "fm"},
            ),
            (
                FakeMeshtasticAdapter,
                {},
            ),
            (
                FakeMeshCoreAdapter,
                {},
            ),
            (
                FakeLxmfAdapter,
                {},
            ),
        ],
    )
    async def test_fake_adapter_returns_adapter_info(
        self,
        make_adapter_context,
        adapter_cls,
        adapter_kwargs,
    ) -> None:
        adapter = adapter_cls(**adapter_kwargs)
        ctx = make_adapter_context(adapter.adapter_id)
        await adapter.start(ctx)
        info = await adapter.health_check()
        assert isinstance(info, AdapterInfo)
        assert isinstance(info.health, str)
        assert info.health in VALID_HEALTH_STRINGS
        assert info.adapter_id == adapter.adapter_id
        assert isinstance(info.role, AdapterRole)
        await adapter.stop()

    async def test_fake_transport_health_before_start(self) -> None:
        adapter = FakeTransportAdapter("pre_start")
        info = await adapter.health_check()
        assert isinstance(info, AdapterInfo)
        assert info.health == "unknown"

    async def test_fake_transport_health_after_start(
        self,
        make_adapter_context,
    ) -> None:
        adapter = FakeTransportAdapter("post_start")
        ctx = make_adapter_context("post_start")
        await adapter.start(ctx)
        info = await adapter.health_check()
        assert info.health == "healthy"

    async def test_faulty_adapter_returns_adapter_info(
        self,
        make_adapter_context,
    ) -> None:
        from medre.adapters.fakes.presentation import FaultyPresentationAdapter

        adapter = FaultyPresentationAdapter(
            adapter_id="faulty",
            failure_mode="succeed",
        )
        ctx = make_adapter_context("faulty")
        await adapter.start(ctx)
        info = await adapter.health_check()
        assert isinstance(info, AdapterInfo)
        assert info.health == "healthy"


# ===================================================================
# End-to-end normalization with real fake adapters
# ===================================================================


class TestEndToEndNormalization:
    """Full normalize_adapter_health call with real fake adapters."""

    async def test_normalize_fake_transport(
        self,
        make_adapter_context,
    ) -> None:
        adapter = FakeTransportAdapter("norm_t")
        ctx = make_adapter_context("norm_t")
        await adapter.start(ctx)
        info = await adapter.health_check()
        result = normalize_adapter_health(info, adapter=adapter)
        assert result["adapter_id"] == "norm_t"
        assert result["platform"] == "fake_transport"
        assert result["role"] == "transport"
        assert result["health"] == "healthy"
        assert result["fake_or_live"] == "fake"
        assert isinstance(result["details"], dict)

    async def test_normalize_fake_matrix(
        self,
        make_adapter_context,
    ) -> None:
        adapter = FakeMatrixAdapter("norm_m")
        ctx = make_adapter_context("norm_m")
        await adapter.start(ctx)
        info = await adapter.health_check()
        result = normalize_adapter_health(info, adapter=adapter)
        assert result["adapter_id"] == "norm_m"
        assert result["platform"] == "matrix"
        assert result["role"] == "presentation"
        assert result["health"] == "healthy"
        assert result["fake_or_live"] == "fake"

    async def test_normalize_with_lifecycle_starting(
        self,
        make_adapter_context,
    ) -> None:
        adapter = FakeTransportAdapter("lifecycle_t")
        # Don't start — adapter reports "unknown"
        info = await adapter.health_check()
        result = normalize_adapter_health(
            info,
            adapter=adapter,
            lifecycle_state=AdapterState.INITIALIZING,
        )
        assert result["health"] == "starting"
        assert result["details"]["lifecycle_state_raw"] == "initializing"

    async def test_normalize_with_lifecycle_stopping(
        self,
        make_adapter_context,
    ) -> None:
        adapter = FakeTransportAdapter("lifecycle_s")
        ctx = make_adapter_context("lifecycle_s")
        await adapter.start(ctx)
        info = await adapter.health_check()
        result = normalize_adapter_health(
            info,
            adapter=adapter,
            lifecycle_state=AdapterState.STOPPING,
        )
        assert result["health"] == "stopping"

    async def test_normalize_preserves_capabilities_in_details(
        self,
        make_adapter_context,
    ) -> None:
        adapter = FakeTransportAdapter("caps_t")
        ctx = make_adapter_context("caps_t")
        await adapter.start(ctx)
        info = await adapter.health_check()
        result = normalize_adapter_health(
            info,
            adapter=adapter,
            details={"has_text": info.capabilities.text},
        )
        assert result["details"]["has_text"] is True
        # Capabilities did not leak to top level
        assert "has_text" not in result.keys()
