# Meshtastic transport internals

Adapter construction, startup and shutdown sequences, queue and packet
internals behind the
[Meshtastic transport setup guide](../../ops/transport-setup/meshtastic.md).
The normative contract lives in the
[Meshtastic transport profile](../../spec/transport-profiles/meshtastic.md).

## Connection Modes

### Fake Mode (default)

```python
from medre.config.adapters.meshtastic import MeshtasticConfig

config = MeshtasticConfig(
    adapter_id="mesh-alpha",
    connection_type="fake",
)
```

- No `mtjk` package required. All adapter submodules import successfully without it.
- `start()` sets `_client = None`. No network or serial activity.
- `deliver()` reports deferred local queue admission. With no connected session, `send_one()` returns `None` and no transport-completion feedback is emitted.
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
    tcp_liveness_interval_seconds=60,
    tcp_liveness_timeout_seconds=30,
)
```

- Uses the pinned mtjk `meshtastic.tcp_interface.TCPInterface(hostname, portNumber)`.
- Connection is synchronous internally; `send_one()` wraps `sendText` in `asyncio.to_thread()`.
- mtjk owns low-level TCP heartbeat/socket recovery. MEDRE adds session-level supervision above it: a bounded local metadata request proves round-trip liveness and complete client recreation continues with capped backoff until recovery or adapter shutdown.
- Set `tcp_liveness_interval_seconds=0` to disable the active probe. This is not recommended for long-running TCP bridges because a half-open path may otherwise remain undetected until another operation fails.

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

## Manual Adapter Wiring

There is no dedicated Meshtastic runner. Wire the adapter manually:

```python
import asyncio
import logging
from datetime import datetime, timezone

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
ctx = AdapterContext(
    adapter_id="mesh-alpha",
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

### Startup Sequence

1. The adapter checks `HAS_MESHTASTIC` (the `mtjk` import guard). If not installed and not fake mode, raises `MeshtasticConnectionError`.
2. A `MeshtasticSession` is created, which delegates transport lifecycle to the session boundary.
3. `session.start(message_callback=...)` creates the appropriate interface and subscribes to receive/connection-loss pubsub callbacks. Client creation is synchronous and blocking.
4. TCP sessions start the MEDRE liveness supervisor after initial connection setup; serial/BLE do not run this TCP-specific probe.
5. A background `_drain_task` is created to continuously drain the outbound queue.
6. `_started` is set to `True`.

Expected output:

```text
INFO  MeshtasticAdapter mesh-alpha started (mode=tcp)
```

### Shutdown Sequence

1. The `_drain_task` is cancelled and awaited with a 5-second timeout.
2. All tracked adapter background tasks are cancelled and drained.
3. `session.stop()` cancels TCP liveness/reconnect supervision, unsubscribes pubsub callbacks, and closes the underlying client interface.
4. State is cleared.

**Queue durability:** The adapter-local queue is in-memory and non-durable across process restart. During an ordinary adapter stop, an item interrupted mid-send is reported through the queue-terminal path; when there is no in-flight cancellation, pending queue items remain on the adapter object for a subsequent `start()` of that same object. Durable delivery receipts/outbox state remain owned by core storage.

Shutdown is idempotent.

## Startup drain and reconnect receive watermark

A Meshtastic radio hands its FROMRADIO queue to the next successful
connection, and BLE, serial and TCP connections can deliver queued mesh
packets without a usable `rxTime` or with an unsynchronized clock. After the
first receive-ready connection of each adapter start, the session drops
relay-classified packets for `startup_packet_drain_seconds` (default 15.0;
`0` disables) before `rxTime` evaluation, decode, staging or publication. A
real message arriving during the interval is also lost; drained packets are
not replayed or retried. Non-relay classification, node information and
native routing correlation are not gated by the drain.

After a transparent reconnect, the radio can hand its pre-reconnect queue to
the fresh connection. Every connection generation records a receive-ready
watermark, and the `rxTime` cutoff judges each packet against its own
generation's watermark with `startup_backlog_suppress_seconds` (default 5.0)
as the cutoff tolerance. A fixed five-second, generation-scoped window drops
relay-classified packets without a usable `rxTime` right after a reconnect:
they cannot prove they are not queue backlog. Provably fresh traffic relays
immediately, a rapid reconnect retires any remaining initial drain, and
`startup_packet_drain_seconds: 0` disables both the initial drain and the
reconnect window. The timestamp cutoff still applies after either drain
expires (or is disabled): a radio clock trailing the host by more than the
configured `startup_backlog_suppress_seconds` tolerance can drop fresh
messages. Check clock synchronization and the `startup_backlog_packets_suppressed`
diagnostic before adjusting the tolerance. A zero tolerance setting disables
the timestamp cutoff; future timestamps are accepted, and unknown-age packets
are accepted after the reconnect window, subject to ordinary admission. See
[configuration](../../ops/configuration.md) for the field reference and the
[transport profile](../../spec/transport-profiles/meshtastic.md)
for the normative contract.

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
| `pressure_state`         | `normal`, `warning`, `critical`, or `full`            |
| `warning_threshold_pct`  | Configured advisory pressure threshold                |
| `critical_threshold_pct` | Configured health-degrading pressure threshold        |
| `peak_depth`             | Maximum observed pending depth                        |

## Packet Classification

The `MeshtasticPacketClassifier` classifies all packet types:

- **Relay** (`relay` action): text messages on shared channels — produce canonical events.
- **Ignore**: ACK, telemetry, position, nodeinfo, admin, direct message, empty text — counted in diagnostics and skipped.
- **Drop**: malformed, encrypted — counted and skipped.
- **Deferred**: detection sensor, unknown portnum, plugin_only — counted and skipped.

Only relay-action text messages produce canonical events. Everything else is classified, counted, and skipped.
