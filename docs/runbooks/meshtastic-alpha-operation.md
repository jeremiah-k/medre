# Meshtastic Alpha Operation Runbook

> Last updated: 2026-05-21
> Scope: Real Meshtastic Operation Alpha
> Status: Alpha. Not production. Not hardened. Not complete. Fake mode is the primary development and testing path. Real connectivity (TCP/serial) is available for live validation.

This runbook describes how to run the MEDRE Meshtastic adapter against a real Meshtastic radio node in alpha mode. Alpha mode means the MeshtasticAdapter connects to a real node using TCP or serial, receives real radio packets via pubsub callbacks, and sends real messages via the queued `send_one` path. It does not mean the system is ready for anything beyond a single operator on a single node.

Everything in this document is conservative. If something has not been tested against a real node and confirmed working, this document says so. If something is known to be broken or missing, this document says that too.

**Fake mode** is the default and recommended path for all development and testing. Real connectivity modes (TCP, serial) are opt-in for live validation only.

## 1. Purpose

Alpha operation validates that the MEDRE Meshtastic adapter works end to end against a real Meshtastic radio node with real radio traffic. This is the first time the adapter leaves fake and mock territory.

Scope boundaries:

- One transport: Meshtastic. No other transports are in scope for this runbook.
- One operator: a single person running against a local or network-accessible node.
- Text messages on a single radio channel. No telemetry, position, nodeinfo, admin, or other portnum types are processed inbound.
- No production deployment, no scaling, no monitoring, no alerting.
- No claims about reliability, durability, or correctness beyond what manual testing confirms.

This runbook complements `docs/runbooks/meshtastic-live-smoke.md`. The smoke test validates raw `mtjk` API and adapter lifecycle methods in isolation. Alpha operation validates the full wiring: config, adapter, codec, inbound pubsub, outbound queue, and health, running together.

## 2. Prerequisites

| Requirement          | Details                                                                                                               |
| -------------------- | --------------------------------------------------------------------------------------------------------------------- |
| Meshtastic node      | A real Meshtastic radio node (e.g. LilyGO T-Beam, Heltec v3, RAK WisBlock) accessible via TCP or serial               |
| Python               | 3.11 or later                                                                                                         |
| Package install      | Core MEDRE: `pip install -e .` (no extra required for fake mode). Real connectivity: `pip install -e ".[meshtastic]"` |
| Network access (TCP) | Your machine can reach the node's IP address on port 4403                                                             |
| Serial access        | USB cable connecting the node; user must be in `dialout` group on Linux                                               |
| Radio channel        | A channel index (default 0) not used for critical or emergency communications                                         |

You do not need Docker for basic alpha operation. Docker guidance is in section 11.

## 3. Node Setup

You need a Meshtastic radio node that the MEDRE process can reach. The node must be powered on, configured for your region, and accessible.

### 3.1 TCP connectivity (recommended)

Most Meshtastic nodes expose a TCP API when connected via WiFi or Ethernet. The default port is 4403.

1. Power on the node and connect it to your network (WiFi or Ethernet).
2. Find the node's IP address (check your router's DHCP table, or use the Meshtastic CLI: `meshtastic --info`).
3. Verify TCP connectivity:

```bash
# Quick connectivity check (will show protobuf noise or timeout)
nc -zv meshtastic.local 4403
```

4. Optionally verify with the Meshtastic CLI:

```bash
pip install mtjk
meshtastic --host meshtastic.local --info
```

### 3.2 Serial connectivity

Connect the node via USB. The node will appear as a serial device.

1. Connect the USB cable.
2. Find the serial port:

```bash
# Linux
ls /dev/ttyUSB* /dev/ttyACM*
# Or use Python
python -c "from serial.tools.list_ports import comports; [print(p.device) for p in comports()]"
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
# Note the MAC address of your Meshtastic node
```

### 3.4 Firmware compatibility

`mtjk` v2.7.8.post2+ is the verified dependency. Firmware compatibility depends on the node's firmware version. If you encounter protocol errors, update both the node firmware and the `mtjk` package.

## 4. Connection Modes

The adapter supports four connection types via `MeshtasticConfig.connection_type`:

### 4.1 Fake mode (default)

No real client. Used for development and testing without hardware.

```python
from medre.config.adapters.meshtastic import MeshtasticConfig

config = MeshtasticConfig(
    adapter_id="mesh-alpha",
    connection_type="fake",
)
```

In fake mode:

- `start()` sets `_client = None`. No network or serial activity.
- `deliver()` enqueues to the internal queue but `send_one()` returns `None` (no real send).
- `simulate_inbound()` is available for injecting test packets.
- `health_check()` returns `"healthy"` after start.

**Fake mode is the recommended default for all development and testing.**

### 4.1.1 No-SDK behavior

Fake mode is designed for zero-dependency development:

- **No `mtjk` package required.** The adapter creates no real client
  (`_client = None`). All adapter submodules (`config`, `codec`,
  `packet_classifier`, `outbound_queue`) import successfully without `mtjk`.
- **Default for all tests.** All `pytest` runs use fake mode unless explicitly
  overridden with live environment variables. No hardware or SDK is needed
  for the standard test suite.
- **Import behavior.** The adapter's import guard (`HAS_MESHTASTIC`) is
  checked only when creating real client instances. Importing
  `meshtastic.tcp_interface` at module level will fail without `mtjk`, but the
  adapter defers all such imports behind runtime guards. Concrete import
  behavior: importing submodules works, but creating real clients fails
  without `mtjk`.
- **Concrete limitation.** Without `mtjk`, calling `start()` with
  `connection_type="tcp"` (or `"serial"`, `"ble"`) raises
  `MeshtasticConnectionError`. Only `connection_type="fake"` works without
  the SDK.

### 4.2 TCP mode

Connects to a Meshtastic node via its TCP API.

```python
config = MeshtasticConfig(
    adapter_id="mesh-alpha",
    connection_type="tcp",
    host="meshtastic.local",   # or IP like "192.168.1.100"
    port=4403,                  # optional, defaults to 4403
)
```

- Uses `meshtastic.tcp_interface.TCPInterface(hostname, portNumber)`.
- Default port: 4403.
- The adapter calls `waitForConfig()` implicitly during client creation (the `TCPInterface` constructor blocks until initial config is received).
- Connection is synchronous internally; `send_one()` wraps `sendText` in `asyncio.to_thread()`.

### 4.3 Serial mode

Connects to a Meshtastic node via USB serial.

```python
config = MeshtasticConfig(
    adapter_id="mesh-alpha",
    connection_type="serial",
    serial_port="/dev/ttyUSB0",
)
```

- Uses `meshtastic.serial_interface.SerialInterface(devPath)`.
- Port must exist and be accessible (user in `dialout` group on Linux).

### 4.4 BLE mode

Connects via Bluetooth Low Energy. Documented but **not validated in alpha**.

```python
config = MeshtasticConfig(
    adapter_id="mesh-alpha",
    connection_type="ble",
    ble_address="AA:BB:CC:DD:EE:FF",
)
```

- Uses `meshtastic.ble_interface.BLEInterface(address)`.
- Requires BLE-capable hardware and OS support (BlueZ on Linux).

### 4.5 Configuration validation

`MeshtasticConfig.validate()` enforces:

| Connection type | Required fields             |
| --------------- | --------------------------- |
| `fake`          | `adapter_id` only           |
| `tcp`           | `adapter_id`, `host`        |
| `serial`        | `adapter_id`, `serial_port` |
| `ble`           | `adapter_id`, `ble_address` |

Invalid configurations raise `MeshtasticConfigError` before any connection attempt.

## 5. Running MEDRE in Alpha Mode

### 5.1 Environment variables

The live smoke tests use environment variables to configure the connection. The adapter itself is configured via `MeshtasticConfig` (see section 4).

| Variable                     | Required for | Default | Example             | Description                                          |
| ---------------------------- | ------------ | ------- | ------------------- | ---------------------------------------------------- |
| `MESHTASTIC_CONNECTION_TYPE` | All          |         | `tcp`               | Connection mode: `tcp`, `serial`, `ble`              |
| `MESHTASTIC_HOST`            | TCP          |         | `meshtastic.local`  | Node hostname or IP                                  |
| `MESHTASTIC_PORT`            | TCP          | `4403`  | `4403`              | TCP port                                             |
| `MESHTASTIC_SERIAL_PORT`     | Serial       |         | `/dev/ttyUSB0`      | Serial device path                                   |
| `MESHTASTIC_BLE_ADDRESS`     | BLE          |         | `AA:BB:CC:DD:EE:FF` | BLE MAC address                                      |
| `MESHTASTIC_CHANNEL_INDEX`   | All          | `0`     | `0`                 | Channel for test messages                            |
| `MESHTASTIC_NODE_ID`         | All          |         | `!25d6e474`         | Meshtastic node ID for identifying the local node    |
| `MESHTASTIC_LIVE_SEND`       | Live TX      |         | `1`                 | **Transmit guard.** Must be `1` for RF transmission. Without this flag, the adapter may connect and health-check but MUST NOT transmit. |

### 5.2 Manual adapter wiring

There is no dedicated Meshtastic runner. For alpha operation, wire the adapter manually:

```python
import asyncio
import logging

from medre.adapters.meshtastic.adapter import MeshtasticAdapter
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.contracts.adapter import AdapterContext
from medre.core.events.event_bus import EventBus

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mesh-alpha")

config = MeshtasticConfig(
    adapter_id="mesh-alpha",
    connection_type="tcp",
    host="meshtastic.local",
    port=4403,
).validate()

adapter = MeshtasticAdapter(config)
event_bus = EventBus()
ctx = AdapterContext(event_bus=event_bus, logger=logger)

async def main():
    await adapter.start(ctx)
    logger.info("Adapter started: %s", await adapter.health_check())

    # Inbound packets arrive via pubsub callback and are published
    # to ctx.publish_inbound(). Subscribe to the event bus to see them.
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
pip install -e ".[meshtastic]"

export MESHTASTIC_CONNECTION_TYPE="tcp"
export MESHTASTIC_HOST="meshtastic.local"
export MESHTASTIC_CHANNEL_INDEX="0"

pytest tests/test_meshtastic_live.py -m live -v
```

See `docs/runbooks/meshtastic-live-smoke.md` for full smoke test documentation.

## 6. Startup and Shutdown Behavior

### 6.1 Startup sequence

When `start(ctx)` is called on a non-fake adapter:

1. The adapter checks `HAS_MESHTASTIC` (the `mtjk` import guard). If `mtjk` is not installed and connection_type is not `"fake"`, raises `MeshtasticConnectionError`.
2. `_create_client()` is called. This creates the appropriate interface (`TCPInterface`, `SerialInterface`, or `BLEInterface`). This call is **synchronous and blocking** — it waits for the initial device config.
3. `_subscribe_callbacks()` subscribes to the `meshtastic.receive` pubsub topic.
4. `_started` is set to `True`.
5. A startup log line is emitted: `"MeshtasticAdapter mesh-alpha started (mode=tcp)"`.

### 6.2 Expected startup output

```console
INFO  MeshtasticAdapter mesh-alpha started (mode=tcp)
```

If startup fails, you will see one of:

- `MeshtasticConnectionError: mtjk not installed; pip install mtjk`
- `MeshtasticConnectionError: Failed to create tcp client: <underlying error>`
- `MeshtasticConnectionError: Failed to subscribe to meshtastic.receive: <error>`

### 6.3 Shutdown sequence

When `stop()` is called:

1. All tracked background tasks (from inbound packet processing) are cancelled and drained (with a 5-second timeout).
2. Pubsub callbacks are unsubscribed.
3. The client's `close()` method is called (if it exists).
4. `_client` is set to `None`, `_started` is set to `False`.
5. A shutdown log line is emitted: `"MeshtasticAdapter mesh-alpha stopped"`.

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

| State     | Meaning                                                         |
| --------- | --------------------------------------------------------------- |
| `unknown` | Adapter has not started, or has been stopped cleanly            |
| `healthy` | Adapter has started successfully; client is connected           |
| `failed`  | Client exists but start did not complete (subscription failure) |

There are no intermediate states. There is no `degraded` or `reconnecting` state. The Meshtastic adapter does not implement automatic reconnection (unlike the Matrix adapter which has `healthy` / `degraded` / `failed` transitions).

### 7.2 Queue diagnostics

The adapter exposes `queue_health` as a snapshot of the outbound queue:

```python
health = adapter.queue_health
# {
#     "pending_count": 0,
#     "total_sent": 3,
#     "total_failed": 0,
#     "delay_between_messages": 0.5,
#     "last_send_time": 1715289600.123,
# }
```

| Field                    | Type  | Meaning                                                     |
| ------------------------ | ----- | ----------------------------------------------------------- |
| `pending_count`          | int   | Number of items currently in the outbound queue             |
| `total_sent`             | int   | Cumulative count of successful sends since adapter creation |
| `total_failed`           | int   | Cumulative count of send failures since adapter creation    |
| `delay_between_messages` | float | Configured minimum pacing delay in seconds                  |
| `last_send_time`         | float | `time.monotonic()` of the last successful send              |
| `drain_task_running`     | bool  | Whether the background queue-drain task is active           |

These counters are cumulative for the lifetime of the adapter instance (not reset on stop/start).

### 7.3 Inbound packet processing

When a real radio packet arrives via the pubsub callback:

1. The `_on_receive_callback(packet, interface)` fires (synchronous, called from the `mtjk` pubsub thread).
2. The packet is classified by `MeshtasticPacketClassifier`. Only `text` category packets are processed; all others (ACK, telemetry, position, nodeinfo, admin, unknown) are silently dropped.
3. The packet is decoded into a `CanonicalEvent` by `MeshtasticCodec`.
4. The canonical event is published inbound via `ctx.publish_inbound()` in an async background task.

There is no periodic "still alive" log. Silence is normal when no packets arrive. The pubsub callback runs passively.

### 7.4 Outbound send via send_one

`send_one()` dequeues one item from the outbound queue, applies pacing delay, and calls `client.sendText(text, channelIndex=channel_index)` via `asyncio.to_thread()`. The send result is a `MeshPacket` protobuf with a populated `id` field.

Successful sends increment `total_sent`. Failed sends increment `total_failed` and **re-raise the exception** to the caller. Failed items are permanently dropped — they are NOT requeued or retried (see section 9).

## 8. Canonical Metadata Structure

The Meshtastic codec preserves the following metadata from native packets into the canonical event:

### 8.1 Native metadata

Every decoded text event carries a `NativeMetadata` block:

```python
NativeMetadata(data={
    "packet_id": 12345,                    # integer packet ID from the radio
    "from_id": "!abc123",                  # sender node ID string (or numeric string fallback)
    "channel": 0,                          # radio channel index
    "portnum": "text_message",             # normalized portnum string
    "to_id": "",                           # destination (empty = broadcast)
    "is_direct_message": False,            # True if addressed to a specific node
})
```

| Field               | Source                                                | Notes                                                                     |
| ------------------- | ----------------------------------------------------- | ------------------------------------------------------------------------- |
| `packet_id`         | `packet["id"]`                                        | Integer assigned by the sending radio                                     |
| `from_id`           | `packet["fromId"]` or `packet["from"]`                | String sender ID; falls back to numeric `from` field converted to string  |
| `channel`           | `packet["channel"]` or `packet["decoded"]["channel"]` | Radio channel index                                                       |
| `portnum`           | `packet["decoded"]["portnum"]`                        | Normalized via classifier (e.g. `"TEXT_MESSAGE_APP"` → `"text_message"`)  |
| `to_id`             | `packet["toId"]`                                      | Destination address; empty string for broadcast                           |
| `is_direct_message` | Derived from `to_id`                                  | True if `to_id` is not a broadcast address (`""`, `"^all"`, `0xFFFFFFFF`) |

### 8.2 Source native ref

Each decoded event carries a `source_native_ref` linking back to the original radio packet:

```python
NativeRef(
    adapter="mesh-alpha",
    native_channel_id="0",      # channel index as string
    native_message_id="12345",  # packet ID as string
)
```

### 8.3 Reply relations

When a packet includes a `replyId` field, the codec creates an `EventRelation` of type `"reply"` targeting the referenced packet's native ref:

```python
EventRelation(
    relation_type="reply",
    target_event_id=None,            # not resolvable at decode time
    target_native_ref=NativeRef(
        adapter="mesh-alpha",
        native_channel_id="0",
        native_message_id="<replyId>",  # the referenced packet ID
    ),
)
```

### 8.4 Canonical event fields

| Field                 | Value                                           |
| --------------------- | ----------------------------------------------- |
| `event_id`            | UUID4 (generated by codec)                      |
| `event_kind`          | `MESSAGE_CREATED`                               |
| `source_transport_id` | Sender node ID (`from_id`)                      |
| `source_channel_id`   | Channel index as string                         |
| `payload`             | `{"body": "<text>", "portnum": "text_message"}` |

## 9. Outbound Delivery and Retry Semantics

### 9.1 Outbound delivery path

1. `deliver(result)` accepts a `RenderingResult` and enqueues the payload to the internal `MeshtasticOutboundQueue`.
2. `send_one()` must be called (by the operator or a future runner loop) to process queued items.
3. `send_one()` applies pacing delay (`message_delay_seconds`, default 0.5s) and calls `client.sendText()`.

In fake mode, `send_one()` returns `None` — no real send occurs.

### 9.2 Retry semantics (current: none)

**There is no outbound retry logic.** When `send_one()` fails:

- The dequeued item is **permanently dropped**. It is NOT requeued or retried.
- `total_failed` is incremented.
- The exception is re-raised to the caller of `send_one()`.

This is an explicit scaffold design choice, documented in the queue module: "Production-grade retry / requeue logic is explicitly deferred to a future tranche."

### 9.3 Duplicate-send risk

Because there is no retry, there is **no duplicate-send risk from the adapter**. Each queued item is processed exactly once. If the send succeeds, the item is consumed. If it fails, the item is dropped.

However, at the radio mesh level, Meshtastic firmware itself may retransmit packets. This is outside the adapter's control and is a characteristic of the radio network, not the adapter.

### 9.4 Packet-loss caveats

Meshtastic is a radio mesh network. Packet loss is **expected and normal**:

- Radio interference, distance, and obstructions cause packet loss.
- Multi-hop routing may drop packets at intermediate nodes.
- The adapter has no ACK tracking, no `wantAck` enforcement, and no delivery confirmation.
- A successful `sendText` return only means the local node accepted the packet for transmission, not that any remote node received it.
- Inbound packets may arrive out of order, duplicated, or not at all.

Operators should expect loss and plan accordingly. The adapter does not provide reliability guarantees.

## 10. Validation Procedures

### 10.1 Adapter lifecycle validation

1. Create a `MeshtasticConfig` with `connection_type="tcp"` (or `"serial"`).
2. Call `adapter.start(ctx)`. Confirm no exception is raised.
3. Call `await adapter.health_check()`. Confirm `health == "healthy"`.
4. Call `await adapter.stop()`. Confirm `health == "unknown"`.

### 10.2 Inbound packet reception

1. Start the adapter with a real node connection.
2. From a second Meshtastic node (or from the same node's app), send a text message on the configured channel.
3. Subscribe to the event bus or check `ctx.publish_inbound` calls.
4. Confirm the canonical event has the expected `source_transport_id`, `payload.body`, and `metadata.native.data` fields.

### 10.3 Outbound delivery validation

1. Start the adapter with a real node connection.
2. Enqueue a message via `deliver(rendering_result)`.
3. Call `await adapter.send_one()`.
4. Confirm the returned `AdapterDeliveryResult` has a `native_message_id`.
5. Check the remote node or another node on the mesh for the message.

### 10.4 Queue health validation

1. Enqueue several messages.
2. Check `adapter.queue_health["pending_count"]` matches the enqueued count.
3. Process all messages via `send_one()` in a loop.
4. Confirm `total_sent` matches the expected count and `total_failed == 0`.

## 11. Docker Operational Guidance

### 11.1 TCP mode (recommended for Docker)

TCP mode works naturally in Docker. The container only needs network access to the Meshtastic node:

```bash
docker run -d --name medre-meshtastic \
  --restart unless-stopped \
  medre-meshtastic:latest
```

**Transmit guard in Docker.** If the container is intended to transmit RF,
you must pass `MESHTASTIC_LIVE_SEND=1`:

```bash
docker run -d --name medre-meshtastic \
  -e MESHTASTIC_LIVE_SEND=1 \
  --restart unless-stopped \
  medre-meshtastic:latest
```

Without this environment variable, the adapter will connect and health-check
but will **not transmit** any RF messages. This is the safe default for
containers that may be running in environments where RF transmission is
unwanted or untested.

Configure via environment variables or mount a config file. The node must be reachable from the container's network.

### 11.2 Serial passthrough

Serial mode in Docker requires device passthrough:

```bash
docker run -d --name medre-meshtastic \
  --device /dev/ttyUSB0:/dev/ttyUSB0 \
  --restart unless-stopped \
  medre-meshtastic:latest
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

Use `--restart unless-stopped` or `--restart on-failure`. Since the adapter has no internal reconnect logic, the Docker restart policy handles process-level crashes. When the container restarts, the adapter creates a fresh client connection.

### 11.5 Persistent storage

The Meshtastic adapter does not currently persist state. There is no database, no message store, and no session state to preserve across restarts. Mounting a volume is not required for Meshtastic-specific data. If using the MEDRE SQLite storage for other purposes, mount the database path as described in the Matrix alpha operation runbook.

## 12. Reconnect Behavior

### 12.1 Current state: no automatic reconnect

**The Meshtastic adapter does not implement automatic reconnection.** Unlike the Matrix adapter (which has bounded exponential backoff and health state transitions), the Meshtastic adapter has no reconnect logic at all.

If the connection to the node is lost:

- The `mtjk` client may raise exceptions or silently stop delivering callbacks.
- The adapter does not detect the disconnection automatically.
- No reconnect attempts are made.
- `health_check()` will continue to report `"healthy"` because `_started` is still `True`.
- Manual intervention is required: call `stop()` then `start()` to re-establish the connection.

### 12.2 What this means for operation

- For short-lived testing sessions, this is acceptable. Start the adapter, run your tests, stop it.
- For long-running operation, you need an external watchdog (e.g., Docker restart policy, systemd service, supervisor process) to detect failures and restart the process.
- There is no exponential backoff, no max attempt counter, no reconnect budget.

### 12.3 How to verify connection health

The best way to verify the connection is alive is to send a message and check for a response:

```python
# Check queue health
health = adapter.queue_health
print(f"Sent: {health['total_sent']}, Failed: {health['total_failed']}")

# If total_failed is climbing, the connection is likely lost.
```

### 12.4 Planned improvements

Automatic reconnection with exponential backoff and health state transitions (`healthy` / `degraded` / `failed`) is planned but not implemented. This is documented in `docs/contracts/18-operational-readiness-gaps.md`.

## 13. Known Limitations

This is an honest list. Everything here is real.

1. **No automatic reconnection.** If the connection to the node is lost, the adapter does not recover. Manual `stop()` + `start()` is required. See section 12.

2. **No outbound retry.** Failed sends are permanently dropped, not requeued. Queue drain is reliably cancelled on stop. See section 9.2.

3. **No inbound persistence.** Inbound events are published directly via `ctx.publish_inbound()`. If the callback is slow or fails, the event is gone. There is no retry, no dead letter queue, no redelivery.

4. **No ACK or delivery confirmation.** The adapter sends with `wantAck=False` (default). There is no confirmation that remote nodes received the message. The `sendText` return only confirms the local node accepted the packet.

5. **Text packets only.** The adapter classifies all inbound packets but only processes `text` category packets. Telemetry, position, nodeinfo, admin, and other portnum types are silently dropped.

6. **No backlog suppression.** When the adapter starts, it may receive a burst of queued packets from the node. There is a `startup_backlog_suppress_seconds` config field (default 5.0s) but it is not wired to filtering logic in the current adapter. Backlog packets are processed like any other packet.

7. **No dedicated runner.** There is no Meshtastic-specific runner. Adapter wiring is manual (see section 5.2).

8. **No structured logging.** The adapter uses `ctx.logger.info/debug/error` with format strings. There are no structured log fields, no trace IDs, no correlation across events.

9. **No metrics.** There is no Prometheus endpoint, no counters, no histograms. The only observability is log output and the `health_check()` / `queue_health` return values.

10. **Synchronous client creation.** `_create_client()` is synchronous and blocking. For TCP, this blocks until the initial device config is received. In an async context, this blocks the event loop during startup.

11. **512-byte text limit.** The adapter's capabilities declare `max_text_bytes=512` and `max_text_chars=512`. The renderer notes this but does not enforce it. Messages exceeding this limit may be truncated or rejected by the radio firmware.

12. **No DM support.** The adapter capabilities declare `direct_messages=False`. Direct messages are classified by the packet classifier (`is_direct_message`) but are processed identically to broadcast messages.

13. **Packet classifier numeric map is scaffold only.** The `_NUMERIC_PORTNUM_MAP` in `packet_classifier.py` is a test fixture approximation, not derived from the real Meshtastic protobuf `PortNum` enum. When the real `mtjk` package is installed, the `compat.get_portnum_table()` function returns authoritative values. See `docs/contracts/10-meshtastic-source-audit.md` for the authoritative table.

14. **RF transmission requires explicit opt-in via `MESHTASTIC_LIVE_SEND`.** The live test suite enforces `MESHTASTIC_LIVE_SEND=1` before any test may call `sendText`, `sendData`, or `adapter.deliver()` against real radio hardware. Tests without this flag may connect and health-check only — they must never transmit. The adapter code itself does not gate on this env var; the guard is at the test layer.

15. **No-SDK fake mode does not validate real packet shapes.** In `connection_type="fake"` mode, all packet handling is simulated. The adapter does not validate that real `mtjk` protobuf packets match the shapes expected by the codec. Discrepancies between fake and real packet shapes will only surface during live testing.

16. **Storage roundtrip tests are fake-only.** `tests/test_meshtastic_storage_roundtrip.py` validates encode → store → decode cycles using fake packets. These tests do not exercise real radio packet storage. Real packet storage roundtrip fidelity is not yet validated.

## 14. Operational Risks

### 14.1 Radio traffic

The adapter sends real radio packets. Ensure the configured channel is not used for critical or emergency communications during testing. Meshtastic operates on license-free bands (868 MHz EU / 915 MHz US). Ensure your node is configured for your regional regulations.

**Transmit guard.** RF transmission is gated by the `MESHTASTIC_LIVE_SEND=1` environment variable. Without this flag, the adapter may connect and health-check but **MUST NOT transmit**. This prevents accidental radio transmissions when:
- Running tests against real hardware without intending to transmit.
- Operating in CI environments where RF is never wanted.
- Developing against a real node but only testing connection lifecycle.

Always verify `MESHTASTIC_LIVE_SEND` is unset when RF transmission is unwanted, and set it explicitly to `1` only when intentional RF transmission is desired.

### 14.2 Connection loss is silent

If the TCP connection drops or the serial cable is disconnected, the adapter does not detect this automatically. It will continue to report `"healthy"` until the next `send_one()` attempt fails (or until you call `stop()` + `start()`).

### 14.3 Message loss is expected

Radio mesh networks have inherent packet loss. Do not rely on the Meshtastic adapter for guaranteed delivery. Use it for best-effort messaging. Critical messages should use a transport with delivery confirmation.

### 14.4 Duty cycle

Meshtastic nodes enforce duty cycle limits on transmission. The adapter's pacing (`message_delay_seconds`, default 0.5s) helps avoid overwhelming the radio, but high-volume sending may still hit firmware limits.

### 14.5 Thread safety

The `mtjk` pubsub callback fires on a background thread managed by the `mtjk` library. The adapter's `_on_packet` method is called from this thread and schedules async work via `asyncio.create_task()`. This is safe because `create_task()` is thread-safe in Python 3.10+.

## 15. Troubleshooting

### 15.1 `MeshtasticConnectionError: mtjk not installed`

You are trying to use a non-fake connection type without the `mtjk` package.

```bash
pip install mtjk
```

Or switch to fake mode:

```python
config = MeshtasticConfig(adapter_id="mesh-alpha", connection_type="fake")
```

### 15.2 `MeshtasticConnectionError: Failed to create tcp client: ...`

TCP connection to the node failed. Check:

1. Is the node powered on?
2. Is the hostname/IP correct? Try `ping meshtastic.local`.
3. Is port 4403 open? Try `nc -zv meshtastic.local 4403`.
4. Is the node connected to the network (WiFi/Ethernet)?
5. Firewall rules blocking port 4403?

### 15.3 `MeshtasticConnectionError: Failed to create serial client: ...`

Serial connection failed. Check:

1. Is the USB cable connected?
2. Does the serial port exist? `ls /dev/ttyUSB*`.
3. Does your user have permission? `sudo usermod -aG dialout $USER`, then re-login.
4. Is another process using the port (e.g., `meshtastic` CLI, minicom)? `lsof /dev/ttyUSB0`.

### 15.4 `MeshtasticConfigError: host is required when connection_type is 'tcp'`

The config is missing the `host` field. Add it:

```python
config = MeshtasticConfig(adapter_id="mesh-alpha", connection_type="tcp", host="192.168.1.100")
```

### 15.5 `MeshtasticConfigError: serial_port is required when connection_type is 'serial'`

The config is missing the `serial_port` field. Add it:

```python
config = MeshtasticConfig(adapter_id="mesh-alpha", connection_type="serial", serial_port="/dev/ttyUSB0")
```

### 15.6 Adapter starts but no inbound packets arrive

Check these things, in order:

1. Is anyone sending messages on the configured channel? The adapter only processes inbound text packets on the channel it is listening to.
2. Is the node receiving messages? Check the node's screen or serial output.
3. Is `mtjk` installed? Without it, the pubsub callback cannot fire.
4. Is the pubsub subscription active? The adapter logs `"started (mode=...)"` on success. If you don't see this, startup failed.
5. Are the arriving packets text messages? The adapter silently drops non-text packets (telemetry, position, nodeinfo, etc.).

### 15.7 `send_one()` raises an exception

The underlying `client.sendText()` call failed. This typically means:

- The TCP connection to the node was lost.
- The serial cable was disconnected.
- The node firmware rejected the message (e.g., too long, wrong channel).

The failed item is permanently dropped (not retried). You must re-enqueue if you want to retry.

### 15.8 `TypeError: MeshtasticAdapter.deliver() accepts RenderingResult only`

You passed a `CanonicalEvent` or raw dict to `deliver()` instead of a `RenderingResult`. The delivery path expects pre-rendered payloads from the rendering pipeline, not raw events.

### 15.9 `ImportError: No module named 'pubsub'`

The `PyPubSub` package is required for the `mtjk` callback mechanism. It should be pulled automatically by `pip install -e ".[meshtastic]"`. If it's missing:

```bash
pip install PyPubSub
```

### 15.10 Live smoke tests all SKIP

Environment variables are not set. Set at minimum:

```bash
export MESHTASTIC_CONNECTION_TYPE="tcp"
export MESHTASTIC_HOST="meshtastic.local"
```

### 15.11 Adapter reports `healthy` but no messages are being sent

Check `queue_health`:

```python
health = adapter.queue_health
print(health)
# If pending_count > 0, items are queued but send_one() hasn't been called.
# If total_failed > 0, sends are failing — check the exception.
```

Remember: `deliver()` only enqueues. You must call `send_one()` (or a loop of `send_one()`) to actually transmit.

### 15.12 `OSError: [Errno 13] Permission denied` on serial port

Your user does not have serial port access:

```bash
sudo usermod -aG dialout $USER
# Log out and log back in
groups  # verify 'dialout' is listed
```

### 15.13 Adapter connects but outbound sends return `None` (no RF transmission)

Check the `MESHTASTIC_LIVE_SEND` transmit guard:

```bash
echo $MESHTASTIC_LIVE_SEND
# If empty or not "1", RF transmission is intentionally blocked.
```

To enable RF transmission:

```bash
export MESHTASTIC_LIVE_SEND=1
```

This is a safety feature, not a bug. Without `MESHTASTIC_LIVE_SEND=1`, the
adapter may connect and health-check but MUST NOT transmit RF messages. See
the "MESHTASTIC_LIVE_SEND Transmit Guard" section in
`docs/runbooks/meshtastic-live-smoke.md` for full documentation.

### 15.14 `MESHTASTIC_LIVE_SEND` set but sends still fail

If `MESHTASTIC_LIVE_SEND=1` is set but sends still fail, check the underlying
connection:

1. Is the node reachable? `ping meshtastic.local` or check serial connection.
2. Is the `mtjk` package installed? `pip show mtjk`.
3. Is the connection type configured? `MESHTASTIC_CONNECTION_TYPE` must be set
   to `tcp`, `serial`, or `ble` (not `fake`).
4. Check `adapter.queue_health` — if `total_failed` is climbing, the
   underlying `sendText()` call is failing for a connection-level reason.

### 15.15 No-SDK tests fail with `ImportError: No module named 'meshtastic'`

No-SDK lifecycle tests (`TestMeshtasticNoSdkLifecycle`) must use
`connection_type="fake"`. If a test is trying to import `meshtastic` at the
module level, that is a bug in the test — the adapter's real-client imports
are deferred behind runtime guards. Fake mode should never trigger a top-level
`import meshtastic`.

## Live Validation Evidence

### Test Results

- **File:** `tests/test_meshtastic_live.py`, `tests/test_meshtastic_storage_roundtrip.py`, `tests/test_meshtastic_evidence_diagnostics.py`
- **Last run:** Not yet run
- **Command:** `pytest tests/test_meshtastic_live.py -m live -v`
- **Result:** Not yet run
- **Environment:**
  - `MESHTASTIC_CONNECTION_TYPE`: required (tcp/serial/ble), not set
  - `MESHTASTIC_HOST`: required for TCP, not set
  - `MESHTASTIC_PORT`: optional (default 4403), not set
  - `MESHTASTIC_SERIAL_PORT`: required for serial, not set
  - `MESHTASTIC_BLE_ADDRESS`: required for BLE, not set
  - `MESHTASTIC_CHANNEL_INDEX`: optional (default 0), not set
  - `MESHTASTIC_NODE_ID`: optional, not set
  - `MESHTASTIC_LIVE_SEND`: required for RF transmission, not set
- **Hardware/Network:** Not available (no Meshtastic radio node connected)
- **Failures/Notes:** Live validation has not been performed in this environment. Alpha operation requires a real Meshtastic radio node with the environment variables configured. Without these, all live tests skip automatically. See the smoke test runbook (`docs/runbooks/meshtastic-live-smoke.md`) for detailed setup and environment variable instructions.

## 16. Explicit Unsupported Features

The following features are not supported in alpha mode. Do not attempt to use them. They are listed here so you do not have to wonder.

| Feature                      | Status                         | Notes                                                                     |
| ---------------------------- | ------------------------------ | ------------------------------------------------------------------------- |
| Automatic reconnection       | Not implemented                | See section 12                                                            |
| Outbound retry               | Not implemented                | Failed sends are permanently dropped                                      |
| ACK / delivery confirmation  | Not implemented                | `wantAck` is not set                                                      |
| Telemetry decoding           | Not supported                  | Telemetry packets are classified but silently dropped                     |
| Position / GPS decoding      | Not supported                  | Position packets are classified but silently dropped                      |
| Node database caching        | Not supported                  | Node info packets are classified but silently dropped                     |
| Admin API                    | Not supported                  | Admin packets are classified but silently dropped                         |
| End-to-end encryption        | Not supported                  | Meshtastic encrypted channels are not handled                             |
| Multi-node mesh testing      | Not tested                     | Alpha has only been validated with a single node                          |
| BLE connectivity             | Documented only                | BLE is a config option but not validated in alpha                         |
| Backlog suppression          | Config field exists, not wired | `startup_backlog_suppress_seconds` is accepted but not used for filtering |
| Store-and-forward            | Not supported                  | No message persistence across restarts                                    |
| Rate limiting / flow control | Not implemented                | Only basic pacing via `message_delay_seconds`                             |
| Transmit guard               | Implemented (`MESHTASTIC_LIVE_SEND`) | RF transmission gated by env var; connect/health allowed without it |
| Non-Meshtastic transports    | Not in scope                   | This runbook covers Meshtastic only                                       |
| Multi-transport bridging     | Not in scope                   | No bridge between Meshtastic and other transports                         |
