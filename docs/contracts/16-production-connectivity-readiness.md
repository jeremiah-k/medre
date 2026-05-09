# Production Connectivity Readiness

> Contract version: 1
> Last updated: 2026-05-08
> Track: 8 (Production Connectivity Readiness)

This document assesses each adapter's readiness for real network operation. It is deliberately conservative. Nothing claimed here should be interpreted as "works against real hardware/services" until it has been explicitly verified against the actual SDK, transport, or service.

All four adapters are in tranche 1. Every adapter uses fake delivery. No adapter has been tested against a real production endpoint.


## Matrix

### What tranche 1 proves

- The decode/render/deliver pipeline works end to end with fake data.
- `MatrixCodec` converts nio-shaped event objects into `CanonicalEvent` instances. The codec is nio-agnostic (expects `.sender`, `.body`, `.event_id`, `.source` attributes but doesn't import nio).
- `MatrixRenderer` builds `m.room.message` content dicts with `m.relates_to` for replies and a `medre.envelope` metadata subtree.
- `MatrixAdapter.deliver()` constructs a proper `AdapterDeliveryResult` with `native_message_id` and `native_channel_id` from a simulated `room_send` response.
- Self-message suppression logic is implemented and tested (sender check + envelope check).
- Room allowlist filtering works.
- The `FakeMatrixAdapter` enforces the rendering boundary: `deliver()` accepts `RenderingResult` only, not `CanonicalEvent`.

### What is still fake/scaffolded

- **No real `nio` client has been connected to a homeserver.** The `start()` method contains real `nio.AsyncClient` code, but it has not been executed against a live Matrix homeserver.
- **No real sync loop has been observed.** `sync_forever()` is started as an asyncio task, but no test has verified that it receives real events and feeds them to `_on_room_message`.
- **No real `room_send` has been executed.** The outbound path in `deliver()` calls `self._client.room_send()`, but only with a fake client in tests.
- **E2EE is not implemented.** No olm/megolm support.
- **Reactions, edits, deletes, and attachments are all deferred.** Only text and replies work.

### What must be done before real operation

1. **Real `mindroom-nio` integration test.** Connect `MatrixAdapter` to a real (or local Synapse/conduit) homeserver. Verify sync, inbound event decoding, and outbound delivery.
2. **Verify `_on_room_message` callback behavior** with real nio event objects, not just test fakes.
3. **Verify `room_send` response handling.** Confirm that `RoomSendResponse.event_id` is populated and that error responses are handled correctly.
4. **Test the sync loop lifecycle.** Verify clean startup, sync operation, and graceful shutdown (`stop()`) against a real server.
5. **Verify self-message suppression** with real echo events from the homeserver.
6. **Test against multiple room types.** Public, private, DMs.

### Likely first production-connectivity tranche focus

Connect `MatrixAdapter` to a local Synapse or conduit homeserver. Send a message, receive a message, verify native ref round-trip. This is the smallest useful connectivity milestone.

### Known risks

- `mindroom-nio` is a fork (`migrating-nio`?). Its maintenance status and API stability relative to upstream `matrix-nio` need verification.
- Sync loop error handling may need hardening for real network conditions (timeouts, reconnects, rate limiting).
- The `access_token` config field is stored as a plain string. Token storage and rotation need a security review before production deployment.


## Meshtastic

### What tranche 1 proves

- The decode/classify/deliver pipeline works with fake packet dicts.
- `MeshtasticCodec` converts Meshtastic-shaped packet dicts into `CanonicalEvent` instances, including `replyId` relation extraction.
- `MeshtasticPacketClassifier` classifies packets by portnum, detects ACKs, and extracts sender/channel/packet_id.
- `MeshtasticAdapter` manages background tasks for async publish from the synchronous `_on_packet` callback.
- The outbound queue (`MeshtasticOutboundQueue`) handles message pacing with configurable delay.
- `FakeMeshtasticAdapter` returns `AdapterDeliveryResult` with deterministic packet IDs via `FakeMeshtasticClient`.

### What is still fake/scaffolded

- **No real Meshtastic client connection.** Even when `connection_type` is not `"fake"`, `self._client` is set to `None`. The comment says "Real client creation is deferred to a later tranche."
- **No real `send_text` has been executed.** Outbound goes through `MeshtasticOutboundQueue.enqueue()` but the queue doesn't actually send anything over the air.
- **No real packet callbacks have been received.** `_on_packet()` is tested with manual dict injection only.
- **The compat module provides `get_portnum_table()`** from the real `mtjk` package, but core classifier logic uses a scaffold map so tests pass without the dependency.

### What must be done before real operation

1. **Real `mtjk` client connection.** Implement TCP/serial/BLE connection in `start()` when `connection_type` is not `"fake"`.
2. **Wire real packet callbacks.** Register `_on_packet` as a callback with the real Meshtastic client.
3. **Verify real `send_text` behavior.** Confirm that outbound messages are actually transmitted and that the response includes a usable packet ID.
4. **Test with real hardware or simulator.** At minimum, one TCP connection to a real Meshtastic node.
5. **Verify startup backlog suppression.** The `startup_backlog_suppress_seconds` config field exists but needs testing against real stale packets from a node's history.
6. **Test channel mapping with real channels.**

### Likely first production-connectivity tranche focus

Connect to a real Meshtastic node via TCP. Receive text packets, decode them, send a text message back. Verify packet ID round-trip.

### Known risks

- `mtjk` is a fork of the Meshtastic Python library. Its version pinning and compatibility with real Meshtastic firmware need verification.
- Meshtastic's 512-byte payload limit is not enforced in the renderer. Real messages longer than the limit will silently fail or be truncated by the radio.
- The `_on_packet` callback is synchronous but publishes async. The background task management works, but error propagation from the async publish back to the callback context needs review.
- Real Meshtastic nodes may send packets with different protobuf schemas than the scaffold map expects.


## MeshCore

### What tranche 1 proves

- The decode/classify/deliver pipeline works with fake event payloads.
- `MeshCoreCodec` converts MeshCore-shaped event dicts into `CanonicalEvent` instances.
- `MeshCorePacketClassifier` classifies events by type, detects ACKs, extracts sender/channel/timestamp.
- `MeshCoreAdapter` follows the same structural pattern as `MeshtasticAdapter` (background tasks, synchronous callback, async publish).
- `FakeMeshCoreAdapter` returns `AdapterDeliveryResult` with deterministic packet IDs.

### What is still fake/scaffolded

- **No real MeshCore SDK or connectivity.** `start()` raises `MeshCoreConnectionError` for any non-fake connection type. There is no real client code at all.
- **No real MeshCore packet format verification.** The packet shape used in tests is based on the source audit (contract 11) but has not been validated against real MeshCore event payloads.
- **No outbound delivery.** `deliver()` returns `None` in tranche 1.
- **No real dependency.** MeshCore doesn't have a known stable PyPI package yet.

### What must be done before real operation

1. **Identify and integrate the MeshCore Python SDK.** The source audit (contract 11) documented the available interfaces, but no SDK has been selected or integrated.
2. **Verify real packet format against real MeshCore events.** The current packet shape is based on documentation and source review, not live observation.
3. **Implement real connection code.** TCP, serial, or BLE connection in `start()`.
4. **Implement real send.** Wire `deliver()` to the actual MeshCore send API.
5. **Verify event payload schema** with real hardware or simulator output.

### Likely first production-connectivity tranche focus

Obtain real MeshCore event samples. Validate that the codec and classifier handle them correctly. If a Python SDK is available, integrate it for basic TCP connection and text send/receive.

### Known risks

- MeshCore's SDK availability and stability are uncertain. The project may not have a mature Python client library.
- Packet format assumptions are based on source code review, not live testing. The real format may differ.
- MeshCore's event model (channels, direct messages, ACKs) may not map cleanly to the current classifier's assumptions.
- The adapter is structurally ready (following the Meshtastic template) but substantively empty. This is the most speculative of the four adapters.


## LXMF

### What tranche 1 proves

- The decode/classify/deliver pipeline works with fake message payloads.
- `LxmfCodec` converts LXMF-shaped message dicts into `CanonicalEvent` instances, including source_hash extraction and fields-based metadata.
- `LxmfPacketClassifier` classifies messages, detects ACKs, extracts sender/message_id/title/fields.
- `LxmfFieldsHelper` embeds and extracts MEDRE metadata envelopes under field key `0xFD`.
- `LxmfRenderer` builds payloads with `content`, `title`, `fields`, and `destination_hash`.
- `FakeLxmfAdapter` returns `AdapterDeliveryResult` with SHA-256-based deterministic message IDs.

### What is still fake/scaffolded

- **No real Reticulum or LXMF library integration.** `start()` raises `LxmfConnectionError` for non-fake types. No `rns` or `lxmf` imports exist.
- **No real identity loading.** The `identity_path` config field is a placeholder.
- **No real message send/receive.** `deliver()` returns `None`.
- **Relation reconstruction from fields envelope is explicitly deferred.** The codec stores the raw envelope dict in metadata but does not create `EventRelation` objects from it.
- **Delivery method selection** (`direct`, `opportunistic`, `propagated`, `paper`) is a config hint only. No actual LXMF delivery method logic exists.

### What must be done before real operation

1. **Integrate `rns` (Reticulum) and `lxmf` Python packages.** These are the real dependencies for LXMF messaging.
2. **Implement real identity loading.** Load or create a Reticulum identity from `identity_path`.
3. **Implement real LXMF router connection.** Connect to an LXMF router, announce, and start receiving messages.
4. **Implement real message sending.** Wire `deliver()` to actual `LXMF.Message.send()` or equivalent.
5. **Implement delivery method selection.** Map `default_delivery_method` config to real LXMF delivery parameters.
6. **Implement relation reconstruction.** Decode the envelope's relation data back into `EventRelation` objects on inbound.
7. **Verify field key `0xFD` doesn't conflict** with real LXMF field usage.

### Likely first production-connectivity tranche focus

Integrate the `rns` and `lxmf` packages. Create a minimal Reticulum identity. Connect to a local LXMF router. Send and receive one message. Verify that the codec handles real `LXMF.Message` objects (or their dict representation).

### Known risks

- Reticulum and LXMF have their own networking stack that may conflict with asyncio's event loop. The async/sync boundary needs careful design.
- LXMF messages can be very large (16KB+), but the renderer doesn't enforce any limit. Real networks may have practical constraints.
- The fields envelope approach (key `0xFD`) is an assumption. It needs validation against real LXMF field usage to ensure no conflicts.
- Identity management (creation, storage, rotation) is a significant piece of work that hasn't been scoped yet.
- LXMF's store-and-forward and propagation mechanisms add complexity that the current adapter doesn't address at all.


## Summary

### Adapter Readiness Ranking (most ready to least)

1. **Matrix** is closest to real operation. It has real `nio` client code in `start()` and `deliver()`. The remaining gap is integration testing against an actual homeserver, not writing new code.

2. **Meshtastic** has the pipeline in place but the real connection code is stubbed (`self._client = None` even for non-fake types). Needs real `mtjk` callback and send verification.

3. **MeshCore** needs production SDK verification first. The adapter follows the right structural pattern but has no real connectivity code and no verified SDK.

4. **LXMF** needs the most work. Reticulum/LXMF integration is a significant undertaking involving identity management, a custom networking stack, and a different message model.

### Cross-cutting Concerns

- **Webhooks remain future work.** No adapter has webhook integration.
- **Admin APIs remain future work.** No adapter exposes admin operations (channel management, user management, etc.).
- **All adapters currently use fake delivery only.** No adapter has verified real message delivery against its target platform.
- **No adapter has been tested with real hardware or real network services.** Everything is unit-tested against fake clients and test data.
- **No adapter handles reconnection or connection loss.** The lifecycle is start/stop with no automatic recovery.

### What "production connectivity" means in this context

For the purposes of this document, "production connectivity" means: the adapter can connect to a real instance of its target platform, send a message that actually arrives, receive a message that was actually sent by another user/node, and correctly round-trip native IDs through the decode/render/deliver pipeline.

None of the four adapters meet this standard as of tranche 1.
