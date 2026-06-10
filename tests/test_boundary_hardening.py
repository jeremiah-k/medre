"""Boundary hardening tests for MeshCore and LXMF adapters.

Validates the three implemented hardening checks (G1–G3):

* **G1** — MeshCore: inbound dedup by native identity + content (bounded
  LRU ``OrderedDict``).  Exact replays suppressed; distinct payloads with
  reused identity are processed.  Dedup is skipped when the classifier
  reports ``packet_id is None`` (missing ``sender_timestamp``).
* **G2** — LXMF: inbound dedup by ``message_id`` + content (bounded LRU
  ``OrderedDict``).  Same LRU hit-refresh semantics as MeshCore.
* **G3** — MeshCore / LXMF: ``_on_message`` / ``_on_packet`` cannot create
  tasks after ``stop()`` clears ``_started``.  Callback-after-stop guards
  are in place for both adapters.

Plus contract tests for metadata namespace and ``delivery_status`` values.

Evidence level: ``fake_pipeline`` / ``fake_adapter_callback`` (tier 1–2).
No network, no hardware, no Docker.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any

from medre.adapters.fakes.lxmf import FakeLxmfAdapter
from medre.adapters.fakes.meshcore import FakeMeshCoreAdapter
from medre.adapters.lxmf.adapter import LxmfAdapter
from medre.adapters.meshcore.adapter import MeshCoreAdapter
from medre.config.adapters.lxmf import LxmfConfig
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.core.contracts.adapter import (
    AdapterContext,
)
from medre.core.rendering.renderer import RenderingResult

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


def _lxmf_text_packet(
    content: str = "hello",
    source_hash: str = "ab" * 16,
    message_id: str = "ff" * 32,
    destination_hash: str = "00" * 16,
    timestamp: float = 1700000000.0,
) -> dict[str, Any]:
    """Build a minimal LXMF text packet that classifies as ``text``."""
    return {
        "content": content,
        "source_hash": source_hash,
        "destination_hash": destination_hash,
        "message_id": message_id,
        "timestamp": timestamp,
        "title": "",
        "fields": {},
        "signature_validated": True,
        "has_fields": False,
    }


def _make_rendering_result(
    adapter_id: str = "test",
    payload: dict[str, Any] | None = None,
) -> RenderingResult:
    return RenderingResult(
        event_id=_unique_id("evt"),
        target_adapter=adapter_id,
        target_channel="ch-0",
        payload=payload or {"body": "hello"},
    )


# ---------------------------------------------------------------------------
# G3 — Callback-after-stop guard: _on_message/_on_packet must not create
#       tasks once _started is False
# ---------------------------------------------------------------------------


async def test_meshcore_on_message_drops_after_started_false():
    """G3: _on_message must not create asyncio tasks when _started is False.

    Simulates the race window where ``_drain_background_tasks`` has completed
    but ``_session.stop()`` has not yet unsubscribed callbacks.  The adapter's
    ``_started`` flag is already cleared.
    """
    config = MeshCoreConfig(adapter_id=_unique_id("mc"), connection_type="fake")
    adapter = MeshCoreAdapter(config)

    published: list[object] = []

    async def track_publish(event: object) -> None:
        published.append(event)

    ctx = _make_context(adapter_id=adapter.adapter_id, publish_inbound=track_publish)
    await adapter.start(ctx)

    # Verify normal operation: _on_message creates a task for a relay packet.
    packet = _meshcore_text_packet()
    adapter._on_message(packet)

    # Let the async task run.
    await asyncio.sleep(0)
    # Drain background tasks so we have a clean slate.
    await adapter._drain_background_tasks(timeout=1.0)
    assert adapter._background_tasks == set(), "pre-condition: no lingering tasks"

    # --- Simulate the race: _started cleared (post-drain) but session
    #     callback still wired. ---
    adapter._started = False
    count_before = len(published)

    adapter._on_message(packet)

    # No new task should have been created.
    assert (
        adapter._background_tasks == set()
    ), "G3: _on_message must not create tasks when _started is False"
    assert (
        len(published) == count_before
    ), "G3: _on_message must not publish when _started is False"

    # Cleanup
    adapter._started = True
    await adapter.stop()


# ---------------------------------------------------------------------------
# G1 — MeshCore: inbound dedup (exact replays suppressed, LRU bounded)
# ---------------------------------------------------------------------------


async def test_meshcore_simulate_inbound_deduplicates_identical_packets():
    """G1: Sending the same MeshCore packet twice must publish only once.

    The dedup key is ``(sender_id, packet_id, channel_index, text)``.
    Exact native replays are suppressed; distinct payloads with a reused
    packet_id are both processed.
    """
    config = MeshCoreConfig(adapter_id=_unique_id("mc"), connection_type="fake")
    adapter = MeshCoreAdapter(config)

    published: list[object] = []

    async def track_publish(event: object) -> None:
        published.append(event)

    ctx = _make_context(adapter_id=adapter.adapter_id, publish_inbound=track_publish)
    await adapter.start(ctx)

    packet = _meshcore_text_packet(
        text="dup-test",
        pubkey_prefix="dedup01",
        sender_timestamp=99999,
        channel_idx=3,
    )

    await adapter.simulate_inbound(packet)
    await adapter.simulate_inbound(packet)

    assert len(published) == 1, (
        "G1: duplicate MeshCore packet (same pubkey_prefix, sender_timestamp, "
        f"channel_idx) must be suppressed; got {len(published)} publishes"
    )
    assert adapter._inbound_published == 1

    await adapter.stop()


async def test_meshcore_simulate_inbound_allows_different_packets():
    """G1: Different MeshCore packets must both be published."""
    config = MeshCoreConfig(adapter_id=_unique_id("mc"), connection_type="fake")
    adapter = MeshCoreAdapter(config)

    published: list[object] = []

    async def track_publish(event: object) -> None:
        published.append(event)

    ctx = _make_context(adapter_id=adapter.adapter_id, publish_inbound=track_publish)
    await adapter.start(ctx)

    await adapter.simulate_inbound(
        _meshcore_text_packet(pubkey_prefix="aaa", sender_timestamp=100, channel_idx=0)
    )
    await adapter.simulate_inbound(
        _meshcore_text_packet(pubkey_prefix="bbb", sender_timestamp=200, channel_idx=0)
    )

    assert (
        len(published) == 2
    ), f"G1: distinct packets must both publish; got {len(published)}"
    assert adapter._inbound_published == 2

    await adapter.stop()


async def test_meshcore_dedup_resets_on_restart():
    """G1: Inbound dedup set is cleared on stop/start, allowing replay."""
    config = MeshCoreConfig(adapter_id=_unique_id("mc"), connection_type="fake")
    adapter = MeshCoreAdapter(config)

    published: list[object] = []

    async def track_publish(event: object) -> None:
        published.append(event)

    ctx = _make_context(adapter_id=adapter.adapter_id, publish_inbound=track_publish)
    await adapter.start(ctx)

    packet = _meshcore_text_packet(pubkey_prefix="ccc", sender_timestamp=300)
    await adapter.simulate_inbound(packet)
    assert len(published) == 1

    await adapter.stop()

    # Restart with fresh context.
    ctx2 = _make_context(adapter_id=adapter.adapter_id, publish_inbound=track_publish)
    await adapter.start(ctx2)

    await adapter.simulate_inbound(packet)
    assert (
        len(published) == 2
    ), "G1: dedup set must be cleared on restart; same packet should publish again"

    await adapter.stop()


async def test_meshcore_on_message_deduplicates_via_callback():
    """G1: _on_message (sync callback path) must also deduplicate."""
    config = MeshCoreConfig(adapter_id=_unique_id("mc"), connection_type="fake")
    adapter = MeshCoreAdapter(config)

    published: list[object] = []

    async def track_publish(event: object) -> None:
        published.append(event)

    ctx = _make_context(adapter_id=adapter.adapter_id, publish_inbound=track_publish)
    await adapter.start(ctx)

    packet = _meshcore_text_packet(
        pubkey_prefix="onmsg_dup",
        sender_timestamp=55555,
        channel_idx=1,
    )

    # First callback: should create a task that publishes.
    adapter._on_message(packet)
    await asyncio.sleep(0)
    await adapter._drain_background_tasks(timeout=1.0)

    # Second callback with identical packet: should be deduped.
    adapter._on_message(packet)
    await asyncio.sleep(0)
    await adapter._drain_background_tasks(timeout=1.0)

    assert (
        len(published) == 1
    ), "G1: _on_message must deduplicate identical packets via callback path"

    await adapter.stop()


# ---------------------------------------------------------------------------
# G2 — LXMF: inbound dedup (exact replays suppressed, LRU bounded)
# ---------------------------------------------------------------------------


async def test_lxmf_simulate_inbound_deduplicates_identical_messages():
    """G2: Sending the same LXMF message twice must publish only once.

    The dedup key is ``message_id`` (hex string of message hash).
    """
    config = LxmfConfig(adapter_id=_unique_id("lx"), connection_type="fake")
    adapter = LxmfAdapter(config)

    published: list[object] = []

    async def track_publish(event: object) -> None:
        published.append(event)

    ctx = _make_context(adapter_id=adapter.adapter_id, publish_inbound=track_publish)
    await adapter.start(ctx)

    msg_id = "aa" * 32  # 64-char hex string
    packet = _lxmf_text_packet(
        content="dup-test",
        source_hash="cc" * 16,
        message_id=msg_id,
    )

    await adapter.simulate_inbound(packet)
    await adapter.simulate_inbound(packet)

    assert len(published) == 1, (
        "G2: duplicate LXMF message (same message_id) must be suppressed; "
        f"got {len(published)} publishes"
    )

    await adapter.stop()


async def test_lxmf_simulate_inbound_allows_different_messages():
    """G2: Different LXMF messages must both be published."""
    config = LxmfConfig(adapter_id=_unique_id("lx"), connection_type="fake")
    adapter = LxmfAdapter(config)

    published: list[object] = []

    async def track_publish(event: object) -> None:
        published.append(event)

    ctx = _make_context(adapter_id=adapter.adapter_id, publish_inbound=track_publish)
    await adapter.start(ctx)

    await adapter.simulate_inbound(
        _lxmf_text_packet(message_id="11" * 32, content="first")
    )
    await adapter.simulate_inbound(
        _lxmf_text_packet(message_id="22" * 32, content="second")
    )

    assert (
        len(published) == 2
    ), f"G2: distinct LXMF messages must both publish; got {len(published)}"

    await adapter.stop()


async def test_lxmf_dedup_resets_on_restart():
    """G2: Inbound dedup set is cleared on stop/start, allowing replay."""
    config = LxmfConfig(adapter_id=_unique_id("lx"), connection_type="fake")
    adapter = LxmfAdapter(config)

    published: list[object] = []

    async def track_publish(event: object) -> None:
        published.append(event)

    ctx = _make_context(adapter_id=adapter.adapter_id, publish_inbound=track_publish)
    await adapter.start(ctx)

    msg_id = "bb" * 32
    packet = _lxmf_text_packet(message_id=msg_id)
    await adapter.simulate_inbound(packet)
    assert len(published) == 1

    await adapter.stop()

    # Restart with fresh context.
    ctx2 = _make_context(adapter_id=adapter.adapter_id, publish_inbound=track_publish)
    await adapter.start(ctx2)

    await adapter.simulate_inbound(packet)
    assert (
        len(published) == 2
    ), "G2: dedup set must be cleared on restart; same message should publish again"

    await adapter.stop()


async def test_lxmf_on_packet_deduplicates_via_callback():
    """G2: _on_packet (sync callback path) must also deduplicate."""
    config = LxmfConfig(adapter_id=_unique_id("lx"), connection_type="fake")
    adapter = LxmfAdapter(config)

    published: list[object] = []

    async def track_publish(event: object) -> None:
        published.append(event)

    ctx = _make_context(adapter_id=adapter.adapter_id, publish_inbound=track_publish)
    await adapter.start(ctx)

    msg_id = "dd" * 32
    packet = _lxmf_text_packet(message_id=msg_id)

    # First callback: should create a task that publishes.
    adapter._on_packet(packet)
    await asyncio.sleep(0)
    await adapter._drain_background_tasks(timeout=1.0)

    # Second callback with identical packet: should be deduped.
    adapter._on_packet(packet)
    await asyncio.sleep(0)
    await adapter._drain_background_tasks(timeout=1.0)

    assert (
        len(published) == 1
    ), "G2: _on_packet must deduplicate identical messages via callback path"

    await adapter.stop()


# ---------------------------------------------------------------------------
# Metadata namespace and delivery_status contract tests
# ---------------------------------------------------------------------------


async def test_meshcore_deliver_metadata_namespace():
    """MeshCoreAdapter deliver() must namespace metadata under ``meshcore``."""
    adapter = FakeMeshCoreAdapter()
    ctx = _make_context(adapter_id=adapter.adapter_id)
    await adapter.start(ctx)

    result = await adapter.deliver(
        _make_rendering_result(
            adapter_id=adapter.adapter_id,
            payload={"text": "hello", "channel_index": 0},
        )
    )

    assert result is not None
    assert (
        "meshcore" in result.metadata
    ), "MeshCoreAdapter deliver() must namespace metadata under 'meshcore'"
    # No top-level MEDRE keys.
    assert "matrix" not in result.metadata
    assert "lxmf" not in result.metadata
    assert "meshtastic" not in result.metadata

    await adapter.stop()


async def test_meshcore_deliver_delivery_status_sent():
    """MeshCoreAdapter deliver() must return delivery_status='sent'."""
    adapter = FakeMeshCoreAdapter()
    ctx = _make_context(adapter_id=adapter.adapter_id)
    await adapter.start(ctx)

    result = await adapter.deliver(
        _make_rendering_result(
            adapter_id=adapter.adapter_id,
            payload={"text": "hello", "channel_index": 0},
        )
    )

    assert result is not None
    assert (
        result.delivery_status == "sent"
    ), "MeshCoreAdapter must return delivery_status='sent' (synchronous local acceptance)"

    await adapter.stop()


async def test_lxmf_deliver_metadata_namespace():
    """LxmfAdapter deliver() must namespace metadata under ``lxmf``."""
    adapter = FakeLxmfAdapter()
    ctx = _make_context(adapter_id=adapter.adapter_id)
    await adapter.start(ctx)

    result = await adapter.deliver(
        _make_rendering_result(
            adapter_id=adapter.adapter_id,
            payload={
                "content": "hello",
                "title": "",
                "destination_hash": "ab" * 16,
            },
        )
    )

    assert result is not None
    assert (
        "lxmf" in result.metadata
    ), "LxmfAdapter deliver() must namespace metadata under 'lxmf'"
    # No top-level MEDRE keys or other transport namespaces.
    assert "matrix" not in result.metadata
    assert "meshcore" not in result.metadata
    assert "meshtastic" not in result.metadata

    # LXMF metadata must contain delivery_state and delivery_method.
    lxmf_meta: Any = result.metadata["lxmf"]
    if isinstance(lxmf_meta, MappingProxyType):
        lxmf_meta = dict(lxmf_meta)
    assert isinstance(lxmf_meta, dict)
    assert "delivery_state" in lxmf_meta, "lxmf metadata must contain 'delivery_state'"
    assert (
        "delivery_method" in lxmf_meta
    ), "lxmf metadata must contain 'delivery_method'"

    await adapter.stop()


async def test_lxmf_deliver_delivery_status_sent():
    """FakeLxmfAdapter deliver() returns delivery_status default ('sent')."""
    adapter = FakeLxmfAdapter()
    ctx = _make_context(adapter_id=adapter.adapter_id)
    await adapter.start(ctx)

    result = await adapter.deliver(
        _make_rendering_result(
            adapter_id=adapter.adapter_id,
            payload={
                "content": "hello",
                "destination_hash": "ab" * 16,
            },
        )
    )

    assert result is not None
    # FakeLxmfAdapter uses the default delivery_status from
    # AdapterDeliveryResult which is "sent".
    assert result.delivery_status == "sent"

    await adapter.stop()


async def test_meshcore_deliver_empty_payload_returns_result():
    """MeshCoreAdapter: deliver with empty text still returns a result (documents actual behavior)."""
    adapter = FakeMeshCoreAdapter()
    ctx = _make_context(adapter_id=adapter.adapter_id)
    await adapter.start(ctx)

    result = await adapter.deliver(
        _make_rendering_result(
            adapter_id=adapter.adapter_id,
            payload={},  # no text, no channel_index
        )
    )

    # FakeMeshCoreAdapter sends empty text, so it does produce a result.
    # The real MeshCoreAdapter also returns a result because send_text
    # doesn't check for empty text. This test documents the actual behavior.
    assert (
        result is not None
    )  # FakeMeshCoreAdapter produces a result even with empty payload

    await adapter.stop()


async def test_lxmf_deliver_empty_payload_returns_result():
    """LxmfAdapter: deliver with no content and no title returns a result (documents fake behavior)."""
    adapter = FakeLxmfAdapter()
    ctx = _make_context(adapter_id=adapter.adapter_id)
    await adapter.start(ctx)

    result = await adapter.deliver(
        _make_rendering_result(
            adapter_id=adapter.adapter_id,
            payload={"destination_hash": "ab" * 16},
        )
    )

    # FakeLxmfAdapter doesn't have the not content and not title guard
    # that real LxmfAdapter has, so it returns a result rather than None.
    assert result is not None  # FakeLxmfAdapter lacks the empty-content guard

    await adapter.stop()


# ---------------------------------------------------------------------------
# Callback-after-stop boundary tests
# ---------------------------------------------------------------------------


async def test_meshcore_ctx_preserved_after_stop():
    """MeshCoreAdapter: after stop, ctx is still set; simulate_inbound
    does not raise RuntimeError (ctx is not cleared).

    This test documents actual behavior: real adapters do not clear ctx
    on stop, so the RuntimeError guard in simulate_inbound does not fire.
    """
    config = MeshCoreConfig(adapter_id=_unique_id("mc"), connection_type="fake")
    adapter = MeshCoreAdapter(config)

    ctx = _make_context(adapter_id=adapter.adapter_id)
    await adapter.start(ctx)
    await adapter.stop()

    # ctx is not cleared by stop(), so simulate_inbound won't raise RuntimeError.
    # This documents the current behavior.
    assert adapter.ctx is not None, "ctx is preserved after stop()"


async def test_lxmf_ctx_preserved_after_stop():
    """LxmfAdapter: after stop, ctx is still set; simulate_inbound
    does not raise RuntimeError (ctx is not cleared).

    This test documents actual behavior: real adapters do not clear ctx
    on stop, so the RuntimeError guard in simulate_inbound does not fire.
    """
    config = LxmfConfig(adapter_id=_unique_id("lx"), connection_type="fake")
    adapter = LxmfAdapter(config)

    ctx = _make_context(adapter_id=adapter.adapter_id)
    await adapter.start(ctx)
    await adapter.stop()

    # ctx is not cleared by stop(), so simulate_inbound won't raise RuntimeError.
    assert adapter.ctx is not None, "ctx is preserved after stop()"


async def test_lxmf_on_packet_drops_after_stop():
    """LXMF _on_packet must not create tasks after stop drains them."""
    config = LxmfConfig(adapter_id=_unique_id("lx"), connection_type="fake")
    adapter = LxmfAdapter(config)

    published: list[object] = []

    async def track_publish(event: object) -> None:
        published.append(event)

    ctx = _make_context(adapter_id=adapter.adapter_id, publish_inbound=track_publish)
    await adapter.start(ctx)

    # Verify normal operation.
    packet = _lxmf_text_packet(message_id="ee" * 32)
    adapter._on_packet(packet)
    await asyncio.sleep(0)
    await adapter._drain_background_tasks(timeout=1.0)
    assert len(published) == 1

    # Simulate post-stop state.
    adapter._started = False
    count_before = len(published)

    adapter._on_packet(packet)

    assert (
        adapter._background_tasks == set()
    ), "LXMF: _on_packet must not create tasks after _started=False"
    assert (
        len(published) == count_before
    ), "LXMF: _on_packet must not publish after _started=False"

    # Cleanup
    adapter._started = True
    await adapter.stop()


async def test_meshcore_on_message_async_checks_ctx_on_publish():
    """MeshCore _on_message_async must not publish if ctx is cleared."""
    config = MeshCoreConfig(adapter_id=_unique_id("mc"), connection_type="fake")
    adapter = MeshCoreAdapter(config)

    published: list[object] = []

    async def track_publish(event: object) -> None:
        published.append(event)

    ctx = _make_context(adapter_id=adapter.adapter_id, publish_inbound=track_publish)
    await adapter.start(ctx)

    packet = _meshcore_text_packet()

    # Create task via _on_message.
    adapter._on_message(packet)
    assert len(adapter._background_tasks) == 1

    # Clear ctx before the async task runs.
    original_ctx = adapter.ctx
    adapter.ctx = None
    await asyncio.sleep(0)
    await adapter._drain_background_tasks(timeout=1.0)

    # The async handler checks ctx, so nothing should publish.
    assert len(published) == 0, "_on_message_async must not publish when ctx is None"

    # Cleanup: restore ctx before stop so teardown is clean.
    adapter.ctx = original_ctx
    await adapter.stop()


# ---------------------------------------------------------------------------
# Bounded dedup eviction tests
# ---------------------------------------------------------------------------


async def test_meshcore_dedup_evicts_oldest_at_capacity():
    """MeshCore dedup evicts the oldest entry when it exceeds capacity.

    Fills the dedup beyond its max size and verifies that:
    1. The oldest entry is evicted (replaying it succeeds).
    2. Recent entries are still suppressed.
    """
    from medre.adapters.meshcore.adapter import _DEDUP_MAX_SIZE

    config = MeshCoreConfig(adapter_id=_unique_id("mc"), connection_type="fake")
    adapter = MeshCoreAdapter(config)

    published: list[object] = []

    async def track_publish(event: object) -> None:
        published.append(event)

    ctx = _make_context(adapter_id=adapter.adapter_id, publish_inbound=track_publish)
    await adapter.start(ctx)

    first_packet = _meshcore_text_packet(
        text="first",
        pubkey_prefix="evict",
        sender_timestamp=1,
        channel_idx=0,
    )
    await adapter.simulate_inbound(first_packet)
    assert len(published) == 1

    # Fill with enough distinct packets to exceed capacity.
    for i in range(_DEDUP_MAX_SIZE + 1):
        await adapter.simulate_inbound(
            _meshcore_text_packet(
                text=f"fill-{i}",
                pubkey_prefix="fill",
                sender_timestamp=1000 + i,
                channel_idx=0,
            )
        )

    # The first packet's dedup entry should have been evicted, so it
    # publishes again.
    await adapter.simulate_inbound(first_packet)
    assert (
        len(published) >= 3
    ), "first packet should publish again after eviction from bounded dedup"

    # The most recent fill packet should still be deduped.
    last_fill_packet = _meshcore_text_packet(
        text=f"fill-{_DEDUP_MAX_SIZE}",
        pubkey_prefix="fill",
        sender_timestamp=1000 + _DEDUP_MAX_SIZE,
        channel_idx=0,
    )
    count_before = len(published)
    await adapter.simulate_inbound(last_fill_packet)
    assert (
        len(published) == count_before
    ), "recent entry must still be deduped within capacity"

    await adapter.stop()


async def test_lxmf_dedup_evicts_oldest_at_capacity():
    """LXMF dedup evicts the oldest entry when it exceeds capacity."""
    from medre.adapters.lxmf.adapter import _DEDUP_MAX_SIZE

    config = LxmfConfig(adapter_id=_unique_id("lx"), connection_type="fake")
    adapter = LxmfAdapter(config)

    published: list[object] = []

    async def track_publish(event: object) -> None:
        published.append(event)

    ctx = _make_context(adapter_id=adapter.adapter_id, publish_inbound=track_publish)
    await adapter.start(ctx)

    first_msg_id = "00" * 32
    first_packet = _lxmf_text_packet(content="first", message_id=first_msg_id)
    await adapter.simulate_inbound(first_packet)
    assert len(published) == 1

    # Fill with enough distinct packets to exceed capacity.
    for i in range(_DEDUP_MAX_SIZE + 1):
        await adapter.simulate_inbound(
            _lxmf_text_packet(
                content=f"fill-{i}",
                message_id=f"{i:064x}",
            )
        )

    # The first packet's dedup entry should have been evicted.
    await adapter.simulate_inbound(first_packet)
    assert (
        len(published) >= 3
    ), "first message should publish again after eviction from bounded dedup"

    # The most recent fill packet should still be deduped.
    last_fill_id = f"{_DEDUP_MAX_SIZE:064x}"
    last_fill_packet = _lxmf_text_packet(
        content=f"fill-{_DEDUP_MAX_SIZE}",
        message_id=last_fill_id,
    )
    count_before = len(published)
    await adapter.simulate_inbound(last_fill_packet)
    assert (
        len(published) == count_before
    ), "recent entry must still be deduped within capacity"

    await adapter.stop()


# ---------------------------------------------------------------------------
# Early stop gating tests
# ---------------------------------------------------------------------------


async def test_meshcore_stop_gates_callbacks_before_drain():
    """MeshCore stop() sets _started=False before draining tasks.

    This means _on_message cannot create new tasks after stop() begins,
    even before _drain_background_tasks completes.
    """
    config = MeshCoreConfig(adapter_id=_unique_id("mc"), connection_type="fake")
    adapter = MeshCoreAdapter(config)

    published: list[object] = []

    async def track_publish(event: object) -> None:
        published.append(event)

    ctx = _make_context(adapter_id=adapter.adapter_id, publish_inbound=track_publish)
    await adapter.start(ctx)

    # Inject a packet via _on_message to create a background task.
    packet = _meshcore_text_packet()
    adapter._on_message(packet)
    assert len(adapter._background_tasks) == 1

    # stop() should set _started=False first, then drain.
    # After stop(), _started must be False.
    await adapter.stop()
    assert not adapter._started, "stop() must clear _started"

    # A late callback after stop must not create tasks.
    adapter._on_message(packet)
    assert (
        adapter._background_tasks == set()
    ), "no new tasks from _on_message after stop()"


async def test_lxmf_stop_gates_callbacks_before_drain():
    """LXMF stop() sets _started=False before draining tasks."""
    config = LxmfConfig(adapter_id=_unique_id("lx"), connection_type="fake")
    adapter = LxmfAdapter(config)

    published: list[object] = []

    async def track_publish(event: object) -> None:
        published.append(event)

    ctx = _make_context(adapter_id=adapter.adapter_id, publish_inbound=track_publish)
    await adapter.start(ctx)

    packet = _lxmf_text_packet()
    adapter._on_packet(packet)
    assert len(adapter._background_tasks) == 1

    await adapter.stop()
    assert not adapter._started, "stop() must clear _started"

    adapter._on_packet(packet)
    assert (
        adapter._background_tasks == set()
    ), "no new tasks from _on_packet after stop()"


# ---------------------------------------------------------------------------
# G1 — MeshCore: missing native identity skips adapter-level dedup
# ---------------------------------------------------------------------------


async def test_meshcore_no_sender_timestamp_skips_dedup():
    """MeshCore packets without ``sender_timestamp`` skip adapter-level dedup.

    When the classifier reports ``packet_id is None`` (no native identity),
    the adapter cannot construct a dedup key and skips dedup entirely.
    Repeated identical routeable packets must therefore both publish.
    """
    config = MeshCoreConfig(adapter_id=_unique_id("mc"), connection_type="fake")
    adapter = MeshCoreAdapter(config)

    published: list[object] = []

    async def track_publish(event: object) -> None:
        published.append(event)

    ctx = _make_context(adapter_id=adapter.adapter_id, publish_inbound=track_publish)
    await adapter.start(ctx)

    # Build a routeable packet WITHOUT sender_timestamp — the classifier
    # will set packet_id=None, causing the adapter to skip dedup.
    packet_no_ts: dict[str, Any] = {
        "text": "no-timestamp",
        "pubkey_prefix": "notime",
        "type": "CHAN",
        "txt_type": 0,
        "channel_idx": 0,
        # sender_timestamp deliberately omitted
    }

    await adapter.simulate_inbound(packet_no_ts)
    await adapter.simulate_inbound(packet_no_ts)

    assert (
        len(published) == 2
    ), "identical packets without sender_timestamp must both publish (dedup skipped)"

    await adapter.stop()


# ---------------------------------------------------------------------------
# G1 — MeshCore: true LRU hit-refresh (move_to_end promotes on re-hit)
# ---------------------------------------------------------------------------


async def test_meshcore_dedup_is_true_lru_not_fifo():
    """MeshCore dedup is true LRU: re-hitting a packet promotes it via move_to_end.

    Strategy:
    1. Insert a "hot" packet, then fill beyond capacity while re-hitting it
       each iteration (promoting it via move_to_end).
    2. Verify the hot packet is still suppressed after the fill (it was
       promoted to the MRU end, never evicted).
    3. Verify the oldest *non-refreshed* fill packet was evicted — replaying
       it publishes again.
    """
    from medre.adapters.meshcore.adapter import _DEDUP_MAX_SIZE

    config = MeshCoreConfig(adapter_id=_unique_id("mc"), connection_type="fake")
    adapter = MeshCoreAdapter(config)

    published: list[object] = []

    async def track_publish(event: object) -> None:
        published.append(event)

    ctx = _make_context(adapter_id=adapter.adapter_id, publish_inbound=track_publish)
    await adapter.start(ctx)

    # 1. Insert the "hot" packet that we will keep re-hitting.
    hot_packet = _meshcore_text_packet(
        text="hot",
        pubkey_prefix="hot",
        sender_timestamp=0,
        channel_idx=0,
    )
    await adapter.simulate_inbound(hot_packet)
    hot_count = len(published)
    assert hot_count == 1, "hot packet must publish on first arrival"

    # 2. Fill well beyond capacity, re-hitting the hot packet each time
    #    so move_to_end promotes it above the eviction boundary.
    for i in range(_DEDUP_MAX_SIZE + 10):
        # Re-hit hot packet (dedup suppressed, move_to_end promoted).
        await adapter.simulate_inbound(hot_packet)
        # Insert a distinct fill packet.
        await adapter.simulate_inbound(
            _meshcore_text_packet(
                text=f"fill-{i}",
                pubkey_prefix="fill",
                sender_timestamp=10_000 + i,
                channel_idx=0,
            )
        )

    # Hot packet must still be suppressed (promoted, not evicted).
    # Re-hit it now: if LRU promoted it correctly, this is a dedup hit
    # and publish count should not increase.
    count_before = len(published)
    await adapter.simulate_inbound(hot_packet)
    assert (
        len(published) == count_before
    ), "hot packet must remain suppressed — true LRU promoted it"

    # 3. The oldest fill packet (fill-0) should have been evicted because
    #    it was never re-hit.  Replaying it must publish again.
    oldest_fill = _meshcore_text_packet(
        text="fill-0",
        pubkey_prefix="fill",
        sender_timestamp=10_000,
        channel_idx=0,
    )
    count_before = len(published)
    await adapter.simulate_inbound(oldest_fill)
    assert (
        len(published) > count_before
    ), "oldest non-refreshed fill packet must have been evicted from LRU"

    await adapter.stop()


# ---------------------------------------------------------------------------
# G2 — LXMF: true LRU hit-refresh (move_to_end promotes on re-hit)
# ---------------------------------------------------------------------------


async def test_lxmf_dedup_is_true_lru_not_fifo():
    """LXMF dedup is true LRU: re-hitting a message promotes it via move_to_end.

    Same strategy as MeshCore LRU test: keep a "hot" message alive by
    re-hitting while filling beyond capacity, then verify it survives
    while the oldest non-refreshed message is evicted.
    """
    from medre.adapters.lxmf.adapter import _DEDUP_MAX_SIZE as LXMF_DEDUP_MAX

    config = LxmfConfig(adapter_id=_unique_id("lx"), connection_type="fake")
    adapter = LxmfAdapter(config)

    published: list[object] = []

    async def track_publish(event: object) -> None:
        published.append(event)

    ctx = _make_context(adapter_id=adapter.adapter_id, publish_inbound=track_publish)
    await adapter.start(ctx)

    # 1. Insert the "hot" message.
    hot_msg_id = "ff" * 32
    hot_packet = _lxmf_text_packet(content="hot", message_id=hot_msg_id)
    await adapter.simulate_inbound(hot_packet)
    hot_count = len(published)
    assert hot_count == 1, "hot message must publish on first arrival"

    # 2. Fill well beyond capacity, re-hitting hot each time.
    for i in range(LXMF_DEDUP_MAX + 10):
        # Re-hit hot message (dedup suppressed, move_to_end promoted).
        await adapter.simulate_inbound(hot_packet)
        # Insert a distinct fill message.
        await adapter.simulate_inbound(
            _lxmf_text_packet(
                content=f"fill-{i}",
                message_id=f"{(i + 1):064x}",
            )
        )

    # Hot message must still be suppressed.
    # Re-hit it now: if LRU promoted it correctly, this is a dedup hit
    # and publish count should not increase.
    count_before = len(published)
    await adapter.simulate_inbound(hot_packet)
    assert (
        len(published) == count_before
    ), "hot message must remain suppressed — true LRU promoted it"

    # 3. Oldest fill message (fill-0, message_id = 0...01) should have
    #    been evicted.  Replaying it must publish again.
    oldest_fill = _lxmf_text_packet(
        content="fill-0",
        message_id=f"{1:064x}",
    )
    count_before = len(published)
    await adapter.simulate_inbound(oldest_fill)
    assert (
        len(published) > count_before
    ), "oldest non-refreshed fill message must have been evicted from LRU"

    await adapter.stop()
