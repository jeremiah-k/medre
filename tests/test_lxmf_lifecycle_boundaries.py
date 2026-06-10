"""LXMF adapter lifecycle boundary tests: post-stop guard and
_on_packet_async started guard.

Split from test_lxmf_adapter.py to keep that file under the 1,200-line
target. These tests verify that lifecycle-stale callbacks do not publish
events after stop() has been called.
"""

from __future__ import annotations

from medre.adapters.lxmf.adapter import LxmfAdapter
from medre.config.adapters.lxmf import LxmfConfig


def _make_config(**overrides) -> LxmfConfig:
    defaults = dict(adapter_id="lxmf-1")
    defaults.update(overrides)
    if (
        defaults.get("connection_type") == "reticulum"
        and "storage_path" not in defaults
    ):
        defaults["storage_path"] = "/tmp/medre-test-lxmf-router"
    return LxmfConfig(**defaults)


def _make_text_packet(
    content: str = "hello",
    source_hash: str = "ab" * 16,
    msg_id: str = "cd" * 32,
) -> dict:
    return {
        "source_hash": source_hash,
        "destination_hash": "00" * 16,
        "message_id": msg_id,
        "timestamp": 1700000000.0,
        "title": "",
        "content": content,
        "fields": {},
        "signature_validated": True,
        "has_fields": False,
    }


# ===================================================================
# Lifecycle guard: simulate_inbound refuses post-stop calls
# ===================================================================


class TestLxmfLifecycleGuardSimulateInbound:
    """simulate_inbound must not publish after stop().

    Oracle finding A: ctx is retained after stop() but _started is
    cleared — a stale ctx must not be sufficient to publish
    lifecycle-stale inbound messages.
    """

    async def test_simulate_inbound_silent_return_after_stop(
        self, make_adapter_context, inbound_collector
    ) -> None:
        """After stop(), simulate_inbound returns silently without publishing."""
        config = _make_config(connection_type="fake")
        adapter = LxmfAdapter(config)
        ctx = make_adapter_context("lxmf-1")
        await adapter.start(ctx)

        # Publish one event while started — should succeed.
        packet = _make_text_packet(content="before stop")
        await adapter.simulate_inbound(packet)
        assert len(inbound_collector.events) == 1

        await adapter.stop()

        # ctx is still set but _started is False.
        assert adapter.ctx is not None
        assert adapter._started is False

        # simulate_inbound must not publish after stop.
        await adapter.simulate_inbound(_make_text_packet(content="after stop"))
        assert len(inbound_collector.events) == 1


# ===================================================================
# Lifecycle parity: _on_packet_async guard matches MeshCore
# ===================================================================


class TestLxmfOnPacketAsyncStartedGuard:
    """_on_packet_async must not publish after _started becomes False.

    Parity nit: MeshCore _on_message_async checks both ctx and _started
    before publish_inbound.  LXMF _on_packet_async must do the same so
    that an already-scheduled background task does not publish after
    stop() begins.
    """

    async def test_on_packet_async_skips_publish_when_not_started(
        self, make_adapter_context, inbound_collector
    ) -> None:
        """Directly call _on_packet_async with _started=False → no publish."""
        config = _make_config(connection_type="fake")
        adapter = LxmfAdapter(config)
        ctx = make_adapter_context("lxmf-1")
        await adapter.start(ctx)
        assert adapter._started is True

        # Decode a canonical event manually for direct injection.
        packet = _make_text_packet(content="guarded")
        canonical = adapter._codec.decode(packet)

        await adapter.stop()
        assert adapter._started is False
        assert adapter.ctx is not None  # ctx retained

        # Directly invoke _on_packet_async — must not publish.
        await adapter._on_packet_async(canonical)
        assert len(inbound_collector.events) == 0
