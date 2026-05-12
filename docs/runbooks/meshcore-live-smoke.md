# MeshCore Live Smoke Test Runbook

> Last updated: 2026-05-12
> Status: **NOT EXECUTED — requires MeshCore radio hardware. No MeshCore node connected to this machine. `meshcore` SDK is installable from PyPI (v2.3.7 confirmed) but live testing impossible without hardware.**
> See: `docs/contracts/19-meshcore-connectivity-readiness.md`
> Scope: `tests/test_meshcore_live.py`
> Audit source: PyPI `meshcore` v2.3.7 wheel, source-extracted inspection

This runbook describes how to test MeshCore connectivity against a real MeshCore radio node. It documents the SDK's connection methods, required environment variables, and expected behaviors so that when someone sits down with a MeshCore radio node, they have a verified procedure to follow.

The MEDRE adapter has session-backed real MeshCore support via `MeshCoreSession`. When `connection_type` is not `"fake"` and the `meshcore` package is installed, the adapter initializes a real MeshCore SDK client, subscribes to events, and can send and receive messages. Without a live node present, real-mode tests skip with `pytest.skip()`. Fake-mode tests run unconditionally.

**All SDK API claims below are labeled CONFIRMED (source-read), INFERRED (pattern-derived), or UNKNOWN (needs hardware).**


## Purpose

The live smoke harness in `tests/test_meshcore_live.py` validates:

- The `meshcore` package installs and imports correctly.
- The MEDRE `MeshCoreAdapter` can `start()` against a real node.
- `health_check()` reports `"healthy"` after successful start.
- `stop()` disconnects cleanly and session reports disconnected.
- `send_text()` delivers a channel message without error.
- Inbound callbacks receive messages with expected fields.
- Diagnostics snapshot is available after start and never exposes secrets.
- Repeated start/stop cycles are stable.

### What live smoke does NOT prove

- Full MEDRE adapter integration with real MeshCore events beyond the live smoke scope.
- Inbound message reception from a second node.
- Multi-hop mesh delivery.
- Bridge compatibility with Meshtastic.
- BLE connectivity with PIN pairing.
- Reconnection handling under real network conditions.
- Production deployment readiness.


## Dependency Installation

The MeshCore SDK is an optional dependency. Core MEDRE tests pass without it.

```bash
pip install meshcore
```

**Notes:**

- **Package name:** `meshcore` on PyPI. Version 2.3.7 audited (CONFIRMED from PyPI source extraction).
- **Import namespace:** `meshcore` (same as package name). CONFIRMED.
- **License:** MIT. CONFIRMED.
- **Dependencies pulled in:** `bleak`, `pyserial-asyncio-fast`, `pycryptodome`, `pycayennelpp`. CONFIRMED.
- **Optional:** Core MEDRE tests pass without `meshcore`. Only live smoke tests require it.
- **Source:** `https://github.com/fdlamotte/meshcore_py` (INFERRED from PyPI; repo returned 404 at audit time).
- **SDK is fully async:** All methods are coroutines. No synchronous wrappers. CONFIRMED.
- **No custom exception classes:** SDK uses standard Python exceptions (`ConnectionError`, `ValueError`, `ImportError`, `OSError`). CONFIRMED.


## Connection Methods

### TCP (recommended for testing)

Connect to a MeshCore node over WiFi/Ethernet via its TCP interface.

```python
from meshcore import MeshCore, EventType

mc = await MeshCore.create_tcp("192.168.1.100", 4000)
# CONFIRMED: Returns connected MeshCore instance, None on appstart failure,
#            or raises ConnectionError on transport failure.
# Always check for None before using mc.

if mc is None:
    print("ERROR: create_tcp returned None (appstart failed)")
else:
    assert mc.is_connected  # CONFIRMED: property
```

**Environment variables:**

```bash
export MESHCORE_CONNECTION_TYPE="tcp"
export MESHCORE_HOST="192.168.1.100"
export MESHCORE_PORT="4000"
```

- Default port: `4000` (from SDK examples, not verified as firmware default).
- Connection is async. `create_tcp` handles transport setup, connect, and `appstart()`. CONFIRMED.
- `appstart()` triggers a `SELF_INFO` event with the node's public key and config. CONFIRMED.
- **Important** (CONFIRMED): `create_tcp` can return `None` if transport connects but `appstart()` fails. Always check for `None`.

### Serial

Connect to a MeshCore node via USB serial.

```python
mc = await MeshCore.create_serial("/dev/ttyUSB0", baudrate=115200)
# CONFIRMED: Same return behavior as create_tcp (MeshCore, None, or ConnectionError)
if mc is None:
    print("ERROR: create_serial returned None")
```

**Environment variables:**

```bash
export MESHCORE_CONNECTION_TYPE="serial"
export MESHCORE_SERIAL_PORT="/dev/ttyUSB0"
```

- Default baudrate: `115200`. CONFIRMED.
- Port must exist. Validate via `ls /dev/ttyUSB*` or `python -m serial.tools.list_ports`.
- Optional `cx_dly` parameter for connection delay (default 0.1s in create_serial). CONFIRMED.
- Serial connect timeout: 10.0 seconds (raises `asyncio.TimeoutError`). CONFIRMED.

### BLE

Connect via Bluetooth Low Energy.

```python
mc = await MeshCore.create_ble("AA:BB:CC:DD:EE:FF", pin="123456")
# CONFIRMED: Same return behavior as create_tcp.
# If address is None, scans for devices with local_name starting "MeshCore".
```

**Environment variables:**

```bash
export MESHCORE_CONNECTION_TYPE="ble"
export MESHCORE_BLE_ADDRESS="AA:BB:CC:DD:EE:FF"
export MESHCORE_BLE_PIN="123456"     # optional
```

- `address` is optional. If omitted, the SDK scans for devices advertising name starting with `"MeshCore"`. CONFIRMED.
- `pin` is optional. Enables BLE pairing authentication (`client.pair()`). CONFIRMED.
- Requires `bleak` (installed with `meshcore`). CONFIRMED.
- Uses Nordic UART Service UUID (`6E400001-B5A3-F393-E0A9-E50E24DCCA9E`). CONFIRMED.
- BLE testing requires BLE-capable hardware and OS support (BlueZ on Linux).
- **Not exercised in any existing harness.** Documented for reference.


## Required Environment Variables

| Variable | Required for | Example | Description |
|----------|-------------|---------|-------------|
| `MESHCORE_CONNECTION_TYPE` | All | `tcp` | Connection mode: `tcp`, `serial`, `ble` |
| `MESHCORE_HOST` | TCP | `192.168.1.100` | Node hostname or IP address |
| `MESHCORE_PORT` | TCP | `4000` | TCP port (default `4000`) |
| `MESHCORE_SERIAL_PORT` | Serial | `/dev/ttyUSB0` | Serial device path |
| `MESHCORE_BLE_ADDRESS` | BLE | `AA:BB:CC:DD:EE:FF` | BLE MAC address |
| `MESHCORE_BLE_PIN` | BLE (optional) | `123456` | BLE pairing PIN |
| `MESHCORE_CHANNEL_INDEX` | All | `0` | Channel for test messages (default `0`) |
| `MESHCORE_DESTINATION` | DM tests | `a1b2c3...` | Hex pubkey prefix for direct message target |

If any required variable is unset, all live tests should skip with a descriptive message.


## Manual Verification Procedure

Until an automated harness exists, use this procedure to verify MeshCore connectivity by hand.

### Step 1: Install and Import

```bash
pip install meshcore
python -c "from meshcore import MeshCore, EventType; print('OK')"
```

Expected: `OK`

### Step 2: Connect

```python
import asyncio
from meshcore import MeshCore, EventType

async def test_connect():
    mc = await MeshCore.create_tcp("192.168.1.100", 4000)
    if mc is None:
        print("ERROR: create_tcp returned None (appstart failed)")
        return
    print(f"Connected: {mc.is_connected}")
    print(f"Self info: {mc.self_info}")
    await mc.disconnect()  # CONFIRMED: disconnect(), NOT close()

asyncio.run(test_connect())
```

Expected:
- `Connected: True`
- `Self info` dict with public key and configuration.

### Step 3: Fetch Contacts

```python
async def test_contacts():
    mc = await MeshCore.create_tcp("192.168.1.100", 4000)
    if mc is None:
        print("ERROR: create_tcp returned None")
        return

    result = await mc.commands.get_contacts()
    if result.is_error():  # CONFIRMED: Event.is_error() helper
        print(f"Error: {result.payload}")
        await mc.disconnect()
        return

    print(f"Event type: {result.type}")
    print(f"Contacts: {len(result.payload)} found")
    for key, contact in result.payload.items():
        print(f"  {contact.get('adv_name', 'unknown')}: {key[:12]}...")
    await mc.disconnect()

asyncio.run(test_contacts())
```

Expected:
- `Event type: EventType.CONTACTS`
- Contact list with public keys and advertised names.

### Step 4: Send Direct Message

```python
async def test_send():
    mc = await MeshCore.create_tcp("192.168.1.100", 4000)
    if mc is None:
        print("ERROR: create_tcp returned None")
        return

    # Get a contact to send to
    result = await mc.commands.get_contacts()
    if result.is_error() or not result.payload:  # CONFIRMED: is_error() helper
        print("No contacts found")
        await mc.disconnect()
        return

    contact = next(iter(result.payload.values()))

    # Send message (CONFIRMED: send_msg returns Event with MSG_SENT or ERROR)
    sent = await mc.commands.send_msg(contact, "MEDRE live smoke test")
    print(f"Send result type: {sent.type}")

    if sent.type == EventType.MSG_SENT:
        exp_ack = sent.payload["expected_ack"].hex()
        timeout_s = sent.payload["suggested_timeout"] / 1000
        print(f"Expected ACK: {exp_ack}")
        print(f"Suggested timeout: {timeout_s}s")

        # Wait for ACK (CONFIRMED: wait_for_event returns Event or None)
        ack = await mc.wait_for_event(
            EventType.ACK,
            attribute_filters={"code": exp_ack},
            timeout=timeout_s * 1.2
        )
        print(f"ACK received: {ack is not None}")

    await mc.disconnect()  # CONFIRMED: disconnect(), NOT close()

asyncio.run(test_send())
```

Expected:
- `Send result type: EventType.MSG_SENT`
- `Expected ACK:` 8-char hex string.
- `ACK received: True` (if target node is reachable).

### Step 5: Send Channel Message

```python
async def test_channel():
    mc = await MeshCore.create_tcp("192.168.1.100", 4000)
    if mc is None:
        print("ERROR: create_tcp returned None")
        return

    # CONFIRMED: send_chan_msg returns Event(OK) or Event(ERROR)
    result = await mc.commands.send_chan_msg(0, "MEDRE live smoke channel test")
    print(f"Channel send result: {result.type}")
    if result.is_error():
        print(f"Error: {result.payload}")
    else:
        print(f"Payload: {result.payload}")

    await mc.disconnect()

asyncio.run(test_channel())
```

Expected:
- `Channel send result: EventType.OK`
- Success payload.

### Step 6: Receive Messages

```python
async def test_receive():
    mc = await MeshCore.create_tcp("192.168.1.100", 4000)
    if mc is None:
        print("ERROR: create_tcp returned None")
        return

    received = []

    async def on_message(event):
        received.append(event)
        print(f"Received: type={event.type}, payload keys={list(event.payload.keys()) if isinstance(event.payload, dict) else type(event.payload)}")

    # CONFIRMED: subscribe returns Subscription, supports attribute_filters
    mc.subscribe(EventType.CONTACT_MSG_RECV, on_message)
    mc.subscribe(EventType.CHANNEL_MSG_RECV, on_message)

    # Start auto-fetching (CONFIRMED: subscribes to MESSAGES_WAITING, loops get_msg)
    await mc.start_auto_message_fetching()

    # Wait for messages (send from another node during this time)
    print("Waiting 30 seconds for incoming messages...")
    await asyncio.sleep(30)

    await mc.stop_auto_message_fetching()
    await mc.disconnect()

    print(f"Total received: {len(received)}")

asyncio.run(test_receive())
```

Expected (if another node sends a message during the 30-second window):
- Callback fires with `EventType.CONTACT_MSG_RECV` or `CHANNEL_MSG_RECV`.
- Payload includes `text`, `pubkey_prefix` (direct) or `channel_idx` (channel).


## Expected Output / Common Failures

### Common Failures

| Symptom | Cause | Fix |
|---------|-------|-----|
| `ImportError: No module named 'meshcore'` | SDK not installed | `pip install meshcore` |
| `ConnectionError: Failed to connect to device` | Node unreachable, wrong host/port | Verify IP; check node is powered on; try `ping 192.168.1.100` |
| `EventType.ERROR` from send | Destination invalid or unreachable | Verify contact exists; check pubkey prefix format |
| ACK timeout (no ACK received) | Target node out of range or off | Ensure target node is powered and within radio range |
| `OSError: [Errno 13] Permission denied` on serial | User not in `dialout` group | `sudo usermod -aG dialout $USER`; re-login |
| BLE scan finds no devices | BlueZ not running or adapter off | `bluetoothctl scan on`; check `rfkill list` |
| `ModuleNotFoundError: No module named 'bleak'` | Incomplete install | `pip install meshcore` (bleak is a declared dependency) |
| All tests SKIP | Env vars not set | Set `MESHCORE_CONNECTION_TYPE` and corresponding params |

### Known Gotchas

- **Factory methods can return None.** (CONFIRMED) `create_tcp`, `create_serial`, `create_ble` return `None` if transport connects but `appstart()` fails. They raise `ConnectionError` only on transport failure. Always check for `None`.
- **Message fetching is pull-based.** (CONFIRMED) You must call `get_msg()` or use `start_auto_message_fetching()` to receive messages. Subscribing to `CONTACT_MSG_RECV` alone is not enough. The device queues messages and notifies via `MESSAGES_WAITING` events.
- **`expected_ack` is bytes, not a string.** (CONFIRMED from send_msg_with_retry) Use `.hex()` for comparison with ACK `code` attribute.
- **`send_chan_msg` returns `OK`/`ERROR`, not `MSG_SENT`.** (CONFIRMED) No `expected_ack` for channel messages.
- **Destination truncation.** (CONFIRMED) `send_msg` truncates the destination pubkey to 6 bytes (12 hex chars) by default. Full 32-byte addressing requires explicit `prefix_length` in `_validate_destination`.
- **Timestamp handling.** (CONFIRMED) If not provided, the SDK generates `int(time.time())`. Two rapid sends may share the same timestamp, which could affect `expected_ack` uniqueness (unverified).
- **Disconnect is `disconnect()`, not `close()`.** (CONFIRMED) The SDK method is `await mc.disconnect()`.
- **`Event.is_error()` helper.** (CONFIRMED) Use `result.is_error()` instead of checking `result.type == EventType.ERROR`.
- **No custom exceptions in SDK.** (CONFIRMED) The SDK uses standard Python exceptions: `ConnectionError`, `ValueError`, `ImportError`, `OSError`, `asyncio.TimeoutError`.
- **Command handler default timeout.** (CONFIRMED) 15.0 seconds (`CommandHandlerBase.DEFAULT_TIMEOUT`).


## Safety Notes

1. **Radio traffic.** Tests send a small number of text messages on the configured channel. Ensure the channel is not used for critical or emergency communications during testing.

2. **Message identification.** All test messages should be prefixed with `MEDRE live smoke` for easy identification.

3. **Frequency regulations.** MeshCore operates on LoRa bands. Ensure your node is configured for your regional regulations. The tests do not modify radio settings.

4. **Duty cycle.** Tests send a minimal number of packets. No stress testing or high-volume transmission is performed.

5. **E2EE.** MeshCore uses always-on encryption. Test messages are encrypted on the wire but readable by any node sharing the channel secret.


## Send Semantics Summary

For full details, see `docs/contracts/19-meshcore-connectivity-readiness.md` Section 3.

| Method | Returns on Success | Returns on Failure | ACK Correlation |
|--------|-------------------|-------------------|-----------------|
| `send_msg(dst, msg)` | `Event(MSG_SENT)` with `expected_ack` + `suggested_timeout` | `Event(ERROR)` | `expected_ack.hex()` matches ACK `code` attribute |
| `send_chan_msg(chan, msg)` | `Event(OK)` | `Event(ERROR)` | No per-message ACK |
| `send_msg_with_retry(dst, msg, ...)` | `Event(MSG_SENT)` on success, `None` on exhaustion | `None` after max attempts | Built-in ACK waiting with configurable timeout |

**Key point for future implementers:** `expected_ack` is the candidate for MEDRE's `native_message_id`. It is a CRC-like token, not an incrementing ID. Two identical sends could produce the same `expected_ack`. This needs hardware verification before relying on it as a unique identifier.


## What It Proves / Does Not Prove

### Would Prove (when harness is built)

- `meshcore` installs and imports correctly.
- TCP/serial/BLE connection to a real MeshCore node works.
- `send_msg` returns `MSG_SENT` with valid `expected_ack` and `suggested_timeout`.
- `send_chan_msg` returns `OK`.
- ACK correlation matches `expected_ack` to incoming ACK events.
- Event subscriptions fire for received messages.
- Contact list fetching works.
- `disconnect()` cleans up resources.

### Does Not Prove

- MEDRE adapter integration with real MeshCore events beyond the live smoke scope.
- Inbound message reception from a second node (tests would use self-receive).
- Multi-hop mesh delivery.
- Bridge compatibility with Meshtastic.
- BLE connectivity with PIN pairing.
- Production reconnection handling.
- Real-time performance under load.
- Compatibility with all firmware versions.
- `expected_ack` uniqueness guarantees under concurrent sends.


## API Findings Table

Source: `meshcore-2.3.7-py3-none-any.whl` (PyPI, source-extracted 2026-05-12).
All findings labeled CONFIRMED (source-read), INFERRED (pattern-derived), or UNKNOWN (needs hardware).

| API Surface | Finding | Status |
|-------------|---------|--------|
| Import name | `from meshcore import MeshCore, EventType` | CONFIRMED |
| `__all__` exports | `MeshCore, EventType, TCPConnection, SerialConnection, BLEConnection, ConnectionManager, BinaryReqType, logger` | CONFIRMED |
| `MeshCore.create_tcp(host, port, ...)` | Async factory classmethod. Returns MeshCore, None, or raises ConnectionError | CONFIRMED |
| `MeshCore.create_serial(port, baudrate=115200, cx_dly=0.1, ...)` | Async factory classmethod. Same return contract as create_tcp | CONFIRMED |
| `MeshCore.create_ble(address=None, client=None, device=None, pin=None, ...)` | Async factory classmethod. Same return contract. Scans for "MeshCore" name prefix | CONFIRMED |
| `mc.is_connected` | Property → `bool` via `connection_manager.is_connected` | CONFIRMED |
| `mc.self_info` | Property → `dict` (populated by SELF_INFO event) | CONFIRMED |
| `mc.contacts` | Property → `dict` (populated by CONTACTS events) | CONFIRMED |
| `mc.subscribe(event_type, callback, attribute_filters)` | Returns `Subscription`. Sync method, delegates to dispatcher | CONFIRMED |
| `mc.unsubscribe(subscription)` | Calls `subscription.unsubscribe()` | CONFIRMED |
| `mc.wait_for_event(event_type, attribute_filters, timeout)` | Returns `Event` or `None` on timeout | CONFIRMED |
| `mc.disconnect()` | Async. Stops dispatcher, stops auto-fetch, disconnects transport | CONFIRMED |
| `mc.start_auto_message_fetching()` | Returns `Subscription`. Auto-calls get_msg() initially | CONFIRMED |
| `mc.stop_auto_message_fetching()` | Async. Unsubscribes, cancels fetch task | CONFIRMED |
| `mc.commands.send_msg(dst, msg, timestamp, attempt)` | Returns `Event(MSG_SENT)` or `Event(ERROR)` | CONFIRMED |
| `mc.commands.send_chan_msg(chan, msg, timestamp)` | Returns `Event(OK)` or `Event(ERROR)` | CONFIRMED |
| `mc.commands.send_msg_with_retry(...)` | Built-in ACK-waiting retry loop. Returns Event or None | CONFIRMED |
| `mc.commands.get_contacts(lastmod, timeout)` | Returns `Event(CONTACTS)` or `Event(ERROR)` | CONFIRMED |
| `mc.commands.get_msg(timeout)` | Returns message event or `Event(NO_MORE_MSGS)` | CONFIRMED |
| `mc.commands.send_appstart()` | Sends `\x01\x03 mccli`. Returns `Event(SELF_INFO)` or `Event(ERROR)` | CONFIRMED |
| `Event` dataclass | Fields: `type: EventType`, `payload: Any`, `attributes: Dict` | CONFIRMED |
| `Event.is_error()` | Returns `self.type == EventType.ERROR` | CONFIRMED |
| `Event.clone()` | Returns copy of event | CONFIRMED |
| EventType enum | 50+ values. Key ones: CONTACT_MSG_RECV, CHANNEL_MSG_RECV, ACK, MSG_SENT, OK, ERROR, CONNECTED, DISCONNECTED, MESSAGES_WAITING, NO_MORE_MSGS, SELF_INFO, CONTACTS | CONFIRMED |
| ErrorMessages | Error codes 1-6 mapped to string names | CONFIRMED |
| Auto-reconnect | Flat 1s delay, iterative loop, max_reconnect_attempts, calls send_appstart on reconnect | CONFIRMED |
| Command serialization | asyncio.Lock (lazy-created), default timeout 15.0s | CONFIRMED |
| Custom exception classes | None. Uses ConnectionError, ValueError, ImportError, OSError, asyncio.TimeoutError | CONFIRMED |
| `expected_ack` exact byte count | ~4 bytes CRC-like (from retry code pattern) | INFERRED |
| `suggested_timeout` unit | Milliseconds (from `/1000` conversion in retry code) | INFERRED |
| Channel secret size | 16 bytes | INFERRED |
| Frame max payload | 300 bytes size limit in frame parser | INFERRED (could be a sanity limit, not actual max) |
| Real hardware packet shape | Matches fixture shapes? | UNKNOWN |
| `expected_ack` collision behavior | Same message, same recipient → same ack? | UNKNOWN |
| BLE PIN interaction with Ed25519 | How pairing relates to identity | UNKNOWN |
| Firmware default port | Is 4000 configurable on device? | UNKNOWN |


## Cleanup

After running tests:

1. **No persistent state is created.** Test messages are sent to the radio but no files, databases, or configuration are written.

2. **Test messages remain on the mesh.** MeshCore does not support message deletion. Messages are prefixed with `MEDRE live smoke` for identification.

3. **Unset environment variables** if running in a shared environment:

   ```bash
   unset MESHCORE_CONNECTION_TYPE MESHCORE_HOST MESHCORE_PORT
   unset MESHCORE_SERIAL_PORT MESHCORE_BLE_ADDRESS MESHCORE_BLE_PIN
   unset MESHCORE_CHANNEL_INDEX MESHCORE_DESTINATION
   ```

4. **Disconnect the node** if it was powered on only for testing.


## Live Validation Evidence

### Test Results

- **File:** `tests/test_meshcore_live.py`
- **Last run:** 2026-05-10
- **Executor:** jeremiah@meshnet-framework
- **Command:** N/A — hardware not available
- **MEDRE commit:** `0e8179e`
- **Python version:** 3.12.3
- **meshcore version:** not installed locally; v2.3.7 audited from PyPI wheel source extraction
- **Connection type:** N/A
- **Node hardware:** **NOT PRESENT**
- **Environment:** Linux, USB devices checked via `lsusb` and `dmesg`
- **Result:** **NOT EXECUTED — requires MeshCore hardware**
- **Passed / Failed / Skipped:** N/A
- **Adapter start:** NOT EXECUTED
- **Health check → healthy:** NOT EXECUTED
- **Send text → success:** NOT EXECUTED
- **Inbound callback received:** NOT EXECUTED
- **Diagnostics snapshot:** NOT EXECUTED
- **Stop → clean teardown:** NOT EXECUTED
- **Reconnect observations:** NOT EXECUTED
- **Caveats observed:** N/A
- **Blocker:** No MeshCore radio hardware connected to this machine. USB devices present: FIDO2 key, Logitech Unifying Receiver, Lenovo mouse, Dell keyboard, Areson wireless receiver, Lite-On camera, QinHeng CH340 USB-to-serial adapter (`/dev/ttyACM0`), C-Media USB audio, eMeet webcam. None of these are MeshCore nodes. The `meshcore` package is installable from PyPI (v2.3.7, pure Python with `bleak`, `pyserial-asyncio-fast`, `pycayennelpp` deps), but without actual MeshCore hardware to connect to, live testing is impossible.
- **Failures/Notes:** Live smoke tests require a MeshCore radio node connected via TCP, serial, or BLE. No MeshCore hardware is available in this environment. The `meshcore` SDK can be installed (`pip install meshcore`) and would import successfully, but all live tests would skip or fail without a connected node. Do not install the SDK without hardware — it serves no validation purpose.


## Explicit Scope Exclusions

The following are explicitly **out of scope** for the MeshCore live smoke harness and the MeshCore tranche 1 adapter:

- Production MeshCore support (remains deferred)
- Bridge design between MeshCore and Meshtastic
- Hardware procurement or firmware flashing
- BLE PIN pairing testing
- Multi-node mesh testing
- Encrypted channel configuration
- Telemetry, position, or device management commands
- Flood message behavior
- Path discovery and routing
- Binary protocol requests
- Auto-reconnect stress testing
- Production deployment instructions

*Production MeshCore support remains deferred. This runbook documents the SDK interface for future use.*
