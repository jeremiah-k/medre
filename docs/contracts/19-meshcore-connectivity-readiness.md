# MeshCore Connectivity Readiness

> Contract version: 1
> Last updated: 2026-05-09
> Supersedes: None (complements 11, 16, 18)

This document audits MeshCore's connectivity readiness for MEDRE: what the SDK provides, what the current adapter scaffold implements, what send semantics look like, and what remains unknown. It does not claim production connectivity. Nothing here should be read as "MeshCore works against real hardware" without explicit verification.

**Tranche status**: Readiness documentation only. No production connection, no hardware testing, no default dependency.


## 1. SDK Availability

### 1.1 Package Identity

| Property | Value |
|----------|-------|
| Package name | `meshcore` |
| Version audited | 2.2.5 |
| License | MIT |
| PyPI | `pip install meshcore` |
| Source | `/home/jeremiah/dev/meshtastic/meshcore/meshcore_py/` |
| GitHub | `https://github.com/fdlamotte/meshcore_py` |
| Import | `import meshcore` |
| Entry point | `meshcore.MeshCore` |
| Python | >=3.10 |
| Dependencies | `bleak`, `pyserial-asyncio-fast`, `pycayennelpp` |

### 1.2 Why MeshCore Is Not a Default Dependency

MEDRE's core tests pass without any MeshCore package installed. The adapter uses `FakeMeshCoreAdapter` with deterministic fixture data. Adding `meshcore` as a required dependency would impose `bleak`, `pyserial-asyncio-fast`, and `pycayennelpp` on all MEDRE installations, including those that never touch MeshCore hardware.

MeshCore remains an optional, user-installed dependency. The live smoke harness (documented in `docs/runbooks/meshcore-live-smoke.md`) is the only path that requires it.


## 2. Confirmed Findings

These facts are verified from source code at `/home/jeremiah/dev/meshtastic/meshcore/meshcore_py/src/meshcore/`.

### 2.1 Connection Constructors

All three constructors are async factory classmethods on `MeshCore`:

```python
# TCP
mc = await MeshCore.create_tcp(host, port, debug=False, only_error=False,
    default_timeout=None, auto_reconnect=False, max_reconnect_attempts=3)

# Serial
mc = await MeshCore.create_serial(port, baudrate=115200, debug=False, ...)

# BLE
mc = await MeshCore.create_ble(address=None, client=None, device=None,
    pin=None, debug=False, ...)
```

Each constructor creates the underlying transport, connects, and sends `appstart()`. Returns a fully initialized `MeshCore` instance or raises `ConnectionError`.

### 2.2 Async API

The entire SDK is async-native. `connect()`, `disconnect()`, `send_msg()`, `send_chan_msg()`, `subscribe()`, `wait_for_event()` are all coroutines. No synchronous wrappers exist. This matches MEDRE's async adapter model directly.

### 2.3 Event System

```python
subscription = mc.subscribe(
    EventType.CONTACT_MSG_RECV,
    async_callback,
    attribute_filters={"pubkey_prefix": "a1b2c3"}
)
```

- Callbacks are async: `async def callback(event: Event) -> None`.
- Attribute filtering is built in. Subscribe to specific `pubkey_prefix` or `channel_idx`.
- `EventDispatcher` runs an internal `asyncio.Queue` with a processing loop.
- `wait_for_event()` returns the first matching event or `None` on timeout.
- `Subscription` objects support `.unsubscribe()`.

### 2.4 EventType Enum

MEDRE-relevant event types confirmed in `meshcore/events.py`:

| EventType | Value | Purpose |
|-----------|-------|---------|
| `CONTACT_MSG_RECV` | `"contact_message"` | Direct message received |
| `CHANNEL_MSG_RECV` | `"channel_message"` | Channel message received |
| `ACK` | `"acknowledgement"` | Message delivery acknowledgment |
| `MSG_SENT` | `"message_sent"` | Outbound send confirmation |
| `MESSAGES_WAITING` | `"messages_waiting"` | Device has queued messages |
| `NO_MORE_MSGS` | `"no_more_messages"` | Message queue empty |
| `OK` | `"command_ok"` | Generic command success |
| `ERROR` | `"command_error"` | Generic command failure |
| `CONNECTED` | `"connected"` | Transport connected |
| `DISCONNECTED` | `"disconnected"` | Transport disconnected |

### 2.5 Identity Model

- 32-byte Ed25519 public keys, represented as hex strings.
- No numeric node ID. Addressing is always pubkey-based.
- Contact list is a dict keyed by full pubkey hex.
- `pubkey_prefix` in events is a truncated prefix (default 6 bytes / 12 hex chars).

### 2.6 Channel Model

- Channels are integer-indexed.
- Channel secrets are 16 bytes.
- `send_chan_msg(chan, msg)` sends to a channel by index.

### 2.7 Wire Protocol

- Custom binary. No protobuf at any layer.
- Frame payload max 255 bytes.
- Always-on E2EE (AES-128 + 2-byte HMAC).
- Completely different from Meshtastic's protobuf-based protocol.

### 2.8 Auto-Reconnect

Optional via constructor parameter `auto_reconnect=True`. Flat 1-second delay between attempts, configurable `max_reconnect_attempts`. Emits `CONNECTED` with `reconnected: True` on success, `DISCONNECTED` with `max_attempts_exceeded: True` on failure.

### 2.9 Zero MeshCore Materials in MEDRE

No `meshcore` imports exist anywhere in the MEDRE codebase. The adapter uses fake delivery only. Contract 11 confirmed this; it remains true.


## 3. Send Semantics

This section documents the exact send API behavior as observed in source. It does not claim these behaviors have been verified against real hardware.

### 3.1 send_msg (Direct Message)

```python
result = await mc.commands.send_msg(dst, msg, timestamp=None, attempt=0)
```

**Parameters:**
- `dst`: public key hex string, contact dict, or raw bytes (truncated to 6 bytes by default).
- `msg`: text string.
- `timestamp`: optional Unix timestamp. Defaults to `int(time.time())`.
- `attempt`: attempt number, included in the wire packet.

**On success** returns `Event` with:
- `type == EventType.MSG_SENT`
- `payload["expected_ack"]`: raw bytes (4 bytes). A CRC-like correlation token for the sent message.
- `payload["suggested_timeout"]`: int. Firmware-recommended ACK timeout in milliseconds.

**On failure** returns `Event` with:
- `type == EventType.ERROR`
- `payload` contains error details.

**ACK correlation:** The `expected_ack` hex string matches the `code` attribute on a subsequent `EventType.ACK` event. This is how delivery confirmation works.

```python
# Example ACK correlation pattern (from SDK source)
sent = await mc.commands.send_msg(contact, "hello")
if sent.type == EventType.MSG_SENT:
    exp_ack = sent.payload["expected_ack"].hex()
    timeout = sent.payload["suggested_timeout"] / 1000 * 1.2
    ack = await mc.wait_for_event(EventType.ACK,
                attribute_filters={"code": exp_ack}, timeout=timeout)
```

### 3.2 send_chan_msg (Channel Message)

```python
result = await mc.commands.send_chan_msg(chan, msg, timestamp=None)
```

**Parameters:**
- `chan`: integer channel index.
- `msg`: text string.
- `timestamp`: optional int (Unix) or 4 bytes. Defaults to current time.

**On success** returns `Event` with:
- `type == EventType.OK`
- Payload contains success confirmation.

**On failure** returns `Event` with:
- `type == EventType.ERROR`
- Payload contains error details.

**Key difference from send_msg:** `send_chan_msg` returns `OK`/`ERROR`, not `MSG_SENT`. There is no `expected_ack` for channel messages. Channel messages do not get individual delivery acknowledgments in the same way direct messages do.

### 3.3 send_msg_with_retry (Built-in Retry)

```python
result = await mc.commands.send_msg_with_retry(
    dst, msg, timestamp=None,
    max_attempts=3, max_flood_attempts=2, flood_after=2,
    timeout=0, min_timeout=0
)
```

This method implements a full retry loop internally:

1. Sends via `send_msg()`, extracts `expected_ack`.
2. Waits for matching `ACK` event with `attribute_filters={"code": expected_ack_hex}`.
3. Timeout is `suggested_timeout * 1.2` (or explicit `timeout` param).
4. On failure, retries up to `max_attempts`.
5. After `flood_after` failed direct attempts, resets the routing path and switches to flood mode.
6. Flood attempts capped at `max_flood_attempts`.
7. Returns the last `MSG_SENT` event on success, or `None` if all attempts fail.

**MEDRE relevance:** The scaffold adapter does not need this. MEDRE's own retry/receipt system handles delivery semantics at a higher level. Documenting it here so future implementers know it exists as an alternative.

### 3.4 expected_ack as Native Message ID

The `expected_ack` field from `send_msg` is the strongest candidate for MEDRE's `native_message_id`:

- It is deterministic (derived from message content, effectively a CRC).
- It correlates with the async ACK event.
- It is unique per send operation (different messages produce different CRCs).
- The firmware suggests a timeout for it.

However, `expected_ack` is a correlation token, not a sequentially assigned ID. Two sends of the same message content to the same destination would produce the same `expected_ack`. This differs from Meshtastic's incrementing packet ID.

**Caution:** The exact behavior of `expected_ack` with real hardware has not been verified. The collision semantics (same message, same recipient, same timestamp) need testing before relying on it as a unique identifier.

### 3.5 Fake Adapter Send Behavior

`FakeMeshCoreAdapter` in MEDRE returns `AdapterDeliveryResult` with deterministic sequential IDs in tests. This is the scaffold's own behavior, not derived from the MeshCore SDK. Real outbound IDs will follow the `expected_ack` pattern documented above when production connectivity is implemented.

**Do not assume real outbound IDs are sequential.** The fake adapter's behavior is an implementation convenience, not a reflection of MeshCore's actual wire behavior.


## 4. Inferred Findings

These are reasonable conclusions from source analysis but have not been confirmed with live hardware.

### 4.1 Target Hardware

MeshCore targets LoRa companion radio nodes running MeshCore firmware. The SDK README calls them "MeshCore companion radio nodes." The firmware source is C/C++ at `/home/jeremiah/dev/meshtastic/meshcore/MeshCore/`. The SDK connects to these nodes as peripherals, not as peers.

### 4.2 Message Fetching Model

MeshCore uses a pull model for incoming messages. The device emits `MESSAGES_WAITING` events when messages are queued. The client calls `get_msg()` to fetch the next one. Auto-fetching (`start_auto_message_fetching()`) wraps this in a loop. This is different from Meshtastic's push-based pubsub callback model.

### 4.3 Connection Lifecycle

The `connect()` method sends `appstart()` after establishing transport. The device responds with `SELF_INFO` containing the node's own public key and configuration. This is the "ready" signal. Disconnection triggers `DISCONNECTED` events and optional auto-reconnect.


## 5. Unknown Findings

These questions cannot be answered from source code alone. They require hardware, firmware documentation, or community knowledge.

### 5.1 Hardware Compatibility

Which LoRa hardware platforms run MeshCore firmware? What radios, MCUs, and firmware versions are compatible? The SDK connects generically (TCP, serial, BLE) but the firmware requirements are not documented in the SDK source.

### 5.2 Firmware Source and Build

The C/C++ firmware source exists at `/home/jeremiah/dev/meshtastic/meshcore/MeshCore/` but its build requirements, supported targets, and flashing procedures have not been audited for MEDRE purposes.

### 5.3 Bridge Feasibility

Can MeshCore and Meshtastic coexist on the same hardware? Can messages be bridged between MeshCore and Meshtastic networks? The protocols are fundamentally different (custom binary vs. protobuf, pubkey vs. nodenum, async vs. sync callbacks), so bridging would require application-level translation, not a simple relay.

### 5.4 Real Packet Shape Accuracy

The event payload shapes used in MEDRE fixtures are derived from the source audit (contract 11). Whether real MeshCore hardware produces exactly these shapes has not been verified. Fields like `pubkey_prefix` truncation length, `txt_type` values, and `sender_timestamp` behavior need live validation.

### 5.5 expected_ack Collision Behavior

If two identical messages are sent to the same recipient in rapid succession, do they produce the same `expected_ack`? If so, how does the ACK correlation distinguish them? This needs hardware testing.

### 5.6 Channel Message Delivery Confirmation

Channel messages return `OK`/`ERROR` but no `expected_ack`. How does the firmware confirm delivery to channel subscribers? Is there any per-recipient ACK for channel messages? This is not documented in the SDK source.

### 5.7 TCP Default Port

The SDK README shows port 4000 in examples. Is this a firmware default? Is it configurable on the device side? Not verified.

### 5.8 BLE PIN Behavior

BLE supports optional PIN pairing. How this interacts with MeshCore's Ed25519 identity model, whether PIN is required or optional per device, and platform-specific behavior (macOS vs. Linux vs. Windows) are not documented beyond the SDK README.


## 6. Current MEDRE Adapter Scaffold Status

### 6.1 What Exists

| Component | File | Status |
|-----------|------|--------|
| Adapter | `adapters/meshcore/adapter.py` | Scaffold. `start()` raises `MeshCoreConnectionError` for non-fake types. `deliver()` returns `None`. |
| Config | `adapters/meshcore/config.py` | Complete. Supports `fake`, `tcp`, `serial`, `ble` connection types. Has `host`, `port`, `serial_port`, `default_channel` fields. |
| Codec | `adapters/meshcore/codec.py` | Scaffold. Converts MeshCore-shaped event dicts to `CanonicalEvent`. |
| Classifier | `adapters/meshcore/packet_classifier.py` | Scaffold. Classifies by event type, detects ACKs. |
| Renderer | `adapters/meshcore/renderer.py` | Scaffold. Builds payloads for outbound. |
| Errors | `adapters/meshcore/errors.py` | Complete. `MeshCoreConnectionError`, `MeshCoreConfigError`. |

### 6.2 What Is Missing

- No `meshcore` SDK import anywhere in the codebase.
- No real client creation code in `start()`.
- No real send code in `deliver()`.
- No event subscription wiring (no `subscribe()` call for `CONTACT_MSG_RECV` or `CHANNEL_MSG_RECV`).
- No ACK watching or correlation.
- No contact list fetching.
- No message fetching (`get_msg()` or auto-fetch).
- No connection lifecycle management (reconnect, health probing).

### 6.3 Config Readiness

`MeshCoreConfig` already has the fields needed for real connectivity:

```python
connection_type: Literal["fake", "tcp", "serial", "ble"] = "fake"
host: str | None = None           # For TCP
port: int | None = None           # For TCP
serial_port: str | None = None    # For serial
default_channel: int = 0
```

Missing config fields that would be needed:
- `ble_address` and `ble_pin` for BLE connections.
- `auto_reconnect` and `max_reconnect_attempts`.
- `default_timeout` for command timeouts.


## 7. Protocol Comparison with Meshtastic

| Aspect | Meshtastic | MeshCore |
|--------|-----------|----------|
| Wire format | Protobuf `MeshPacket` | Custom binary (no protobuf) |
| Identity | NodeNum (int) + fromId (str) | Ed25519 public key (hex) |
| Addressing | Broadcast + DM by NodeNum | Contact-based by pubkey |
| Send return | `MeshPacket` with incrementing `id` | `Event` with `expected_ack` CRC + `suggested_timeout` |
| Channel send | `sendText(channelIndex=...)` | `send_chan_msg(chan, msg)` returns `OK`/`ERROR` |
| ACK | `ROUTING_APP` protobuf | Separate `ACK` event with code attribute |
| Reply threading | `replyId` (int) field | No native mechanism |
| Reactions | `emoji` (int) field | No native mechanism |
| Encryption | Optional per-packet | Always-on E2EE |
| Callback model | Sync pubsub (`meshtastic.receive`) | Async event dispatcher with attribute filtering |
| Message fetching | Push (pubsub fires on receive) | Pull (`MESSAGES_WAITING` → `get_msg()`) |
| SDK maturity | Fork `mtjk` v2.7.8 | `meshcore` v2.2.5 |
| MEDRE real client code | Exists (`_create_client`), untested | None |

These protocols are not compatible at the wire level. Bridging would require application-level message translation, not protocol-level relay.


## 8. Production Connectivity Readiness Assessment

### 8.1 What Would Be Needed

1. Add `meshcore` as an optional dependency (extras group, not default).
2. Implement real client creation in `start()` using `MeshCore.create_tcp()` / `create_serial()` / `create_ble()`.
3. Wire event subscriptions for `CONTACT_MSG_RECV` and `CHANNEL_MSG_RECV`.
4. Wire `deliver()` to `send_msg()` or `send_chan_msg()`.
5. Extract `expected_ack` as `native_message_id` from `send_msg` results.
6. Implement ACK watching for delivery confirmation.
7. Add `ble_address`, `ble_pin`, `auto_reconnect`, `default_timeout` to config.
8. Verify packet shapes against real hardware output.

### 8.2 Readiness Ranking

MeshCore is third out of four adapters in readiness (per contract 16):

1. Matrix (closest, has real client code)
2. Meshtastic (pipeline in place, client stubbed)
3. **MeshCore** (SDK documented, adapter scaffold ready, no real code)
4. LXMF (most work needed)

### 8.3 What This Document Does NOT Claim

- MeshCore connectivity works against real hardware.
- The SDK's send semantics have been verified with radio transmissions.
- The adapter scaffold is ready for production deployment.
- MeshCore is a recommended or supported transport for MEDRE.
- Any timeline for production MeshCore support.


## 9. Contract Cross-References

| Topic | Contract |
|-------|----------|
| MeshCore source audit (identity, packets, wire protocol) | `11-meshcore-source-audit.md` |
| Production connectivity readiness per adapter | `16-production-connectivity-readiness.md` |
| Operational readiness gaps | `18-operational-readiness-gaps.md` |
| Live smoke runbook | `docs/runbooks/meshcore-live-smoke.md` |
| Adapter runtime contract | `02-adapter-runtime-contract.md` |
| Adapter baseline consolidation | `15-adapter-baseline-consolidation.md` |

Contract 16 is the readiness authority. Where this document and Contract 16 conflict on readiness facts, Contract 16 takes precedence.


## 10. Explicit Out-of-Scope

- No production connectivity implementation.
- No production connection, live hardware testing, or real SDK integration.
- No default dependency on `meshcore`.
- No hardware procurement or firmware flashing instructions.
- No bridge design between MeshCore and Meshtastic.
- No timeline or priority for MeshCore production support.

*This document records readiness state. It does not advance it.*
