# Adapter Ingress Audit

Audit of inbound message paths for each MEDRE transport adapter.
Covers callback registration, CanonicalEvent mapping, self-message
filtering, duplicate handling, known gaps, and test coverage.

Last audited: 2026-05-15.

## Audit matrix

| Adapter    | Inbound loop | Callback registration                                                                                                                                                                              | CanonicalEvent mapping                                                                                                                                                                                    | Self-message filter                                                                                                                                                                      | Duplicate handling                                                                | Known gaps                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             | Test coverage                                                                                                                                                |
| ---------- | ------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Matrix     | Yes          | `MatrixSession.start()` registers `_on_room_message` via nio `client.add_event_callback(callback, RoomMessageText)`. Session runs `sync_forever` as background task.                               | `_on_room_message` → `MatrixCodec.decode(event, room_id)` → `CanonicalEvent`. Tracks room encryption state.                                                                                               | Yes: checks `event.sender == config.user_id`, skips own messages. Also suppresses MEDRE-origin events via `MatrixMetadataEnvelope` check (`envelope.source_adapter == self.adapter_id`). | None. Redeliveries from homeserver are not deduplicated.                          | Room allowlist filter applied (`room.room_id in config.room_allowlist`). Undecryptable events counted but not forwarded. No dedup.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | Full fake-pipeline + live smoke against Synapse.                                                                                                             |
| Meshtastic | Yes          | `MeshtasticSession.start()` subscribes via `pub.subscribe(self._on_receive, "meshtastic.receive")`. Session callback forwards to adapter's `message_callback` (set to `adapter._on_packet`).       | `_on_receive` → `_on_packet` → classify (text only, skip ACKs) → `MeshtasticCodec.decode(packet)` → `asyncio.create_task(_on_packet_async)` → `ctx.publish_inbound`.                                      | No. No sender identity comparison. Radio mesh has no reliable sender-equals-self check.                                                                                                  | None. Duplicate packets from retransmission are not detected.                     | Only `category=="text"` and `!is_ack` pass through. No self-message filter — echo loops prevented only at the MEDRE envelope layer (loop-prevention accounting). **Docker inbound xfail detail**: `test_two_client_real_packet_injection` connects a second `TCPInterface` to meshtasticd, sends via `sendText()`, and polls for the adapter's `publish_inbound` callback. Marked `xfail(strict=False)` because meshtasticd simulation mode (`-s` flag) may not relay packets between TCP clients. If it passes, it is bonus evidence proving the full pubsub callback path: daemon → `meshtastic.receive` → `_on_receive` → `_on_packet` → codec → `publish_inbound`. If it fails, only the simulate_inbound codec path is proven (not real pubsub delivery). No live radio evidence. |
| MeshCore   | Yes          | `MeshCoreSession.start()` calls `self._meshcore.subscribe(...)` for DM, channel, and disconnect events. Session reader loop dispatches to `message_callback` (set to `adapter._on_message`).       | `_on_message` → classify (text only, skip ACKs) → `MeshCoreCodec.decode(packet)` → `asyncio.create_task(_on_message_async)` → `ctx.publish_inbound`.                                                      | No. Sender identity is a 6-byte pubkey prefix; no reliable self-check.                                                                                                                   | None(†). Duplicate sends possible under retry (session retries up to 3x).         | Only text messages pass. No adapter-level self-message filter. No adapter-level dedup (pipeline handles it). Duplicate-send risk documented in session module. **Docker feasibility**: Not feasible yet. `meshcore_py` is a client library connecting to real radio hardware. No MeshCore simulator/daemon exists (unlike meshtasticd). Docker SDK-boundary test requires upstream simulator support or physical hardware.                                                                                                                                                                                                                                                                                                                                                             | Unit-tested only (fake-pipeline). Wrapper callback bridge test added (simulate_inbound → pipeline → fake outbound). No Docker SDK-boundary or live evidence. |
| LXMF       | Yes          | `LxmfSession.start()` calls `self._router.register_delivery_callback(self._on_lxmf_delivery)`. Session normalises raw `LXMessage` to plain dict before forwarding to adapter's `message_callback`. | `_on_lxmf_delivery` → session normalises to dict → `adapter._on_packet` → classify (text only, skip ACKs) → `LxmfCodec.decode(packet)` → `asyncio.create_task(_on_packet_async)` → `ctx.publish_inbound`. | No. No sender-equals-self check on LXMF delivery.                                                                                                                                        | None(†). LXMF store-and-forward may redeliver; not deduplicated at adapter layer. | Only text messages pass. No adapter-level self-message filter. No adapter-level dedup (pipeline handles it). Delivery state tracked but not used for ingress dedup. **Docker feasibility**: Theoretically possible but not implemented. RNS can run headless (`RNS.Reticulum(None)`) and LXMRouter needs only a `storagepath`. However, two LXMF routers need a shared transport (TCP/serial/radio) to communicate. A two-container setup is possible but requires a custom Docker image, transport bridge, and identity management. Not a simple "pull image and run" situation. No immediate plan to build this.                                                                                                                                                                     | Unit-tested only (fake-pipeline). Wrapper callback bridge test added (\_on_packet → pipeline → fake outbound). No Docker SDK-boundary or live evidence.      |

## Summary of cross-cutting concerns

- **Self-message filtering**: Only Matrix implements sender-equals-self filtering. Meshtastic, MeshCore, and LXMF rely on MEDRE's higher-level loop-prevention accounting rather than adapter-level sender checks. This is architecturally consistent — radio transports lack reliable sender identity for self-comparison.
- **Duplicate handling**: No adapter implements inbound deduplication. This is a known limitation documented in the beta-readiness checklist. Consumers must tolerate duplicate canonical events. Pipeline-level native-ref dedup applies to all adapters (Stage 1.5), but adapters themselves do not deduplicate at the adapter layer. See Contract 49 §6.
- **CanonicalEvent mapping**: All four adapters follow the same pattern: classify → filter → codec.decode → async publish_inbound. The codec is the sole mapping boundary.
- **Test coverage**: Matrix has live smoke evidence against Synapse. Meshtastic has Docker SDK-boundary evidence (containerized meshtasticd) — outbound delivery and SDK lifecycle are proven; inbound packet reception from a second meshtasticd client via pubsub is not confirmed (meshtasticd simulation mode may not relay between TCP clients; see Docker feasibility section below for detail). MeshCore has no Docker SDK-boundary path — the `meshcore_py` SDK is a client library with no simulator/daemon component, so containerized testing requires upstream support or physical hardware. LXMF has no Docker SDK-boundary path — RNS can run headless but a two-container setup requires custom infrastructure (image, transport bridge, identity management) that does not yet exist.
- **Wrapper callback bridge tests (MeshCore, LXMF)**: Both adapters now have tests that invoke the real adapter callback (`_on_message` for MeshCore, `_on_packet` for LXMF) with simulated inbound packets, confirming the full callback → codec → pipeline → fake-outbound delivery path works. These remain unit-test-only — no Docker containers, no live hardware. Evidence level: `fake_pipeline` (not `docker_sdk_boundary` or `live`).
- **run_session (adapter_callback) evidence**: The `run_bridge_session` mode with `adapter_callback` delivery exercises the real adapter callback and records persisted receipts and accounting, but does not produce `DeliveryOutcomes` from the target adapter. Evidence level: `fake_run_session_adapter_callback`. This is stronger than a unit codec test but weaker than a Docker SDK-boundary bridge smoke. Receipts and accounting are persisted; delivery outcomes are not.
- **Callback isolation**: Each adapter's inbound callback is wrapped in a try/except that logs exceptions and continues processing future callbacks. One malformed inbound payload does not prevent subsequent valid callbacks. A corrupt Meshtastic packet does not block the next valid packet from entering the pipeline. This isolation is at the callback-dispatch level, not at the SDK subscription level — if the underlying SDK loop crashes, callbacks cease entirely.
- **Shutdown-under-traffic**: The pipeline handles `SHUTDOWN_REJECTION` when new deliveries are attempted after shutdown begins. Events already in storage are preserved. In-flight deliveries complete or fail deterministically — there is no ambiguous "maybe delivered" state at shutdown. New ingress after shutdown initiation is rejected, not queued.
- **Successful delivery meaning**: "Successful delivery" means the adapter accepted the event for transport (local SDK handoff). It does not mean remote receipt. Radio transports (Meshtastic, MeshCore) are fire-and-forget — `sent` means the local node queued the packet. Matrix confirms homeserver acceptance only — `sent` means Synapse returned an `event_id`. LXMF enters store-and-forward propagation — `sent` means the local `LXMRouter` accepted the message. See docs/runbooks/bridge-operation.md §2 for per-transport delivery semantics.

## run_bridge_session evidence levels

The `run_bridge_session` harness supports multiple modes. Each mode produces
evidence at a different level:

| Mode                             | Evidence produced                                                                                                                                                                                              | Evidence level                      |
| -------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------- |
| `run_session` (adapter_callback) | Persisted `DeliveryReceipt` records, `RuntimeAccounting` counters, `NativeMessageRef` entries. No `DeliveryOutcome` objects — delivery confirmation comes from receipt persistence, not adapter-level outcome. | `fake_run_session_adapter_callback` |
| `run_session` (full_pipeline)    | `DeliveryOutcome` objects from target adapter, plus all receipt/accounting/native-ref artifacts.                                                                                                               | `fake_pipeline` (full)              |

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

## Transport Evidence Matrix

| Adapter    | Fake callback | Wrapper callback | Docker SDK-boundary (outbound) |      Docker SDK-boundary (inbound)       | Live network/radio |
| ---------- | :-----------: | :--------------: | :----------------------------: | :--------------------------------------: | :----------------: |
| Matrix     |      ✅       |        ✅        |               ✅               |              ✅ (sync_loop)              |    ✅ (Synapse)    |
| Meshtastic |      ✅       |        ✅        |               ✅               | ❓ (inbound from 2nd client unconfirmed) |   ❌ Not claimed   |
| MeshCore   |      ✅       |        ✅        |       ❌ No Docker setup       |                    ❌                    |         ❌         |
| LXMF       |      ✅       |        ✅        |       ❌ No Docker setup       |                    ❌                    |         ❌         |

Key:

- ✅ = proven
- ❌ = not proven / not claimed
- ❓ = partial or unconfirmed

Fake callback = simulate_inbound, Wrapper callback = adapter callback → pipeline → fake outbound, Docker SDK-boundary = real SDK against containerized service, Live = real endpoint/hardware.

## Docker / Local SDK Boundary Feasibility

Per-transport assessment of what would be required for a Docker-backed SDK
boundary test (analogous to the existing meshtasticd tests), whether it is
feasible with current dependencies, and what the honest status is.

### Meshtastic — Docker boundary exists, inbound unconfirmed

**What exists.** The `meshtasticd` daemon ships an official Docker image
(`meshtastic/meshtasticd:2.7.15`) with a simulation-mode flag (`-s`) that
requires no LoRa hardware. The MEDRE CI starts this container in
`conftest.py::meshtasticd_env` and runs four SDK-boundary smoke tests
plus one bonus test.

**What is proven (Docker).**

1. Real `TCPInterface` lifecycle (connect, subscribe `meshtastic.receive`,
   health check, diagnostics, clean stop).
2. Real outbound SDK boundary (`deliver()` → queue → `send_one()` → real
   `sendText()` → meshtasticd returns a real packet ID).
3. Real pipeline bridge with active session (`simulate_inbound()` → codec
   → pipeline → `FakeMeshtasticAdapter` → SQLite receipts).

**What is NOT proven (Docker).**

- **Inbound packet reception from a second TCP client via pubsub.**
  Test `test_two_client_real_packet_injection` is marked `xfail(strict=False)`
  because meshtasticd simulation mode may not relay packets between TCP
  clients. The test connects a second `TCPInterface` (injector) to the
  same meshtasticd instance, calls `injector.sendText()`, and polls for
  the adapter's `publish_inbound` callback to fire. If meshtasticd
  simulation mode does not relay between clients, the pubsub callback
  never fires and the assertion times out.

- **Why xfail and not skip.** The test is `xfail(strict=False)` rather than
  `skip` because it provides _bonus evidence_ when it passes — some
  meshtasticd versions or configurations may relay between clients. When it
  fails, the failure is expected and does not block CI.

- **What would need to change for it to pass.** Either (a) meshtasticd
  simulation mode adds inter-client relay (upstream feature), (b) a newer
  meshtasticd version supports this out of the box, or (c) testing moves
  to live radio hardware (two physical Meshtastic nodes).

- **Alternative path for stronger inbound proof.** A live radio test with
  two physical Meshtastic nodes — one running medre, one acting as sender.
  This would be true "live" evidence, not Docker-boundary.

### MeshCore — No Docker boundary, not feasible yet

**No Docker setup exists.** There is no `MeshCoreEnvironment` fixture, no
`conftest.py` entries for MeshCore, and no integration test files targeting
a containerized MeshCore node.

**Why not feasible with current dependencies.** The `meshcore_py` SDK is a
_client library_ that connects to real MeshCore radio hardware via TCP,
serial, or BLE. There is no standalone MeshCore daemon or simulator
analogous to `meshtasticd`. The SDK's factory methods
(`MeshCore.create_tcp(host, port)`, etc.) expect a real radio node on the
other end of the connection.

**What would be needed.**

1. A MeshCore firmware emulator or simulation daemon that accepts TCP/serial
   connections and responds to SDK commands (equivalent to `meshtasticd -s`).
2. Or: access to physical MeshCore hardware for live testing.
3. The SDK itself supports async operation and would work in a containerized
   test environment _if_ there were something to connect to.

**Honest status.** Not feasible yet. The MeshCore ecosystem does not provide
a simulation or emulator component. Docker SDK-boundary testing requires
upstream support (a simulator) or physical hardware.

### LXMF — No Docker boundary, theoretically possible but not implemented

**No Docker setup exists.** There is no `LxmfEnvironment` fixture, no
`conftest.py` entries for LXMF, and no integration test files targeting a
containerized Reticulum/LXMF node.

**Reticulum headless mode.** RNS can run without hardware —
`RNS.Reticulum(None)` creates an instance with default config. LXMF's
`LXMRouter` requires a `storagepath` (works fine in a container). The
session code handles the RNS singleton constraint (`get_instance()`).

**Why not implemented.** Two LXMF routers need a shared transport layer to
communicate (TCP, serial, or radio). A two-container setup (one running a
"server" Reticulum+LXMRouter, the test runner as "client") is theoretically
possible but requires:

1. A Docker image with RNS + LXMF installed.
2. A startup script that initialises Reticulum, creates an identity, announces
   an LXMF destination, and listens for inbound messages.
3. A TCP transport interface bridging the two containers.
4. Identity exchange so both sides know each other's destination hash.

RNS is designed as a singleton per process, which the session code handles.
But the setup complexity is significant — it is not a simple
"pull image and run" situation like meshtasticd.

**Honest status.** Theoretically possible but not implemented. Requires
significant infrastructure work (custom Docker image, transport bridge,
identity management). No immediate plan to build this. Live hardware testing
with two Reticulum nodes remains the most direct path to inbound proof.
