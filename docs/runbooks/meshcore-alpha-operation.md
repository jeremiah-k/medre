# MeshCore Alpha Operation Runbook

> Last updated: 2026-05-09
> Scope: Real MeshCore Operation Alpha
> Status: Alpha. Not production. Not hardened. Not complete. Fake mode is the primary development and testing path. Real connectivity (TCP/serial) is implemented via `MeshCoreSession`; BLE is future.

This runbook describes how to run the MEDRE MeshCore adapter against a real MeshCore radio node in alpha mode. Alpha mode means the MeshCoreAdapter connects to a real node using TCP, serial, or BLE, receives real events via the SDK's async event dispatcher, and sends real messages via the channel or direct-message send API. It does not mean the system is ready for anything beyond a single operator on a single node.

Everything in this document is conservative. If something has not been tested against a real node and confirmed working, this document says so. If something is known to be broken or missing, this document says that too.

**Fake mode** is the default and recommended path for all development and testing. Real connectivity modes (TCP, serial, BLE) are opt-in for live validation only.


## 1. Purpose

Alpha operation validates that the MEDRE MeshCore adapter works end to end against a real MeshCore radio node with real radio traffic. This is the first time the adapter leaves fake and mock territory.

Scope boundaries:

- One transport: MeshCore. No other transports are in scope for this runbook.
- One operator: a single person running against a local or network-accessible node.
- Text messages on a single channel. Direct messages are classified but not independently routed in alpha.
- No production deployment, no scaling, no monitoring, no alerting.
- No claims about reliability, durability, or correctness beyond what manual testing confirms.

This runbook complements `docs/runbooks/meshcore-live-smoke.md`. The smoke test documents SDK connectivity procedures. Alpha operation validates the full wiring: config, adapter, codec, inbound event dispatch, outbound delivery, and health, running together.


## 2. Prerequisites

| Requirement | Details |
|------------|---------|
| MeshCore node | A MeshCore companion radio node accessible via TCP, serial, or BLE |
| Python | 3.11 or later |
| Package install | Core MEDRE: `pip install -e .` (no extra required for fake mode). Real connectivity: `pip install meshcore` |
| Network access (TCP) | Your machine can reach the node's IP address on port 4000 |
| Serial access | USB cable connecting the node; user must be in `dialout` group on Linux |
| BLE access | BLE-capable hardware and BlueZ on Linux (optional) |

You do not need Docker for basic alpha operation. Docker guidance is in section 11.


## 3. Node Setup

You need a MeshCore companion radio node that the MEDRE process can reach. The node must be powered on and flashed with MeshCore firmware.

### 3.1 TCP connectivity (recommended for testing)

Most MeshCore nodes expose a TCP interface when connected via WiFi or Ethernet. The SDK default port is 4000.

1. Power on the node and connect it to your network (WiFi or Ethernet).
2. Find the node's IP address (check your router's DHCP table, or consult the node's display or serial output).
3. Verify TCP connectivity:

```bash
# Quick connectivity check
nc -zv 192.168.1.100 4000
```

4. Optionally verify with the MeshCore SDK directly:

```python
import asyncio
from meshcore import MeshCore

async def check():
    mc = await MeshCore.create_tcp("192.168.1.100", 4000)
    print(f"Connected: {mc.is_connected}")
    print(f"Self info: {mc.self_info}")
    await mc.disconnect()

asyncio.run(check())
```

### 3.2 Serial connectivity

Connect the node via USB. The node will appear as a serial device.

1. Connect the USB cable.
2. Find the serial port:

```bash
# Linux
ls /dev/ttyUSB* /dev/ttyACM*
# Or use Python
python -c "import serial.tools.list_ports; [print(p.device) for p in serial.tools.list_ports.comports()]"
```

3. Ensure your user has serial port access:

```bash
sudo usermod -aG dialout $USER
# Log out and back in
```

### 3.3 BLE connectivity

BLE is a supported connection type in the adapter but is **not exercised in the live smoke harness** and has not been validated against real hardware in alpha. It is documented for completeness.

```bash
# Requires BlueZ on Linux
bluetoothctl scan on
# Note the MAC address of your MeshCore node
```

### 3.4 Firmware compatibility

`meshcore` v2.2.5 is the audited SDK version. Firmware compatibility depends on the node's firmware version. If you encounter protocol errors, update both the node firmware and the `meshcore` package.


## 4. Connection Modes

The adapter supports four connection types via `MeshCoreConfig.connection_type`:

### 4.1 Fake mode (default)

No real client. Used for development and testing without hardware.

```python
from medre.adapters.meshcore.config import MeshCoreConfig

config = MeshCoreConfig(
    adapter_id="meshcore-alpha",
    connection_type="fake",
)
```

In fake mode:
- `start()` sets `_client = None`. No network or serial activity.
- `deliver()` returns `None` (no real send).
- `simulate_inbound()` is available for injecting test packets.
- `health_check()` returns `"healthy"` after start.

**Fake mode is the recommended default for all development and testing.**

### 4.2 TCP mode

Connects to a MeshCore node via its TCP interface.

```python
config = MeshCoreConfig(
    adapter_id="meshcore-alpha",
    connection_type="tcp",
    host="192.168.1.100",   # node IP address
    port=4000,               # optional, defaults to 4000
)
```

- Uses `await MeshCore.create_tcp(host, port)` from the SDK.
- Default port: 4000 (from SDK examples, not verified as firmware default).
- Connection is fully async. `create_tcp` handles transport setup, connect, and `appstart()`.
- `appstart()` triggers a `SELF_INFO` event with the node's public key and config.
- Real client creation is handled by `MeshCoreSession`. The session calls `MeshCore.create_tcp()` and subscribes to `CONTACT_MSG_RECV` and `CHANNEL_MSG_RECV`.

**Current status:** Real TCP connections work via `MeshCoreSession`. See section 6.

### 4.3 Serial mode

Connects to a MeshCore node via USB serial.

```python
config = MeshCoreConfig(
    adapter_id="meshcore-alpha",
    connection_type="serial",
    serial_port="/dev/ttyUSB0",
)
```

- Uses `await MeshCore.create_serial(port, baudrate=115200)`.
- Default baudrate: 115200.
- Port must exist and be accessible (user in `dialout` group on Linux).

**Current status:** Real serial connections work via `MeshCoreSession`. Same TCP behavior.

### 4.4 BLE mode

Connects via Bluetooth Low Energy.

```python
config = MeshCoreConfig(
    adapter_id="meshcore-alpha",
    connection_type="ble",
    ble_address="AA:BB:CC:DD:EE:FF",
)
```

- Uses `await MeshCore.create_ble(address, pin=None)`.
- `address` is the BLE MAC address. If omitted, the SDK scans for devices.
- Optional `pin` enables BLE pairing authentication.
- Requires `bleak` (installed automatically with `meshcore`).
- BLE testing requires BLE-capable hardware and OS support (BlueZ on Linux).

**Current status:** BLE is not yet implemented in the session. Use TCP or serial for live testing.

### 4.5 Configuration validation

`MeshCoreConfig.validate()` enforces:

| Connection type | Required fields |
|----------------|-----------------|
| `fake` | `adapter_id` only |
| `tcp` | `adapter_id`, `host` |
| `serial` | `adapter_id`, `serial_port` |
| `ble` | `adapter_id`, `ble_address` |

Additional validation rules:
- `identity` (if provided) must be a non-empty string.
- `pubkey` (if provided) must be a non-empty hex string.
- `node_config` must not contain keys named `private_key`, `secret`, or `password`.
- `message_delay_seconds` >= 0, `default_channel` >= 0, `sync_timeout_ms` > 0.

Invalid configurations raise `MeshCoreConfigError` before any connection attempt.


## 5. Running MEDRE in Alpha Mode

### 5.1 Environment variables

The live smoke tests use environment variables to configure the connection. The adapter itself is configured via `MeshCoreConfig` (see section 4).

| Variable | Required for | Default | Example | Description |
|----------|-------------|---------|---------|-------------|
| `MESHCORE_CONNECTION_TYPE` | All | | `tcp` | Connection mode: `tcp`, `serial`, `ble` |
| `MESHCORE_HOST` | TCP | | `192.168.1.100` | Node hostname or IP |
| `MESHCORE_PORT` | TCP | `4000` | `4000` | TCP port |
| `MESHCORE_SERIAL_PORT` | Serial | | `/dev/ttyUSB0` | Serial device path |
| `MESHCORE_BLE_ADDRESS` | BLE | | `AA:BB:CC:DD:EE:FF` | BLE MAC address |
| `MESHCORE_BLE_PIN` | BLE (optional) | | `123456` | BLE pairing PIN |
| `MESHCORE_CHANNEL_INDEX` | All | `0` | `0` | Channel for test messages |
| `MESHCORE_DESTINATION` | DM tests | | `a1b2c3...` | Hex pubkey prefix for direct message target |

### 5.2 Manual adapter wiring

There is no dedicated MeshCore runner (unlike Matrix which has `python -m medre.runner`). For alpha operation, wire the adapter manually:

```python
import asyncio
import logging

from medre.adapters.meshcore.adapter import MeshCoreAdapter
from medre.adapters.meshcore.config import MeshCoreConfig
from medre.adapters.base import AdapterContext
from medre.core.events.event_bus import EventBus

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("meshcore-alpha")

config = MeshCoreConfig(
    adapter_id="meshcore-alpha",
    connection_type="tcp",
    host="192.168.1.100",
    port=4000,
).validate()

adapter = MeshCoreAdapter(config)
event_bus = EventBus()
ctx = AdapterContext(event_bus=event_bus, logger=logger)

async def main():
    await adapter.start(ctx)
    logger.info("Adapter started: %s", await adapter.health_check())

    # Inbound events arrive via SDK event dispatcher callbacks
    # and are published to ctx.publish_inbound().
    # Subscribe to the event bus to see them.
    # See section 10 for validation procedures.

    try:
        await asyncio.Event().wait()  # block until interrupted
    except asyncio.CancelledError:
        pass
    finally:
        await adapter.stop()
        logger.info("Adapter stopped")

asyncio.run(main())
```

### 5.3 Using the live smoke tests for validation

For quick validation, use the live smoke tests:

```bash
pip install meshcore

export MESHCORE_CONNECTION_TYPE="tcp"
export MESHCORE_HOST="192.168.1.100"
export MESHCORE_CHANNEL_INDEX="0"

pytest tests/test_meshcore_live.py -m live -v
```

See `docs/runbooks/meshcore-live-smoke.md` for full smoke test documentation.


## 6. Startup and Shutdown Behavior

### 6.1 Startup sequence

When `start(ctx)` is called:

**Fake mode (current default):**
1. The adapter sets `_client = None`. No network or serial activity.
2. `_started` is set to `True`.
3. A startup log line is emitted: `"MeshCoreAdapter meshcore-alpha started (mode=fake)"`.

**Real mode (TCP/serial — implemented via MeshCoreSession):**
1. The adapter creates a `MeshCoreSession`, which checks `HAS_MESHCORE` (the `meshcore` import guard in `compat.py`). If `meshcore` is not installed, raises `MeshCoreConnectionError`.
2. The session calls the appropriate async SDK factory (`MeshCore.create_tcp()`, `create_serial()`). This call is **async** and blocks until the connection is established and `appstart()` completes.
3. On success, the SDK emits a `SELF_INFO` event containing the node's Ed25519 public key and configuration.
4. The session subscribes to `CONTACT_MSG_RECV` and `CHANNEL_MSG_RECV` via the SDK's event dispatcher.
5. `_started` is set to `True`.
6. A startup log line is emitted: `"MeshCoreAdapter meshcore-alpha started (mode=tcp)"`.

**BLE mode:** BLE connectivity is documented from SDK source analysis but not yet implemented in the session. Use TCP or serial for live testing.

### 6.2 Expected startup output

**Fake mode:**
```
INFO  MeshCoreAdapter meshcore-alpha started (mode=fake)
```

**Real mode (TCP/serial):**
```
INFO  MeshCoreAdapter meshcore-alpha started (mode=tcp)
```

If startup fails, you will see one of:
- `MeshCoreConnectionError: meshcore SDK not installed; pip install meshcore or use connection_type='fake'`
- `MeshCoreConnectionError: Failed to create tcp client: <underlying error>`
- `MeshCoreConnectionError: Failed to subscribe to events: <error>`

### 6.3 Shutdown sequence

When `stop()` is called:

1. All tracked background tasks (from inbound event processing) are cancelled and drained (with a configurable timeout, default 5 seconds).
2. Event subscriptions are unsubscribed via `_unsubscribe_events()`.
3. The client's `close()` method is called (if it exists on the SDK client).
4. `_client` is set to `None`, `_started` is set to `False`.
5. A shutdown log line is emitted: `"MeshCoreAdapter meshcore-alpha stopped"`.

Shutdown is **idempotent** — calling `stop()` on an already-stopped adapter is a no-op.

### 6.4 Start/stop cycle safety

`start()` and `stop()` are both idempotent. Repeated start/stop cycles are safe:

```python
await adapter.start(ctx)   # connects
await adapter.stop()       # disconnects, clears state
await adapter.start(ctx)   # reconnects fresh
await adapter.stop()       # disconnects again
```

State resets on stop: `_client = None`, `_started = False`, `_subscribed = False`, background tasks cleared. Each `start()` creates a new client from scratch.


## 7. Expected Logs and Diagnostics

### 7.1 Health check states

The adapter's `health_check()` returns an `AdapterInfo` with a `health` field:

| State | Meaning |
|-------|---------|
| `unknown` | Adapter has not started, or has been stopped cleanly |
| `healthy` | Adapter has started successfully; `_started` is `True` |
| `failed` | Client exists but start did not complete (subscription failure) |

There are no intermediate states. There is no `degraded` or `reconnecting` state in the current adapter.

### 7.2 Diagnostics interpretation

When diagnostics are available (from a future runner or manual inspection):

| Diagnostic field | Meaning | How to verify |
|-----------------|---------|---------------|
| `connected` | SDK client reports `is_connected` | Check `mc.is_connected` on the SDK client |
| `node_id` | Node's Ed25519 public key (hex) from `SELF_INFO` | Emitted on startup via `appstart()` response |
| `reconnecting` | SDK auto-reconnect is attempting reconnection | Emitted as `CONNECTED` event with `reconnected: True` |
| `contacts_count` | Number of contacts in the node's contact list | Via `mc.commands.get_contacts()` |
| `delivery_attempts` | Outbound send attempts since start | Adapter-level counter |
| `delivery_successes` | Successful sends (MSG_SENT or OK result) | Adapter-level counter |
| `delivery_failures` | Failed sends (ERROR result or exception) | Adapter-level counter |

### 7.3 Inbound event processing

When a real event arrives via the SDK's event dispatcher:

1. The subscribed callback fires for `CONTACT_MSG_RECV` or `CHANNEL_MSG_RECV`.
2. The event payload dict is classified by `MeshCorePacketClassifier`. Only `text` category packets are processed; ACK packets (`code` field present) are silently dropped.
3. The event payload is decoded into a `CanonicalEvent` by `MeshCoreCodec`.
4. The canonical event is published inbound via `ctx.publish_inbound()` in an async background task.

There is no periodic "still alive" log. Silence is normal when no events arrive. The SDK event dispatcher runs passively.

### 7.4 Outbound send path

`deliver(result)` accepts a `RenderingResult`, and the outbound path calls `send_text()` on the session (which delegates to `send_chan_msg()` or `send_msg()` on the SDK client):

- **Channel messages:** `await mc.commands.send_chan_msg(chan, msg)` returns `Event` with `type == OK` on success or `type == ERROR` on failure.
- **Direct messages:** `await mc.commands.send_msg(dst, msg)` returns `Event` with `type == MSG_SENT` and `payload["expected_ack"]` on success, or `type == ERROR` on failure.

In fake mode, `deliver()` returns `None` — no real send occurs.


## 8. Canonical Metadata Structure

The MeshCore codec preserves the following metadata from native event payloads into the canonical event:

### 8.1 Native metadata

Every decoded text event carries a `NativeMetadata` block:

```python
NativeMetadata(data={
    "packet_id": 1715289600,          # sender_timestamp integer from the event
    "sender_id": "a1b2c3d4e5f6",      # pubkey_prefix (truncated pubkey hex)
    "channel": 0,                      # channel_idx from the event
    "pubkey_prefix": "a1b2c3d4e5f6",  # same as sender_id
    "txt_type": None,                  # txt_type field from the event
    "is_direct_message": False,        # True if type == "PRIV"
})
```

| Field | Source | Notes |
|-------|--------|-------|
| `packet_id` | `packet["sender_timestamp"]` | Integer timestamp from sender |
| `sender_id` | `packet["pubkey_prefix"]` | Truncated Ed25519 public key prefix (default 6 bytes / 12 hex chars) |
| `channel` | `packet["channel_idx"]` | Channel index for channel messages |
| `pubkey_prefix` | `packet["pubkey_prefix"]` | Same value as `sender_id` |
| `txt_type` | `packet["txt_type"]` | Text type field (may be `None`) |
| `is_direct_message` | Derived from `packet["type"]` | `True` if `type == "PRIV"`, `False` if `type == "CHAN"` |

### 8.2 Source native ref

Each decoded event carries a `source_native_ref` linking back to the original event:

```python
NativeRef(
    adapter="meshcore-alpha",
    native_channel_id="0",              # channel index as string
    native_message_id="1715289600",     # sender_timestamp as string
)
```

### 8.3 Canonical event fields

| Field | Value |
|-------|-------|
| `event_id` | UUID4 (generated by codec) |
| `event_kind` | `MESSAGE_CREATED` |
| `source_transport_id` | Sender pubkey prefix (`sender_id`) |
| `source_channel_id` | Channel index as string |
| `payload` | `{"body": "<text>"}` |

### 8.4 Identity model note

MeshCore uses Ed25519 keypair identity. There is no numeric node ID. Addressing is always by public key hex string. The `pubkey_prefix` in events is a truncated prefix (default 6 bytes / 12 hex chars), not the full public key. This means:
- Two different nodes could theoretically share the same short prefix.
- The full public key is available in the SDK's contact list but is not carried in individual event payloads.
- Do not assume `sender_id` is globally unique. For uniqueness guarantees, resolve the full public key via `get_contacts()`.


## 9. Outbound Delivery and Retry Semantics

### 9.1 Outbound delivery path

1. `deliver(result)` accepts a `RenderingResult` and extracts the pre-rendered payload.
2. For channel messages: `await mc.commands.send_chan_msg(chan, msg)`.
3. For direct messages: `await mc.commands.send_msg(dst, msg)`.

In fake mode, `deliver()` returns `None` — no real send occurs.

### 9.2 Retry semantics (current: none at adapter level)

**The adapter does not implement outbound retry logic.** When a send fails:
- The failed item is **permanently dropped**.
- The exception is re-raised to the caller.

This is an explicit scaffold design choice.

### 9.3 SDK built-in retry (for reference)

The MeshCore SDK provides `send_msg_with_retry()` with built-in retry:

```python
result = await mc.commands.send_msg_with_retry(
    dst, msg,
    max_attempts=3,
    max_flood_attempts=2,
    flood_after=2,
)
```

Behavior:
- Sends via `send_msg()`, extracts `expected_ack`.
- Waits for matching `ACK` event with timeout = `suggested_timeout * 1.2`.
- On failure, retries up to `max_attempts` (default 3).
- After `flood_after` failed direct attempts, resets routing path and switches to flood mode.
- Flood attempts capped at `max_flood_attempts`.
- Returns the last `MSG_SENT` event on success, or `None` if all attempts fail.

**MEDRE does not use `send_msg_with_retry()` in the current adapter.** MEDRE's own retry/receipt system would handle delivery semantics at a higher level if implemented. This is documented for reference.

### 9.4 Duplicate-send risk

Because the adapter has no outbound retry, there is **no duplicate-send risk from the adapter itself** in the current scaffold. Each delivery attempt is made exactly once.

However, if `send_msg_with_retry()` is used in the future:
- The SDK retries the same message to the same recipient. If the first attempt actually delivered but the ACK was lost, the retry delivers a duplicate.
- `expected_ack` is derived from message content (effectively a CRC). Two identical messages to the same recipient produce the same `expected_ack`, which could confuse ACK correlation.
- Operators should be aware of this risk when using SDK-level retry.

At the radio mesh level, MeshCore firmware may also retransmit packets. This is outside the adapter's control.

### 9.5 Packet-loss caveats

MeshCore is a LoRa mesh network. Packet loss is **expected and normal**:

- Radio interference, distance, and obstructions cause packet loss.
- Multi-hop routing may drop packets at intermediate nodes.
- Channel messages return `OK`/`ERROR` but no `expected_ack` — there is no per-recipient delivery confirmation for channel messages.
- Direct messages return `expected_ack` for ACK correlation, but ACKs are not guaranteed.
- A successful `send_chan_msg` return only means the local node accepted the packet, not that any remote node received it.
- Inbound events may arrive out of order, duplicated, or not at all.

Operators should expect loss and plan accordingly. The adapter does not provide reliability guarantees.


## 10. Validation Procedures

### 10.1 Adapter lifecycle validation

1. Create a `MeshCoreConfig` with `connection_type="fake"`.
2. Call `adapter.start(ctx)`. Confirm no exception is raised.
3. Call `await adapter.health_check()`. Confirm `health == "healthy"`.
4. Call `await adapter.stop()`. Confirm `health == "unknown"`.

### 10.2 Inbound event reception (requires real node)

1. Start the adapter with a real node connection (TCP or serial).
2. From a second MeshCore node, send a text message on the configured channel.
3. Subscribe to the event bus or check `ctx.publish_inbound` calls.
4. Confirm the canonical event has the expected `source_transport_id` (pubkey prefix), `payload.body`, and `metadata.native.data` fields.

### 10.3 Outbound delivery validation (requires real node)

1. Start the adapter with a real node connection (TCP or serial).
2. Enqueue a message via `deliver(rendering_result)`.
3. Confirm the SDK `send_chan_msg()` or `send_msg()` call completes.
4. Check the remote node for the message.

### 10.4 Manual SDK validation (independent of adapter)

Until the adapter's real client code is implemented, validate SDK connectivity independently:

```python
import asyncio
from meshcore import MeshCore, EventType

async def validate():
    # Connect
    mc = await MeshCore.create_tcp("192.168.1.100", 4000)
    print(f"Connected: {mc.is_connected}")
    print(f"Self info: {mc.self_info}")

    # Fetch contacts
    result = await mc.commands.get_contacts()
    print(f"Contacts: {len(result.payload)} found")

    # Send channel message
    sent = await mc.commands.send_chan_msg(0, "MEDRE alpha validation")
    print(f"Send result: {sent.type}")

    await mc.disconnect()

asyncio.run(validate())
```

See `docs/runbooks/meshcore-live-smoke.md` for the full manual verification procedure.


## 11. Docker Operational Guidance

### 11.1 TCP mode (recommended for Docker)

TCP mode works naturally in Docker. The container only needs network access to the MeshCore node:

```bash
docker run -d --name medre-meshcore \
  --restart unless-stopped \
  medre-meshcore:latest
```

Configure via environment variables or mount a config file. The node must be reachable from the container's network.

### 11.2 Serial passthrough

Serial mode in Docker requires device passthrough:

```bash
docker run -d --name medre-meshcore \
  --device /dev/ttyUSB0:/dev/ttyUSB0 \
  --restart unless-stopped \
  medre-meshcore:latest
```

Requirements:
- The `--device` flag maps the host's serial port into the container.
- The container user must have read/write access to the device.
- The device path must match the `serial_port` configuration.
- Hot-plugging USB devices requires container restart.

### 11.3 BLE in Docker

BLE in Docker is theoretically possible but is **not validated** and requires:
- `--net host` for BlueZ access, or
- Bluetooth device passthrough via `--device /dev/bus/usb/...`

This is not recommended for alpha.

### 11.4 Docker restart policy

Use `--restart unless-stopped` or `--restart on-failure`. When the container restarts, the adapter creates a fresh client connection.

### 11.5 Persistent storage

The MeshCore adapter does not currently persist state. There is no database, no message store, and no session state to preserve across restarts. Mounting a volume is not required for MeshCore-specific data. If using the MEDRE SQLite storage for other purposes, mount the database path as described in the Matrix alpha operation runbook.


## 12. Reconnect Behavior

### 12.1 Current state: no automatic reconnect at adapter level

**The MeshCore adapter does not implement its own reconnection logic.** If the connection to the node is lost:
- The adapter does not detect the disconnection automatically (in the current scaffold).
- No reconnect attempts are made at the adapter level.
- `health_check()` will continue to report `"healthy"` because `_started` is still `True`.
- Manual intervention is required: call `stop()` then `start()` to re-establish the connection.

### 12.2 SDK-level auto-reconnect

The MeshCore SDK supports optional auto-reconnect via constructor parameters:

```python
mc = await MeshCore.create_tcp(
    "192.168.1.100", 4000,
    auto_reconnect=True,
    max_reconnect_attempts=3,
)
```

SDK reconnect behavior (from source analysis):
- Flat 1-second delay between reconnect attempts (not exponential backoff).
- Configurable `max_reconnect_attempts` (default 3).
- Emits `CONNECTED` event with `reconnected: True` on successful reconnect.
- Emits `DISCONNECTED` event with `max_attempts_exceeded: True` on final failure.

**Important:** This is SDK-level reconnect, not adapter-level. The adapter's `MeshCoreSession` enables `auto_reconnect=True` in the SDK constructor to give the transport layer automatic recovery. The adapter observes reconnection via `CONNECTED` events.

### 12.3 What this means for operation

- For short-lived testing sessions, manual `stop()` + `start()` is acceptable.
- For long-running operation, you need an external watchdog (e.g., Docker restart policy, systemd service, supervisor process) to detect failures and restart the process.
- When SDK auto-reconnect is enabled, the SDK handles transport-level recovery internally, but the adapter does not currently expose reconnect state in `health_check()`.

### 12.4 How to verify connection health

```python
# Check adapter health
info = await adapter.health_check()
print(f"Health: {info.health}")

# Check SDK client directly (when real client exists)
if adapter._client is not None:
    print(f"Connected: {adapter._client.is_connected}")
```

### 12.5 Restart expectations

When the adapter process restarts (whether via manual stop/start, Docker restart, or process manager):

1. A fresh SDK client is created from scratch.
2. `appstart()` is sent, triggering a new `SELF_INFO` event.
3. Event subscriptions are re-established.
4. The contact list is re-fetched from the node.
5. Any messages that arrived during the downtime are **lost** — the adapter does not persist or replay missed events. MeshCore uses a pull model (`MESSAGES_WAITING` → `get_msg()`), but the adapter does not currently implement backlog fetching on restart.

### 12.6 Planned improvements

Automatic reconnection with exponential backoff and health state transitions (`healthy` / `degraded` / `failed`) is planned but not implemented. See `docs/contracts/19-meshcore-connectivity-readiness.md` for the readiness assessment.


## 13. Known Limitations

This is an honest list. Everything here is real.

1. **BLE mode not yet implemented.** TCP and serial real connections work via `MeshCoreSession`. BLE connectivity is documented from SDK source analysis but not wired in the session. See section 6.

2. **No automatic reconnection at adapter level.** If the connection to the node is lost, the session attempts bounded reconnect with exponential backoff (up to 10 attempts). See section 12.

3. **No outbound retry.** Failed sends are permanently dropped, not requeued. See section 9.2.

4. **No inbound persistence.** Inbound events are published directly via `ctx.publish_inbound()`. If the callback is slow or fails, the event is gone. There is no retry, no dead letter queue, no redelivery.

5. **No ACK or delivery confirmation tracking.** The adapter does not track `expected_ack` values or correlate ACK events. The SDK provides this mechanism, but the adapter does not use it. Channel messages have no per-recipient ACK at all.

6. **Text packets only.** The adapter classifies all inbound events but only processes `text` category events. ACK events are silently dropped.

7. **No backlog suppression.** When the adapter starts, it may receive a burst of queued events from the node. There is a `startup_backlog_suppress_seconds` config field (default 5.0s) but it is not wired to filtering logic in the current adapter. Backlog events are processed like any other event.

8. **No dedicated runner.** Unlike Matrix (`python -m medre.runner`), there is no MeshCore-specific runner. Adapter wiring is manual (see section 5.2).

9. **No structured logging.** The adapter uses `ctx.logger.info/debug/error` with format strings. There are no structured log fields, no trace IDs, no correlation across events.

10. **No metrics.** There is no Prometheus endpoint, no counters, no histograms. The only observability is log output and the `health_check()` return value.

11. **512-byte text limit.** The adapter's capabilities declare `max_text_bytes=512` and `max_text_chars=512`. The renderer notes this but does not enforce it. The MeshCore wire protocol has a 255-byte frame payload maximum. Messages exceeding the wire limit may be truncated or rejected by the firmware.

12. **No DM support.** The adapter capabilities declare `direct_messages=False`. Direct messages are classified by the packet classifier (`is_direct_message`) but are processed identically to channel messages.

13. **Identity collision risk.** The `pubkey_prefix` used as `sender_id` is a truncated prefix (default 12 hex chars / 6 bytes). Two different nodes could theoretically share the same prefix. Do not assume `sender_id` is globally unique.

14. **Packet shapes unverified.** The event payload shapes used in MEDRE fixtures are derived from source audit (contract 11). Whether real MeshCore hardware produces exactly these shapes has not been verified. Fields like `pubkey_prefix` truncation length, `txt_type` values, and `sender_timestamp` behavior need live validation.


## 14. Operational Risks

### 14.1 Radio traffic

The adapter sends real radio packets when using real connectivity modes. Ensure the configured channel is not used for critical or emergency communications during testing. MeshCore operates on LoRa radio bands. Ensure your node is configured for your regional regulations.

### 14.2 Connection loss is silent

If the TCP connection drops, the serial cable is disconnected, or the BLE link is lost, the adapter does not detect this automatically in the current scaffold. It will continue to report `"healthy"` until the next send attempt fails (or until you call `stop()` + `start()`).

### 14.3 Message loss is expected

LoRa mesh networks have inherent packet loss. Do not rely on the MeshCore adapter for guaranteed delivery. Use it for best-effort messaging. Critical messages should use a transport with delivery confirmation.

### 14.4 Always-on encryption

MeshCore uses always-on E2EE (AES-128 + 2-byte HMAC). This means all messages are encrypted on the wire. The adapter does not manage encryption keys — the SDK handles this transparently. However, this also means messages cannot be inspected in transit without the node's keypair.

### 14.5 Duty cycle

LoRa radio nodes enforce duty cycle limits on transmission. The adapter's pacing (`message_delay_seconds`, default 0.5s) helps avoid overwhelming the radio, but high-volume sending may still hit firmware limits.

### 14.6 expected_ack collision risk

The `expected_ack` field from `send_msg()` is derived from message content (effectively a CRC). Two identical messages to the same recipient produce the same `expected_ack`. If you send the same message content twice rapidly, ACK correlation may be ambiguous. This needs live hardware verification. See `docs/contracts/19-meshcore-connectivity-readiness.md` section 5.5.


## 15. Troubleshooting

### 15.1 `MeshCoreConnectionError: meshcore SDK not installed`

You are trying to use a non-fake connection type without the `meshcore` package.

```bash
pip install meshcore
```

Or switch to fake mode:

```python
config = MeshCoreConfig(adapter_id="meshcore-alpha", connection_type="fake")
```

### 15.2 `MeshCoreConnectionError: Real MeshCore connections not yet implemented`

You are trying to use a real connection type (TCP/serial/BLE) but the adapter's real client code has not been implemented yet. This is the current scaffold behavior.

Use fake mode for now:

```python
config = MeshCoreConfig(adapter_id="meshcore-alpha", connection_type="fake")
```

### 15.3 `MeshCoreConnectionError: Failed to create tcp client: ...`

TCP connection to the node failed. Check:

1. Is the node powered on?
2. Is the IP address correct? Try `ping 192.168.1.100`.
3. Is port 4000 open? Try `nc -zv 192.168.1.100 4000`.
4. Is the node connected to the network (WiFi/Ethernet)?
5. Firewall rules blocking port 4000?

### 15.4 `MeshCoreConnectionError: Failed to create serial client: ...`

Serial connection failed. Check:

1. Is the USB cable connected?
2. Does the serial port exist? `ls /dev/ttyUSB*`.
3. Does your user have permission? `sudo usermod -aG dialout $USER`, then re-login.
4. Is another process using the port? `lsof /dev/ttyUSB0`.

### 15.5 `MeshCoreConfigError: host is required when connection_type is 'tcp'`

The config is missing the `host` field. Add it:

```python
config = MeshCoreConfig(adapter_id="meshcore-alpha", connection_type="tcp", host="192.168.1.100")
```

### 15.6 `MeshCoreConfigError: serial_port is required when connection_type is 'serial'`

The config is missing the `serial_port` field. Add it:

```python
config = MeshCoreConfig(adapter_id="meshcore-alpha", connection_type="serial", serial_port="/dev/ttyUSB0")
```

### 15.7 `MeshCoreConfigError: ble_address is required when connection_type is 'ble'`

The config is missing the `ble_address` field. Add it:

```python
config = MeshCoreConfig(adapter_id="meshcore-alpha", connection_type="ble", ble_address="AA:BB:CC:DD:EE:FF")
```

### 15.8 `MeshCoreConfigError: node_config must not contain secret keys: private_key, ...`

You included a forbidden key in `node_config`. Secrets must be provisioned through a secure channel, not embedded in configuration:

```python
# WRONG
config = MeshCoreConfig(adapter_id="x", node_config={"private_key": "abc123"})

# RIGHT — provision secrets externally
config = MeshCoreConfig(adapter_id="x", node_config={})
```

### 15.9 Adapter starts but no inbound events arrive

Check these things, in order:

1. Is anyone sending messages on the configured channel?
2. Is the node receiving messages? Check the node's serial output.
3. Is `meshcore` installed? Without it, the event dispatcher cannot fire.
4. Are the arriving events text messages? The adapter silently drops ACK events and unknown types.
5. Is `CONTACT_MSG_RECV` or `CHANNEL_MSG_RECV` firing? The SDK's event dispatcher uses attribute filtering.

### 15.10 `TypeError: MeshCoreAdapter.deliver() accepts RenderingResult only`

You passed a `CanonicalEvent` or raw dict to `deliver()` instead of a `RenderingResult`. The delivery path expects pre-rendered payloads from the rendering pipeline, not raw events.

### 15.11 Live smoke tests all SKIP

Environment variables are not set. Set at minimum:

```bash
export MESHCORE_CONNECTION_TYPE="tcp"
export MESHCORE_HOST="192.168.1.100"
```

### 15.12 `OSError: [Errno 13] Permission denied` on serial port

Your user does not have serial port access:

```bash
sudo usermod -aG dialout $USER
# Log out and log back in
groups  # verify 'dialout' is listed
```

### 15.13 BLE scan finds no devices

1. Is Bluetooth enabled on your machine? `rfkill list`.
2. Is BlueZ running? `systemctl status bluetooth`.
3. Is the MeshCore node powered on and advertising?
4. Try scanning for longer: `bluetoothctl scan on` (leave running for 30+ seconds).

### 15.14 `ImportError: No module named 'meshcore'`

The MeshCore SDK is not installed:

```bash
pip install meshcore
```

Core MEDRE tests pass without it. Only real connectivity modes require the SDK.


## 16. Explicit Unsupported Features

The following features are not supported in alpha mode. Do not attempt to use them. They are listed here so you do not have to wonder.

| Feature | Status | Notes |
|---------|--------|-------|
| Real client connections | Scaffolded | `start()` raises `MeshCoreConnectionError` for non-fake types |
| Automatic reconnection | Not implemented at adapter level | SDK supports `auto_reconnect` param, not wired |
| Outbound retry | Not implemented | Failed sends are permanently dropped |
| ACK / delivery confirmation tracking | Not implemented | `expected_ack` is not tracked or correlated |
| Direct message routing | Not supported | DMs classified but processed identically to channel messages |
| Reply threading | Not supported | MeshCore protocol has no native reply mechanism |
| Reactions | Not supported | MeshCore protocol has no reaction mechanism |
| Edits | Not supported | MeshCore protocol has no edit mechanism |
| Deletes | Not supported | MeshCore protocol has no delete mechanism |
| Attachments / files | Not supported | Binary payload not handled |
| Telemetry decoding | Not supported | Non-text events are silently dropped |
| Position / GPS decoding | Not supported | Non-text events are silently dropped |
| Contact list caching | Not supported | Contact list available via SDK but not cached by adapter |
| Multi-node mesh testing | Not tested | Alpha has only been validated with a single node |
| BLE connectivity | Documented only | BLE is a config option but not validated in alpha |
| Backlog suppression | Config field exists, not wired | `startup_backlog_suppress_seconds` accepted but not used |
| Store-and-forward | Not supported | No message persistence across restarts |
| Rate limiting / flow control | Not implemented | Only basic pacing via `message_delay_seconds` |
| Cross-transport orchestration | Not in scope | No bridge between MeshCore and other transports |
| Bridge-policy redesign | Not in scope | No policy changes for MeshCore integration |
| Non-MeshCore transports | Not in scope | This runbook covers MeshCore only |
