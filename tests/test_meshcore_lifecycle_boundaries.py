"""MeshCore lifecycle boundary tests: stop/start dedup clearing, simulate_inbound guards.

Validates that MeshCoreAdapter correctly handles lifecycle transitions at the
inbound processing boundary:

* ``stop()`` clears ``_inbound_dedup`` independently of restart.
* ``simulate_inbound`` raises ``RuntimeError`` before first ``start()``.
* ``simulate_inbound`` publishes normally while started.
* ``simulate_inbound`` silently returns after ``stop()`` (``_started`` guard).
* After stop+start, previously seen packets publish again (dedup cleared).

Evidence level: ``fake_pipeline`` / ``fake_adapter_callback`` (tier 1–2).
No network, no hardware, no Docker.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

import pytest

from medre.adapters.meshcore.adapter import MeshCoreAdapter
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.core.contracts.adapter import (
    AdapterContext,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_COUNT = itertools.count()


def _unique_id(prefix: str = "id") -> str:
    return f"{prefix}-{next(_COUNT)}"


def _make_context(
    adapter_id: str = "test",
    publish_inbound: Callable[..., Awaitable[None]] | None = None,
) -> AdapterContext:
    return AdapterContext(
        adapter_id=adapter_id,
        event_bus=None,
        publish_inbound=publish_inbound or _async_noop,
        logger=logging.getLogger(f"test.{adapter_id}"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


async def _async_noop(event: object) -> None:
    pass


def _meshcore_text_packet(
    text: str = "hello",
    pubkey_prefix: str = "abc123",
    sender_timestamp: int = 12345,
    channel_idx: int = 0,
    msg_type: str = "CHAN",
) -> dict[str, Any]:
    """Build a minimal MeshCore text packet that classifies as ``relay``."""
    return {
        "text": text,
        "pubkey_prefix": pubkey_prefix,
        "sender_timestamp": sender_timestamp,
        "type": msg_type,
        "txt_type": 0,
        "channel_idx": channel_idx,
    }


# ---------------------------------------------------------------------------
# MeshCore lifecycle: stop-only dedup clear, simulate_inbound lifecycle
# ---------------------------------------------------------------------------


async def test_meshcore_stop_clears_dedup_without_restart():
    """MeshCore stop() clears _inbound_dedup independently of restart.

    After stop(), the dedup OrderedDict must be empty — no restart is
    required for the clearing to take effect.
    """
    config = MeshCoreConfig(adapter_id=_unique_id("mc"), connection_type="fake")
    adapter = MeshCoreAdapter(config)

    published: list[object] = []

    async def track_publish(event: object) -> None:
        published.append(event)

    ctx = _make_context(adapter_id=adapter.adapter_id, publish_inbound=track_publish)
    await adapter.start(ctx)

    # Publish a packet so dedup has an entry.
    packet = _meshcore_text_packet(
        text="dedup-clear",
        pubkey_prefix="clear",
        sender_timestamp=77777,
    )
    await adapter.simulate_inbound(packet)
    assert len(published) == 1
    assert len(adapter._inbound_dedup) == 1, "dedup must contain one entry"

    # stop() must clear the dedup.
    await adapter.stop()
    assert (
        len(adapter._inbound_dedup) == 0
    ), "stop() must clear _inbound_dedup without requiring restart"


async def test_meshcore_simulate_inbound_raises_before_start():
    """MeshCore simulate_inbound raises RuntimeError before first start().

    When ctx is None (adapter never started), simulate_inbound must raise.
    """
    config = MeshCoreConfig(adapter_id=_unique_id("mc"), connection_type="fake")
    adapter = MeshCoreAdapter(config)

    assert adapter.ctx is None
    assert not adapter._started

    packet = _meshcore_text_packet()

    with pytest.raises(RuntimeError, match="has not been started"):
        await adapter.simulate_inbound(packet)


async def test_meshcore_simulate_inbound_publishes_while_started():
    """MeshCore simulate_inbound publishes normally while adapter is started."""
    config = MeshCoreConfig(adapter_id=_unique_id("mc"), connection_type="fake")
    adapter = MeshCoreAdapter(config)

    published: list[object] = []

    async def track_publish(event: object) -> None:
        published.append(event)

    ctx = _make_context(adapter_id=adapter.adapter_id, publish_inbound=track_publish)
    await adapter.start(ctx)

    packet = _meshcore_text_packet(
        text="active", pubkey_prefix="alive", sender_timestamp=11111
    )
    await adapter.simulate_inbound(packet)

    assert len(published) == 1, "simulate_inbound must publish while started"
    assert adapter._inbound_published == 1

    await adapter.stop()


async def test_meshcore_simulate_inbound_silent_after_stop():
    """MeshCore simulate_inbound silently returns after stop().

    After stop(), ctx is retained but _started is False.  simulate_inbound
    must not raise RuntimeError (ctx is not None) and must not publish
    (the _started guard prevents it).
    """
    config = MeshCoreConfig(adapter_id=_unique_id("mc"), connection_type="fake")
    adapter = MeshCoreAdapter(config)

    published: list[object] = []

    async def track_publish(event: object) -> None:
        published.append(event)

    ctx = _make_context(adapter_id=adapter.adapter_id, publish_inbound=track_publish)
    await adapter.start(ctx)

    # Publish while started to establish baseline.
    packet = _meshcore_text_packet(
        text="before-stop", pubkey_prefix="pre", sender_timestamp=22222
    )
    await adapter.simulate_inbound(packet)
    assert len(published) == 1
    assert adapter._inbound_published == 1

    await adapter.stop()

    # ctx is retained but _started is False.
    assert adapter.ctx is not None
    assert not adapter._started

    # simulate_inbound must silently return without raising or publishing.
    await adapter.simulate_inbound(packet)
    assert len(published) == 1, "simulate_inbound must not publish after stop()"
    assert (
        adapter._inbound_published == 1
    ), "inbound_published must not increment after stop()"


async def test_meshcore_restart_allows_same_packet_to_publish():
    """MeshCore: after stop+start, the same packet publishes again.

    Verifies that stop() clears dedup and start() clears counters,
    so a previously seen packet is treated as fresh after restart.
    """
    config = MeshCoreConfig(adapter_id=_unique_id("mc"), connection_type="fake")
    adapter = MeshCoreAdapter(config)

    published: list[object] = []

    async def track_publish(event: object) -> None:
        published.append(event)

    ctx = _make_context(adapter_id=adapter.adapter_id, publish_inbound=track_publish)
    await adapter.start(ctx)

    packet = _meshcore_text_packet(
        text="restart", pubkey_prefix="rst", sender_timestamp=44444
    )
    await adapter.simulate_inbound(packet)
    assert len(published) == 1
    assert adapter._inbound_published == 1

    # Dedup should suppress the duplicate.
    await adapter.simulate_inbound(packet)
    assert len(published) == 1

    await adapter.stop()
    # stop() cleared dedup; start() will also clear counters via _reset_inbound_counters.

    ctx2 = _make_context(adapter_id=adapter.adapter_id, publish_inbound=track_publish)
    await adapter.start(ctx2)

    # Same packet must publish again after restart.
    await adapter.simulate_inbound(packet)
    assert len(published) == 2, "same packet must publish again after stop+start"
    assert (
        adapter._inbound_published == 1
    ), "inbound_published reset to 0 on start, now 1"

    await adapter.stop()
