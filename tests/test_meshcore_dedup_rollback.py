"""MeshCore dedup rollback regression tests.

Verifies that dedup keys are not retained after failed decode or publish
attempts, so that redelivery of the same (sender_id, packet_id, channel_index,
text) is not suppressed.

The two inbound paths handle key insertion differently:

- ``simulate_inbound``: dedup key is never inserted on any failure (decode or
  publish).  Success-only final insertion.
- ``_on_message`` (SDK callback): dedup key is inserted after decode to guard
  the async publish window against concurrent duplicates, then rolled back
  if ``_on_message_async`` publish fails.  Decode failure means the key is
  never inserted.

Mirrors the LXMF rollback coverage from ``test_lxmf_dedup_rollback.py``.

Covers both the ``_on_message`` (sync → async) path and the
``simulate_inbound`` (all-async) path.

Evidence level: ``fake_pipeline`` / ``fake_adapter_callback`` (tier 1–2).
No network, no hardware, no Docker.
"""

from __future__ import annotations

import asyncio
from typing import Literal
from unittest.mock import AsyncMock

import pytest

from medre.adapters.meshcore.adapter import MeshCoreAdapter
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.core.contracts.adapter import AdapterContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(
    *,
    adapter_id: str = "mc-dedup-test",
    connection_type: Literal["fake", "tcp", "serial", "ble"] = "fake",
) -> MeshCoreConfig:
    return MeshCoreConfig(adapter_id=adapter_id, connection_type=connection_type)


def _meshcore_text_packet(
    text: str = "hello",
    pubkey_prefix: str = "abc123",
    sender_timestamp: int = 42,
    channel_idx: int = 0,
    msg_type: str = "CHAN",
) -> dict:
    """Build a minimal MeshCore text packet that classifies as ``relay``."""
    return {
        "text": text,
        "pubkey_prefix": pubkey_prefix,
        "sender_timestamp": sender_timestamp,
        "type": msg_type,
        "txt_type": 0,
        "channel_idx": channel_idx,
    }


def _dedup_key(
    sender_id: str = "abc123",
    packet_id: int = 42,
    channel_index: int = 0,
    text: str = "hello",
) -> tuple[str, int, int | None, str]:
    """Build the expected dedup key tuple for a packet."""
    return (sender_id, packet_id, channel_index, text)


# ===================================================================
# simulate_inbound: dedup rollback on decode failure
# ===================================================================


async def test_simulate_inbound_decode_failure_no_dedup_key(
    make_adapter_context,
) -> None:
    """If decode raises, the dedup key must NOT remain in _inbound_dedup."""
    config = _make_config(connection_type="fake")
    adapter = MeshCoreAdapter(config)
    ctx = make_adapter_context("mc-dedup-test")
    await adapter.start(ctx)

    packet = _meshcore_text_packet(
        text="bad", pubkey_prefix="dec", sender_timestamp=1001
    )

    # Force decode to raise.
    original_decode = adapter._codec.decode

    def _bad_decode(pkt):
        raise ValueError("simulated decode error")

    adapter._codec.decode = _bad_decode  # type: ignore[assignment]

    with pytest.raises(ValueError, match="simulated decode error"):
        await adapter.simulate_inbound(packet)

    # Dedup key must not be present.
    key = _dedup_key(sender_id="dec", packet_id=1001, text="bad")
    assert key not in adapter._inbound_dedup

    # Restore so teardown is clean.
    adapter._codec.decode = original_decode
    await adapter.stop()


# ===================================================================
# simulate_inbound: dedup key not retained after publish failure
# (simulate_inbound never inserts the key until publish succeeds)
# ===================================================================


async def test_simulate_inbound_publish_failure_no_dedup_key(
    make_adapter_context,
) -> None:
    """If publish_inbound raises in simulate_inbound, dedup key is not retained."""
    config = _make_config(connection_type="fake")
    adapter = MeshCoreAdapter(config)

    # Wire a publish_inbound that always fails.
    failing_publish = AsyncMock(side_effect=RuntimeError("publish broke"))

    ctx_base = make_adapter_context("mc-dedup-test")
    ctx_fail = AdapterContext(
        adapter_id="mc-dedup-test",
        event_bus=None,
        publish_inbound=failing_publish,
        logger=ctx_base.logger,
        clock=ctx_base.clock,
        shutdown_event=ctx_base.shutdown_event,
    )
    await adapter.start(ctx_fail)

    packet = _meshcore_text_packet(
        text="pub-fail", pubkey_prefix="pub", sender_timestamp=2002
    )

    with pytest.raises(RuntimeError, match="publish broke"):
        await adapter.simulate_inbound(packet)

    key = _dedup_key(sender_id="pub", packet_id=2002, text="pub-fail")
    assert key not in adapter._inbound_dedup

    await adapter.stop()


# ===================================================================
# simulate_inbound: success inserts key, duplicate is suppressed
# ===================================================================


async def test_simulate_inbound_success_inserts_dedup_key(
    make_adapter_context,
    inbound_collector,
) -> None:
    """Successful decode+publish inserts dedup key; duplicate suppressed."""
    config = _make_config(connection_type="fake")
    adapter = MeshCoreAdapter(config)
    ctx = make_adapter_context("mc-dedup-test")
    await adapter.start(ctx)

    packet = _meshcore_text_packet(text="ok", pubkey_prefix="ok", sender_timestamp=3003)

    await adapter.simulate_inbound(packet)
    assert len(inbound_collector.events) == 1

    key = _dedup_key(sender_id="ok", packet_id=3003, text="ok")
    assert key in adapter._inbound_dedup

    # Duplicate delivery suppressed.
    await adapter.simulate_inbound(packet)
    assert len(inbound_collector.events) == 1

    await adapter.stop()


# ===================================================================
# _on_message decode failure does not insert dedup key
# ===================================================================


async def test_on_message_decode_failure_no_dedup_key(
    make_adapter_context,
) -> None:
    """If decode raises inside _on_message, dedup key is never inserted."""
    config = _make_config(connection_type="fake")
    adapter = MeshCoreAdapter(config)
    ctx = make_adapter_context("mc-dedup-test")
    await adapter.start(ctx)

    original_decode = adapter._codec.decode

    def _bad_decode(pkt):
        raise ValueError("decode fail in _on_message")

    adapter._codec.decode = _bad_decode  # type: ignore[assignment]

    packet = _meshcore_text_packet(
        text="sync-fail", pubkey_prefix="sync", sender_timestamp=4004
    )
    # _on_message catches exceptions internally, so it won't raise.
    adapter._on_message(packet)

    key = _dedup_key(sender_id="sync", packet_id=4004, text="sync-fail")
    assert key not in adapter._inbound_dedup

    adapter._codec.decode = original_decode
    await adapter.stop()


# ===================================================================
# _on_message: async publish failure rolls back dedup key inserted
# after decode (key is temporarily present, then removed)
# ===================================================================


async def test_on_message_async_publish_failure_rolls_back_dedup(
    make_adapter_context,
) -> None:
    """If _on_message_async publish fails, dedup key is rolled back (not retained)."""
    config = _make_config(connection_type="fake")
    adapter = MeshCoreAdapter(config)

    # Wire a publish_inbound that always fails.
    failing_publish = AsyncMock(side_effect=RuntimeError("async pub fail"))

    ctx_base = make_adapter_context("mc-dedup-test")
    ctx_fail = AdapterContext(
        adapter_id="mc-dedup-test",
        event_bus=None,
        publish_inbound=failing_publish,
        logger=ctx_base.logger,
        clock=ctx_base.clock,
        shutdown_event=ctx_base.shutdown_event,
    )
    await adapter.start(ctx_fail)

    packet = _meshcore_text_packet(
        text="async-fail", pubkey_prefix="afail", sender_timestamp=5005
    )

    # _on_message is sync and spawns an async task.
    adapter._on_message(packet)

    # Wait for the background task to complete without cancelling it
    # (_drain_background_tasks would cancel and raise CancelledError,
    # which is BaseException, not Exception — so the rollback except
    # block would not fire).
    if adapter._background_tasks:
        await asyncio.gather(*adapter._background_tasks, return_exceptions=True)

    key = _dedup_key(sender_id="afail", packet_id=5005, text="async-fail")
    assert key not in adapter._inbound_dedup

    await adapter.stop()


# ===================================================================
# LRU semantics: move_to_end on dedup hit, popitem(last=False)
# ===================================================================


async def test_simulate_inbound_lru_eviction_and_promotion(
    make_adapter_context,
    inbound_collector,
) -> None:
    """LRU: move_to_end on dedup hit; insertion order reflects recency."""
    config = _make_config(connection_type="fake")
    adapter = MeshCoreAdapter(config)
    ctx = make_adapter_context("mc-dedup-test")
    await adapter.start(ctx)

    # Insert two distinct packets.
    await adapter.simulate_inbound(
        _meshcore_text_packet(text="first", pubkey_prefix="a", sender_timestamp=10)
    )
    await adapter.simulate_inbound(
        _meshcore_text_packet(text="second", pubkey_prefix="b", sender_timestamp=20)
    )

    keys = list(adapter._inbound_dedup.keys())
    assert keys[0] == _dedup_key(sender_id="a", packet_id=10, text="first")
    assert keys[1] == _dedup_key(sender_id="b", packet_id=20, text="second")
    assert len(inbound_collector.events) == 2

    # Re-deliver first → moves to end (LRU promotion).
    await adapter.simulate_inbound(
        _meshcore_text_packet(text="first", pubkey_prefix="a", sender_timestamp=10)
    )

    keys_after = list(adapter._inbound_dedup.keys())
    assert keys_after[0] == _dedup_key(sender_id="b", packet_id=20, text="second")
    assert keys_after[1] == _dedup_key(sender_id="a", packet_id=10, text="first")
    # Duplicate first suppressed — still only 2 events.
    assert len(inbound_collector.events) == 2

    await adapter.stop()


# ===================================================================
# stop/start clears dedup and allows redelivery
# ===================================================================


async def test_stop_start_clears_dedup_allows_redelivery(
    make_adapter_context,
    inbound_collector,
) -> None:
    """After stop+start cycle, previously-seen packets are delivered again."""
    config = _make_config(connection_type="fake")
    adapter = MeshCoreAdapter(config)
    ctx = make_adapter_context("mc-dedup-test")

    packet = _meshcore_text_packet(
        text="restart", pubkey_prefix="rst", sender_timestamp=9009
    )

    await adapter.start(ctx)
    await adapter.simulate_inbound(packet)
    assert len(inbound_collector.events) == 1

    await adapter.stop()

    # Restart — dedup is cleared.
    await adapter.start(ctx)
    await adapter.simulate_inbound(packet)
    assert len(inbound_collector.events) == 2

    await adapter.stop()


# ===================================================================
# packet_id=None skips adapter-level dedup
# ===================================================================


async def test_simulate_inbound_no_sender_timestamp_skips_dedup(
    make_adapter_context,
    inbound_collector,
) -> None:
    """Packets without sender_timestamp (packet_id=None) skip dedup entirely.

    Both deliveries must publish because no dedup key is constructed.
    """
    config = _make_config(connection_type="fake")
    adapter = MeshCoreAdapter(config)
    ctx = make_adapter_context("mc-dedup-test")
    await adapter.start(ctx)

    # No sender_timestamp → classifier reports packet_id=None.
    packet = {
        "text": "no-ts",
        "pubkey_prefix": "nots",
        "type": "CHAN",
        "txt_type": 0,
        "channel_idx": 0,
    }

    await adapter.simulate_inbound(packet)
    await adapter.simulate_inbound(packet)

    # Both should publish (no dedup key possible).
    assert len(inbound_collector.events) == 2

    await adapter.stop()


# ===================================================================
# duplicate packet_id with different text processes both
# ===================================================================


async def test_simulate_inbound_same_packet_id_different_text_processes_both(
    make_adapter_context,
    inbound_collector,
) -> None:
    """Packets with the same packet_id but different text are distinct.

    The dedup key includes text, so both are processed.
    """
    config = _make_config(connection_type="fake")
    adapter = MeshCoreAdapter(config)
    ctx = make_adapter_context("mc-dedup-test")
    await adapter.start(ctx)

    pkt_a = _meshcore_text_packet(
        text="msg-a", pubkey_prefix="dup", sender_timestamp=7777
    )
    pkt_b = _meshcore_text_packet(
        text="msg-b", pubkey_prefix="dup", sender_timestamp=7777
    )

    await adapter.simulate_inbound(pkt_a)
    await adapter.simulate_inbound(pkt_b)

    assert len(inbound_collector.events) == 2

    await adapter.stop()


# ===================================================================
# exact duplicate suppresses second successful replay
# ===================================================================


async def test_simulate_inbound_exact_duplicate_suppressed(
    make_adapter_context,
    inbound_collector,
) -> None:
    """Exact duplicate packet is suppressed after first successful publish."""
    config = _make_config(connection_type="fake")
    adapter = MeshCoreAdapter(config)
    ctx = make_adapter_context("mc-dedup-test")
    await adapter.start(ctx)

    packet = _meshcore_text_packet(
        text="exact", pubkey_prefix="ex", sender_timestamp=8888
    )

    await adapter.simulate_inbound(packet)
    assert len(inbound_collector.events) == 1

    # Exact same packet → suppressed.
    await adapter.simulate_inbound(packet)
    assert len(inbound_collector.events) == 1

    await adapter.stop()
