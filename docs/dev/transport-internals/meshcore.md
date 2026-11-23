# MeshCore transport internals

Adapter construction, BLE session handling, startup and shutdown sequences,
and outbound delivery paths behind the
[MeshCore transport setup guide](../../ops/transport-setup/meshcore.md).
The normative contract lives in the
[MeshCore transport profile](../../spec/transport-profiles/meshcore.md).

## How MEDRE handles BLE connections

The session layer performs several steps automatically before calling
`MeshCore.create_ble()`:

1. **Stale BlueZ cleanup.** After a BLE disconnect, BlueZ may keep a cached
   connection entry for the device address. Subsequent connection attempts can
   fail with `le-connection-abort-by-local` because BlueZ tries to reuse the
   stale entry. MEDRE checks for a lingering `BleakClient` for the target
   address and disconnects it, then issues a bounded system-level
   `bluetoothctl disconnect` for the address. The system-level release matters
   when the board's companion firmware still holds its single BLE connection
   slot from a previous session: that link was never owned by this process,
   so no per-client call can terminate it. Hosts without `bluetoothctl` skip
   the release silently.
2. **Pre-scan for BLEDevice.** On some BlueZ stacks, passing a raw MAC address
   to `BleakClient` triggers the abort-by-local error, while passing a
   `BLEDevice` object from a live scan succeeds. MEDRE runs a short scan
   (5-second timeout) to find the device and obtain a `BLEDevice` before
   connecting.
3. **Retry with re-scan.** BLE device discovery is racy: a device may be
   advertising one moment and gone the next. MEDRE makes up to 3 connection
   attempts, re-scanning between each one, before declaring failure.

Operators do not need to configure any of this. It runs automatically when
`connection_type` is `ble`.

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
- `start()` sets `_meshcore = None`. No network or serial activity.
- `deliver()` returns a synthetic `AdapterHandoffResult` with `confirmation_level="unknown"`; no real send occurs.
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
    # ble_pin="123456",  # Optional: BLE pairing PIN for programmatic pairing
)
```

### Configure the adapter

The `ble_address` field in `MeshCoreConfig` tells MEDRE which device to connect
to. Set it to the MAC address you recorded during pairing.

```python
config = MeshCoreConfig(
    adapter_id="meshcore-ble",
    connection_type="ble",
    ble_address="AA:BB:CC:DD:EE:FF",
)
```

Or via environment variables:

```bash
export MEDRE_ADAPTER__MESHCORE_BLE__TRANSPORT=meshcore
export MEDRE_ADAPTER__MESHCORE_BLE__CONNECTION_TYPE=ble
export MEDRE_ADAPTER__MESHCORE_BLE__BLE_ADDRESS=AA:BB:CC:DD:EE:FF
```

- MEDRE calls `MeshCore.create_ble(address=..., device=..., pin=...)` after
  pre-scanning via `BleakScanner.find_device_by_filter()`. When a
  `BLEDevice` is found, it is passed as `device=`; otherwise `device=None`
  triggers the address-only fallback path. `pin=` is passed only when
  `ble_pin` is configured.
- Optional `ble_pin` config field enables BLE pairing authentication for
  automated/headless deployments. When set, it is forwarded as the `pin=`
  argument to `create_ble()`. `ble_pin` is a sensitive value — it is never
  exposed in diagnostics, logs, or JSON output.
- **Recommended:** pair the device with `bluetoothctl` first (see the
  transport setup guide). Use `ble_pin` only when host-level pairing is not
  feasible (e.g. headless Pi, CI). Serial connectivity is preferred for
  stable deployments.
- Requires `bleak` (installed automatically with `meshcore`).
- Operators only need to configure `ble_address`. The pre-scan and fallback
  logic runs automatically.

## Manual Adapter Wiring

```python
import asyncio
import logging
from datetime import datetime, timezone

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
ctx = AdapterContext(
    adapter_id="meshcore-alpha",
    publish_inbound=event_bus.publish,
    logger=logger,
    clock=lambda: datetime.now(timezone.utc),
    shutdown_event=asyncio.Event(),
)

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

## Outbound Delivery

The adapter sends directly through the session without an intermediary queue. A successful send means local node acceptance, not mesh delivery or RF confirmation.

- **Channel messages:** `await mc.commands.send_chan_msg(chan, msg)` returns `Event` with `type == OK` on success or `type == ERROR` on failure.
- **Direct messages:** `await mc.commands.send_msg(dst, msg)` returns `Event` with `type == MSG_SENT` and `payload["expected_ack"]` on success, or `type == ERROR` on failure.

In fake mode, `deliver()` returns a synthetic transport-handoff fact with no native message ID and `confirmation_level="unknown"`; no real send occurs.
