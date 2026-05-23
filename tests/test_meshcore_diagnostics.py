"""Focused tests for MeshCore aggregate classifier diagnostics/counters.

Tests cover:
- Initial zero counters on fresh adapters
- Text relay increments seen/relayed/inbound_published
- ACK packets increment seen/ignored only; unknown/malformed increment seen/deferred only
- Fake adapter counter parity with real adapter
- Diagnostics primitive shape (all values are int)
- DM relay increments relayed and preserves is_direct_message metadata
"""

from __future__ import annotations

from typing import Any, Literal

from medre.adapters.fake_meshcore import FakeMeshCoreAdapter
from medre.adapters.meshcore.adapter import MeshCoreAdapter
from medre.config.adapters.meshcore import MeshCoreConfig


def _make_config(
    *,
    connection_type: Literal["fake", "tcp", "serial", "ble"] = "fake",
) -> MeshCoreConfig:
    return MeshCoreConfig(
        adapter_id="meshcore-diag-test",
        connection_type=connection_type,
    )


def _channel_text_packet(
    text: str = "hello",
    sender: str = "abc123",
    channel: int = 0,
    timestamp: int = 42,
) -> dict[str, Any]:
    return {
        "text": text,
        "pubkey_prefix": sender,
        "sender_timestamp": timestamp,
        "channel_idx": channel,
        "type": "CHAN",
        "txt_type": 0,
    }


def _dm_text_packet(
    text: str = "dm hello",
    sender: str = "def456",
    timestamp: int = 99,
) -> dict[str, Any]:
    return {
        "text": text,
        "pubkey_prefix": sender,
        "sender_timestamp": timestamp,
        "type": "PRIV",
        "txt_type": 0,
    }


def _ack_packet(
    code: int = 0, sender: str = "xyz", timestamp: int = 10
) -> dict[str, Any]:
    return {
        "code": code,
        "pubkey_prefix": sender,
        "sender_timestamp": timestamp,
    }


def _empty_packet() -> dict[str, Any]:
    return {}


def _unknown_packet(sender: str = "unk", timestamp: int = 5) -> dict[str, Any]:
    return {
        "pubkey_prefix": sender,
        "sender_timestamp": timestamp,
        "type": "MYSTERY",
    }


def _assert_counter_keys_zero(diag: dict[str, Any]) -> None:
    """Assert all classifier counter keys are present and zero."""
    assert diag["classifier_packets_seen"] == 0
    assert diag["classifier_packets_relayed"] == 0
    assert diag["classifier_packets_ignored"] == 0
    assert diag["classifier_packets_dropped"] == 0
    assert diag["classifier_packets_deferred"] == 0
    assert diag["inbound_published"] == 0


def _assert_counter_primitives(diag: dict[str, Any]) -> None:
    """Assert all classifier counter values are ints."""
    for key in (
        "classifier_packets_seen",
        "classifier_packets_relayed",
        "classifier_packets_ignored",
        "classifier_packets_dropped",
        "classifier_packets_deferred",
        "inbound_published",
    ):
        assert key in diag, f"Missing key {key!r} in diagnostics"
        assert isinstance(
            diag[key], int
        ), f"{key!r} is {type(diag[key]).__name__}, expected int"


# ===================================================================
# Initial zero counters
# ===================================================================


class TestInitialZeroCounters:
    """Fresh adapters start with all counters at zero."""

    def test_real_adapter_initial_counters_zero(self) -> None:
        config = _make_config()
        adapter = MeshCoreAdapter(config)
        diag = adapter.diagnostics()
        _assert_counter_keys_zero(diag)

    def test_fake_adapter_initial_counters_zero(self) -> None:
        adapter = FakeMeshCoreAdapter()
        diag = adapter.diagnostics()
        _assert_counter_keys_zero(diag)


# ===================================================================
# Text relay increments
# ===================================================================


class TestTextRelayCounters:
    """Channel text packets increment seen/relayed/inbound_published."""

    async def test_fake_channel_text_increments_seen_relayed_published(
        self, make_adapter_context
    ) -> None:
        adapter = FakeMeshCoreAdapter()
        ctx = make_adapter_context("mc-diag")
        await adapter.start(ctx)

        packet = _channel_text_packet(text="hello world")
        await adapter.simulate_inbound(packet)

        diag = adapter.diagnostics()
        assert diag["classifier_packets_seen"] == 1
        assert diag["classifier_packets_relayed"] == 1
        assert diag["classifier_packets_ignored"] == 0
        assert diag["classifier_packets_dropped"] == 0
        assert diag["classifier_packets_deferred"] == 0
        assert diag["inbound_published"] == 1

    async def test_fake_multiple_text_packets_accumulate(
        self, make_adapter_context
    ) -> None:
        adapter = FakeMeshCoreAdapter()
        ctx = make_adapter_context("mc-diag")
        await adapter.start(ctx)

        for i in range(5):
            await adapter.simulate_inbound(_channel_text_packet(text=f"msg-{i}"))

        diag = adapter.diagnostics()
        assert diag["classifier_packets_seen"] == 5
        assert diag["classifier_packets_relayed"] == 5
        assert diag["inbound_published"] == 5

    async def test_real_simulate_inbound_channel_text_increments(
        self, make_adapter_context
    ) -> None:
        config = _make_config(connection_type="fake")
        adapter = MeshCoreAdapter(config)
        ctx = make_adapter_context("mc-diag")
        await adapter.start(ctx)

        packet = _channel_text_packet(text="real test")
        await adapter.simulate_inbound(packet)

        diag = adapter.diagnostics()
        assert diag["classifier_packets_seen"] == 1
        assert diag["classifier_packets_relayed"] == 1
        assert diag["inbound_published"] == 1


# ===================================================================
# ACK / unknown / empty — ignored only
# ===================================================================


class TestIgnoredPackets:
    """ACK packets increment seen/ignored; unknown/malformed increment seen/deferred."""

    async def test_fake_ack_increments_seen_ignored_only(
        self, make_adapter_context
    ) -> None:
        adapter = FakeMeshCoreAdapter()
        ctx = make_adapter_context("mc-diag")
        await adapter.start(ctx)

        await adapter.simulate_inbound(_ack_packet())

        diag = adapter.diagnostics()
        assert diag["classifier_packets_seen"] == 1
        assert diag["classifier_packets_ignored"] == 1
        assert diag["classifier_packets_relayed"] == 0
        assert diag["inbound_published"] == 0

    async def test_fake_empty_packet_increments_seen_deferred_only(
        self, make_adapter_context
    ) -> None:
        adapter = FakeMeshCoreAdapter()
        ctx = make_adapter_context("mc-diag")
        await adapter.start(ctx)

        await adapter.simulate_inbound(_empty_packet())

        diag = adapter.diagnostics()
        assert diag["classifier_packets_seen"] == 1
        assert diag["classifier_packets_deferred"] == 1
        assert diag["classifier_packets_relayed"] == 0
        assert diag["inbound_published"] == 0

    async def test_fake_unknown_packet_increments_seen_deferred_only(
        self, make_adapter_context
    ) -> None:
        adapter = FakeMeshCoreAdapter()
        ctx = make_adapter_context("mc-diag")
        await adapter.start(ctx)

        await adapter.simulate_inbound(_unknown_packet())

        diag = adapter.diagnostics()
        assert diag["classifier_packets_seen"] == 1
        assert diag["classifier_packets_deferred"] == 1
        assert diag["classifier_packets_relayed"] == 0
        assert diag["inbound_published"] == 0

    async def test_real_ack_increments_seen_ignored_only(
        self, make_adapter_context
    ) -> None:
        config = _make_config(connection_type="fake")
        adapter = MeshCoreAdapter(config)
        ctx = make_adapter_context("mc-diag")
        await adapter.start(ctx)

        await adapter.simulate_inbound(_ack_packet())

        diag = adapter.diagnostics()
        assert diag["classifier_packets_seen"] == 1
        assert diag["classifier_packets_ignored"] == 1
        assert diag["classifier_packets_relayed"] == 0
        assert diag["inbound_published"] == 0

    async def test_mixed_packets_accumulate_correctly(
        self, make_adapter_context
    ) -> None:
        adapter = FakeMeshCoreAdapter()
        ctx = make_adapter_context("mc-diag")
        await adapter.start(ctx)

        # 2 text → relay, 1 ack → ignore, 1 unknown → deferred
        await adapter.simulate_inbound(_channel_text_packet(text="t1"))
        await adapter.simulate_inbound(_ack_packet())
        await adapter.simulate_inbound(_unknown_packet())
        await adapter.simulate_inbound(_channel_text_packet(text="t2"))

        diag = adapter.diagnostics()
        assert diag["classifier_packets_seen"] == 4
        assert diag["classifier_packets_relayed"] == 2
        assert diag["classifier_packets_ignored"] == 1
        assert diag["classifier_packets_deferred"] == 1
        assert diag["inbound_published"] == 2


# ===================================================================
# Fake adapter parity
# ===================================================================


class TestFakeAdapterParity:
    """Fake adapter diagnostics shape matches real adapter."""

    def test_both_adapters_have_same_counter_keys(self) -> None:
        real_config = _make_config()
        real_diag = MeshCoreAdapter(real_config).diagnostics()
        fake_diag = FakeMeshCoreAdapter().diagnostics()

        counter_keys = [
            "classifier_packets_seen",
            "classifier_packets_relayed",
            "classifier_packets_ignored",
            "classifier_packets_dropped",
            "classifier_packets_deferred",
            "inbound_published",
        ]
        for key in counter_keys:
            assert key in real_diag, f"Real adapter missing {key!r}"
            assert key in fake_diag, f"Fake adapter missing {key!r}"

    async def test_fake_simulate_inbound_parity_with_real(
        self, make_adapter_context
    ) -> None:
        """Same packet produces same counter values on both adapters."""
        config = _make_config(connection_type="fake")
        real_adapter = MeshCoreAdapter(config)
        fake_adapter = FakeMeshCoreAdapter()
        ctx = make_adapter_context("mc-diag")
        await real_adapter.start(ctx)
        await fake_adapter.start(ctx)

        packet = _channel_text_packet(text="parity test")
        await real_adapter.simulate_inbound(packet)
        await fake_adapter.simulate_inbound(packet)

        real_diag = real_adapter.diagnostics()
        fake_diag = fake_adapter.diagnostics()

        assert (
            real_diag["classifier_packets_seen"] == fake_diag["classifier_packets_seen"]
        )
        assert (
            real_diag["classifier_packets_relayed"]
            == fake_diag["classifier_packets_relayed"]
        )
        assert real_diag["inbound_published"] == fake_diag["inbound_published"]


# ===================================================================
# Diagnostics primitive shape
# ===================================================================


class TestDiagnosticsPrimitiveShape:
    """All counter values are int primitives (JSON-safe)."""

    def test_real_adapter_counters_are_int(self) -> None:
        config = _make_config()
        adapter = MeshCoreAdapter(config)
        _assert_counter_primitives(adapter.diagnostics())

    def test_fake_adapter_counters_are_int(self) -> None:
        adapter = FakeMeshCoreAdapter()
        _assert_counter_primitives(adapter.diagnostics())

    async def test_real_adapter_after_start_counters_are_int(
        self, make_adapter_context
    ) -> None:
        config = _make_config(connection_type="fake")
        adapter = MeshCoreAdapter(config)
        ctx = make_adapter_context("mc-diag")
        await adapter.start(ctx)
        await adapter.simulate_inbound(_channel_text_packet())
        _assert_counter_primitives(adapter.diagnostics())

    async def test_fake_adapter_after_simulate_counters_are_int(
        self, make_adapter_context
    ) -> None:
        adapter = FakeMeshCoreAdapter()
        ctx = make_adapter_context("mc-diag")
        await adapter.start(ctx)
        await adapter.simulate_inbound(_channel_text_packet())
        _assert_counter_primitives(adapter.diagnostics())


# ===================================================================
# DM relay preserves metadata
# ===================================================================


class TestDMRelayCounters:
    """DM (PRIV) text packets increment relayed and preserve is_direct_message."""

    async def test_fake_dm_increments_relayed_and_published(
        self, make_adapter_context
    ) -> None:
        adapter = FakeMeshCoreAdapter()
        ctx = make_adapter_context("mc-diag")
        await adapter.start(ctx)

        packet = _dm_text_packet(text="private hello")
        await adapter.simulate_inbound(packet)

        diag = adapter.diagnostics()
        assert diag["classifier_packets_seen"] == 1
        assert diag["classifier_packets_relayed"] == 1
        assert diag["inbound_published"] == 1
        assert diag["classifier_packets_ignored"] == 0

    async def test_fake_dm_preserves_is_direct_message_metadata(
        self, make_adapter_context
    ) -> None:
        adapter = FakeMeshCoreAdapter()
        ctx = make_adapter_context("mc-diag")
        await adapter.start(ctx)

        packet = _dm_text_packet(text="dm meta")
        await adapter.simulate_inbound(packet)

        assert len(adapter.inbound_events) == 1
        event = adapter.inbound_events[0]
        # is_direct_message is stored in native metadata
        assert event.metadata.native is not None
        native_data = event.metadata.native.data
        assert native_data.get("meshcore.is_direct_message") is True

    async def test_real_dm_increments_relayed_and_published(
        self, make_adapter_context
    ) -> None:
        config = _make_config(connection_type="fake")
        adapter = MeshCoreAdapter(config)
        ctx = make_adapter_context("mc-diag")
        await adapter.start(ctx)

        packet = _dm_text_packet(text="real dm")
        await adapter.simulate_inbound(packet)

        diag = adapter.diagnostics()
        assert diag["classifier_packets_seen"] == 1
        assert diag["classifier_packets_relayed"] == 1
        assert diag["inbound_published"] == 1

    async def test_channel_text_is_not_marked_direct(
        self, make_adapter_context
    ) -> None:
        adapter = FakeMeshCoreAdapter()
        ctx = make_adapter_context("mc-diag")
        await adapter.start(ctx)

        packet = _channel_text_packet(text="channel msg")
        await adapter.simulate_inbound(packet)

        assert len(adapter.inbound_events) == 1
        event = adapter.inbound_events[0]
        assert event.metadata.native is not None
        native_data = event.metadata.native.data
        assert native_data.get("meshcore.is_direct_message") is False


# ===================================================================
# Counter reset on restart
# ===================================================================


class TestCounterResetOnRestart:
    """Calling start() on a reused adapter resets all counters to zero."""

    async def test_fake_adapter_resets_counters_on_restart(
        self, make_adapter_context
    ) -> None:
        adapter = FakeMeshCoreAdapter()
        ctx = make_adapter_context("mc-diag")
        await adapter.start(ctx)

        # Accumulate some counters.
        await adapter.simulate_inbound(_channel_text_packet(text="msg1"))
        await adapter.simulate_inbound(_channel_text_packet(text="msg2"))
        await adapter.simulate_inbound(_ack_packet())

        diag = adapter.diagnostics()
        assert diag["classifier_packets_seen"] == 3
        assert diag["classifier_packets_relayed"] == 2
        assert diag["inbound_published"] == 2

        # Stop and restart — counters must reset to zero.
        await adapter.stop()
        await adapter.start(ctx)

        _assert_counter_keys_zero(adapter.diagnostics())

    async def test_real_adapter_resets_counters_on_restart(
        self, make_adapter_context
    ) -> None:
        config = _make_config(connection_type="fake")
        adapter = MeshCoreAdapter(config)
        ctx = make_adapter_context("mc-diag")
        await adapter.start(ctx)

        # Accumulate some counters.
        await adapter.simulate_inbound(_channel_text_packet(text="msg1"))
        await adapter.simulate_inbound(_ack_packet())

        diag = adapter.diagnostics()
        assert diag["classifier_packets_seen"] == 2
        assert diag["classifier_packets_relayed"] == 1
        assert diag["inbound_published"] == 1

        # Stop and restart — counters must reset to zero.
        await adapter.stop()
        await adapter.start(ctx)

        _assert_counter_keys_zero(adapter.diagnostics())

    async def test_fake_adapter_counters_accumulate_after_restart(
        self, make_adapter_context
    ) -> None:
        """After restart, counters accumulate from zero correctly."""
        adapter = FakeMeshCoreAdapter()
        ctx = make_adapter_context("mc-diag")
        await adapter.start(ctx)
        await adapter.simulate_inbound(_channel_text_packet(text="before"))
        await adapter.stop()

        await adapter.start(ctx)
        await adapter.simulate_inbound(_channel_text_packet(text="after"))

        diag = adapter.diagnostics()
        assert diag["classifier_packets_seen"] == 1
        assert diag["classifier_packets_relayed"] == 1
        assert diag["inbound_published"] == 1
