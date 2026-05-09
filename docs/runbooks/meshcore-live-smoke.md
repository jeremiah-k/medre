# MeshCore Live Smoke Test Runbook

> Last updated: 2026-05-09
> Status: **Reference only. No live harness exists yet.**
> See: `docs/contracts/19-meshcore-connectivity-readiness.md`

This runbook describes how to test MeshCore connectivity against real hardware when a live smoke harness is eventually built. It documents the SDK's connection methods, required environment variables, and expected behaviors so that when someone sits down with a MeshCore radio node, they have a verified procedure to follow.

**This is not a test you can run today.** The MEDRE adapter has no real MeshCore client code. The adapter's `start()` raises `MeshCoreConnectionError` for any non-fake connection type. This runbook exists to document the SDK's connectivity interface so the eventual harness can be built without re-researching the SDK.


## Purpose

When built, the MeshCore live smoke harness would validate:

- The `meshcore` package installs and imports correctly.
- A `MeshCore` instance connects to a real MeshCore radio node via TCP, serial, or BLE.
- `send_msg()` completes and returns an `Event` with `type == MSG_SENT` and a valid `expected_ack`.
- `send_chan_msg()` completes and returns `Event` with `type == OK`.
- Event subscriptions fire for `CONTACT_MSG_RECV` and `CHANNEL_MSG_RECV`.
- ACK correlation works (expected_ack matches incoming ACK event).
- The full lifecycle (connect, send, receive, disconnect) works cleanly.

### What live smoke would NOT prove

- Full MEDRE adapter integration with real MeshCore events (adapter has no real client code).
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

- **Package name:** `meshcore` on PyPI. Version 2.2.5 audited.
- **Import namespace:** `meshcore` (same as package name).
- **License:** MIT.
- **Dependencies pulled in:** `bleak`, `pyserial-asyncio-fast`, `pycayennelpp`.
- **Optional:** Core MEDRE tests pass without `meshcore`. Only live smoke tests would require it.
- **Source:** `https://github.com/fdlamotte/meshcore_py`
- **SDK is fully async:** All methods are coroutines. No synchronous wrappers.


## Connection Methods

### TCP (recommended for testing)

Connect to a MeshCore node over WiFi/Ethernet via its TCP interface.

```python
from meshcore import MeshCore, EventType

mc = await MeshCore.create_tcp("192.168.1.100", 4000)
# Returns connected MeshCore instance or raises ConnectionError

# Check connection
assert mc.is_connected
```

**Environment variables:**

```bash
export MESHCORE_CONNECTION_TYPE="tcp"
export MESHCORE_HOST="192.168.1.100"
export MESHCORE_PORT="4000"
```

- Default port: `4000` (from SDK examples, not verified as firmware default).
- Connection is async. `create_tcp` handles transport setup, connect, and `appstart()`.
- `appstart()` triggers a `SELF_INFO` event with the node's public key and config.

### Serial

Connect to a MeshCore node via USB serial.

```python
mc = await MeshCore.create_serial("/dev/ttyUSB0", baudrate=115200)
```

**Environment variables:**

```bash
export MESHCORE_CONNECTION_TYPE="serial"
export MESHCORE_SERIAL_PORT="/dev/ttyUSB0"
```

- Default baudrate: `115200`.
- Port must exist. Validate via `ls /dev/ttyUSB*` or `python -m serial.tools.list_ports`.
- Optional `cx_dly` parameter for connection delay (default 0.1s).

### BLE

Connect via Bluetooth Low Energy.

```python
mc = await MeshCore.create_ble("AA:BB:CC:DD:EE:FF", pin="123456")
```

**Environment variables:**

```bash
export MESHCORE_CONNECTION_TYPE="ble"
export MESHCORE_BLE_ADDRESS="AA:BB:CC:DD:EE:FF"
export MESHCORE_BLE_PIN="123456"     # optional
```

- `address` is optional. If omitted, the SDK scans for devices.
- `pin` is optional. Enables BLE pairing authentication.
- Requires `bleak` (installed with `meshcore`).
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
    print(f"Connected: {mc.is_connected}")
    print(f"Self info: {mc.self_info}")
    await mc.disconnect()

asyncio.run(test_connect())
```

Expected:
- `Connected: True`
- `Self info` dict with public key and configuration.

### Step 3: Fetch Contacts

```python
async def test_contacts():
    mc = await MeshCore.create_tcp("192.168.1.100", 4000)
    result = await mc.commands.get_contacts()
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

    # Get a contact to send to
    result = await mc.commands.get_contacts()
    if result.type == EventType.ERROR or not result.payload:
        print("No contacts found")
        await mc.disconnect()
        return

    contact = next(iter(result.payload.values()))

    # Send message
    sent = await mc.commands.send_msg(contact, "MEDRE live smoke test")
    print(f"Send result type: {sent.type}")

    if sent.type == EventType.MSG_SENT:
        exp_ack = sent.payload["expected_ack"].hex()
        timeout_s = sent.payload["suggested_timeout"] / 1000
        print(f"Expected ACK: {exp_ack}")
        print(f"Suggested timeout: {timeout_s}s")

        # Wait for ACK
        ack = await mc.wait_for_event(
            EventType.ACK,
            attribute_filters={"code": exp_ack},
            timeout=timeout_s * 1.2
        )
        print(f"ACK received: {ack is not None}")

    await mc.disconnect()

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

    result = await mc.commands.send_chan_msg(0, "MEDRE live smoke channel test")
    print(f"Channel send result: {result.type}")
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

    received = []

    async def on_message(event):
        received.append(event)
        print(f"Received: type={event.type}, payload keys={list(event.payload.keys())}")

    mc.subscribe(EventType.CONTACT_MSG_RECV, on_message)
    mc.subscribe(EventType.CHANNEL_MSG_RECV, on_message)

    # Start auto-fetching
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

- **Message fetching is pull-based.** You must call `get_msg()` or use `start_auto_message_fetching()` to receive messages. Subscribing to `CONTACT_MSG_RECV` alone is not enough. The device queues messages and notifies via `MESSAGES_WAITING` events.
- **`expected_ack` is bytes, not a string.** Use `.hex()` for comparison with ACK `code` attribute.
- **`send_chan_msg` returns `OK`/`ERROR`, not `MSG_SENT`.** No `expected_ack` for channel messages.
- **Destination truncation.** `send_msg` truncates the destination pubkey to 6 bytes (12 hex chars) by default. Full 32-byte addressing requires explicit prefix_length in `_validate_destination`.
- **Timestamp handling.** If not provided, the SDK generates `int(time.time())`. Two rapid sends may share the same timestamp, which could affect `expected_ack` uniqueness (unverified).


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

- MEDRE adapter integration with real MeshCore events (adapter has no real client code).
- Inbound message reception from a second node (tests would use self-receive).
- Multi-hop mesh delivery.
- Bridge compatibility with Meshtastic.
- BLE connectivity with PIN pairing.
- Production reconnection handling.
- Real-time performance under load.
- Compatibility with all firmware versions.
- `expected_ack` uniqueness guarantees under concurrent sends.


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
