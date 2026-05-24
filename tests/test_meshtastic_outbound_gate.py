"""Tests for Meshtastic outbound listen-only gate (Tranche 4).

Verifies:
- Default outbound_mode="enabled" passes normal queue/enqueue path.
- outbound_mode="listen_only" suppresses before queue enqueue.
- Diagnostics expose outbound_mode and outbound_gate_suppressed counter.
- Invalid outbound_mode values are rejected by config validation.
- Env override MEDRE_ADAPTER__<TOKEN>__OUTBOUND_MODE=listen_only works.
- Queue full remains transient when gate disabled (existing behaviour).
- Gate active prevents queue-full path (suppressed before queue).
- Evidence/detail classification for suppression.
- Fake adapter mirrors the gate.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest

from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
from medre.config.adapters.errors import MeshtasticConfigError
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.contracts.adapter import (
    AdapterContext,
    AdapterPermanentError,
    AdapterSendError,
)
from medre.core.events.bus import EventBus
from medre.core.planning.delivery_plan import (
    DeliveryFailureKind,
    RetryExecutor,
)
from medre.core.rendering.renderer import RenderingResult
from medre.runtime.reporting import _derive_failure_kind_detail

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(adapter_id: str = "test-gate") -> AdapterContext:
    return AdapterContext(
        adapter_id=adapter_id,
        event_bus=EventBus(),
        publish_inbound=AsyncMock(),
        logger=logging.getLogger("test"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


def _make_result(event_id: str = "evt-1", text: str = "hello") -> RenderingResult:
    return RenderingResult(
        event_id=event_id,
        target_adapter="test-gate",
        target_channel="0",
        payload={"text": text, "channel_index": 0},
    )


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestOutboundModeConfigValidation:
    """outbound_mode field validation."""

    def test_default_is_enabled(self) -> None:
        config = MeshtasticConfig(adapter_id="test")
        assert config.outbound_mode == "enabled"

    def test_enabled_is_valid(self) -> None:
        config = MeshtasticConfig(adapter_id="test", outbound_mode="enabled")
        assert config.validate() is config
        assert config.outbound_mode == "enabled"

    def test_listen_only_is_valid(self) -> None:
        config = MeshtasticConfig(adapter_id="test", outbound_mode="listen_only")
        assert config.validate() is config
        assert config.outbound_mode == "listen_only"

    def test_invalid_value_raises(self) -> None:
        config = MeshtasticConfig(
            adapter_id="test", outbound_mode="disabled"  # type: ignore[arg-type]
        )
        with pytest.raises(MeshtasticConfigError, match="outbound_mode"):
            config.validate()

    def test_empty_string_raises(self) -> None:
        config = MeshtasticConfig(
            adapter_id="test", outbound_mode=""  # type: ignore[arg-type]
        )
        with pytest.raises(MeshtasticConfigError, match="outbound_mode"):
            config.validate()

    def test_bool_raises(self) -> None:
        config = MeshtasticConfig(
            adapter_id="test", outbound_mode=True  # type: ignore[arg-type]
        )
        with pytest.raises(MeshtasticConfigError, match="outbound_mode"):
            config.validate()

    def test_none_raises(self) -> None:
        config = MeshtasticConfig(
            adapter_id="test", outbound_mode=None  # type: ignore[arg-type]
        )
        with pytest.raises(MeshtasticConfigError, match="outbound_mode"):
            config.validate()

    def test_integer_raises(self) -> None:
        config = MeshtasticConfig(
            adapter_id="test", outbound_mode=1  # type: ignore[arg-type]
        )
        with pytest.raises(MeshtasticConfigError, match="outbound_mode"):
            config.validate()


# ---------------------------------------------------------------------------
# Real adapter: default enabled passes normal path
# ---------------------------------------------------------------------------


class TestDefaultEnabledPassesNormalPath:
    """Default outbound_mode='enabled' enqueues normally."""

    @pytest.mark.asyncio
    async def test_enabled_enqueues_and_returns_delivery_result(self) -> None:
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = MeshtasticConfig(
            adapter_id="test-enabled",
            connection_type="fake",
        )
        adapter = MeshtasticAdapter(config)
        ctx = _make_ctx("test-enabled")
        await adapter.start(ctx)
        try:
            result = _make_result()
            delivery = await adapter.deliver(result)
            assert delivery is not None
            assert delivery.delivery_note == "locally enqueued"
            assert adapter._queue.queue_depth == 1
        finally:
            await adapter.stop()


# ---------------------------------------------------------------------------
# Real adapter: listen_only suppresses before queue
# ---------------------------------------------------------------------------


class TestListenOnlySuppressesBeforeQueue:
    """outbound_mode='listen_only' raises AdapterPermanentError before queue."""

    @pytest.mark.asyncio
    async def test_listen_only_raises_permanent_error(self) -> None:
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = MeshtasticConfig(
            adapter_id="test-listen",
            connection_type="fake",
            outbound_mode="listen_only",
        )
        adapter = MeshtasticAdapter(config)
        ctx = _make_ctx("test-listen")
        await adapter.start(ctx)
        try:
            result = _make_result()
            with pytest.raises(AdapterPermanentError) as exc_info:
                await adapter.deliver(result)
            assert "outbound suppressed: listen_only mode" in str(exc_info.value)
        finally:
            await adapter.stop()

    @pytest.mark.asyncio
    async def test_listen_only_does_not_enqueue(self) -> None:
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = MeshtasticConfig(
            adapter_id="test-no-enqueue",
            connection_type="fake",
            outbound_mode="listen_only",
        )
        adapter = MeshtasticAdapter(config)
        ctx = _make_ctx("test-no-enqueue")
        await adapter.start(ctx)
        try:
            result = _make_result()
            with pytest.raises(AdapterPermanentError):
                await adapter.deliver(result)
            # Queue must remain empty
            assert adapter._queue.queue_depth == 0
        finally:
            await adapter.stop()

    @pytest.mark.asyncio
    async def test_listen_only_suppression_counter_increments(self) -> None:
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = MeshtasticConfig(
            adapter_id="test-counter",
            connection_type="fake",
            outbound_mode="listen_only",
        )
        adapter = MeshtasticAdapter(config)
        ctx = _make_ctx("test-counter")
        await adapter.start(ctx)
        try:
            for i in range(3):
                with pytest.raises(AdapterPermanentError):
                    await adapter.deliver(_make_result(event_id=f"evt-{i}"))
            assert adapter._outbound_gate_suppressed == 3
        finally:
            await adapter.stop()


# ---------------------------------------------------------------------------
# Diagnostics: outbound_mode and counter exposed
# ---------------------------------------------------------------------------


class TestDiagnosticsOutboundGate:
    """Diagnostics expose outbound_mode and outbound_gate_suppressed."""

    @pytest.mark.asyncio
    async def test_diagnostics_include_outbound_mode(self) -> None:
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = MeshtasticConfig(
            adapter_id="test-diag-mode",
            connection_type="fake",
            outbound_mode="listen_only",
        )
        adapter = MeshtasticAdapter(config)
        ctx = _make_ctx("test-diag-mode")
        await adapter.start(ctx)
        try:
            diag = adapter.diagnostics()
            assert diag["outbound_mode"] == "listen_only"
            assert "outbound_gate_suppressed" in diag
        finally:
            await adapter.stop()

    @pytest.mark.asyncio
    async def test_diagnostics_counter_starts_at_zero(self) -> None:
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = MeshtasticConfig(
            adapter_id="test-diag-zero",
            connection_type="fake",
        )
        adapter = MeshtasticAdapter(config)
        ctx = _make_ctx("test-diag-zero")
        await adapter.start(ctx)
        try:
            diag = adapter.diagnostics()
            assert diag["outbound_mode"] == "enabled"
            assert diag["outbound_gate_suppressed"] == 0
        finally:
            await adapter.stop()

    @pytest.mark.asyncio
    async def test_diagnostics_counter_after_suppression(self) -> None:
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = MeshtasticConfig(
            adapter_id="test-diag-count",
            connection_type="fake",
            outbound_mode="listen_only",
        )
        adapter = MeshtasticAdapter(config)
        ctx = _make_ctx("test-diag-count")
        await adapter.start(ctx)
        try:
            for i in range(2):
                with pytest.raises(AdapterPermanentError):
                    await adapter.deliver(_make_result(event_id=f"evt-{i}"))
            diag = adapter.diagnostics()
            assert diag["outbound_gate_suppressed"] == 2
        finally:
            await adapter.stop()


# ---------------------------------------------------------------------------
# Gate active prevents queue-full path
# ---------------------------------------------------------------------------


class TestGatePreventsQueueFull:
    """When listen_only is active, the gate fires before queue capacity check."""

    @pytest.mark.asyncio
    async def test_gate_fires_before_queue_full_check(self) -> None:
        """Even if queue is full, listen_only suppression fires first."""
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = MeshtasticConfig(
            adapter_id="test-prevent-full",
            connection_type="fake",
            outbound_mode="listen_only",
        )
        adapter = MeshtasticAdapter(config)
        ctx = _make_ctx("test-prevent-full")
        await adapter.start(ctx)
        try:
            # Manually fill the queue to capacity.
            max_size = adapter._queue.max_queue_size
            assert max_size is not None
            for i in range(max_size):
                await adapter._queue.enqueue({"text": f"fill-{i}"}, channel_index=0)
            assert adapter._queue.queue_depth == max_size

            # Deliver should still be suppressed by gate, NOT by queue-full.
            with pytest.raises(AdapterPermanentError) as exc_info:
                await adapter.deliver(_make_result())
            assert "outbound suppressed: listen_only mode" in str(exc_info.value)
            # Counter incremented, queue unchanged.
            assert adapter._outbound_gate_suppressed == 1
            assert adapter._queue.queue_depth == max_size
        finally:
            await adapter.stop()


# ---------------------------------------------------------------------------
# Queue full remains transient when gate disabled (enabled)
# ---------------------------------------------------------------------------


class TestQueueFullRemainsTransient:
    """Existing queue-full transient behaviour is preserved when gate is enabled."""

    @pytest.mark.asyncio
    async def test_queue_full_still_transient_when_enabled(self) -> None:
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = MeshtasticConfig(
            adapter_id="test-full-transient",
            connection_type="fake",
            outbound_mode="enabled",
        )
        adapter = MeshtasticAdapter(config)
        ctx = _make_ctx("test-full-transient")
        await adapter.start(ctx)
        try:
            max_size = adapter._queue.max_queue_size
            assert max_size is not None
            for i in range(max_size):
                await adapter.deliver(_make_result(event_id=f"fill-{i}"))

            with pytest.raises(AdapterSendError) as exc_info:
                await adapter.deliver(_make_result(event_id="overflow"))
            assert exc_info.value.transient is True
            assert adapter._outbound_gate_suppressed == 0
        finally:
            await adapter.stop()


# ---------------------------------------------------------------------------
# Failure classification evidence
# ---------------------------------------------------------------------------


class TestSuppressionFailureClassification:
    """Suppression error classifies as adapter_permanent with clear detail."""

    def test_permanent_error_classifies_as_adapter_permanent(self) -> None:
        err = AdapterPermanentError("outbound suppressed: listen_only mode")
        kind = RetryExecutor.classify_failure(err)
        assert kind is DeliveryFailureKind.ADAPTER_PERMANENT
        assert kind.is_retryable is False

    def test_failure_kind_detail_derives_suppressed(self) -> None:
        detail = _derive_failure_kind_detail(
            failure_kind="adapter_permanent",
            error="outbound suppressed: listen_only mode",
            target_adapter="radio-a",
        )
        assert detail == "meshtastic_outbound_suppressed"

    def test_failure_kind_detail_case_insensitive(self) -> None:
        detail = _derive_failure_kind_detail(
            failure_kind="adapter_permanent",
            error="Outbound Suppressed: listen_only Mode",
            target_adapter="radio-a",
        )
        assert detail == "meshtastic_outbound_suppressed"

    def test_failure_kind_detail_no_match_when_no_listen_only(self) -> None:
        detail = _derive_failure_kind_detail(
            failure_kind="adapter_permanent",
            error="some other permanent error",
            target_adapter="radio-a",
        )
        assert detail == "adapter_permanent"

    def test_failure_kind_detail_none_when_kind_none(self) -> None:
        detail = _derive_failure_kind_detail(
            failure_kind=None,
            error="outbound suppressed: listen_only mode",
            target_adapter="radio-a",
        )
        assert detail is None

    def test_existing_queue_rejected_pattern_not_affected(self) -> None:
        """Existing meshtastic_queue_rejected pattern still works."""
        detail = _derive_failure_kind_detail(
            failure_kind="adapter_transient",
            error="queue is full: cannot enqueue",
            target_adapter="radio-a",
        )
        assert detail == "meshtastic_queue_rejected"


# ---------------------------------------------------------------------------
# Fake adapter mirrors gate
# ---------------------------------------------------------------------------


class TestFakeAdapterGateMirror:
    """FakeMeshtasticAdapter mirrors the outbound gate."""

    @pytest.mark.asyncio
    async def test_fake_enabled_delivers_normally(self) -> None:
        config = MeshtasticConfig(adapter_id="fake-enabled")
        adapter = FakeMeshtasticAdapter(config)
        ctx = _make_ctx("fake-enabled")
        await adapter.start(ctx)

        result = _make_result()
        delivery = await adapter.deliver(result)
        assert delivery is not None
        assert delivery.native_message_id is not None
        assert len(adapter.delivered_payloads) == 1

    @pytest.mark.asyncio
    async def test_fake_listen_only_raises_permanent(self) -> None:
        config = MeshtasticConfig(adapter_id="fake-listen", outbound_mode="listen_only")
        adapter = FakeMeshtasticAdapter(config)
        ctx = _make_ctx("fake-listen")
        await adapter.start(ctx)

        result = _make_result()
        with pytest.raises(AdapterPermanentError) as exc_info:
            await adapter.deliver(result)
        assert "outbound suppressed: listen_only mode" in str(exc_info.value)
        assert len(adapter.delivered_payloads) == 0

    @pytest.mark.asyncio
    async def test_fake_listen_only_counter(self) -> None:
        config = MeshtasticConfig(
            adapter_id="fake-counter", outbound_mode="listen_only"
        )
        adapter = FakeMeshtasticAdapter(config)
        ctx = _make_ctx("fake-counter")
        await adapter.start(ctx)

        for i in range(3):
            with pytest.raises(AdapterPermanentError):
                await adapter.deliver(_make_result(event_id=f"evt-{i}"))
        assert adapter._outbound_gate_suppressed == 3

    @pytest.mark.asyncio
    async def test_fake_diagnostics_expose_outbound_mode(self) -> None:
        config = MeshtasticConfig(adapter_id="fake-diag", outbound_mode="listen_only")
        adapter = FakeMeshtasticAdapter(config)
        ctx = _make_ctx("fake-diag")
        await adapter.start(ctx)

        diag = adapter.diagnostics()
        assert diag["outbound_mode"] == "listen_only"
        assert diag["outbound_gate_suppressed"] == 0

    @pytest.mark.asyncio
    async def test_fake_listen_only_beats_deliver_failure(self) -> None:
        """listen_only gate fires before _deliver_failure so it always wins."""
        config = MeshtasticConfig(
            adapter_id="fake-priority", outbound_mode="listen_only"
        )
        adapter = FakeMeshtasticAdapter(config)
        ctx = _make_ctx("fake-priority")
        await adapter.start(ctx)

        # Enable simulated failure — but listen_only should fire first.
        adapter.set_deliver_failure(True)
        result = _make_result()
        with pytest.raises(AdapterPermanentError) as exc_info:
            await adapter.deliver(result)
        assert "outbound suppressed: listen_only mode" in str(exc_info.value)
        # Not the simulated failure error.
        assert "simulated send failure" not in str(exc_info.value)
        assert adapter._outbound_gate_suppressed == 1
        assert len(adapter.delivered_payloads) == 0


# ---------------------------------------------------------------------------
# Env override
# ---------------------------------------------------------------------------


class TestOutboundModeEnvOverride:
    """MEDRE_ADAPTER__<TOKEN>__OUTBOUND_MODE env override."""

    def test_env_override_listen_only(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        from medre.config.env import apply_env_overrides
        from medre.config.loader import load_config

        toml = """\
[runtime]
name = "env-override-test"

[storage]
backend = "memory"

[adapters.meshtastic.radio_a]
connection_type = "fake"

[routes.a_to_b]
source_adapters = ["radio-a"]
dest_adapters = ["radio-b"]
directionality = "source_to_dest"
enabled = true
"""
        config_path = tmp_path / "env_override.toml"
        config_path.write_text(toml)
        config, _source, _paths = load_config(str(config_path))

        # Override via env
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A__OUTBOUND_MODE", "listen_only")
        config = apply_env_overrides(config)

        radio_a = config.adapters.meshtastic["radio_a"]
        assert radio_a is not None
        assert radio_a.config is not None
        assert radio_a.config.outbound_mode == "listen_only"

    def test_env_override_enabled_explicit(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        from medre.config.env import apply_env_overrides
        from medre.config.loader import load_config

        toml = """\
[runtime]
name = "env-override-enabled"

[storage]
backend = "memory"

[adapters.meshtastic.radio_a]
connection_type = "fake"
outbound_mode = "listen_only"

[routes.a_to_b]
source_adapters = ["radio-a"]
dest_adapters = ["radio-b"]
directionality = "source_to_dest"
enabled = true
"""
        config_path = tmp_path / "env_override_enabled.toml"
        config_path.write_text(toml)
        config, _source, _paths = load_config(str(config_path))

        # Override back to enabled via env
        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A__OUTBOUND_MODE", "enabled")
        config = apply_env_overrides(config)

        radio_a = config.adapters.meshtastic["radio_a"]
        assert radio_a is not None
        assert radio_a.config is not None
        assert radio_a.config.outbound_mode == "enabled"

    def test_env_override_invalid_value_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        from medre.config.env import apply_env_overrides
        from medre.config.loader import load_config

        toml = """\
[runtime]
name = "env-override-invalid"

[storage]
backend = "memory"

[adapters.meshtastic.radio_a]
connection_type = "fake"

[routes.a_to_b]
source_adapters = ["radio-a"]
dest_adapters = ["radio-b"]
directionality = "source_to_dest"
enabled = true
"""
        config_path = tmp_path / "env_override_invalid.toml"
        config_path.write_text(toml)
        config, _source, _paths = load_config(str(config_path))

        monkeypatch.setenv("MEDRE_ADAPTER__RADIO_A__OUTBOUND_MODE", "invalid_value")

        with pytest.raises(MeshtasticConfigError, match="outbound_mode"):
            apply_env_overrides(config)
