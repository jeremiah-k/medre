"""MeshCore ingress identity regression.

Exercises the production ingress seam end to end: the real
:class:`MeshCoreAdapter` (fake connection — the SDK-free hook), wired
exactly like ``runtime/app.py`` wires live adapters
(``ctx.publish_inbound`` → ``runner.admit_ingress(event, "live")``),
against real SQLite storage and real routing/delivery.

MeshCore received payloads carry no native message identifier — only a
sender-assigned one-second ``sender_timestamp``.  These tests pin the
observable identity contract at durable admission:

* distinct same-second messages from one sender/channel are admitted,
  persisted, and delivered;
* sender, channel, sub-type, and text differences can never alias two
  messages onto one native ref;
* a genuine retransmission keeps one identity across adapter restarts
  (durable admission suppresses the replay);
* input with no ``sender_timestamp`` claims no native identity at all
  (never a shared placeholder key);
* two messages indistinguishable on the wire (identical sender, channel,
  timestamp, sub-type, and text) are treated as one retransmission — the
  documented protocol ambiguity, not an exactly-once guarantee.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from medre.adapters.fakes.meshcore import FakeMeshCoreAdapter
from medre.adapters.meshcore.adapter import MeshCoreAdapter
from medre.adapters.meshcore.renderer import MeshCoreRenderer
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.core.contracts.adapter import AdapterContext
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.events.bus import EventBus
from medre.core.planning.fallback_resolution import FallbackResolver
from medre.core.planning.relation_resolution import RelationResolver
from medre.core.rendering.renderer import RenderingPipeline
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage.sqlite.storage import SQLiteStorage

MC_IN = "mc-identity-in"
MC_OUT = "mc-identity-out"
_TS = 1_750_000_000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _channel_packet(
    *,
    text: str,
    sender: str = "chan-sender",
    timestamp: int = _TS,
    channel_idx: int = 0,
    txt_type: int = 0,
) -> dict[str, Any]:
    """CHANNEL_MSG_RECV-shaped payload (channel broadcast text)."""
    return {
        "text": text,
        "channel_idx": channel_idx,
        "sender_timestamp": timestamp,
        "type": "CHAN",
        "txt_type": txt_type,
        "pubkey_prefix": sender,
    }


def _dm_packet(
    *,
    text: str,
    sender: str,
    timestamp: int = _TS,
    txt_type: int = 0,
) -> dict[str, Any]:
    """CONTACT_MSG_RECV-shaped payload (direct text message)."""
    return {
        "text": text,
        "pubkey_prefix": sender,
        "sender_timestamp": timestamp,
        "type": "PRIV",
        "txt_type": txt_type,
    }


def _make_context(adapter_id: str, publish: Any) -> AdapterContext:
    return AdapterContext(
        adapter_id=adapter_id,
        publish_inbound=publish,
        logger=logging.getLogger(f"test.{adapter_id}"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


@asynccontextmanager
async def _admission_stack(temp_storage: SQLiteStorage):
    """Real adapter + durable admission + real routing/delivery stack.

    Mirrors the production wiring: the inbound adapter's
    ``ctx.publish_inbound`` performs durable admission
    (``runner.admit_ingress(event, "live")``); claimed work items are
    routed via ``runner.process_admitted_event`` exactly as the durable
    ingress worker does.
    """
    in_adapter = MeshCoreAdapter(
        MeshCoreConfig(adapter_id=MC_IN, connection_type="fake")
    )
    out_adapter = FakeMeshCoreAdapter(MeshCoreConfig(adapter_id=MC_OUT))

    route = Route(
        id="mc-identity-route",
        source=RouteSource(
            adapter=MC_IN,
            event_kinds=("message.created",),
            channel="0",
        ),
        targets=[RouteTarget(adapter=MC_OUT, channel="0")],
    )

    rp = RenderingPipeline()
    rp.register(
        MeshCoreRenderer(configs={MC_OUT: MeshCoreConfig(adapter_id=MC_OUT)}),
        priority=50,
    )
    rp.register_adapter_platform(MC_OUT, "meshcore")
    rp.register(TextRenderer(), priority=100)

    runner = PipelineRunner(
        PipelineConfig(
            storage=temp_storage,
            router=Router(routes=[route]),
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={MC_IN: in_adapter, MC_OUT: out_adapter},
            rendering_pipeline=rp,
            event_bus=EventBus(),
        )
    )
    await runner.start()

    async def _publish(event: Any) -> None:
        await runner.admit_ingress(event, "live")

    async def _sink(_event: Any) -> None:
        return None

    await in_adapter.start(_make_context(MC_IN, _publish))
    await out_adapter.start(_make_context(MC_OUT, _sink))
    try:
        yield in_adapter, out_adapter, runner
    finally:
        await in_adapter.stop()
        await out_adapter.stop()
        await runner.stop()


async def _drain_pending(runner: PipelineRunner, storage: SQLiteStorage) -> list[str]:
    """Claim pending durable ingress work and route it (worker protocol)."""
    items = await storage.claim_ingress_work(
        worker_id="mc-identity-regression", limit=16
    )
    for item in items:
        await runner.process_admitted_event(item.event_id)
    return [item.event_id for item in items]


async def _inbound_native_rows(storage: SQLiteStorage) -> list[dict[str, Any]]:
    return await storage._read_all(
        "SELECT event_id, native_channel_id, native_message_id "
        "FROM native_message_refs WHERE direction = 'inbound'"
    )


async def _stored_bodies(storage: SQLiteStorage) -> set[str]:
    rows = await storage._read_all("SELECT payload FROM canonical_events")
    return {json.loads(row["payload"])["body"] for row in rows}


def _delivered_texts(out_adapter: FakeMeshCoreAdapter) -> list[str]:
    return [
        str(result.payload.get("text", "")) for result in out_adapter.delivered_payloads
    ]


# ---------------------------------------------------------------------------
# Distinct same-second messages
# ---------------------------------------------------------------------------


async def test_same_timestamp_distinct_text_both_admitted_stored_delivered(
    temp_storage: SQLiteStorage,
) -> None:
    """Two distinct messages sent in the same second by one sender on one
    channel are both durably admitted, persisted, and delivered.

    This is the core regression: the sender_timestamp alone is not a
    message identifier, so it must not become the durable idempotency key.
    """
    async with _admission_stack(temp_storage) as (in_adapter, out_adapter, runner):
        await in_adapter.simulate_inbound(_channel_packet(text="alpha", timestamp=_TS))
        await in_adapter.simulate_inbound(_channel_packet(text="bravo", timestamp=_TS))

        routed = await _drain_pending(runner, temp_storage)

        assert len(routed) == 2, "both same-second messages must become work"
        assert await _stored_bodies(temp_storage) == {"alpha", "bravo"}

        rows = await _inbound_native_rows(temp_storage)
        assert len(rows) == 2
        assert (
            len({row["native_message_id"] for row in rows}) == 2
        ), "distinct messages must not share a native ref"
        assert len({row["event_id"] for row in rows}) == 2

        texts = _delivered_texts(out_adapter)
        assert len(texts) == 2
        assert any("alpha" in t for t in texts)
        assert any("bravo" in t for t in texts)


async def test_same_timestamp_same_text_different_senders_cannot_alias(
    temp_storage: SQLiteStorage,
) -> None:
    """Two direct messages with identical text in the same second from
    different senders are distinct messages, not duplicates."""
    async with _admission_stack(temp_storage) as (in_adapter, _out, runner):
        await in_adapter.simulate_inbound(
            _dm_packet(text="ping", sender="aaaaaa", timestamp=_TS)
        )
        await in_adapter.simulate_inbound(
            _dm_packet(text="ping", sender="bbbbbb", timestamp=_TS)
        )

        routed = await _drain_pending(runner, temp_storage)
        assert len(routed) == 2, "sender difference must prevent aliasing"

        rows = await _inbound_native_rows(temp_storage)
        assert len(rows) == 2
        assert len({row["native_message_id"] for row in rows}) == 2
        assert {row["native_channel_id"] for row in rows} == {None}


async def test_same_timestamp_same_text_different_channels_cannot_alias(
    temp_storage: SQLiteStorage,
) -> None:
    """Identical text in the same second on different channels stays
    distinct end to end."""
    async with _admission_stack(temp_storage) as (in_adapter, out_adapter, runner):
        await in_adapter.simulate_inbound(
            _channel_packet(text="net check", channel_idx=0, timestamp=_TS)
        )
        await in_adapter.simulate_inbound(
            _channel_packet(text="net check", channel_idx=1, timestamp=_TS)
        )

        routed = await _drain_pending(runner, temp_storage)
        assert len(routed) == 2, "channel difference must prevent aliasing"

        rows = await _inbound_native_rows(temp_storage)
        assert len(rows) == 2
        assert len({row["native_message_id"] for row in rows}) == 2
        assert {row["native_channel_id"] for row in rows} == {"0", "1"}

        # Only channel 0 is routed in this stack; both events persist.
        assert await _stored_bodies(temp_storage) == {"net check"}
        assert len(_delivered_texts(out_adapter)) == 1


async def test_same_timestamp_different_txt_type_cannot_alias(
    temp_storage: SQLiteStorage,
) -> None:
    """A plain and a signed variant (different txt_type) of otherwise
    identical input are distinct messages."""
    async with _admission_stack(temp_storage) as (in_adapter, _out, runner):
        await in_adapter.simulate_inbound(
            _channel_packet(text="signed?", txt_type=0, timestamp=_TS)
        )
        await in_adapter.simulate_inbound(
            _channel_packet(text="signed?", txt_type=2, timestamp=_TS)
        )

        routed = await _drain_pending(runner, temp_storage)
        assert len(routed) == 2, "txt_type difference must prevent aliasing"

        rows = await _inbound_native_rows(temp_storage)
        assert len({row["native_message_id"] for row in rows}) == 2


# ---------------------------------------------------------------------------
# Retransmission stability
# ---------------------------------------------------------------------------


async def test_retransmission_after_adapter_restart_suppressed_durably(
    temp_storage: SQLiteStorage,
) -> None:
    """A repeated genuine retransmission keeps one identity across an
    adapter restart: durable admission suppresses the replay."""
    packet = _channel_packet(text="only once", timestamp=_TS)

    async with _admission_stack(temp_storage) as (in_adapter, out_adapter, runner):
        await in_adapter.simulate_inbound(packet)
        assert len(await _drain_pending(runner, temp_storage)) == 1

        # Restart: fresh adapter instance (empty in-memory dedup), same
        # adapter identity and storage — only the durable native-ref
        # idempotency key can suppress the replay now.
        restarted = MeshCoreAdapter(
            MeshCoreConfig(adapter_id=MC_IN, connection_type="fake")
        )

        async def _publish(event: Any) -> None:
            await runner.admit_ingress(event, "live")

        await restarted.start(_make_context(MC_IN, _publish))
        try:
            await restarted.simulate_inbound(dict(packet))
        finally:
            await restarted.stop()

        assert await _drain_pending(runner, temp_storage) == []
        assert await _stored_bodies(temp_storage) == {"only once"}
        assert len(await _inbound_native_rows(temp_storage)) == 1
        assert len(_delivered_texts(out_adapter)) == 1


async def test_wire_indistinguishable_duplicate_treated_as_retransmission(
    temp_storage: SQLiteStorage,
) -> None:
    """Identical sender/channel/second/sub-type/text input is
    indistinguishable on the wire; the second delivery is treated as a
    retransmission of the first (documented protocol ambiguity)."""
    packet = _channel_packet(text="deja vu", timestamp=_TS)

    async with _admission_stack(temp_storage) as (in_adapter, out_adapter, runner):
        await in_adapter.simulate_inbound(dict(packet))
        await in_adapter.simulate_inbound(dict(packet))

        assert len(await _drain_pending(runner, temp_storage)) == 1
        assert await _stored_bodies(temp_storage) == {"deja vu"}
        assert len(await _inbound_native_rows(temp_storage)) == 1
        assert len(_delivered_texts(out_adapter)) == 1


# ---------------------------------------------------------------------------
# Missing identity
# ---------------------------------------------------------------------------


async def test_missing_timestamp_claims_no_shared_native_identity(
    temp_storage: SQLiteStorage,
) -> None:
    """Packets without sender_timestamp claim no native identity at all:
    both are admitted as distinct events and no shared placeholder
    native ref is invented for them."""
    async with _admission_stack(temp_storage) as (in_adapter, _out, runner):
        for text in ("no ts one", "no ts two"):
            packet = _channel_packet(text=text)
            del packet["sender_timestamp"]
            await in_adapter.simulate_inbound(packet)

        routed = await _drain_pending(runner, temp_storage)
        assert len(routed) == 2, "missing identity must not collapse messages"
        assert await _stored_bodies(temp_storage) == {"no ts one", "no ts two"}
        assert await _inbound_native_rows(temp_storage) == []
