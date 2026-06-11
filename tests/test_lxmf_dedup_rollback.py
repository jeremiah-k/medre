"""LXMF dedup rollback regression tests.

Verifies that dedup keys are not retained after failed decode or publish
attempts, so that redelivery of the same (message_id, content) is not
suppressed.

The two inbound paths handle key insertion differently:

- ``simulate_inbound``: dedup key is never inserted on any failure (decode or
  publish).  Success-only final insertion.
- ``_on_packet`` (SDK callback): dedup key is inserted after decode to guard
  the async publish window against concurrent duplicates, then rolled back
  if ``_on_packet_async`` publish fails.  Decode failure means the key is
  never inserted.

Covers both the ``_on_packet`` (sync → async) path and the
``simulate_inbound`` (all-async) path.

Evidence level: ``fake_pipeline`` / ``fake_adapter_callback`` (tier 1–2).
No network, no hardware, no Docker.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from medre.adapters.lxmf.adapter import LxmfAdapter
from medre.config.adapters.lxmf import LxmfConfig


def _make_config(**overrides) -> LxmfConfig:
    defaults = dict(adapter_id="lxmf-dedup-test")
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
        "has_fields": False,
        "fields": {},
        "signature_validated": True,
    }


# ===================================================================
# simulate_inbound: dedup rollback on decode failure
# ===================================================================


async def test_simulate_inbound_decode_failure_no_dedup_key(
    make_adapter_context,
) -> None:
    """If decode raises, the dedup key must NOT remain in _inbound_dedup."""
    config = _make_config(connection_type="fake")
    adapter = LxmfAdapter(config)
    ctx = make_adapter_context("lxmf-dedup-test")
    await adapter.start(ctx)

    packet = _make_text_packet(content="bad", msg_id="msg-decode-fail")

    # Force decode to raise.
    original_decode = adapter._codec.decode

    def _bad_decode(pkt):
        raise ValueError("simulated decode error")

    adapter._codec.decode = _bad_decode  # type: ignore[assignment]

    with pytest.raises(ValueError, match="simulated decode error"):
        await adapter.simulate_inbound(packet)

    # Dedup key must not be present.
    dedup_key = ("msg-decode-fail", "bad")
    assert dedup_key not in adapter._inbound_dedup

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
    adapter = LxmfAdapter(config)
    ctx = make_adapter_context("lxmf-dedup-test")

    # Wire a publish_inbound that always fails.
    failing_publish = AsyncMock(side_effect=RuntimeError("publish broke"))
    from medre.core.contracts.adapter import AdapterContext

    ctx_fail = AdapterContext(
        adapter_id="lxmf-dedup-test",
        event_bus=None,
        publish_inbound=failing_publish,
        logger=ctx.logger,
        clock=ctx.clock,
        shutdown_event=ctx.shutdown_event,
    )
    await adapter.start(ctx_fail)

    packet = _make_text_packet(content="pub-fail", msg_id="msg-pub-fail")

    with pytest.raises(RuntimeError, match="publish broke"):
        await adapter.simulate_inbound(packet)

    dedup_key = ("msg-pub-fail", "pub-fail")
    assert dedup_key not in adapter._inbound_dedup

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
    adapter = LxmfAdapter(config)
    ctx = make_adapter_context("lxmf-dedup-test")
    await adapter.start(ctx)

    packet = _make_text_packet(content="ok", msg_id="msg-ok")

    await adapter.simulate_inbound(packet)
    assert len(inbound_collector.events) == 1

    dedup_key = ("msg-ok", "ok")
    assert dedup_key in adapter._inbound_dedup

    # Duplicate delivery suppressed.
    await adapter.simulate_inbound(packet)
    assert len(inbound_collector.events) == 1

    await adapter.stop()


# ===================================================================
# _on_packet: async publish failure rolls back dedup key inserted
# after decode (key is temporarily present, then removed)
# ===================================================================


async def test_on_packet_async_publish_failure_rolls_back_dedup(
    make_adapter_context,
) -> None:
    """If _on_packet_async publish fails, dedup key is rolled back (not retained)."""
    config = _make_config(connection_type="fake")
    adapter = LxmfAdapter(config)

    # Wire a publish_inbound that always fails.
    failing_publish = AsyncMock(side_effect=RuntimeError("async pub fail"))
    from medre.core.contracts.adapter import AdapterContext

    ctx_base = make_adapter_context("lxmf-dedup-test")
    ctx_fail = AdapterContext(
        adapter_id="lxmf-dedup-test",
        event_bus=None,
        publish_inbound=failing_publish,
        logger=ctx_base.logger,
        clock=ctx_base.clock,
        shutdown_event=ctx_base.shutdown_event,
    )
    await adapter.start(ctx_fail)

    packet = _make_text_packet(content="async-fail", msg_id="msg-async-fail")

    # _on_packet is sync and spawns an async task.
    adapter._on_packet(packet)

    # Wait for the background task to complete without cancelling it
    # (_drain_background_tasks would cancel and raise CancelledError,
    # which is BaseException, not Exception — so the rollback except
    # block would not fire).
    if adapter._background_tasks:
        await asyncio.gather(*adapter._background_tasks, return_exceptions=True)

    dedup_key = ("msg-async-fail", "async-fail")
    assert dedup_key not in adapter._inbound_dedup

    await adapter.stop()


# ===================================================================
# _on_packet decode failure does not insert dedup key
# ===================================================================


async def test_on_packet_decode_failure_no_dedup_key(
    make_adapter_context,
) -> None:
    """If decode raises inside _on_packet, dedup key is never inserted."""
    config = _make_config(connection_type="fake")
    adapter = LxmfAdapter(config)
    ctx = make_adapter_context("lxmf-dedup-test")
    await adapter.start(ctx)

    original_decode = adapter._codec.decode

    def _bad_decode(pkt):
        raise ValueError("decode fail in _on_packet")

    adapter._codec.decode = _bad_decode  # type: ignore[assignment]

    packet = _make_text_packet(content="sync-fail", msg_id="msg-sync-fail")
    # _on_packet catches exceptions internally, so it won't raise.
    adapter._on_packet(packet)

    dedup_key = ("msg-sync-fail", "sync-fail")
    assert dedup_key not in adapter._inbound_dedup

    adapter._codec.decode = original_decode
    await adapter.stop()


# ===================================================================
# LRU semantics: move_to_end on dedup hit, popitem(last=False) on
# successful insert
# ===================================================================


async def test_simulate_inbound_lru_recency_promotion(
    make_adapter_context,
    inbound_collector,
) -> None:
    """LRU recency promotion: move_to_end on dedup hit preserves access order."""

    config = _make_config(connection_type="fake")
    adapter = LxmfAdapter(config)
    ctx = make_adapter_context("lxmf-dedup-test")
    await adapter.start(ctx)

    # Insert additional entries alongside the real cap to verify
    # that a successful publish appends without evicting under the
    # 1024-entry ceiling, then verify recency ordering below.
    for i in range(3):
        key = (f"msg-{i}", f"content-{i}")
        adapter._inbound_dedup[key] = None

    assert len(adapter._inbound_dedup) == 3

    # This simulate_inbound appends a new key; no eviction expected
    # because the dedup cap is 1024 (well above 4 entries total).
    packet = _make_text_packet(content="extra", msg_id="msg-extra")
    await adapter.simulate_inbound(packet)

    # The new key should be present.
    assert ("msg-extra", "extra") in adapter._inbound_dedup

    # Now verify move_to_end recency promotion.
    adapter._inbound_dedup.clear()
    await adapter.simulate_inbound(
        _make_text_packet(content="first", msg_id="msg-first")
    )
    await adapter.simulate_inbound(
        _make_text_packet(content="second", msg_id="msg-second")
    )
    keys = list(adapter._inbound_dedup.keys())
    assert keys[0] == ("msg-first", "first")
    assert keys[1] == ("msg-second", "second")

    # Re-deliver first → moves to end.
    await adapter.simulate_inbound(
        _make_text_packet(content="first", msg_id="msg-first")
    )
    keys_after = list(adapter._inbound_dedup.keys())
    assert keys_after[0] == ("msg-second", "second")
    assert keys_after[1] == ("msg-first", "first")
    assert (
        len(inbound_collector.events) == 3
    )  # extra + first + second (duplicate first suppressed)

    await adapter.stop()


# ===================================================================
# stop/start clears dedup; redelivery after restart succeeds
# ===================================================================


async def test_stop_start_clears_dedup_allows_redelivery(
    make_adapter_context,
    inbound_collector,
) -> None:
    """After stop+start cycle, previously-seen packets are delivered again."""
    config = _make_config(connection_type="fake")
    adapter = LxmfAdapter(config)
    ctx = make_adapter_context("lxmf-dedup-test")

    packet = _make_text_packet(content="restart", msg_id="msg-restart")

    await adapter.start(ctx)
    await adapter.simulate_inbound(packet)
    assert len(inbound_collector.events) == 1

    await adapter.stop()

    # Restart — dedup is cleared.
    await adapter.start(ctx)
    await adapter.simulate_inbound(packet)
    assert len(inbound_collector.events) == 2

    await adapter.stop()
