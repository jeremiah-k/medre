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
| MeshCore | Yes | `MeshCoreSession.start()` calls `self._meshcore.subscribe(...)` for DM, channel, and disconnect events. Session reader loop dispatches to `message_callback` (set to `adapter._on_message`). | `_on_message` → classify (text only, skip ACKs) → `MeshCoreCodec.decode(packet)` → `asyncio.create_task(_on_message_async)` → `ctx.publish_inbound`. | No. Sender identity is a 6-byte pubkey prefix; no reliable self-check. | None(†). Duplicate sends possible under retry (session retries up to 3x). | Only text messages pass. No adapter-level self-message filter. No adapter-level dedup (pipeline handles it). Duplicate-send risk documented in session module. | Unit-tested only (fake-pipeline). Wrapper callback bridge test added (simulate_inbound → pipeline → fake outbound). No Docker SDK-boundary or live evidence. |
| LXMF | Yes | `LxmfSession.start()` calls `self._router.register_delivery_callback(self._on_lxmf_delivery)`. Session normalises raw `LXMessage` to plain dict before forwarding to adapter's `message_callback`. | `_on_lxmf_delivery` → session normalises to dict → `adapter._on_packet` → classify (text only, skip ACKs) → `LxmfCodec.decode(packet)` → `asyncio.create_task(_on_packet_async)` → `ctx.publish_inbound`. | No. No sender-equals-self check on LXMF delivery. | None(†). LXMF store-and-forward may redeliver; not deduplicated at adapter layer. | Only text messages pass. No adapter-level self-message filter. No adapter-level dedup (pipeline handles it). Delivery state tracked but not used for ingress dedup. | Unit-tested only (fake-pipeline). Wrapper callback bridge test added (_on_packet → pipeline → fake outbound). No Docker SDK-boundary or live evidence. |

## Summary of cross-cutting concerns

- **Self-message filtering**: Only Matrix implements sender-equals-self filtering. Meshtastic, MeshCore, and LXMF rely on MEDRE's higher-level loop-prevention accounting rather than adapter-level sender checks. This is architecturally consistent — radio transports lack reliable sender identity for self-comparison.
- **Duplicate handling**: No adapter implements inbound deduplication. This is a known limitation documented in the beta-readiness checklist. Consumers must tolerate duplicate canonical events. Pipeline-level native-ref dedup applies to all adapters (Stage 1.5), but adapters themselves do not deduplicate at the adapter layer. See Contract 49 §6.
- **CanonicalEvent mapping**: All four adapters follow the same pattern: classify → filter → codec.decode → async publish_inbound. The codec is the sole mapping boundary.
- **Test coverage**: Matrix has live smoke evidence against Synapse. Meshtastic has Docker SDK-boundary evidence (containerized meshtasticd) only. MeshCore and LXMF have unit-test fake-pipeline evidence plus wrapper callback bridge tests that exercise the real adapter callback path (simulate_inbound → codec → pipeline → fake outbound). Neither has Docker SDK-boundary or live radio validation.
- **Wrapper callback bridge tests (MeshCore, LXMF)**: Both adapters now have tests that invoke the real adapter callback (`_on_message` for MeshCore, `_on_packet` for LXMF) with simulated inbound packets, confirming the full callback → codec → pipeline → fake-outbound delivery path works. These remain unit-test-only — no Docker containers, no live hardware. Evidence level: `fake_pipeline` (not `docker_sdk_boundary` or `live`).
- **run_session (adapter_callback) evidence**: The `run_bridge_session` mode with `adapter_callback` delivery exercises the real adapter callback and records persisted receipts and accounting, but does not produce `DeliveryOutcomes` from the target adapter. Evidence level: `fake_run_session_adapter_callback`. This is stronger than a unit codec test but weaker than a Docker SDK-boundary bridge smoke. Receipts and accounting are persisted; delivery outcomes are not.
- **Callback isolation**: Each adapter's inbound callback is wrapped in a try/except that logs exceptions and continues processing future callbacks. One malformed inbound payload does not prevent subsequent valid callbacks. A corrupt Meshtastic packet does not block the next valid packet from entering the pipeline. This isolation is at the callback-dispatch level, not at the SDK subscription level — if the underlying SDK loop crashes, callbacks cease entirely.
- **Shutdown-under-traffic**: The pipeline handles `SHUTDOWN_REJECTION` when new deliveries are attempted after shutdown begins. Events already in storage are preserved. In-flight deliveries complete or fail deterministically — there is no ambiguous "maybe delivered" state at shutdown. New ingress after shutdown initiation is rejected, not queued.
- **Successful delivery meaning**: "Successful delivery" means the adapter accepted the event for transport (local SDK handoff). It does not mean remote receipt. Radio transports (Meshtastic, MeshCore) are fire-and-forget — `sent` means the local node queued the packet. Matrix confirms homeserver acceptance only — `sent` means Synapse returned an `event_id`. LXMF enters store-and-forward propagation — `sent` means the local `LXMRouter` accepted the message. See docs/runbooks/bridge-operation.md §2 for per-transport delivery semantics.

## run_bridge_session evidence levels

The `run_bridge_session` harness supports multiple modes. Each mode produces
evidence at a different level:

| Mode | Evidence produced | Evidence level |
|------|------------------|----------------|
| `run_session` (adapter_callback) | Persisted `DeliveryReceipt` records, `RuntimeAccounting` counters, `NativeMessageRef` entries. No `DeliveryOutcome` objects — delivery confirmation comes from receipt persistence, not adapter-level outcome. | `fake_run_session_adapter_callback` |
| `run_session` (full_pipeline) | `DeliveryOutcome` objects from target adapter, plus all receipt/accounting/native-ref artifacts. | `fake_pipeline` (full) |

The `adapter_callback` mode is useful for validating that the adapter callback → codec → pipeline → receipt chain works end-to-end without requiring the target adapter to produce delivery outcomes. It does not prove that the target adapter correctly delivers the rendered payload — only that the pipeline processed and recorded the delivery attempt.

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

(†) Pipeline-level native-ref dedup via `handle_ingress` Stage 1.5 applies to all adapters.
