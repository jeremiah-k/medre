# Meshtastic Connection Boundary Design Note

> **Status:** Design note only
> **Classification:** Design Note
> **Authority:** Records design constraints for future real connection; no implementation exists
> **Last reviewed:** 2026-05-24
>
> Contract version: 1
> Last updated: 2026-05-08

This document records the expected ownership boundaries and design
constraints for a future real Meshtastic connection implementation.
It is deliberately **not implemented** in tranche 2.

---

## 1. Ownership Boundaries

### 1.1 MeshtasticAdapter Owns Connection Lifecycle

The adapter is responsible for:

- Creating and configuring the mtjk `MeshInterface` (TCP, serial, or BLE)
- Calling `connect()` / `close()` on the interface
- Detecting disconnection and scheduling reconnection with backoff
- Calling `waitConnected()` or equivalent to confirm the interface is ready

The adapter MUST NOT:

- Expose connection internals to the pipeline
- Allow the pipeline to access the mtjk interface directly
- Block pipeline startup on connection completion (connection should be
  async-negotiated)

### 1.2 MeshtasticAdapter Owns Callback Registration

The adapter is responsible for:

- Registering with mtjk's pubsub topic `"meshtastic.receive"` to receive
  inbound packet callbacks
- Owning the callback -> `_on_packet()` mapping
- Managing callback lifecycle across reconnections (unsubscribe on
  disconnect, re-subscribe on reconnect)
- Draining stale callbacks during startup (backlog suppression)

The adapter MUST NOT:

- Register multiple callbacks for the same topic
- Leak callbacks across adapter `stop()` / `start()` cycles

### 1.3 MeshtasticAdapter Owns Radio Queue/Pacing

The adapter is responsible for:

- Serializing outbound sends through `MeshtasticOutboundQueue`
- Enforcing minimum delay between sends (firmware minimum ~2.0s)
- Enqueuing rendered payloads and processing them from the queue
- Dequeueing one item, sending via mtjk, waiting for delay, then
  processing the next item

The pipeline MUST NOT:

- Perform Meshtastic-specific sleeping (`asyncio.sleep` for pacing)
- Bypass the queue to call mtjk directly

### 1.4 Runtime Pipeline Must Not Sleep for Radio Pacing

The pipeline delivers pre-rendered payloads to `adapter.deliver()`. The
adapter enqueues and paces independently. The pipeline's delivery to
other adapters must not be delayed by Meshtastic radio pacing.

### 1.5 Codec/Classifier Remain Pure

The codec and classifier are stateless pure functions. They operate on
plain dicts and have no dependency on connection state, node database, or
adapter lifecycle. They must never import or reference mtjk.

### 1.6 Renderer Remains Adapter-Owned

`MeshtasticRenderer` lives in the adapter package. The rendering pipeline
dispatches to it by target adapter identity, but the renderer code is
owned by the adapter. No core module imports from the meshtastic package.

### 1.7 Storage Remains Runtime-Owned

Storage is owned by the runtime pipeline, not the adapter. The adapter
reports native refs via `AdapterDeliveryResult` and the pipeline persists
them. The adapter never writes to storage directly.

---

## 2. Connection Types

Future implementation should support these connection types, each
instantiated by `MeshtasticConfig.connection_type`:

### 2.1 TCP

```python
import meshtastic

iface = meshtastic.tcp_interface.TCPInterface(
    hostname=config.host,
    portNumber=config.port or 4403,
    timeout=config.sync_timeout_ms / 1000,
)
```

- Default port: 4403
- Connection is synchronous in mtjk (blocking); should be wrapped in
  `asyncio.get_event_loop().run_in_executor()`
- Health probes via ADMIN_APP with `get_device_metadata_request`

### 2.2 Serial

```python
iface = meshtastic.serial_interface.SerialInterface(
    config.serial_port,
    timeout=config.sync_timeout_ms / 1000,
)
```

- Port must exist: validate via `serial.tools.list_ports.comports()`
- Raise `MeshtasticConnectionError` on missing port

### 2.3 BLE

```python
iface = meshtastic.ble_interface.BLEInterface(
    address=config.ble_address,
    noProto=False,
    noNodes=False,
    timeout=config.sync_timeout_ms / 1000,
)
```

- BLE address must be validated (MAC format)
- BLE connection should be scoped to a dedicated executor
- MTJK BLE fork includes `auto_reconnect=False` for controlled reconnection
- See MMRelay BLE lifecycle for reference (generation tracking, stale
  future cleanup)

### 2.4 Fake

```python
# No connection needed — FakeMeshtasticClient handles everything in-memory
```

- Already implemented in tranche 1
- No mtjk dependency required

---

## 3. Reconnection Strategy (Future)

A future connection implementation should consider:

1. **Exponential backoff**: base delay 1s, max 60s, with jitter
2. **Configurable retry limit**: 0 = infinite, N = max attempts
3. **State preservation**: reconnection must re-register callbacks and
   re-arm backlog suppression
4. **Clean disconnect**: cancel pending sends, unsubscribe callbacks,
   close interface
5. **Health monitoring**: periodic ADMIN_APP probe to detect silent
   disconnection (configurable interval, default 60s)
6. **BTC disconnect detection**: BLE has real-time disconnect events;
   TCP/serial require health probes

---

## 4. Backlog Suppression (Future)

A future startup backlog suppression implementation should consider:

1. **Time-based drain**: drop packets received within N seconds of connect
   (configurable via `startup_backlog_suppress_seconds`, default 5.0s,
   MMRelay used 15.0s)
2. **rxTime-based filtering**: drop packets whose `rxTime` precedes the
   connection start time (with clock skew calibration like MMRelay)
3. **Clock skew calibration**: calibrate from first received packet or
   health probe response
4. **Reconnect grace**: allow one pre-start packet on reconnection for
   skew calibration (like MMRelay's `RECONNECT_PRESTART_BOOTSTRAP_WINDOW_SECS`)

---

## 5. Deferred Items

These are explicitly deferred and should not be implemented in the
connection tranche:

- Telemetry decoding (battery, voltage, air utilization)
- Position decoding (GPS coordinates)
- Node database cache and name resolution
- End-to-end encryption (E2EE) key management
- MMRelay configuration compatibility
- Meshtastic plugin commands (`!command` handling)
- Store-and-forward integration
- Remote hardware control (REMOTE_HARDWARE_APP)
- Admin message handling (ADMIN_APP) beyond health probes

---

_This document describes expected ownership and design for a future
Meshtastic real connection implementation. No code implementing these
patterns exists in the current codebase._
