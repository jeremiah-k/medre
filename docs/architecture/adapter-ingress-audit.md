# Adapter Ingress Audit

Audit of inbound message paths for each MEDRE transport adapter.
Covers callback registration, CanonicalEvent mapping, self-message
filtering, duplicate handling, known gaps, and test coverage.

Last audited: 2026-05-15.

## Audit matrix

| Adapter | Inbound loop | Callback registration | CanonicalEvent mapping | Self-message filter | Duplicate handling | Known gaps | Test coverage |
|---------|-------------|----------------------|----------------------|--------------------|--------------------|------------|--------------|
| Matrix | Yes | `MatrixSession.start()` registers `_on_room_message` via nio `client.add_event_callback(callback, RoomMessageText)`. Session runs `sync_forever` as background task. | `_on_room_message` → `MatrixCodec.decode(event, room_id)` → `CanonicalEvent`. Tracks room encryption state. | Yes: checks `event.sender == config.user_id`, skips own messages. Also suppresses MEDRE-origin events via `MatrixMetadataEnvelope` check (`envelope.source_adapter == self.adapter_id`). | None. Redeliveries from homeserver are not deduplicated. | Room allowlist filter applied (`room.room_id in config.room_allowlist`). Undecryptable events counted but not forwarded. No dedup. | Full fake-pipeline + live smoke against Synapse. |
| Meshtastic | Yes | `MeshtasticSession.start()` subscribes via `pub.subscribe(self._on_receive, "meshtastic.receive")`. Session callback forwards to adapter's `message_callback` (set to `adapter._on_packet`). | `_on_receive` → `_on_packet` → classify (text only, skip ACKs) → `MeshtasticCodec.decode(packet)` → `asyncio.create_task(_on_packet_async)` → `ctx.publish_inbound`. | No. No sender identity comparison. Radio mesh has no reliable sender-equals-self check. | None. Duplicate packets from retransmission are not detected. | Only `category=="text"` and `!is_ack` pass through. No self-message filter — echo loops prevented only at the MEDRE envelope layer (loop-prevention accounting). | Full fake-pipeline + Docker SDK-boundary (containerized meshtasticd). No live radio evidence. |
| MeshCore | Yes | `MeshCoreSession.start()` calls `self._meshcore.subscribe(...)` for DM, channel, and disconnect events. Session reader loop dispatches to `message_callback` (set to `adapter._on_message`). | `_on_message` → classify (text only, skip ACKs) → `MeshCoreCodec.decode(packet)` → `asyncio.create_task(_on_message_async)` → `ctx.publish_inbound`. | No. Sender identity is a 6-byte pubkey prefix; no reliable self-check. | None. Duplicate sends possible under retry (session retries up to 3x). | Only text messages pass. No self-message filter. Duplicate-send risk documented in session module. | Unit-tested only (fake-pipeline). No live evidence. |
| LXMF | Yes | `LxmfSession.start()` calls `self._router.register_delivery_callback(self._on_lxmf_delivery)`. Session normalises raw `LXMessage` to plain dict before forwarding to adapter's `message_callback`. | `_on_lxmf_delivery` → session normalises to dict → `adapter._on_packet` → classify (text only, skip ACKs) → `LxmfCodec.decode(packet)` → `asyncio.create_task(_on_packet_async)` → `ctx.publish_inbound`. | No. No sender-equals-self check on LXMF delivery. | None. LXMF store-and-forward may redeliver; not deduplicated. | Only text messages pass. No self-message filter. Delivery state tracked but not used for ingress dedup. | Unit-tested only (fake-pipeline). No live evidence. |

## Summary of cross-cutting concerns

- **Self-message filtering**: Only Matrix implements sender-equals-self filtering. Meshtastic, MeshCore, and LXMF rely on MEDRE's higher-level loop-prevention accounting rather than adapter-level sender checks. This is architecturally consistent — radio transports lack reliable sender identity for self-comparison.
- **Duplicate handling**: No adapter implements inbound deduplication. This is a known limitation documented in the beta-readiness checklist. Consumers must tolerate duplicate canonical events.
- **CanonicalEvent mapping**: All four adapters follow the same pattern: classify → filter → codec.decode → async publish_inbound. The codec is the sole mapping boundary.
- **Test coverage**: Matrix has live smoke evidence against Synapse. Meshtastic has Docker SDK-boundary evidence (containerized meshtasticd) only. MeshCore and LXMF are unit-tested only (fake-pipeline).

## Ingress path diagram (generic)

```
SDK callback / pubsub subscription
  → Session normalises to plain dict
    → Adapter._on_packet / _on_room_message
      → PacketClassifier.classify (filter: text only, skip ACKs)
        → Codec.decode → CanonicalEvent
          → asyncio.create_task(async_publish)
            → AdapterContext.publish_inbound
              → PipelineRunner.handle_ingress
```

Matrix is the exception: the adapter callback is async and calls
`ctx.publish_inbound` directly (no intermediate `create_task`), because
nio's event callback runs in the async event loop.
