# Production Connectivity Readiness

> **Status:** Assessment
> **Classification:** Assessment
> **Authority:** Per-adapter readiness assessment; cross-transport view superseded by `28-alpha-readiness-report.md`
> **Last reviewed:** 2026-05-24
>
> Contract version: 4
> Last updated: 2026-05-09
> Track: 8 (Production Connectivity Readiness)

> **Note:** This is a point-in-time assessment. MeshCore and LXMF now have real session code (source-audited, mock-tested). See contracts 19/20 and runbooks for current readiness status.

This document assesses each adapter's readiness for real network operation. It is deliberately conservative. Nothing claimed here should be interpreted as "works against real hardware/services" until it has been explicitly verified against the actual SDK, transport, or service.

All four adapters are in tranche 1. Fake mode is the default development path for all four. All four adapters now have **optional** live smoke harnesses that can verify real connectivity when explicitly enabled via environment variables. No adapter has been tested against a real production endpoint in default CI.

**Note (2026-05-09):** The per-transport sections below were written before the MeshCore and LXMF real session code was merged. MeshCore and LXMF now have real session implementations (see contracts 19, 20) and live smoke harnesses. The current cross-transport assessment is consolidated in contract 28 (`28-alpha-readiness-report.md`). The diagnostics consistency audit is in contract 27 (`27-diagnostics-consistency-audit.md`).

## Matrix

### Matrix: What tranche 1 proves (deterministic / fake-only)

- The decode/render/deliver pipeline works end to end with fake data.
- `MatrixCodec` converts nio-shaped event objects into `CanonicalEvent` instances. The codec is nio-agnostic (expects `.sender`, `.body`, `.event_id`, `.source` attributes but doesn't import nio).
- `MatrixRenderer` builds `m.room.message` content dicts with `m.relates_to` for replies and a `medre.envelope` metadata subtree.
- `MatrixAdapter.deliver()` constructs a proper `AdapterDeliveryResult` with `native_message_id` and `native_channel_id` from a simulated `room_send` response.
- Self-message suppression logic is implemented and tested (sender check + envelope check).
- Room allowlist filtering works.
- The `FakeMatrixAdapter` enforces the rendering boundary: `deliver()` accepts `RenderingResult` only, not `CanonicalEvent`.

### Matrix: What the optional live smoke harness proves

A skipped-by-default live test harness at `tests/test_matrix_live.py` with a companion runbook at `docs/runbooks/matrix-live-smoke.md` provides optional real-homeserver validation. When enabled via environment variables, it proves:

- The adapter connects to a real Matrix homeserver and authenticates via access token.
- `health_check()` transitions correctly: `"unknown"` → `"healthy"` → `"unknown"`.
- Outbound `room_send` produces a real `event_id` (starts with `$`).
- The full lifecycle (start → send → healthy → stop → unknown) works as an ordered sequence.
- No asyncio tasks are leaked after stop.

The live harness is **optional** and **does not gate CI**. Default `pytest` remains fake-only. See `docs/runbooks/matrix-live-smoke.md` for setup and usage.

### Matrix: What the live smoke harness does NOT prove

- **Inbound message reception.** Requires a second Matrix account to send a message into the room. With one account, timing-sensitive polling would be needed, making tests flaky. Inbound codec correctness is covered by deterministic unit tests.
- **Self-message suppression with real sync echoes.** The homeserver echoes outbound messages back. Verifying suppression requires waiting for the echo with a timeout, which is unreliable. Self-message suppression is covered by deterministic unit tests.
- **MEDRE-origin envelope suppression.** Secondary suppression path, unit-tested.
- **E2EE, reactions, edits, deletes, attachments, media.** Not implemented in tranche 1.
- **Admin API, webhooks, HTTP server.** Out of scope.
- **Non-Matrix connectivity.** Meshtastic, MeshCore, LXMF are out of scope.
- **Auth command / credential storage.** Current tranche uses env-var access tokens. A future mmrelay-like auth command may be useful but is not implemented.
- **Real operation scope.** The live harness confirms transport-level connectivity for Matrix tranche 1 features only. It does not prove production readiness for bridging, federation, encrypted rooms, or multi-user scenarios.

### Matrix: What is still fake/scaffolded

- **No inbound message reception has been verified against a real homeserver.** The sync loop starts, but no test has verified that a real inbound event flows through `_on_room_message` → codec → `publish_inbound`.
- **E2EE is not implemented.** No olm/megolm support.
- **Reactions, edits, deletes, and attachments are all deferred.** Only text and replies work.
- **Storage is authoritative.** The metadata envelope is secondary and diagnostic. The live harness does not test storage round-trips against a real homeserver.

### Matrix: What must be done before real operation

1. **Verify `_on_room_message` callback behavior** with real nio event objects from a second user, not just test fakes.
2. **Verify self-message suppression** with real echo events from the homeserver (requires a second account or device).
3. **Test against multiple room types.** Public, private, DMs.
4. **Test federation.** Cross-server message delivery.
5. **Token storage and rotation.** The `access_token` is a plain string. Production deployment needs a security review. A future mmrelay-like auth command may address this.

### Matrix: Likely first production-connectivity tranche focus

The live smoke harness already covers the smallest useful connectivity milestone (connect, send, lifecycle). The next step is verifying inbound reception with a second account and testing self-message suppression with real echoes.

### Matrix: Known risks

- `mindroom-nio` is a fork. Its maintenance status and API stability relative to upstream `matrix-nio` need verification.
- Sync loop error handling may need hardening for real network conditions (timeouts, reconnects, rate limiting).
- The `access_token` config field is stored as a plain string. Token storage and rotation need a security review before production deployment.

## Meshtastic

### Meshtastic: What tranche 1 proves

- The decode/classify/deliver pipeline works with fake packet dicts.
- `MeshtasticCodec` converts Meshtastic-shaped packet dicts into `CanonicalEvent` instances, including `replyId` relation extraction.
- `MeshtasticPacketClassifier` classifies packets by portnum, detects ACKs, and extracts sender/channel/packet_id.
- `MeshtasticAdapter` manages background tasks for async publish from the synchronous `_on_packet` callback.
- The outbound queue (`MeshtasticOutboundQueue`) handles message pacing with configurable delay.
- `FakeMeshtasticAdapter` returns `AdapterDeliveryResult` with deterministic packet IDs via `FakeMeshtasticClient`.
- Fixture provenance labels (mtjk-derived, MMRelay-derived, synthetic scaffold, unverified) document the derivation source of each test packet shape.

### Meshtastic: What the optional live smoke harness proves

A skipped-by-default live test harness at `tests/test_meshtastic_live.py` with a companion runbook at `docs/runbooks/meshtastic-live-smoke.md` provides optional real-node validation. When enabled via environment variables, it proves:

- The `mtjk` package is installed and importable as `meshtastic`.
- A `TCPInterface` (or `SerialInterface`) can connect to a real Meshtastic node.
- `sendText()` completes and returns a `MeshPacket` with a populated `id`.
- `sendData()` completes and returns a `MeshPacket` with a populated `id`.
- The `meshtastic.receive` pubsub callback fires on packet reception.
- Received packets have the expected shape (`decoded`, `id`, `portnum`).

The live harness is **optional** and **does not gate CI**. Default `pytest` remains fake-only. See `docs/runbooks/meshtastic-live-smoke.md` for setup and usage.

### Meshtastic: What the live smoke harness does NOT prove

- **MEDRE adapter integration.** The tests use the raw `mtjk` interface directly. The MEDRE adapter's real connection code is not yet implemented.
- **Inbound reception from a second node.** Tests use self-receive only.
- **Multi-hop mesh delivery.** Only direct node communication is tested.
- **Encrypted channels.** No E2EE support.
- **BLE connectivity.** BLE API is documented but not exercised in the harness.
- **Reconnection handling.** No automatic recovery testing.
- **Production deployment readiness.** The harness validates transport-level connectivity for a single session only.

### Meshtastic: What is still fake/scaffolded

- **No real Meshtastic client connection.** Even when `connection_type` is not `"fake"`, `self._client` is set to `None`. The comment says "Real client creation is deferred to a later tranche."
- **No real `send_text` has been executed.** Outbound goes through `MeshtasticOutboundQueue.enqueue()` but the queue doesn't actually send anything over the air.
- **No real packet callbacks have been received.** `_on_packet()` is tested with manual dict injection only.
- **The compat module provides `get_portnum_table()`** from the real `mtjk` package, but core classifier logic uses a scaffold map so tests pass without the dependency.

### Meshtastic: What must be done before real operation

1. **Real `mtjk` client connection.** Implement TCP/serial/BLE connection in `start()` when `connection_type` is not `"fake"`.
2. **Wire real packet callbacks.** Register `_on_packet` as a callback with the real Meshtastic client.
3. **Verify real `send_text` behavior.** Confirm that outbound messages are actually transmitted and that the response includes a usable packet ID.
4. **Test with real hardware or simulator.** At minimum, one TCP connection to a real Meshtastic node.
5. **Verify startup backlog suppression.** The `startup_backlog_suppress_seconds` config field exists but needs testing against real stale packets from a node's history.
6. **Test channel mapping with real channels.**

### Meshtastic: Likely first production-connectivity tranche focus

Connect to a real Meshtastic node via TCP. Receive text packets, decode them, send a text message back. Verify packet ID round-trip.

### Meshtastic: Known risks

- `mtjk` is a fork of the Meshtastic Python library. Its version pinning and compatibility with real Meshtastic firmware need verification.
- Meshtastic's 512-byte payload limit is not enforced in the renderer. Real messages longer than the limit will silently fail or be truncated by the radio.
- The `_on_packet` callback is synchronous but publishes async. The background task management works, but error propagation from the async publish back to the callback context needs review.
- Real Meshtastic nodes may send packets with different protobuf schemas than the scaffold map expects.

## MeshCore

### MeshCore: What tranche 1 proves

- The decode/classify/deliver pipeline works with fake event payloads.
- `MeshCoreCodec` converts MeshCore-shaped event dicts into `CanonicalEvent` instances.
- `MeshCorePacketClassifier` classifies events by type, detects ACKs, extracts sender/channel/timestamp.
- `MeshCoreAdapter` follows the same structural pattern as `MeshtasticAdapter` (background tasks, synchronous callback, async publish).
- `FakeMeshCoreAdapter` returns `AdapterDeliveryResult` with deterministic packet IDs.

### MeshCore: What is still fake/scaffolded

- **Session code exists but is not live-validated.** TCP/serial/BLE factory wiring, event subscriptions, bounded reconnect, and bounded transient retry exist (source-audited, mock-tested). No live hardware validation has been performed.
- **No ACK correlation or delivery confirmation.** The SDK provides ACK events and `expected_ack` fields, but the adapter does not track or correlate them.
- **No real MeshCore packet format verification.** The packet shape used in tests is based on the source audit (Contract 64) but has not been validated against real MeshCore event payloads.

### MeshCore: What must be done before real operation

1. **Verify real packet format against real MeshCore events.** The current packet shape is based on documentation and source review, not live observation.
2. **Implement ACK watching for delivery confirmation.** The SDK provides ACK events; correlate `expected_ack` from sends with inbound ACK events.
3. **Implement contact-list resolution** for human-readable sender display names.
4. **Verify event payload schema** with real hardware or simulator output.

### MeshCore: Likely first production-connectivity tranche focus

Obtain real MeshCore event samples. Validate that the codec and classifier handle them correctly. If a Python SDK is available, integrate it for basic TCP connection and text send/receive.

### MeshCore: Known risks

- MeshCore's SDK availability and stability are uncertain. The project may not have a mature Python client library.
- Packet format assumptions are based on source code review, not live testing. The real format may differ.
- MeshCore's event model (channels, direct messages, ACKs) may not map cleanly to the current classifier's assumptions.
- The adapter has session code (following the Meshtastic template) but is not live-validated. This is the most speculative of the four adapters.

## LXMF

### LXMF: What tranche 1 proves

- The decode/classify/deliver pipeline works with fake message payloads.
- `LxmfCodec` converts LXMF-shaped message dicts into `CanonicalEvent` instances, including source_hash extraction and fields-based metadata.
- `LxmfPacketClassifier` classifies messages, detects ACKs, extracts sender/message_id/title/fields.
- `LxmfFieldsHelper` embeds and extracts MEDRE metadata envelopes under field key `0xFD`.
- `LxmfRenderer` builds payloads with `content`, `title`, `fields`, and `destination_hash`.
- `FakeLxmfAdapter` returns `AdapterDeliveryResult` with SHA-256-based deterministic message IDs.

### LXMF: What is still fake/scaffolded

- **Session code exists but is not live-validated.** Reticulum/LXMRouter lifecycle, identity load/create, delivery callback registration, LXMessage→dict normalization, outbound send via `handle_outbound`, delivery-state tracking, and `call_soon_threadsafe` callback bridge exist (source-audited, mock-tested). No live Reticulum network validation has been performed.
- **Delivery-state model is unvalidated.** The 8-state progression (GENERATING→OUTBOUND→SENDING→SENT→DELIVERED/FAILED/REJECTED/CANCELLED) is implemented but real-world timing, callback ordering, and cross-thread delivery-state events are untested against a live Reticulum daemon.
- **Relation reconstruction from fields envelope is explicitly deferred.** The codec stores the raw envelope dict in metadata but does not create `EventRelation` objects from it.

### LXMF: What must be done before real operation

1. **Validate delivery-state model against a live Reticulum daemon.** Confirm real-world callback timing, ordering, and cross-thread behavior match expectations.
2. **Implement `register_delivery_identity`.** Required for real-mode inbound message reception (without it, the router has no delivery destination).
3. **Implement relation reconstruction.** Decode the envelope's relation data back into `EventRelation` objects on inbound.
4. **Verify field key `0xFD` doesn't conflict** with real LXMF field usage.

### LXMF: Likely first production-connectivity tranche focus

Integrate the `rns` and `lxmf` packages. Create a minimal Reticulum identity. Connect to a local LXMF router. Send and receive one message. Verify that the codec handles real `LXMF.Message` objects (or their dict representation).

### LXMF: Known risks

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
