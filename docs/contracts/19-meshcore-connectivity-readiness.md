# MeshCore Connectivity Readiness

> Contract version: 2
> Last updated: 2026-05-12
> Supersedes: None (complements 11, 16, 18)
> Audit source: PyPI package `meshcore` v2.3.7 wheel, source-extracted inspection

This document audits MeshCore's connectivity readiness for MEDRE: what the SDK provides, what the current adapter scaffold implements, what send semantics look like, and what remains unknown. It does not claim production connectivity. Nothing here should be read as "MeshCore works against real hardware" without explicit verification.

**Tranche status**: Readiness documentation only. No production connection, no hardware testing, no default dependency.

**Audit method**: Downloaded `meshcore-2.3.7-py3-none-any.whl` from PyPI, extracted, and read all source files. No local installation. No hardware. Inspection commands used:
```bash
pip install meshcore --dry-run          # confirm PyPI availability, deps
pip download meshcore==2.3.7 --no-deps -d /tmp/meshcore_inspect
cd /tmp/meshcore_inspect && unzip -q meshcore-2.3.7-py3-none-any.whl -d meshcore_extracted
# Then Read tool on all .py files under meshcore_extracted/meshcore/
```


## 1. SDK Availability

### 1.1 Package Identity

| Property | Value | Status |
|----------|-------|--------|
| Package name | `meshcore` | CONFIRMED (PyPI) |
| Version audited | 2.3.7 | CONFIRMED (PyPI download, Apr 25 2026) |
| Previous version audited | 2.2.5 | CONFIRMED (historical, local source tree) |
| License | MIT | CONFIRMED (PyPI metadata) |
| PyPI | `pip install meshcore` | CONFIRMED |
| Source | `https://github.com/fdlamotte/meshcore_py` | INFERRED (PyPI Homepage field; repo returned 404 at time of audit) |
| Import | `import meshcore` | CONFIRMED (`__init__.py` exports) |
| Entry point | `meshcore.MeshCore` | CONFIRMED (`meshcore/meshcore.py` class `MeshCore`) |
| Python | >=3.10 | CONFIRMED (PyPI metadata) |
| Dependencies | `bleak`, `pyserial-asyncio-fast`, `pycryptodome`, `pycayennelpp` | CONFIRMED (imports in source) |
| Locally installed | No | CONFIRMED (`pip show meshcore` → NOT_INSTALLED) |

**Public `__all__` exports** (CONFIRMED from `__init__.py`):
```python
__all__ = [
    "BinaryReqType", "BLEConnection", "ConnectionManager",
    "EventType", "MeshCore", "SerialConnection",
    "TCPConnection", "logger",
]
```

### 1.2 Why MeshCore Is Not a Default Dependency

MEDRE's core tests pass without any MeshCore package installed. The adapter uses `FakeMeshCoreAdapter` with deterministic fixture data. Adding `meshcore` as a required dependency would impose `bleak`, `pyserial-asyncio-fast`, and `pycayennelpp` on all MEDRE installations, including those that never touch MeshCore hardware.

MeshCore remains an optional, user-installed dependency. The live smoke harness (documented in `docs/runbooks/meshcore-live-smoke.md`) is the only path that requires it.


## 2. Confirmed Findings

These facts are verified from source code extracted from `meshcore-2.3.7-py3-none-any.whl`. All findings labeled **CONFIRMED** are sourced from reading the actual Python source. **INFERRED** means reasonable from source patterns but not fully traced. **UNKNOWN** means cannot be determined without hardware.

### 2.1 Connection Constructors

All three constructors are async factory classmethods on `MeshCore` (CONFIRMED: `meshcore.py` lines 81-182):

```python
# TCP (CONFIRMED)
mc = await MeshCore.create_tcp(host, port, debug=False, only_error=False,
    default_timeout=None, auto_reconnect=False, max_reconnect_attempts=3)
# Returns: MeshCore instance on success, None on failure (appstart failed),
#          raises ConnectionError on transport failure.

# Serial (CONFIRMED)
mc = await MeshCore.create_serial(port, baudrate=115200, debug=False, ...)
# Same return behavior as create_tcp.
# Additional param: cx_dly=0.1 (connection delay for serial init)

# BLE (CONFIRMED)
mc = await MeshCore.create_ble(address=None, client=None, device=None,
    pin=None, debug=False, ...)
# Same return behavior as create_tcp.
# If address is None, scans for devices with local_name starting "MeshCore".
# Uses standard Nordic UART Service UUID: 6E400001-B5A3-F393-E0A9-E50E24DCCA9E
```

**Return behavior** (CONFIRMED from `meshcore.py` lines 92-108):
1. Creates transport object → creates MeshCore wrapping it → calls `mc.connect()`.
2. `mc.connect()` calls `connection_manager.connect()`, which calls transport `.connect()`.
3. If transport `.connect()` returns `None`, `mc.connect()` raises `ConnectionError("Failed to connect to device")`.
4. If transport connects but `send_appstart()` returns `None` or `ERROR`, the factory method returns `None` (does NOT raise).
5. On full success, returns the `MeshCore` instance.

**Important**: The factory can return `None` without raising. Callers must check for `None`.

### 2.2 Async API

The entire SDK is async-native (CONFIRMED). All transport classes use `asyncio.Protocol`. `connect()`, `disconnect()`, `send_msg()`, `send_chan_msg()`, `subscribe()`, `wait_for_event()` are all coroutines. No synchronous wrappers exist. This matches MEDRE's async adapter model directly.

**Dispatcher lifecycle** (CONFIRMED from `events.py`):
- `EventDispatcher.start()` must be called before any dispatch. Creates the internal `asyncio.Queue`.
- `EventDispatcher.stop()` drains the queue, waits for in-flight async callbacks, then cancels the processing task.
- `MeshCore.connect()` calls `dispatcher.start()` automatically.
- `MeshCore.disconnect()` calls `dispatcher.stop()` automatically.

**Command handler** (CONFIRMED from `commands/__init__.py`):
- `mc.commands` is a `CommandHandler` that inherits from `DeviceCommands`, `ContactCommands`, `MessagingCommands`, `BinaryCommandHandler`, `ControlDataCommandHandler`.
- Default timeout: 15.0 seconds (`CommandHandlerBase.DEFAULT_TIMEOUT`).
- Commands are serialized through an `asyncio.Lock` (lazy-created on first access).

### 2.3 Event System

```python
subscription = mc.subscribe(
    EventType.CONTACT_MSG_RECV,
    async_callback,
    attribute_filters={"pubkey_prefix": "a1b2c3"}
)
```

- Callbacks are async: `async def callback(event: Event) -> None`. CONFIRMED.
- Attribute filtering is built in: checks `event.attributes.get(key) == value` for all filter entries. CONFIRMED.
- `EventDispatcher` runs an internal `asyncio.Queue` with a processing loop (`_process_events`). CONFIRMED.
- `wait_for_event()` creates a temporary subscription, awaits via `asyncio.Future` with `asyncio.wait_for()`, returns `Event` or `None` on timeout. CONFIRMED.
- `Subscription` objects support `.unsubscribe()`. CONFIRMED.
- Async callbacks are spawned as background tasks (`_spawn_background`); sync callbacks are called inline. CONFIRMED.
- Events are cloned before dispatch to each subscriber (`event.clone()`). CONFIRMED.

### 2.3.1 Event Dataclass Shape (CONFIRMED from `events.py`)

```python
@dataclass
class Event:
    type: EventType
    payload: Any
    attributes: Dict[str, Any]  # default_factory=dict

    def is_error(self) -> bool:
        """Returns True if self.type == EventType.ERROR"""
        return self.type == EventType.ERROR

    def clone(self) -> Event:
        """Creates a copy of the event."""
        ...
```

**Key**: `payload` is `Any`, not always a dict. For ERROR events, payload is `{"reason": "..."}`. For CONTACTS events, payload is a dict of contacts. For SELF_INFO, payload is a dict of device info. The `attributes` field carries metadata used for filtering (e.g., `pubkey_prefix`, `code`, `lastmod`).

### 2.3.2 Error Code Mapping (CONFIRMED from `events.py`)

```python
ErrorMessages = {
    1: "ERR_CODE_UNSUPPORTED_CMD",
    2: "ERR_CODE_NOT_FOUND",
    3: "ERR_CODE_TABLE_FULL",
    4: "ERR_CODE_BAD_STATE",
    5: "ERR_CODE_FILE_IO_ERROR",
    6: "ERR_CODE_ILLEGAL_ARG",
}
```

### 2.4 EventType Enum

MEDRE-relevant event types CONFIRMED in `meshcore/events.py` (v2.3.7). The full enum has 50+ values; only MEDRE-relevant ones listed:

| EventType | Value | Purpose | Status |
|-----------|-------|---------|--------|
| `CONTACT_MSG_RECV` | `"contact_message"` | Direct message received | CONFIRMED |
| `CHANNEL_MSG_RECV` | `"channel_message"` | Channel message received | CONFIRMED |
| `ACK` | `"acknowledgement"` | Message delivery acknowledgment | CONFIRMED |
| `MSG_SENT` | `"message_sent"` | Outbound send confirmation | CONFIRMED |
| `MESSAGES_WAITING` | `"messages_waiting"` | Device has queued messages | CONFIRMED |
| `NO_MORE_MSGS` | `"no_more_messages"` | Message queue empty | CONFIRMED |
| `OK` | `"command_ok"` | Generic command success | CONFIRMED |
| `ERROR` | `"command_error"` | Generic command failure | CONFIRMED |
| `CONNECTED` | `"connected"` | Transport connected | CONFIRMED |
| `DISCONNECTED` | `"disconnected"` | Transport disconnected | CONFIRMED |
| `SELF_INFO` | `"self_info"` | Device identity/config on appstart | CONFIRMED |
| `CONTACTS` | `"contacts"` | Contact list response | CONFIRMED |
| `NEW_CONTACT` | `"new_contact"` | New contact discovered | CONFIRMED |
| `NEXT_CONTACT` | `"next_contact"` | Paginated contact response | CONFIRMED |
| `CURRENT_TIME` | `"time_update"` | Device time update | CONFIRMED |
| `BATTERY` | `"battery_info"` | Battery status | CONFIRMED |
| `DEVICE_INFO` | `"device_info"` | Device info response | CONFIRMED |
| `ADVERT_PATH` | `"advert_path"` | Advertisement path data | CONFIRMED |
| `AUTOADD_CONFIG` | `"autoadd_config"` | Auto-add configuration | CONFIRMED |
| `LOGIN_SUCCESS` | `"login_success"` | Remote login succeeded | CONFIRMED |
| `LOGIN_FAILED` | `"login_failed"` | Remote login failed | CONFIRMED |

Additional event types exist for advanced features (telemetry, binary data, tracing, stats, ACL, MMA, etc.) — see `events.py` source for complete list.

### 2.5 Identity Model

- 32-byte Ed25519 public keys, represented as hex strings. CONFIRMED (from `_validate_destination` and contact handling).
- No numeric node ID. Addressing is always pubkey-based. CONFIRMED.
- Contact list is a dict keyed by full pubkey hex. CONFIRMED (`self._contacts` dict, updated via `CONTACTS` events).
- `pubkey_prefix` in events is a truncated prefix (default 6 bytes / 12 hex chars). CONFIRMED (`_validate_destination` default `prefix_length=6`).
- Contact lookup by name: `mc.get_contact_by_name(name)` — case-insensitive. CONFIRMED.
- Contact lookup by key prefix: `mc.get_contact_by_key_prefix(prefix)` — partial prefix match. CONFIRMED.

### 2.6 Channel Model

- Channels are integer-indexed. CONFIRMED (`send_chan_msg(chan, msg)`).
- Channel secrets are 16 bytes. INFERRED (from protocol analysis; not visible in Python SDK layer).
- `send_chan_msg(chan, msg)` sends to a channel by index. CONFIRMED.

### 2.7 Wire Protocol

- Custom binary. No protobuf at any layer. CONFIRMED (all commands are raw byte construction).
- Frame format (serial send): `0x3c` + 2-byte little-endian length + payload. CONFIRMED (`serial_cx.py` line 144).
- Frame format (receive, serial/TCP): searches for `0x3e` start byte, 2-byte little-endian length, then payload. CONFIRMED (`serial_cx.py` and `tcp_cx.py` handle_rx).
- Frame size limit: frames > 300 bytes treated as invalid and discarded. CONFIRMED (`frame_expected_size > 300` check).
- Always-on E2EE (AES-128 + 2-byte HMAC). INFERRED (Crypto imports present; actual crypto in parser layer not fully traced).
- Completely different from Meshtastic's protobuf-based protocol. CONFIRMED.

### 2.8 Auto-Reconnect

Optional via constructor parameter `auto_reconnect=True`. CONFIRMED from `connection_manager.py`:
- Flat 1-second delay between attempts: `await asyncio.sleep(1)`. CONFIRMED.
- Configurable `max_reconnect_attempts`. CONFIRMED.
- On reconnect success: emits `CONNECTED` with `{"connection_info": result, "reconnected": True}`. CONFIRMED.
- On reconnect failure: emits `DISCONNECTED` with `{"reason": "reconnect_failed", "max_attempts_exceeded": True}`. CONFIRMED.
- Reconnect callback: `ConnectionManager` calls `self._reconnect_callback()` after successful reconnect. `MeshCore` registers `_on_reconnect` which calls `send_appstart()`. CONFIRMED.
- Reconnect loop is iterative (not recursive). CONFIRMED.

### 2.9 Disconnect and Cleanup (CONFIRMED from `meshcore.py`)

```python
async def disconnect(self):
    await self.dispatcher.stop()           # stop event processing
    if hasattr(self, "_auto_fetch_subscription") and self._auto_fetch_subscription:
        await self.stop_auto_message_fetching()
    await self.connection_manager.disconnect()  # NOT close()
```

- Method is `disconnect()`, not `close()`. CONFIRMED.
- `connection_manager.disconnect()` cancels any reconnect task, then calls `transport.disconnect()`.
- `dispatcher.stop()` drains queue, waits for in-flight callbacks, cancels processing task.
- Auto-fetch subscription is cleaned up if active.

### 2.10 Exception Classes / Failure Modes

The SDK does NOT define custom exception classes. CONFIRMED (no `exceptions.py`, no custom exception types).
- Transport failure: raises `ConnectionError("Failed to connect to device")`. CONFIRMED.
- Invalid destination: raises `ValueError`. CONFIRMED (`_validate_destination`).
- BLE without bleak: raises `ImportError("BLE requires 'bleak' package to be installed")`. CONFIRMED.
- Serial transport loss: triggers disconnect callback, not an exception. CONFIRMED.
- TCP connection lost: triggers disconnect callback via `connection_lost()`. CONFIRMED.
- Command timeout: `asyncio.TimeoutError` from `wait_for_event()`. CONFIRMED.
- Serial write failure: `OSError` caught, triggers disconnect callback. CONFIRMED.

### 2.11 Zero MeshCore Materials in MEDRE

No `meshcore` imports exist anywhere in the MEDRE codebase. The adapter uses fake delivery only. Contract 64 confirmed this; it remains true. CONFIRMED (`pip show meshcore` → NOT_INSTALLED).


## 3. Send Semantics

This section documents the exact send API behavior as observed in source. It does not claim these behaviors have been verified against real hardware.

### 3.1 send_msg (Direct Message)

```python
result = await mc.commands.send_msg(dst, msg, timestamp=None, attempt=0)
```

CONFIRMED from `commands/messaging.py` lines 82-101.

**Parameters:**
- `dst`: public key hex string, contact dict (with `"public_key"` field), or raw bytes (truncated to 6 bytes by default via `_validate_destination`). CONFIRMED.
- `msg`: text string. CONFIRMED.
- `timestamp`: optional Unix timestamp. Defaults to `int(time.time())`. CONFIRMED.
- `attempt`: attempt number (1 byte, little-endian), included in the wire packet. CONFIRMED.

**On success** returns `Event` with:
- `type == EventType.MSG_SENT`. CONFIRMED (awaited event types list).
- `payload["expected_ack"]`: raw bytes. INFERRED as ~4 bytes CRC-like correlation token (payload shape comes from response parser, not from send code).
- `payload["suggested_timeout"]`: int. INFERRED as milliseconds (firmware-recommended ACK timeout).

**On failure** returns `Event` with:
- `type == EventType.ERROR`. CONFIRMED.
- `payload` contains `{"reason": "..."}`. CONFIRMED.

**ACK correlation:** The `expected_ack` hex string matches the `code` attribute on a subsequent `EventType.ACK` event. INFERRED from `send_msg_with_retry` implementation which does `exp_ack = result.payload["expected_ack"].hex()` and waits for `attribute_filters={"code": exp_ack}`.

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

CONFIRMED from `commands/messaging.py` lines 167-189.

**Parameters:**
- `chan`: integer channel index (1 byte, little-endian). CONFIRMED.
- `msg`: text string. CONFIRMED.
- `timestamp`: optional int (Unix) or 4 bytes. Defaults to `int(time.time()).to_bytes(4, "little")`. CONFIRMED (explicit int|bytes type union).

**On success** returns `Event` with:
- `type == EventType.OK`. CONFIRMED (awaited event types list).
- Payload contains success confirmation.

**On failure** returns `Event` with:
- `type == EventType.ERROR`. CONFIRMED.
- Payload contains error details.

**Key difference from send_msg:** `send_chan_msg` returns `OK`/`ERROR`, not `MSG_SENT`. There is no `expected_ack` for channel messages. Channel messages do not get individual delivery acknowledgments in the same way direct messages do. CONFIRMED.

### 3.3 send_msg_with_retry (Built-in Retry)

```python
result = await mc.commands.send_msg_with_retry(
    dst, msg, timestamp=None,
    max_attempts=3, max_flood_attempts=2, flood_after=2,
    timeout=0, min_timeout=0
)
```

CONFIRMED from `commands/messaging.py` lines 103-165.

This method implements a full retry loop internally:

1. Sends via `send_msg()`, extracts `expected_ack`. CONFIRMED.
2. Waits for matching `ACK` event with `attribute_filters={"code": expected_ack_hex}`. CONFIRMED.
3. Timeout is `suggested_timeout / 1000 * 1.2` (or explicit `timeout` param). CONFIRMED.
4. On failure, retries up to `max_attempts`. CONFIRMED.
5. After `flood_after` failed direct attempts, resets the routing path via `reset_path()` and switches to flood mode. CONFIRMED.
6. Flood attempts capped at `max_flood_attempts`. CONFIRMED.
7. Returns the last `MSG_SENT` event on success, or `None` if all attempts fail. CONFIRMED.

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

MeshCore targets LoRa companion radio nodes running MeshCore firmware. The SDK README calls them "MeshCore companion radio nodes." INFERRED. The SDK connects to these nodes as peripherals (TCP, serial, or BLE client), not as peers.

### 4.2 Message Fetching Model

MeshCore uses a pull model for incoming messages. CONFIRMED from `meshcore.py` `start_auto_message_fetching()`:
- The device emits `MESSAGES_WAITING` events when messages are queued.
- The client calls `get_msg()` to fetch the next one. CONFIRMED from `commands/messaging.py`.
- Auto-fetching (`start_auto_message_fetching()`) subscribes to `MESSAGES_WAITING` and loops `get_msg()` until `NO_MORE_MSGS` or `ERROR`. CONFIRMED.
- Auto-fetch calls `get_msg()` once immediately on start (checks for pending messages). CONFIRMED.
- This is different from Meshtastic's push-based pubsub callback model. CONFIRMED.

### 4.3 Connection Lifecycle

The `connect()` method (CONFIRMED from `meshcore.py`):
1. Calls `dispatcher.start()` to create queue and start processing task.
2. Calls `connection_manager.connect()` which calls transport `.connect()`.
3. If transport returns `None`, raises `ConnectionError`.
4. Calls `commands.send_appstart()` which sends `\x01\x03      mccli` to the device.
5. `appstart()` awaits `[EventType.SELF_INFO, EventType.ERROR]`.
6. Returns the `SELF_INFO` event (contains node's public key and config).

### 4.4 BLE Connection Details (CONFIRMED from `ble_cx.py`)

- BLE scanning filters for devices with `local_name` starting with `"MeshCore"`.
- Uses Nordic UART Service UUIDs for communication.
- PIN pairing calls `client.pair()` on the BleakClient.
- If pairing fails, connection is left in "half-usable state" (logged as error but not disconnected).
- BLE device discovery is via `BleakScanner.find_device_by_filter()`.
- Pre-existing `BleakClient` or `BLEDevice` can be passed directly.

### 4.5 Serial Connection Details (CONFIRMED from `serial_cx.py`)

- Uses `pyserial_asyncio_fast` (not `pyserial_asyncio`).
- Connection has a `cx_dly` parameter (default 0.2 in SerialConnection, but MeshCore.create_serial passes 0.1).
- Serial transport has a `_connected_event` (asyncio.Event) for connection confirmation.
- Default connect timeout: 10.0 seconds. Raises `asyncio.TimeoutError` on expiry.
- RTS line is set to `False` on connection (prevents reset on some hardware).
- Send frame format: `0x3c` + 2-byte little-endian length + payload.
- Receive frame format: searches for `0x3e` start byte.

### 4.6 TCP Connection Details (CONFIRMED from `tcp_cx.py`)

- Uses `asyncio.Protocol` with `loop.create_connection()`.
- Receive frame format: searches for `0x3e` start byte, 2-byte little-endian length, then payload.
- Same frame parsing logic as serial (shared handle_rx pattern).
- TCP disconnect threshold counter for detecting silent disconnections.


## 5. Unknown Findings

These questions cannot be answered from source code alone. They require hardware, firmware documentation, or community knowledge.

### 5.1 Hardware Compatibility

Which LoRa hardware platforms run MeshCore firmware? What radios, MCUs, and firmware versions are compatible? The SDK connects generically (TCP, serial, BLE) but the firmware requirements are not documented in the SDK source.

### 5.2 Firmware Source and Build

The C/C++ firmware source is referenced by the SDK author but its build requirements, supported targets, and flashing procedures have not been audited for MEDRE purposes. The GitHub repo (`fdlamotte/meshcore_py`) returned 404 at time of audit.

### 5.3 Bridge Feasibility

Can MeshCore and Meshtastic coexist on the same hardware? Can messages be bridged between MeshCore and Meshtastic networks? The protocols are fundamentally different (custom binary vs. protobuf, pubkey vs. nodenum, async vs. sync callbacks), so bridging would require application-level translation, not a simple relay.

### 5.4 Real Packet Shape Accuracy

The event payload shapes used in MEDRE fixtures are derived from the source audit (Contract 64). Whether real MeshCore hardware produces exactly these shapes has not been verified. Fields like `pubkey_prefix` truncation length, `txt_type` values, and `sender_timestamp` behavior need live validation.

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
- `default_timeout` for command timeouts (SDK default: 15.0 seconds).
- `cx_dly` for serial connection delay (SDK default: 0.1 in create_serial).
- `debug` and `only_error` for logging control.


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
| Callback model | Sync pubsub (`meshtastic.pub`) | Async EventDispatcher with queue + attribute filters |
| Disconnect method | N/A (stream-based) | `await mc.disconnect()` (NOT `close()`) |
| Message fetching | Push (pubsub fires on receive) | Pull (`MESSAGES_WAITING` → `get_msg()`) |
| SDK maturity | Fork `mtjk` v2.7.8 | `meshcore` v2.3.7 (PyPI) |
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
| MeshCore source audit (identity, packets, wire protocol) | `64-meshcore-source-audit.md` |
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
