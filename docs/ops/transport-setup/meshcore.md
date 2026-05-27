# MeshCore Transport Setup

Setting up and running the MEDRE MeshCore adapter against a real radio node. Alpha status — not production.

## Prerequisites

| Requirement          | Details                                                                                          |
| -------------------- | ------------------------------------------------------------------------------------------------ |
| MeshCore node        | A MeshCore companion radio node accessible via TCP, serial, or BLE                               |
| Python               | 3.11 or later                                                                                    |
| Package install      | Core: `pip install -e .` (fake mode). Real connectivity: `pip install meshcore` (v2.3.7 audited) |
| Network access (TCP) | Your machine can reach the node's IP address on port 4000                                        |
| Serial access        | USB cable connecting the node; user in `dialout` group on Linux                                  |
| BLE access           | BLE-capable hardware and BlueZ on Linux (optional)                                               |

Fake mode is the default and recommended path for all development and testing. Real connectivity modes are opt-in for live validation only.

## Node Setup

### TCP Connectivity (recommended for testing)

Most MeshCore nodes expose a TCP interface. SDK default port: 4000.

1. Power on the node and connect it to your network.
2. Find the node's IP address.
3. Verify TCP connectivity:

```bash
nc -zv 192.168.1.100 4000
```

4. Optionally verify with the MeshCore SDK directly:

```python
import asyncio
from meshcore import MeshCore

async def check():
    mc = await MeshCore.create_tcp("192.168.1.100", 4000)
    if mc is None:
        print("ERROR: create_tcp returned None (appstart failed)")
        return
    print(f"Connected: {mc.is_connected}")
    print(f"Self info: {mc.self_info}")
    await mc.disconnect()

asyncio.run(check())
```

**Note:** `create_tcp` can return `None` if the transport connects but `appstart()` fails. Always check for `None`.

### Serial Connectivity

```bash
# Find the serial port
ls /dev/ttyUSB* /dev/ttyACM*

# Ensure serial port access
sudo usermod -aG dialout $USER
# Log out and back in
```

### BLE Connectivity

BLE is implemented at the session layer. Hardware validation is pending.

```bash
bluetoothctl scan on
# Note the MAC address of your MeshCore node
```

### Firmware Compatibility

`meshcore` v2.3.7 is the audited SDK version. Verify installation:

```bash
python -c "import meshcore; print(meshcore.__all__)"
# Should output: ['BinaryReqType', 'BLEConnection', 'ConnectionManager', 'EventType', 'MeshCore', 'SerialConnection', 'TCPConnection', 'logger']
```

## Connection Modes

### Fake Mode (default)

```python
from medre.config.adapters.meshcore import MeshCoreConfig

config = MeshCoreConfig(
    adapter_id="meshcore-alpha",
    connection_type="fake",
)
```

- No `meshcore` package required.
- `start()` sets `_client = None`. No network or serial activity.
- `deliver()` returns `None` (no real send).
- `simulate_inbound()` is available for injecting test packets.
- `health_check()` returns `"healthy"` after start.

### TCP Mode

```python
config = MeshCoreConfig(
    adapter_id="meshcore-alpha",
    connection_type="tcp",
    host="192.168.1.100",
    port=4000,
)
```

- Uses `await MeshCore.create_tcp(host, port)`.
- Connection is fully async. `create_tcp` handles transport setup, connect, and `appstart()`.
- `appstart()` triggers a `SELF_INFO` event with the node's public key and config.
- Real client creation is handled by `MeshCoreSession`.

### Serial Mode

```python
config = MeshCoreConfig(
    adapter_id="meshcore-alpha",
    connection_type="serial",
    serial_port="/dev/ttyUSB0",
)
```

- Uses `await MeshCore.create_serial(port, baudrate=115200)`.
- Default baudrate: 115200.

### BLE Mode

```python
config = MeshCoreConfig(
    adapter_id="meshcore-alpha",
    connection_type="ble",
    ble_address="AA:BB:CC:DD:EE:FF",
)
```

- Uses `await MeshCore.create_ble(address, pin=None)`.
- Optional `pin` enables BLE pairing authentication.
- Requires `bleak` (installed automatically with `meshcore`).

### Configuration Validation

`MeshCoreConfig.validate()` enforces:

| Connection type | Required fields             |
| --------------- | --------------------------- |
| `fake`          | `adapter_id` only           |
| `tcp`           | `adapter_id`, `host`        |
| `serial`        | `adapter_id`, `serial_port` |
| `ble`           | `adapter_id`, `ble_address` |

Additional rules:

- `identity` (if provided) must be a non-empty string.
- `pubkey` (if provided) must be a non-empty hex string.
- `node_config` must not contain keys named `private_key`, `secret`, or `password`.
- `message_delay_seconds >= 0`, `default_channel >= 0`, `sync_timeout_ms > 0`.

## Environment Variables

| Variable                   | Required for   | Default | Example             | Description                                 |
| -------------------------- | -------------- | ------- | ------------------- | ------------------------------------------- |
| `MESHCORE_CONNECTION_TYPE` | All            |         | `tcp`               | Connection mode                             |
| `MESHCORE_HOST`            | TCP            |         | `192.168.1.100`     | Node hostname or IP                         |
| `MESHCORE_PORT`            | TCP            | `4000`  | `4000`              | TCP port                                    |
| `MESHCORE_SERIAL_PORT`     | Serial         |         | `/dev/ttyUSB0`      | Serial device path                          |
| `MESHCORE_BLE_ADDRESS`     | BLE            |         | `AA:BB:CC:DD:EE:FF` | BLE MAC address                             |
| `MESHCORE_BLE_PIN`         | BLE (optional) |         | `123456`            | BLE pairing PIN                             |
| `MESHCORE_CHANNEL_INDEX`   | All            | `0`     | `0`                 | Channel for test messages                   |
| `MESHCORE_DESTINATION`     | DM tests       |         | `a1b2c3...`         | Hex pubkey prefix for direct message target |

### Env-First Adapter Creation

```bash
export MEDRE_ADAPTER__MESHCORE_TBEAM__TRANSPORT=meshcore
export MEDRE_ADAPTER__MESHCORE_TBEAM__CONNECTION_TYPE=ble
export MEDRE_ADAPTER__MESHCORE_TBEAM__BLE_ADDRESS=C4:4F:33:6A:B0:23
```

Legacy `MEDRE_MESHCORE_*` runtime config variables are unsupported. Migrate to `MEDRE_ADAPTER__<TOKEN>__<FIELD>`.

## Manual Adapter Wiring

```python
import asyncio
import logging

from medre.adapters.meshcore.adapter import MeshCoreAdapter
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.core.contracts.adapter import AdapterContext
from medre.core.events.bus import EventBus

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

### Startup Sequence (real mode)

1. The adapter creates a `MeshCoreSession`, which checks `HAS_MESHCORE` (the `meshcore` import guard). If not installed, raises `MeshCoreConnectionError`.
2. The session calls the appropriate async SDK factory (`MeshCore.create_tcp()`, `create_serial()`). This blocks until the connection is established and `appstart()` completes.
3. The factory can return `None` if transport connects but `appstart()` fails. The session handles this.
4. On success, the SDK emits a `SELF_INFO` event containing the node's Ed25519 public key and configuration.
5. The session subscribes to `CONTACT_MSG_RECV` and `CHANNEL_MSG_RECV` via the SDK's event dispatcher.
6. `_started` is set to `True`.

Expected output:

```text
INFO  MeshCoreAdapter meshcore-alpha started (mode=tcp)
```

### Shutdown Sequence

1. All tracked background tasks are cancelled and drained (5-second timeout).
2. Event subscriptions are unsubscribed.
3. The client's `disconnect()` method is called.
4. State is cleared.

Shutdown is idempotent. Start/stop cycles are safe.

## Health States

| State     | Meaning                                      |
| --------- | -------------------------------------------- |
| `unknown` | Adapter has not started, or has been stopped |
| `healthy` | Adapter started successfully                 |
| `failed`  | Client exists but start did not complete     |

No intermediate states. No `degraded` or `reconnecting` state.

## Outbound Delivery

The adapter sends directly through the session without an intermediary queue. A successful send means local node acceptance, not mesh delivery or RF confirmation.

- **Channel messages:** `await mc.commands.send_chan_msg(chan, msg)` returns `Event` with `type == OK` on success or `type == ERROR` on failure.
- **Direct messages:** `await mc.commands.send_msg(dst, msg)` returns `Event` with `type == MSG_SENT` and `payload["expected_ack"]` on success, or `type == ERROR` on failure.

In fake mode, `deliver()` returns `None` — no real send occurs.

## Known Limitations

1. **No Docker setup for MeshCore.** No containerized MeshCore node for Docker SDK-boundary tests.
2. **BLE hardware validation pending.** BLE is implemented at session layer but not validated against real hardware.
3. **Fire-and-forget delivery.** `sent` means local node acceptance. No remote receipt confirmation.
4. **No auto-reconnect.** The adapter does not automatically reconnect on disconnect.
5. **Contact-based sender resolution is scaffold.** `source_transport_id` carries the raw pubkey prefix.
6. **message_delay_seconds is accepted but not enforced.** Reserved for future pacing.

## See Also

- [live-validation/meshcore.md](../live-validation/meshcore.md) — live smoke test procedures
- [diagnostics-and-evidence.md](../diagnostics-and-evidence.md) — evidence provenance and bundle collection
- [recovery-and-replay.md](../recovery-and-replay.md) — crash recovery and replay
