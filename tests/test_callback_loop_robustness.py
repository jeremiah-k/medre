"""Callback loop robustness tests.

Proves that malformed inbound payloads, callback exceptions, and
duplicate/native-IDs are isolated per callback and do not poison future
callbacks across all four transport adapters.

Each test exercises a specific failure mode and then verifies that a
subsequent valid callback processes correctly — the adapter continues
operating after the fault.

No Docker, no live transports, no SDK dependencies required.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from medre.adapters.lxmf.adapter import LxmfAdapter
from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.meshcore.adapter import MeshCoreAdapter
from medre.adapters.meshtastic.adapter import MeshtasticAdapter
from medre.config.adapters.lxmf import LxmfConfig
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.contracts.adapter import AdapterContext
from tests.helpers.bridge import make_meshcore_packet, make_text_packet
from tests.helpers.matrix import make_matrix_config, make_nio_event, make_nio_room

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _InboundCollector:
    """Collects published inbound CanonicalEvent instances."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def __call__(self, event: Any) -> None:
        self.events.append(event)


def _make_ctx(adapter_id: str, collector: _InboundCollector) -> AdapterContext:
    """Build an AdapterContext wired to the collector."""
    return AdapterContext(
        adapter_id=adapter_id,
        event_bus=None,
        publish_inbound=collector,
        logger=logging.getLogger(f"test.callback_robustness.{adapter_id}"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


async def _await_background_tasks(adapter: Any, timeout: float = 5.0) -> None:
    """Wait for tracked background tasks to finish without cancelling them."""
    if adapter._background_tasks:
        await asyncio.wait_for(
            asyncio.gather(*list(adapter._background_tasks), return_exceptions=True),
            timeout=timeout,
        )


def _make_lxmf_packet(
    content: str = "hello lxmf",
    source_hash: str = "ab" * 16,
    msg_id: str | None = None,
) -> dict[str, Any]:
    """Minimal valid LXMF text packet."""
    return {
        "source_hash": source_hash,
        "destination_hash": "00" * 16,
        "message_id": msg_id or ("cd" * 32),
        "timestamp": 1700000000.0,
        "title": "",
        "content": content,
        "fields": {},
        "signature_validated": True,
        "has_fields": False,
        "delivery_method": "direct",
    }


# ---------------------------------------------------------------------------
# Test 1 — Matrix: _on_room_message exception isolation
# ---------------------------------------------------------------------------


class TestMatrixCallbackIsolation:
    """Prove MatrixAdapter._on_room_message isolates per-callback faults."""

    async def test_one_bad_on_room_message_does_not_poison_matrix(
        self,
    ) -> None:
        """A malformed event (missing .source) is caught by the try/except;
        the next valid event is processed normally."""
        config = make_matrix_config(adapter_id="mx-robust")
        adapter = MatrixAdapter(config)
        collector = _InboundCollector()
        adapter.ctx = _make_ctx("mx-robust", collector)

        room = make_nio_room("!robust_room:example.com")

        # --- BAD: event without .source → codec raises MatrixCodecError ---
        bad_event = SimpleNamespace(
            sender="@intruder:example.com",
            event_id="$bad-001",
            body="boom",
            # no .source attribute → decode() raises
        )
        # Should NOT raise — the try/except in _on_room_message catches it
        await adapter._on_room_message(room, bad_event)
        assert len(collector.events) == 0, "Bad event should not publish"

        # --- GOOD: valid event ---
        good_event = make_nio_event(
            sender="@alice:example.com",
            event_id="$good-001",
            body="after the storm",
        )
        await adapter._on_room_message(room, good_event)
        assert len(collector.events) == 1, "Valid event should publish"
        assert collector.events[0].payload.get("body") == "after the storm"

    async def test_bad_then_good_room_message_preserves_adapter_state(
        self,
    ) -> None:
        """Adapter diagnostic counters remain consistent after a bad event."""
        config = make_matrix_config(adapter_id="mx-state")
        adapter = MatrixAdapter(config)
        collector = _InboundCollector()
        adapter.ctx = _make_ctx("mx-state", collector)

        room = make_nio_room("!state_room:example.com")

        bad_event = SimpleNamespace(sender="@x:example.com", event_id="$b1", body="x")
        await adapter._on_room_message(room, bad_event)

        good_event = make_nio_event(sender="@y:example.com", event_id="$g1", body="ok")
        await adapter._on_room_message(room, good_event)

        # Published counter reflects only the successful callback
        assert adapter._inbound_published == 1

    async def test_multiple_bad_events_do_not_accumulate_state_leaks(
        self,
    ) -> None:
        """Three consecutive bad events then one good — only the good one
        increments the published counter."""
        config = make_matrix_config(adapter_id="mx-leak")
        adapter = MatrixAdapter(config)
        collector = _InboundCollector()
        adapter.ctx = _make_ctx("mx-leak", collector)

        room = make_nio_room("!leak_room:example.com")

        for i in range(3):
            bad = SimpleNamespace(
                sender=f"@bad{i}:example.com",
                event_id=f"$bad-{i}",
                body=f"fail {i}",
            )
            await adapter._on_room_message(room, bad)

        good = make_nio_event(
            sender="@ok:example.com", event_id="$good-final", body="survivor"
        )
        await adapter._on_room_message(room, good)

        assert adapter._inbound_published == 1
        assert len(collector.events) == 1
        assert collector.events[0].payload.get("body") == "survivor"


# ---------------------------------------------------------------------------
# Test 2 — Meshtastic: simulate_inbound exception isolation
# ---------------------------------------------------------------------------


class TestMeshtasticCallbackIsolation:
    """Prove MeshtasticAdapter callback and simulate_inbound fault isolation."""

    async def test_one_bad_simulate_inbound_does_not_poison_meshtastic(
        self,
    ) -> None:
        """A malformed packet raises from simulate_inbound; the next valid
        packet is processed normally."""
        adapter = MeshtasticAdapter(
            MeshtasticConfig(adapter_id="mesh-robust", connection_type="fake")
        )
        collector = _InboundCollector()
        ctx = _make_ctx("mesh-robust", collector)

        # Must start to set .ctx (simulate_inbound raises RuntimeError without)
        await adapter.start(ctx)

        try:
            # --- BAD: non-dict packet → MeshtasticCodecError ---
            with pytest.raises(Exception):
                await adapter.simulate_inbound("not a dict")  # type: ignore[arg-type]

            assert len(collector.events) == 0

            # --- GOOD: valid text packet ---
            valid = make_text_packet(text="after bad mesh", sender="!node-ok")
            await adapter.simulate_inbound(valid)

            assert len(collector.events) == 1
            assert collector.events[0].payload.get("body") == "after bad mesh"
        finally:
            await adapter.stop()

    async def test_one_bad_on_packet_does_not_poison_meshtastic(
        self,
    ) -> None:
        """The sync _on_packet callback catches exceptions internally; a
        subsequent valid packet publishes successfully."""
        adapter = MeshtasticAdapter(
            MeshtasticConfig(adapter_id="mesh-sync", connection_type="fake")
        )
        collector = _InboundCollector()
        ctx = _make_ctx("mesh-sync", collector)
        await adapter.start(ctx)

        try:
            # --- BAD: None packet → AttributeError caught by try/except ---
            adapter._on_packet(None)  # type: ignore[arg-type]  # should not raise

            # --- GOOD: valid packet → submitted via run_coroutine_threadsafe ---
            valid = make_text_packet(text="sync recovery", sender="!sync-node")
            adapter._on_packet(valid)

            # Yield to let the coroutine submitted via run_coroutine_threadsafe
            # execute on the event loop.
            await asyncio.sleep(0.1)

            assert len(collector.events) == 1
            assert collector.events[0].payload.get("body") == "sync recovery"
        finally:
            await adapter.stop()

    async def test_bad_codec_output_does_not_poison_meshtastic(
        self,
    ) -> None:
        """A packet that classifies as non-text returns early (no crash);
        next valid text packet succeeds."""
        adapter = MeshtasticAdapter(
            MeshtasticConfig(adapter_id="mesh-cat", connection_type="fake")
        )
        collector = _InboundCollector()
        ctx = _make_ctx("mesh-cat", collector)
        await adapter.start(ctx)

        try:
            # Non-text packet (telemetry) → simulate_inbound returns early
            telemetry = {
                "fromId": "!node1",
                "toId": "",
                "id": 999,
                "decoded": {"portnum": "telemetry"},
            }
            await adapter.simulate_inbound(telemetry)
            assert len(collector.events) == 0

            # ACK packet → also returns early
            ack = {
                "fromId": "!node1",
                "toId": "",
                "id": 998,
                "decoded": {"portnum": "text_message_ack"},
            }
            await adapter.simulate_inbound(ack)
            assert len(collector.events) == 0

            # Valid text packet → succeeds
            valid = make_text_packet(text="post-telemetry", sender="!node-ok")
            await adapter.simulate_inbound(valid)
            assert len(collector.events) == 1
        finally:
            await adapter.stop()


# ---------------------------------------------------------------------------
# Test 3 — MeshCore: simulate_inbound exception isolation
# ---------------------------------------------------------------------------


class TestMeshCoreCallbackIsolation:
    """Prove MeshCoreAdapter callback and simulate_inbound fault isolation."""

    async def test_one_bad_simulate_inbound_does_not_poison_meshcore(
        self,
    ) -> None:
        """A malformed packet raises from simulate_inbound; the next valid
        packet is processed normally."""
        adapter = MeshCoreAdapter(
            MeshCoreConfig(adapter_id="mc-robust", connection_type="fake")
        )
        collector = _InboundCollector()
        ctx = _make_ctx("mc-robust", collector)
        await adapter.start(ctx)

        try:
            # --- BAD: non-dict → codec raises MeshCoreCodecError ---
            with pytest.raises(Exception):
                await adapter.simulate_inbound(42)  # type: ignore[arg-type]

            assert len(collector.events) == 0

            # --- GOOD: valid channel message ---
            valid = make_meshcore_packet(
                text="meshcore recovery", sender="mcrec", channel=0, packet_id=1001
            )
            await adapter.simulate_inbound(valid)

            assert len(collector.events) == 1
            assert collector.events[0].payload.get("body") == "meshcore recovery"
        finally:
            await adapter.stop()

    async def test_one_bad_on_message_does_not_poison_meshcore(
        self,
    ) -> None:
        """The sync _on_message callback catches exceptions internally; a
        subsequent valid packet publishes successfully."""
        adapter = MeshCoreAdapter(
            MeshCoreConfig(adapter_id="mc-sync", connection_type="fake")
        )
        collector = _InboundCollector()
        ctx = _make_ctx("mc-sync", collector)
        await adapter.start(ctx)

        try:
            # --- BAD: packet with ACK code → classified as ack, returns early ---
            ack_packet = {"code": 0, "text": "ack-msg"}
            adapter._on_message(ack_packet)
            assert len(adapter._background_tasks) == 0

            # --- BAD: None → caught by try/except ---
            adapter._on_message(None)  # type: ignore[arg-type]

            # --- GOOD: valid channel message ---
            valid = make_meshcore_packet(
                text="post-ack-msg", sender="mcok", channel=0, packet_id=2001
            )
            adapter._on_message(valid)

            # Wait for background task to complete (do NOT drain/cancel)
            await _await_background_tasks(adapter)

            assert len(collector.events) == 1
            assert collector.events[0].payload.get("body") == "post-ack-msg"
        finally:
            await adapter.stop()

    async def test_non_text_category_does_not_poison_meshcore(
        self,
    ) -> None:
        """A packet without text (category=unknown) is silently skipped;
        next valid text packet succeeds."""
        adapter = MeshCoreAdapter(
            MeshCoreConfig(adapter_id="mc-cat", connection_type="fake")
        )
        collector = _InboundCollector()
        ctx = _make_ctx("mc-cat", collector)
        await adapter.start(ctx)

        try:
            # Packet without "text" key → category "unknown" → early return
            empty = {"pubkey_prefix": "no_text_node", "sender_timestamp": 3001}
            await adapter.simulate_inbound(empty)
            assert len(collector.events) == 0

            # ACK packet → also skipped
            ack = {"code": 1}
            await adapter.simulate_inbound(ack)
            assert len(collector.events) == 0

            # Valid text packet → succeeds
            valid = make_meshcore_packet(
                text="valid text", sender="mck2", channel=1, packet_id=3002
            )
            await adapter.simulate_inbound(valid)
            assert len(collector.events) == 1
        finally:
            await adapter.stop()


# ---------------------------------------------------------------------------
# Test 4 — LXMF: simulate_inbound exception isolation
# ---------------------------------------------------------------------------


class TestLxmfCallbackIsolation:
    """Prove LxmfAdapter callback and simulate_inbound fault isolation."""

    async def test_one_bad_simulate_inbound_does_not_poison_lxmf(
        self,
    ) -> None:
        """A malformed packet raises from simulate_inbound; the next valid
        packet is processed normally."""
        adapter = LxmfAdapter(
            LxmfConfig(adapter_id="lx-robust", connection_type="fake")
        )
        collector = _InboundCollector()
        ctx = _make_ctx("lx-robust", collector)
        await adapter.start(ctx)

        try:
            # --- BAD: non-dict → codec raises LxmfCodecError ---
            with pytest.raises(Exception):
                await adapter.simulate_inbound("not a dict")  # type: ignore[arg-type]

            assert len(collector.events) == 0

            # --- GOOD: valid LXMF text packet ---
            valid = _make_lxmf_packet(content="lxmf recovery")
            await adapter.simulate_inbound(valid)

            assert len(collector.events) == 1
            assert collector.events[0].payload.get("body") == "lxmf recovery"
        finally:
            await adapter.stop()

    async def test_one_bad_on_packet_does_not_poison_lxmf(
        self,
    ) -> None:
        """The sync _on_packet callback catches exceptions; next valid packet
        publishes via background task."""
        adapter = LxmfAdapter(LxmfConfig(adapter_id="lx-sync", connection_type="fake"))
        collector = _InboundCollector()
        ctx = _make_ctx("lx-sync", collector)
        await adapter.start(ctx)

        try:
            # --- BAD: None → caught by try/except ---
            adapter._on_packet(None)  # type: ignore[arg-type]
            assert len(adapter._background_tasks) == 0

            # --- GOOD: valid packet ---
            valid = _make_lxmf_packet(content="sync lxmf ok")
            adapter._on_packet(valid)

            # Wait for background task to complete (do NOT drain/cancel)
            await _await_background_tasks(adapter)

            assert len(collector.events) == 1
            assert collector.events[0].payload.get("body") == "sync lxmf ok"
        finally:
            await adapter.stop()

    async def test_non_text_category_does_not_poison_lxmf(
        self,
    ) -> None:
        """Packet without content → category unknown → early return;
        next valid text packet succeeds."""
        adapter = LxmfAdapter(LxmfConfig(adapter_id="lx-cat", connection_type="fake"))
        collector = _InboundCollector()
        ctx = _make_ctx("lx-cat", collector)
        await adapter.start(ctx)

        try:
            # No content → category "unknown" → early return
            no_content = {
                "source_hash": "ab" * 16,
                "destination_hash": "00" * 16,
                "message_id": "cd" * 32,
                "content": "",
                "fields": {},
            }
            await adapter.simulate_inbound(no_content)
            assert len(collector.events) == 0

            # Only fields, no content → category "unsupported" → early return
            fields_only = {
                "source_hash": "ab" * 16,
                "destination_hash": "00" * 16,
                "message_id": "cd" * 32,
                "content": "",
                "fields": {"attachment": "data"},
            }
            await adapter.simulate_inbound(fields_only)
            assert len(collector.events) == 0

            # Valid text → succeeds
            valid = _make_lxmf_packet(content="lxmf valid text")
            await adapter.simulate_inbound(valid)
            assert len(collector.events) == 1
        finally:
            await adapter.stop()


# ---------------------------------------------------------------------------
# Test 5 — Duplicate event IDs do not crash
# ---------------------------------------------------------------------------


class TestDuplicateEventId:
    """Duplicate event IDs must not crash the adapter or pipeline."""

    async def test_duplicate_meshtastic_packet_id_not_crash(self) -> None:
        """Two packets with the same id are both processed (no dedup in
        callback); adapter continues working."""
        adapter = MeshtasticAdapter(
            MeshtasticConfig(adapter_id="mesh-dup", connection_type="fake")
        )
        collector = _InboundCollector()
        await adapter.start(_make_ctx("mesh-dup", collector))

        try:
            pkt1 = make_text_packet(text="first", sender="!dup-node", packet_id=7777)
            pkt2 = make_text_packet(text="second", sender="!dup-node", packet_id=7777)

            await adapter.simulate_inbound(pkt1)
            await adapter.simulate_inbound(pkt2)

            # Both events published — no dedup at callback level
            assert len(collector.events) == 2
        finally:
            await adapter.stop()

    async def test_duplicate_meshcore_packet_id_not_crash(self) -> None:
        """Two MeshCore packets with same sender_timestamp both process."""
        adapter = MeshCoreAdapter(
            MeshCoreConfig(adapter_id="mc-dup", connection_type="fake")
        )
        collector = _InboundCollector()
        await adapter.start(_make_ctx("mc-dup", collector))

        try:
            pkt1 = make_meshcore_packet(text="mc-first", sender="dup1", packet_id=5555)
            pkt2 = make_meshcore_packet(text="mc-second", sender="dup1", packet_id=5555)

            await adapter.simulate_inbound(pkt1)
            await adapter.simulate_inbound(pkt2)

            assert len(collector.events) == 2
        finally:
            await adapter.stop()

    async def test_duplicate_lxmf_message_id_not_crash(self) -> None:
        """Two LXMF packets with same message_id both process."""
        adapter = LxmfAdapter(LxmfConfig(adapter_id="lx-dup", connection_type="fake"))
        collector = _InboundCollector()
        await adapter.start(_make_ctx("lx-dup", collector))

        try:
            pkt1 = _make_lxmf_packet(content="lx-first", msg_id="aa" * 32)
            pkt2 = _make_lxmf_packet(content="lx-second", msg_id="aa" * 32)

            await adapter.simulate_inbound(pkt1)
            await adapter.simulate_inbound(pkt2)

            assert len(collector.events) == 2
        finally:
            await adapter.stop()

    async def test_duplicate_matrix_event_id_not_crash(self) -> None:
        """Two Matrix events with same event_id both process."""
        config = make_matrix_config(adapter_id="mx-dup")
        adapter = MatrixAdapter(config)
        collector = _InboundCollector()
        adapter.ctx = _make_ctx("mx-dup", collector)
        room = make_nio_room("!dup_room:example.com")

        evt1 = make_nio_event(
            sender="@a:example.com", event_id="$dup-001", body="mx-first"
        )
        evt2 = make_nio_event(
            sender="@b:example.com", event_id="$dup-001", body="mx-second"
        )

        await adapter._on_room_message(room, evt1)
        await adapter._on_room_message(room, evt2)

        assert len(collector.events) == 2


# ---------------------------------------------------------------------------
# Test 6 — Empty payload does not crash
# ---------------------------------------------------------------------------


class TestEmptyPayload:
    """Empty payload dicts must not crash the adapter."""

    async def test_empty_meshtastic_decoded_text_not_crash(self) -> None:
        """A Meshtastic packet with empty decoded text processes fine."""
        adapter = MeshtasticAdapter(
            MeshtasticConfig(adapter_id="mesh-empty", connection_type="fake")
        )
        collector = _InboundCollector()
        await adapter.start(_make_ctx("mesh-empty", collector))

        try:
            pkt = {
                "fromId": "!empty-node",
                "toId": "",
                "channel": 0,
                "id": 8888,
                "decoded": {
                    "portnum": "text_message",
                    "text": "",  # empty text
                },
            }
            await adapter.simulate_inbound(pkt)
            assert len(collector.events) == 1
            assert collector.events[0].payload.get("body") == ""
        finally:
            await adapter.stop()

    async def test_empty_meshcore_text_not_crash(self) -> None:
        """A MeshCore packet with empty text string processes fine."""
        adapter = MeshCoreAdapter(
            MeshCoreConfig(adapter_id="mc-empty", connection_type="fake")
        )
        collector = _InboundCollector()
        await adapter.start(_make_ctx("mc-empty", collector))

        try:
            pkt = make_meshcore_packet(text="", sender="empty1", packet_id=9001)
            await adapter.simulate_inbound(pkt)
            assert len(collector.events) == 1
            assert collector.events[0].payload.get("body") == ""
        finally:
            await adapter.stop()

    async def test_empty_lxmf_content_not_crash(self) -> None:
        """An LXMF packet with empty content returns early (no text category)."""
        adapter = LxmfAdapter(LxmfConfig(adapter_id="lx-empty", connection_type="fake"))
        collector = _InboundCollector()
        await adapter.start(_make_ctx("lx-empty", collector))

        try:
            pkt = _make_lxmf_packet(content="")
            # Empty content → category "unknown" → returns early
            await adapter.simulate_inbound(pkt)
            assert len(collector.events) == 0
        finally:
            await adapter.stop()

    async def test_empty_matrix_body_not_crash(self) -> None:
        """A Matrix event with empty body processes fine."""
        config = make_matrix_config(adapter_id="mx-empty")
        adapter = MatrixAdapter(config)
        collector = _InboundCollector()
        adapter.ctx = _make_ctx("mx-empty", collector)
        room = make_nio_room("!empty_room:example.com")

        evt = make_nio_event(sender="@a:example.com", event_id="$empty-001", body="")
        await adapter._on_room_message(room, evt)
        assert len(collector.events) == 1
        assert collector.events[0].payload.get("body") == ""


# ---------------------------------------------------------------------------
# Test 7 — Missing required fields do not crash
# ---------------------------------------------------------------------------


class TestMissingRequiredFields:
    """Packets missing required fields are caught gracefully; adapter
    continues processing subsequent valid packets."""

    async def test_missing_source_on_matrix_event_not_crash(self) -> None:
        """Matrix event without .source is caught by try/except; next
        valid event succeeds."""
        config = make_matrix_config(adapter_id="mx-missing")
        adapter = MatrixAdapter(config)
        collector = _InboundCollector()
        adapter.ctx = _make_ctx("mx-missing", collector)
        room = make_nio_room("!missing_room:example.com")

        # Missing .source
        bad = SimpleNamespace(
            sender="@x:example.com", event_id="$bad-no-source", body="x"
        )
        await adapter._on_room_message(room, bad)
        assert len(collector.events) == 0

        # Valid
        good = make_nio_event(
            sender="@y:example.com", event_id="$good-after-missing", body="ok"
        )
        await adapter._on_room_message(room, good)
        assert len(collector.events) == 1

    async def test_missing_decoded_on_meshtastic_not_crash(self) -> None:
        """Meshtastic packet without 'decoded' key classifies as unknown;
        adapter continues."""
        adapter = MeshtasticAdapter(
            MeshtasticConfig(adapter_id="mesh-missing", connection_type="fake")
        )
        collector = _InboundCollector()
        await adapter.start(_make_ctx("mesh-missing", collector))

        try:
            # No decoded → classifier returns unknown portnum → codec raises
            bad = {"fromId": "!node1", "toId": "", "id": 1111}
            # simulate_inbound: classify returns category="unknown" → returns early
            await adapter.simulate_inbound(bad)
            assert len(collector.events) == 0

            # Valid packet after
            valid = make_text_packet(text="after missing", sender="!node-ok")
            await adapter.simulate_inbound(valid)
            assert len(collector.events) == 1
        finally:
            await adapter.stop()

    async def test_missing_text_on_meshcore_not_crash(self) -> None:
        """MeshCore packet without 'text' is classified as unknown;
        adapter continues."""
        adapter = MeshCoreAdapter(
            MeshCoreConfig(adapter_id="mc-missing", connection_type="fake")
        )
        collector = _InboundCollector()
        await adapter.start(_make_ctx("mc-missing", collector))

        try:
            # No text key → category "unknown" → simulate_inbound returns early
            bad = {"pubkey_prefix": "no_text", "sender_timestamp": 4444}
            await adapter.simulate_inbound(bad)
            assert len(collector.events) == 0

            # Valid after
            valid = make_meshcore_packet(
                text="after missing fields", sender="mcok2", packet_id=4445
            )
            await adapter.simulate_inbound(valid)
            assert len(collector.events) == 1
        finally:
            await adapter.stop()

    async def test_missing_content_on_lxmf_not_crash(self) -> None:
        """LXMF packet without content is classified as unknown;
        adapter continues."""
        adapter = LxmfAdapter(
            LxmfConfig(adapter_id="lx-missing", connection_type="fake")
        )
        collector = _InboundCollector()
        await adapter.start(_make_ctx("lx-missing", collector))

        try:
            # No content → category "unknown"
            bad = {"source_hash": "ab" * 16, "message_id": "cd" * 32, "fields": {}}
            await adapter.simulate_inbound(bad)
            assert len(collector.events) == 0

            # Valid after
            valid = _make_lxmf_packet(content="after lxmf missing")
            await adapter.simulate_inbound(valid)
            assert len(collector.events) == 1
        finally:
            await adapter.stop()


# ---------------------------------------------------------------------------
# Test 8 — Background task failure isolation
# ---------------------------------------------------------------------------


class TestAsyncPublishFailureIsolation:
    """Verify that a failure in publish_inbound does not corrupt adapter state
    and subsequent callbacks still work."""

    async def test_failing_publish_does_not_poison_meshtastic_on_packet(
        self,
    ) -> None:
        """_on_packet creates a background task that calls _on_packet_async.
        If publish_inbound raises, the task logs it. Next call works."""
        adapter = MeshtasticAdapter(
            MeshtasticConfig(adapter_id="mesh-pub-fail", connection_type="fake")
        )

        call_count = 0

        async def _failing_publish(event: Any) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("simulated publish failure")

        ctx = _make_ctx("mesh-pub-fail", _InboundCollector())
        # Replace publish_inbound with our failing version
        ctx = AdapterContext(
            adapter_id="mesh-pub-fail",
            event_bus=None,
            publish_inbound=_failing_publish,
            logger=logging.getLogger("test.mesh-pub-fail"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )
        await adapter.start(ctx)

        # Collector for the second call
        collector = _InboundCollector()
        good_ctx = AdapterContext(
            adapter_id="mesh-pub-fail",
            event_bus=None,
            publish_inbound=collector,
            logger=logging.getLogger("test.mesh-pub-fail"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )

        try:
            # First packet: publish_inbound raises in background task
            pkt1 = make_text_packet(text="will fail to publish", sender="!fail-node")
            adapter._on_packet(pkt1)

            # Yield to let the coroutine submitted via run_coroutine_threadsafe
            # execute on the event loop.
            await asyncio.sleep(0.1)

            # Now swap to working context
            adapter.ctx = good_ctx

            # Second packet: should succeed
            pkt2 = make_text_packet(text="will succeed", sender="!ok-node")
            adapter._on_packet(pkt2)
            await asyncio.sleep(0.1)

            assert len(collector.events) == 1
            assert collector.events[0].payload.get("body") == "will succeed"
        finally:
            await adapter.stop()

    async def test_failing_publish_does_not_poison_lxmf_on_packet(
        self,
    ) -> None:
        """Same pattern for LXMF: publish failure in background task is
        isolated, next call works."""
        adapter = LxmfAdapter(
            LxmfConfig(adapter_id="lx-pub-fail", connection_type="fake")
        )

        call_count = 0

        async def _failing_publish(event: Any) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("lxmf publish failure")

        ctx = AdapterContext(
            adapter_id="lx-pub-fail",
            event_bus=None,
            publish_inbound=_failing_publish,
            logger=logging.getLogger("test.lx-pub-fail"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )
        await adapter.start(ctx)

        collector = _InboundCollector()
        good_ctx = AdapterContext(
            adapter_id="lx-pub-fail",
            event_bus=None,
            publish_inbound=collector,
            logger=logging.getLogger("test.lx-pub-fail"),
            clock=lambda: datetime.now(timezone.utc),
            shutdown_event=asyncio.Event(),
        )

        try:
            # First packet fails in background task
            pkt1 = _make_lxmf_packet(content="lxmf fail")
            adapter._on_packet(pkt1)
            await _await_background_tasks(adapter)

            # Swap to working context
            adapter.ctx = good_ctx

            # Second packet succeeds
            pkt2 = _make_lxmf_packet(content="lxmf ok")
            adapter._on_packet(pkt2)
            await _await_background_tasks(adapter)

            assert len(collector.events) == 1
            assert collector.events[0].payload.get("body") == "lxmf ok"
        finally:
            await adapter.stop()


# ---------------------------------------------------------------------------
# Test 9 — Rapid fire: bad-good-bad-good pattern
# ---------------------------------------------------------------------------


class TestRapidFireBadGood:
    """Interleaved bad and good callbacks in rapid succession all produce
    correct results."""

    async def test_interleaved_bad_good_meshtastic(self) -> None:
        """Bad → good → bad → good all resolve correctly."""
        adapter = MeshtasticAdapter(
            MeshtasticConfig(adapter_id="mesh-rapid", connection_type="fake")
        )
        collector = _InboundCollector()
        await adapter.start(_make_ctx("mesh-rapid", collector))

        try:
            # bad (not a dict)
            with pytest.raises(Exception):
                await adapter.simulate_inbound(None)  # type: ignore[arg-type]

            # good
            await adapter.simulate_inbound(
                make_text_packet(text="good-1", sender="!g1", packet_id=101)
            )

            # bad (non-text)
            await adapter.simulate_inbound({"decoded": {"portnum": "telemetry"}})

            # good
            await adapter.simulate_inbound(
                make_text_packet(text="good-2", sender="!g2", packet_id=102)
            )

            assert len(collector.events) == 2
            bodies = [e.payload.get("body") for e in collector.events]
            assert "good-1" in bodies
            assert "good-2" in bodies
        finally:
            await adapter.stop()

    async def test_interleaved_bad_good_meshcore(self) -> None:
        """Bad → good → bad → good all resolve correctly."""
        adapter = MeshCoreAdapter(
            MeshCoreConfig(adapter_id="mc-rapid", connection_type="fake")
        )
        collector = _InboundCollector()
        await adapter.start(_make_ctx("mc-rapid", collector))

        try:
            # bad (not a dict)
            with pytest.raises(Exception):
                await adapter.simulate_inbound(12345)  # type: ignore[arg-type]

            # good
            await adapter.simulate_inbound(
                make_meshcore_packet(text="mc-good-1", sender="g1", packet_id=201)
            )

            # bad (ACK)
            await adapter.simulate_inbound({"code": 0})

            # good
            await adapter.simulate_inbound(
                make_meshcore_packet(text="mc-good-2", sender="g2", packet_id=202)
            )

            assert len(collector.events) == 2
            bodies = [e.payload.get("body") for e in collector.events]
            assert "mc-good-1" in bodies
            assert "mc-good-2" in bodies
        finally:
            await adapter.stop()

    async def test_interleaved_bad_good_lxmf(self) -> None:
        """Bad → good → bad → good all resolve correctly."""
        adapter = LxmfAdapter(LxmfConfig(adapter_id="lx-rapid", connection_type="fake"))
        collector = _InboundCollector()
        await adapter.start(_make_ctx("lx-rapid", collector))

        try:
            # bad (not a dict)
            with pytest.raises(Exception):
                await adapter.simulate_inbound(b"bytes-not-dict")  # type: ignore[arg-type]

            # good
            await adapter.simulate_inbound(_make_lxmf_packet(content="lx-good-1"))

            # bad (no content → unknown)
            await adapter.simulate_inbound({"source_hash": "ab" * 16, "fields": {}})

            # good
            await adapter.simulate_inbound(_make_lxmf_packet(content="lx-good-2"))

            assert len(collector.events) == 2
            bodies = [e.payload.get("body") for e in collector.events]
            assert "lx-good-1" in bodies
            assert "lx-good-2" in bodies
        finally:
            await adapter.stop()

    async def test_interleaved_bad_good_matrix(self) -> None:
        """Bad → good → bad → good _on_room_message calls all resolve."""
        config = make_matrix_config(adapter_id="mx-rapid")
        adapter = MatrixAdapter(config)
        collector = _InboundCollector()
        adapter.ctx = _make_ctx("mx-rapid", collector)
        room = make_nio_room("!rapid_room:example.com")

        # bad (no .source)
        await adapter._on_room_message(
            room, SimpleNamespace(sender="@x:example.com", event_id="$rb1", body="x")
        )

        # good
        await adapter._on_room_message(
            room,
            make_nio_event(sender="@a:example.com", event_id="$rg1", body="mx-good-1"),
        )

        # bad (no .source again)
        await adapter._on_room_message(
            room, SimpleNamespace(sender="@y:example.com", event_id="$rb2", body="y")
        )

        # good
        await adapter._on_room_message(
            room,
            make_nio_event(sender="@b:example.com", event_id="$rg2", body="mx-good-2"),
        )

        assert len(collector.events) == 2
        bodies = [e.payload.get("body") for e in collector.events]
        assert "mx-good-1" in bodies
        assert "mx-good-2" in bodies


# ---------------------------------------------------------------------------
# Test 10 — Native ID isolation across callbacks
# ---------------------------------------------------------------------------


class TestNativeIdIsolation:
    """Verify that native message IDs from failed callbacks do not leak
    into subsequent successful callbacks."""

    async def test_failed_meshtastic_native_ref_not_leaked(self) -> None:
        """A failed decode does not leave stale native refs for the next
        successful callback."""
        adapter = MeshtasticAdapter(
            MeshtasticConfig(adapter_id="mesh-native", connection_type="fake")
        )
        collector = _InboundCollector()
        await adapter.start(_make_ctx("mesh-native", collector))

        try:
            # Bad: triggers exception in classify/decode
            with pytest.raises(Exception):
                await adapter.simulate_inbound(None)  # type: ignore[arg-type]

            # Good: distinct packet_id
            valid = make_text_packet(
                text="clean native", sender="!clean", packet_id=9999
            )
            await adapter.simulate_inbound(valid)

            assert len(collector.events) == 1
            event = collector.events[0]
            # The successful event should reference only its own native ID
            if event.source_native_ref is not None:
                assert event.source_native_ref.native_message_id == "9999"
        finally:
            await adapter.stop()

    async def test_failed_meshcore_native_ref_not_leaked(self) -> None:
        """Same for MeshCore."""
        adapter = MeshCoreAdapter(
            MeshCoreConfig(adapter_id="mc-native", connection_type="fake")
        )
        collector = _InboundCollector()
        await adapter.start(_make_ctx("mc-native", collector))

        try:
            with pytest.raises(Exception):
                await adapter.simulate_inbound(None)  # type: ignore[arg-type]

            valid = make_meshcore_packet(text="mc clean", sender="cln", packet_id=7777)
            await adapter.simulate_inbound(valid)

            assert len(collector.events) == 1
            event = collector.events[0]
            if event.source_native_ref is not None:
                assert event.source_native_ref.native_message_id == "7777"
        finally:
            await adapter.stop()

    async def test_failed_lxmf_native_ref_not_leaked(self) -> None:
        """Same for LXMF."""
        adapter = LxmfAdapter(
            LxmfConfig(adapter_id="lx-native", connection_type="fake")
        )
        collector = _InboundCollector()
        await adapter.start(_make_ctx("lx-native", collector))

        try:
            with pytest.raises(Exception):
                await adapter.simulate_inbound(None)  # type: ignore[arg-type]

            valid = _make_lxmf_packet(content="lx clean", msg_id="ff" * 32)
            await adapter.simulate_inbound(valid)

            assert len(collector.events) == 1
            event = collector.events[0]
            if event.source_native_ref is not None:
                assert event.source_native_ref.native_message_id == "ff" * 32
        finally:
            await adapter.stop()

    async def test_failed_matrix_native_ref_not_leaked(self) -> None:
        """Same for Matrix."""
        config = make_matrix_config(adapter_id="mx-native")
        adapter = MatrixAdapter(config)
        collector = _InboundCollector()
        adapter.ctx = _make_ctx("mx-native", collector)
        room = make_nio_room("!native_room:example.com")

        # Bad: no .source
        bad = SimpleNamespace(sender="@x:example.com", event_id="$bad-native", body="x")
        await adapter._on_room_message(room, bad)

        # Good
        good = make_nio_event(
            sender="@y:example.com", event_id="$good-native", body="clean"
        )
        await adapter._on_room_message(room, good)

        assert len(collector.events) == 1
        event = collector.events[0]
        assert event.source_native_ref is not None
        assert event.source_native_ref.native_message_id == "$good-native"
