# Meshtastic Live Smoke Test Runbook

> Last updated: 2026-05-09
> Scope: `tests/test_meshtastic_live.py`

This runbook describes how to run the Meshtastic live smoke tests against
a real Meshtastic radio node, what the tests cover, and what they do not
cover.


## Purpose

The live smoke harness provides **two distinct categories** of tests, which
must not be conflated:

### Category A: Raw `mtjk` API Smoke Tests

These tests exercise the **raw** `meshtastic` (mtjk) library directly —
without going through the MEDRE adapter.  They validate that:

- The `mtjk` package is installed and importable as `meshtastic`.
- A `TCPInterface` (or `SerialInterface`) can connect to a real node.
- `sendText()` completes and returns a `MeshPacket` with a populated `id`.
- `sendData()` completes and returns a `MeshPacket` with a populated `id`.
- The `meshtastic.receive` pubsub callback fires on packet reception.
- Received packets have the expected shape (`decoded`, `id`, `portnum`).

**These tests do NOT exercise the MEDRE adapter's connection, codec, or
send pipeline.**  They prove that the underlying `mtjk` library works
against real hardware.

### Category B: MEDRE Adapter Lifecycle Smoke Tests

These tests exercise the **MEDRE** `MeshtasticAdapter` against a real node:

- The adapter creates a real client via `_create_client()`.
- `start()` connects and subscribes to pubsub callbacks.
- `health_check()` reports `"healthy"` after a successful start.
- `stop()` closes the client and unsubscribes cleanly.

**These tests do NOT exercise full MEDRE adapter `send_one` / `deliver`
integration.**  The adapter lifecycle (connect → health → disconnect) is
tested, but outbound delivery through the MEDRE queue + `send_one` path
against real hardware is deferred to a future harness.

### What is NOT tested live

- Full MEDRE adapter `send_one` integration with real hardware (adapter's
  queue → pacing → real `sendText` via `send_one` is not exercised in
  the live harness; it is tested with monkeypatched clients in unit tests).
- Inbound message reception from a **second** node (tests use self-receive).
- Multi-hop mesh delivery.
- Encrypted channel support.
- Telemetry, position, nodeinfo, or admin packet processing.
- Production-grade reconnection handling.
- BLE connectivity (documented but not exercised in this harness).

The harness is **optional** and **skipped by default**.  Default `pytest`
runs remain fake-only.


## Dependency Installation

The Meshtastic live tests require the `mtjk` package:

```bash
pip install mtjk
```

**Important notes:**

- **Distribution name:** `mtjk` on PyPI.
- **Import namespace:** `meshtastic` (not `mtjk`).  The package is a
  drop-in fork of the upstream Meshtastic Python library.
- **Source:** Fork maintained at `github.com/jeremiah-k/mtjk`.
- **Version:** 2.7.8.post2+ verified.  The upstream/fork master `pyproject`
  name is `meshtastic` v2.7.8.
- **Optional:** Core MEDRE tests pass without `mtjk`.  Only live smoke
  tests require it.

Additional dependency:

```bash
# pubsub is required by mtjk's callback mechanism
pip install pubsub
```


## Connection Types

### TCP (recommended)

Connect to a Meshtastic node over WiFi/Ethernet via its TCP API.

```bash
export MESHTASTIC_CONNECTION_TYPE="tcp"
export MESHTASTIC_HOST="meshtastic.local"    # or IP like 192.168.1.100
# export MESHTASTIC_PORT="4403"              # optional, default 4403
```

**Verified API:**

```python
import meshtastic.tcp_interface

iface = meshtastic.tcp_interface.TCPInterface(
    hostname="meshtastic.local",
    portNumber=4403,
    noNodes=False,
)
iface.waitForConfig()
iface.sendText("hello", channelIndex=0)
iface.close()
```

- Default port: `4403` (verified from mtjk master branch).
- `noNodes=False` allows node database discovery.
- `waitForConfig()` blocks until the initial device config is received.
- Connection is synchronous; wrap in `run_in_executor()` for async code.

### Serial

Connect to a Meshtastic node via USB serial.

```bash
export MESHTASTIC_CONNECTION_TYPE="serial"
export MESHTASTIC_SERIAL_PORT="/dev/ttyUSB0"
```

**Verified API:**

```python
import meshtastic.serial_interface

iface = meshtastic.serial_interface.SerialInterface(
    devPath="/dev/ttyUSB0",
)
```

- Port must exist: validate via `serial.tools.list_ports.comports()`.
- No port number needed.

### BLE

Connect to a Meshtastic node via Bluetooth Low Energy.

```bash
export MESHTASTIC_CONNECTION_TYPE="ble"
export MESHTASTIC_BLE_ADDRESS="AA:BB:CC:DD:EE:FF"
```

**Verified API:**

```python
import meshtastic.ble_interface

iface = meshtastic.ble_interface.BLEInterface(
    address="AA:BB:CC:DD:EE:FF",
)
```

- The constructor uses `address` (not `ble_address` config key).
- `address` is `Optional[str]` — may be `None` for auto-discovery.
- BLE testing requires BLE-capable hardware and OS support (BlueZ on Linux).
- **Not exercised in the current live harness** — added for documentation
  completeness.


## Required Environment Variables

| Variable                      | Required for | Example                   | Description                         |
|-------------------------------|-------------|---------------------------|-------------------------------------|
| `MESHTASTIC_CONNECTION_TYPE`  | All         | `tcp`                     | Connection mode: `tcp`, `serial`, `ble` |
| `MESHTASTIC_HOST`             | TCP         | `meshtastic.local`        | Node hostname or IP address         |
| `MESHTASTIC_PORT`             | TCP         | `4403`                    | TCP port (default `4403`)           |
| `MESHTASTIC_SERIAL_PORT`      | Serial      | `/dev/ttyUSB0`            | Serial device path                  |
| `MESHTASTIC_BLE_ADDRESS`      | BLE         | `AA:BB:CC:DD:EE:FF`       | BLE MAC address                     |
| `MESHTASTIC_CHANNEL_INDEX`    | All         | `0`                       | Channel for test messages (default `0`) |

If any required variable is unset, all live tests skip with a descriptive
message.


## Running the Tests

```bash
# Install the Meshtastic dependency
pip install mtjk

# Set environment variables (TCP example)
export MESHTASTIC_CONNECTION_TYPE="tcp"
export MESHTASTIC_HOST="meshtastic.local"

# Run live tests only
pytest tests/test_meshtastic_live.py -m live -v

# Run all tests EXCEPT live (default behavior)
pytest

# Run everything including live
pytest -m ""
```

### Expected Output (successful run)

```
tests/test_meshtastic_live.py::TestMeshtasticLiveSmoke::test_tcp_interface_connects PASSED
tests/test_meshtastic_live.py::TestMeshtasticLiveSmoke::test_adapter_starts_and_reports_healthy PASSED
tests/test_meshtastic_live.py::TestMeshtasticLiveSmoke::test_send_text_via_raw_interface PASSED
tests/test_meshtastic_live.py::TestMeshtasticLiveSmoke::test_send_data_via_raw_interface PASSED
tests/test_meshtastic_live.py::TestMeshtasticLiveSmoke::test_pubsub_callback_receives_packets PASSED
tests/test_meshtastic_live.py::TestMeshtasticLiveSmoke::test_backlog_suppression_not_implemented_note PASSED
tests/test_meshtastic_live.py::TestMeshtasticLiveSmoke::test_inbound_dm_not_supported_note PASSED
tests/test_meshtastic_live.py::TestMeshtasticLiveSmoke::test_sendtext_returns_packet_with_id_note PASSED
```

### Expected Output (missing env vars — skip behavior)

```
tests/test_meshtastic_live.py::TestMeshtasticLiveSmoke::test_tcp_interface_connects SKIPPED
tests/test_meshtastic_live.py::TestMeshtasticLiveSmoke::test_adapter_starts_and_reports_healthy SKIPPED
...
8 skipped in X.XXs
```

With reason: *"Set MESHTASTIC_CONNECTION_TYPE (tcp/serial/ble) to run
live Meshtastic tests"*


## Common Failures

| Symptom | Cause | Fix |
|---------|-------|-----|
| `ImportError: No module named 'meshtastic'` | `mtjk` not installed | `pip install mtjk` |
| `ConnectionRefusedError` or timeout | Node unreachable, wrong host/port | Verify hostname/IP; check node is powered on; try `ping meshtastic.local` |
| `sendText` returns `None` or empty packet | Node firmware issue | Update node firmware; try with `meshtastic` CLI tool first |
| All tests SKIP | Env vars not set | Set `MESHTASTIC_CONNECTION_TYPE` and corresponding connection params |
| `OSError: [Errno 13] Permission denied` on serial port | User not in `dialout` group | `sudo usermod -aG dialout $USER`; re-login |
| BLE connection fails | BlueZ not running or address wrong | Verify `bluetoothctl scan on` sees the device; check MAC format |
| Pubsub callback never fires | Mesh is silent | Send a message from another node or use self-receive test |
| `MeshtasticConnectionError: mtjk library not installed` | `mtjk` missing but `connection_type != "fake"` | `pip install mtjk` or use `connection_type="fake"` |


## Safety Notes

1. **Radio traffic.** Tests send a small number of text messages (2-3) on
   the configured channel.  Ensure the channel is not used for critical or
   emergency communications during testing.

2. **Message identification.** All test messages are prefixed with
   `MEDRE live smoke` for easy identification and cleanup.

3. **Frequency regulations.** Meshtastic operates on license-free bands
   (primarily 868 MHz EU / 915 MHz US).  Ensure your node is configured
   for your regional regulations.  The tests do not modify radio settings.

4. **Duty cycle.** Tests send a minimal number of packets.  No stress
   testing or high-volume transmission is performed.

5. **Firmware compatibility.** `mtjk` v2.7.8.post2 has been verified
   against the source code.  Actual firmware compatibility depends on
   the node's firmware version.  If you encounter protocol errors,
   update both the node firmware and the `mtjk` package.


## sendText / sendData Findings

### sendText

```python
iface.sendText(
    text,
    destinationId=BROADCAST_ADDR,  # "^all"
    wantAck=False,
    wantResponse=False,
    onResponse=None,
    channelIndex=0,
    portNum=TEXT_MESSAGE_APP,
    replyId=None,
    hopLimit=None,
) -> mesh_pb2.MeshPacket
```

- Returns a `MeshPacket` protobuf with the `id` field populated.
- The `id` is generated by the interface's packet ID generator.
- `channelIndex` defaults to `0`.
- `replyId` can reference a previous packet for reply threading.
- Verified from mtjk master branch source code.

### sendData

```python
iface.sendData(
    data,                           # bytes
    destinationId=BROADCAST_ADDR,
    portNum=TEXT_MESSAGE_APP,
    wantAck=False,
    wantResponse=False,
    onResponse=None,
    channelIndex=0,
    hopLimit=None,
) -> mesh_pb2.MeshPacket
```

- `sendText` is a thin wrapper around `sendData` that encodes text to UTF-8.
- Both return the same type: `MeshPacket` with populated `id`.
- `sendData` allows explicit `portNum` for non-text portnums.

### Uncertainty

- The exact timing of when the `id` is assigned (client-side vs.
  firmware-confirmed) has not been verified with hardware captures.
- The `onResponse` callback mechanism for ACK/NAK tracking has not been
  tested.  It is documented as receiving the routing response packet.
- `hopLimit` behavior (default value, interaction with firmware defaults)
  is not verified beyond the source code.


## What It Proves / Does Not Prove

### Proves

- `mtjk` installs correctly and imports as `meshtastic`.  (Category A)
- TCP/serial connection to a real node works.  (Category A)
- `sendText` and `sendData` return packets with IDs.  (Category A)
- Pubsub callbacks fire for received packets.  (Category A)
- Packet shape includes expected fields (`decoded`, `id`, `portnum`).  (Category A)
- MEDRE `MeshtasticAdapter.start()` connects to a real node.  (Category B)
- MEDRE `health_check()` reports `"healthy"` after start.  (Category B)
- MEDRE `stop()` disconnects cleanly.  (Category B)

### Does Not Prove

- Full MEDRE adapter `send_one` integration with real hardware (outbound
  delivery through the queue + pacing + real `sendText` via `send_one` is
  not tested live; tested with monkeypatched clients in unit tests only).
- Inbound packet reception from a **different** node.
- Multi-hop mesh delivery or routing.
- Encrypted channel support.
- Telemetry, position, nodeinfo, or admin packet handling.
- Reconnection or connection loss recovery.
- BLE connectivity (documented but not tested).
- Real-time performance under load.
- Compatibility with all firmware versions.


## Cleanup

After running tests:

1. **No persistent state is created.**  Test messages are sent to the
   radio channel but no files, databases, or configuration are written.

2. **Test messages remain on the mesh.**  Meshtastic does not support
   message deletion.  Messages are prefixed with `MEDRE live smoke` for
   identification.

3. **Unset environment variables** if running in a shared environment:

   ```bash
   unset MESHTASTIC_CONNECTION_TYPE MESHTASTIC_HOST MESHTASTIC_PORT
   unset MESHTASTIC_SERIAL_PORT MESHTASTIC_BLE_ADDRESS MESHTASTIC_CHANNEL_INDEX
   ```

4. **Disconnect the node** if it was powered on only for testing.


## Live Validation Evidence

### Test Results

- **File:** `tests/test_meshtastic_live.py`, `tests/test_soak.py::TestMeshtasticSoak`
- **Last run:** 2026-05-10
- **Executor:** Live agent (automated)
- **Command:** `pytest tests/test_meshtastic_live.py -m live -v`
- **MEDRE commit:** Pre-beta HEAD (2026-05-10)
- **Python version:** 3.12
- **mtjk version:** 2.7.8.post2+ (imported as `meshtastic`)
- **Connection type:** TCP
- **Node hardware:** Meshtastic device connected via TCP
- **Firmware version:** Reported by node via `waitForConfig`
- **Environment:** Local development machine
- **Wall time:** 34.47s
- **Result:** ✅ **10 passed**, 0 failed, 0 skipped
- **Raw mtjk sendText:** ✅ Returned `MeshPacket` with populated `id`. Packet IDs unique across sends.
- **Raw mtjk sendData:** ✅ Returned `MeshPacket` with populated `id`.
- **Raw mtjk receive callback:** ✅ Pubsub callback fired on packet reception. Inbound telemetry packet observed alongside text packets.
- **MEDRE adapter start:** ✅ Created client, connected, subscribed to pubsub callbacks.
- **MEDRE adapter health → healthy:** ✅ `health_check()` returned `"healthy"` after start.
- **MEDRE adapter stop:** ✅ Closed client, unsubscribed cleanly.
- **Reconnect observations:** Connection maintained stable throughout 34.47s run. No reconnect events triggered.
- **Caveats observed:** Initial harness had two bugs fixed in-tree before final pass: (1) `isConnected` attribute used instead of correct connection-check API; (2) `pypubsub` callback signature mismatch (`pub.sendMessage` vs `pypubsub.subscribe` parameter). Final 10/10 reflects corrected harness.
- **Destructive operations:** None. No admin packets, firmware changes, or config writes.
- **Second-node inbound:** **NOT EXECUTED** — requires a second Meshtastic node not present.
- **Soak test result:** **NOT EXECUTED** (see `tests/test_soak.py::TestMeshtasticSoak`)


## Explicit Scope Exclusions

The following are explicitly **out of scope** for the live smoke harness
and the Meshtastic tranche 1 adapter:

- End-to-end encryption (E2EE)
- Telemetry decoding (battery, voltage, environment metrics)
- Position / GPS decoding
- Node database caching
- Admin API or admin portnum messages
- Remote hardware control
- MMRelay configuration compatibility
- Meshtastic plugin commands
- Store-and-forward integration
- BLE connectivity testing
- Production reconnection handling
- Multi-node mesh testing
- Production deployment instructions
