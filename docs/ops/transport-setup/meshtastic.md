# Meshtastic Transport Setup

Setting up and running the MEDRE Meshtastic adapter against a real radio node. Pre-release — no stable public API.

## Prerequisites

| Requirement          | Details                                                                                      |
| -------------------- | -------------------------------------------------------------------------------------------- |
| Meshtastic node      | A real radio node (e.g. LilyGO T-Beam, Heltec v3, RAK WisBlock) accessible via TCP or serial |
| Python               | 3.11 or later                                                                                |
| Package install      | Core: `pip install -e .` (fake mode). Real connectivity: `pip install -e ".[meshtastic]"`    |
| Network access (TCP) | Your machine can reach the node's IP address on port 4403                                    |
| Serial access        | USB cable connecting the node; user in `dialout` group on Linux                              |
| Radio channel        | A channel index (default 0) not used for critical or emergency communications                |

Fake mode is the default and recommended path for all development and testing. Real connectivity modes (TCP, serial) are opt-in for live validation only.

## Node Setup

### TCP Connectivity (recommended)

Most Meshtastic nodes expose a TCP API when connected via WiFi or Ethernet. Default port: 4403.

1. Power on the node and connect it to your network.
2. Find the node's IP address (check DHCP table, or use `meshtastic --info`).
3. Verify TCP connectivity:

```bash
nc -zv meshtastic.local 4403
```

4. Optionally verify with the Meshtastic CLI:

```bash
pip install mtjk
meshtastic --host meshtastic.local --info
```

### Serial Connectivity

Connect the node via USB. The node appears as a serial device.

```bash
# Find the serial port
ls /dev/ttyUSB* /dev/ttyACM*

# Ensure serial port access
sudo usermod -aG dialout $USER
# Log out and back in
```

### BLE Connectivity

BLE is a supported connection type but has not been validated against real hardware.

```bash
bluetoothctl scan on
# Note the MAC address of your Meshtastic node
```

### Firmware Compatibility

`mtjk` v2.7.8.post2+ is the verified dependency. If you encounter protocol errors, update both the node firmware and the `mtjk` package.

## Connection Modes

### Fake Mode (default)

No real client. Used for development and testing without hardware.

```python
from medre.config.adapters.meshtastic import MeshtasticConfig

config = MeshtasticConfig(
    adapter_id="mesh-alpha",
    connection_type="fake",
)
```

- No `mtjk` package required. All adapter submodules import successfully without it.
- `start()` sets `_client = None`. No network or serial activity.
- `deliver()` enqueues to the internal queue but `send_one()` returns `None` (no real send).
- `simulate_inbound()` is available for injecting test packets.
- `health_check()` returns `"healthy"` after start.

### TCP Mode

Connects to a Meshtastic node via its TCP API.

```python
config = MeshtasticConfig(
    adapter_id="mesh-alpha",
    connection_type="tcp",
    host="meshtastic.local",
    port=4403,
)
```

- Uses `meshtastic.tcp_interface.TCPInterface(hostname, portNumber)`.
- Connection is synchronous internally; `send_one()` wraps `sendText` in `asyncio.to_thread()`.

### Serial Mode

Connects via USB serial.

```python
config = MeshtasticConfig(
    adapter_id="mesh-alpha",
    connection_type="serial",
    serial_port="/dev/ttyUSB0",
)
```

- Uses `meshtastic.serial_interface.SerialInterface(devPath)`.
- Port must exist and be accessible.

### BLE Mode

Connects via Bluetooth Low Energy. Documented but not validated against real hardware.

```python
config = MeshtasticConfig(
    adapter_id="mesh-alpha",
    connection_type="ble",
    ble_address="AA:BB:CC:DD:EE:FF",
)
```

### Configuration Validation

`MeshtasticConfig.validate()` enforces:

| Connection type | Required fields             |
| --------------- | --------------------------- |
| `fake`          | `adapter_id` only           |
| `tcp`           | `adapter_id`, `host`        |
| `serial`        | `adapter_id`, `serial_port` |
| `ble`           | `adapter_id`, `ble_address` |

Invalid configurations raise `MeshtasticConfigError` before any connection attempt.

## Environment Variables

| Variable                     | Required for | Default | Example             | Description                                  |
| ---------------------------- | ------------ | ------- | ------------------- | -------------------------------------------- |
| `MESHTASTIC_CONNECTION_TYPE` | All          |         | `tcp`               | Connection mode: `tcp`, `serial`, `ble`      |
| `MESHTASTIC_HOST`            | TCP          |         | `meshtastic.local`  | Node hostname or IP                          |
| `MESHTASTIC_PORT`            | TCP          | `4403`  | `4403`              | TCP port                                     |
| `MESHTASTIC_SERIAL_PORT`     | Serial       |         | `/dev/ttyUSB0`      | Serial device path                           |
| `MESHTASTIC_BLE_ADDRESS`     | BLE          |         | `AA:BB:CC:DD:EE:FF` | BLE MAC address                              |
| `MESHTASTIC_CHANNEL_INDEX`   | All          | `0`     | `0`                 | Channel for test messages                    |
| `MESHTASTIC_NODE_ID`         | All          |         | `!25d6e474`         | Meshtastic node ID                           |
| `MESHTASTIC_LIVE_SEND`       | Live TX      |         | `1`                 | Transmit guard. `1` enables RF transmission. |

### Multi-Instance Env Overrides

Override an existing TOML adapter:

```bash
export MEDRE_ADAPTER__RADIO_A__SERIAL_PORT=/dev/ttyUSB0
```

Create an adapter entirely from env vars:

```bash
export MEDRE_ADAPTER__RADIO_A__TRANSPORT=meshtastic
export MEDRE_ADAPTER__RADIO_A__CONNECTION_TYPE=serial
export MEDRE_ADAPTER__RADIO_A__SERIAL_PORT=/dev/ttyUSB0
```

Legacy `MEDRE_MESHTASTIC_*` runtime config variables are unsupported. Migrate to `MEDRE_ADAPTER__<TOKEN>__<FIELD>`.

## Manual Adapter Wiring

There is no dedicated Meshtastic runner. Wire the adapter manually:

```python
import asyncio
import logging

from medre.adapters.meshtastic.adapter import MeshtasticAdapter
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.contracts.adapter import AdapterContext
from medre.core.events.bus import EventBus

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
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        await adapter.stop()
        logger.info("Adapter stopped")

asyncio.run(main())
```

## Startup and Shutdown

### Startup Sequence

1. The adapter checks `HAS_MESHTASTIC` (the `mtjk` import guard). If not installed and not fake mode, raises `MeshtasticConnectionError`.
2. A `MeshtasticSession` is created, which delegates transport lifecycle to the session boundary.
3. `session.start(message_callback=...)` creates the appropriate interface and subscribes to `meshtastic.receive` pubsub callbacks. Client creation is synchronous and blocking.
4. A background `_drain_task` is created to continuously drain the outbound queue.
5. `_started` is set to `True`.

Expected output:

```text
INFO  MeshtasticAdapter mesh-alpha started (mode=tcp)
```

### Shutdown Sequence

1. The `_drain_task` is cancelled and awaited with a 5-second timeout. Items mid-send are dropped.
2. All tracked background tasks are cancelled and drained.
3. `session.stop()` unsubscribes pubsub callbacks and closes the underlying client interface.
4. State is cleared.

**Shutdown queue abandonment:** Items remaining in the adapter-local outbound queue at shutdown are lost — not persisted, not requeued. The queue is in-memory and non-durable. Delivery receipts already written to SQLite survive.

Shutdown is idempotent.

## Health States

| State     | Meaning                                                         |
| --------- | --------------------------------------------------------------- |
| `unknown` | Adapter has not started, or has been stopped                    |
| `healthy` | Adapter started successfully; client is connected               |
| `failed`  | Client exists but start did not complete (subscription failure) |

## Delivery Semantics

### Two-Phase Delivery

Meshtastic delivery is two-phase: `queued` (local queue acceptance) then `sent` (queue drain completed radio send). Neither `queued` nor `sent` means RF confirmation, remote-node receipt, or ACK.

- The queue is in-memory and non-durable across process restart.
- If the process crashes between phases, evidence correctly shows `queued` with no `sent` receipt.
- `sent` means the local node queued the packet for LoRa transmission. Remote receipt is unknown. Fire-and-forget.

### Outbound Gate

When configured with `outbound_mode = "listen_only"`, outbound deliveries are suppressed before RF transmission. Suppressed deliveries appear as non-retryable adapter failures with detail `outbound suppressed: listen_only mode`.

### Renderer Truncation

The renderer applies a UTF-8 byte budget of 227 bytes (`max_text_bytes=227` default) for outbound text. Messages exceeding this are truncated.

## Queue Diagnostics

The adapter exposes `queue_health` as a snapshot of the outbound queue:

| Field                    | Meaning                                               |
| ------------------------ | ----------------------------------------------------- |
| `pending_count`          | Items currently in the outbound queue                 |
| `total_sent`             | Cumulative successful sends                           |
| `total_failed`           | Cumulative send failures (after exhausting retries)   |
| `total_requeued`         | Items requeued for retry after transient send failure |
| `total_exhausted`        | Items that exhausted `max_attempts` and were dropped  |
| `total_permanent_failed` | Items that failed permanently on first attempt        |
| `max_queue_size`         | Maximum queue capacity                                |
| `utilization_pct`        | Current queue utilization as percentage               |

## Packet Classification

The `MeshtasticPacketClassifier` classifies all packet types:

- **Relay** (`relay` action): text messages on shared channels — produce canonical events.
- **Ignore**: ACK, telemetry, position, nodeinfo, admin, direct message, empty text — counted in diagnostics and skipped.
- **Drop**: malformed, encrypted — counted and skipped.
- **Deferred**: detection sensor, unknown portnum, plugin_only — counted and skipped.

Only relay-action text messages produce canonical events. Everything else is classified, counted, and skipped.

## Known Limitations

1. **Fire-and-forget delivery.** `sent` means local node acceptance. No remote receipt confirmation.
2. **In-memory queue is non-durable.** Process crash loses queued items. Receipts in SQLite survive.
3. **No auto-reconnect.** The session does not automatically reconnect on disconnect. Restart the runtime.
4. **No ACK confirmation for broadcast sends.** Meshtastic CLI does not print ACK for broadcast messages on shared channels.
5. **BLE not validated.** BLE connectivity is implemented but not exercised against real hardware.
6. **Single-channel operation.** Current prerelease validation covers text on a single channel index only.
7. **No inbound pubsub delivery proven at Docker level.** meshtasticd simulation mode may not relay packets between TCP clients.

## See Also

- [live-validation/meshtastic.md](../live-validation/meshtastic.md) — live smoke test procedures
- [diagnostics-and-evidence.md](../diagnostics-and-evidence.md) — evidence provenance and bundle collection
- [recovery-and-replay.md](../recovery-and-replay.md) — crash recovery and replay
