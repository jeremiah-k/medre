# Meshtastic Live Smoke Test Runbook

> Last updated: 2026-05-21
> Scope: `tests/test_meshtastic_live.py`, `tests/test_meshtastic_storage_roundtrip.py`, `tests/test_meshtastic_evidence_diagnostics.py`

This runbook describes how to run the Meshtastic live smoke tests against
a real Meshtastic radio node, what the tests cover, and what they do not
cover.

## Purpose

The live smoke harness provides **two distinct categories** of tests, which
must not be conflated:

### Category A: Raw `mtjk` API Smoke Tests

These tests exercise the **raw** `meshtastic` (mtjk) library directly —
without going through the MEDRE adapter. They validate that:

- The `mtjk` package is installed and importable as `meshtastic`.
- A `TCPInterface` (or `SerialInterface`) can connect to a real node.
- `sendText()` completes and returns a `MeshPacket` with a populated `id`.
- `sendData()` completes and returns a `MeshPacket` with a populated `id`.
- The `meshtastic.receive` pubsub callback fires on packet reception.
- Received packets have the expected shape (`decoded`, `id`, `portnum`).

**These tests do NOT exercise the MEDRE adapter's connection, codec, or
send pipeline.** They prove that the underlying `mtjk` library works
against real hardware.

### Category B: MEDRE Adapter Lifecycle Smoke Tests

These tests exercise the **MEDRE** `MeshtasticAdapter` against a real node:

- The adapter creates a real client via `_create_client()`.
- `start()` connects and subscribes to pubsub callbacks.
- `health_check()` reports `"healthy"` after a successful start.
- `stop()` closes the client and unsubscribes cleanly.

**These tests do NOT exercise full MEDRE adapter `send_one` / `deliver`
integration.** The adapter lifecycle (connect → health → disconnect) is
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

The harness is **optional** and **skipped by default**. Default `pytest`
runs remain fake-only.

### Category C: No-SDK Lifecycle Tests (`TestMeshtasticNoSdkLifecycle`)

These tests exercise the MEDRE adapter lifecycle **without the `mtjk` package
installed**. They validate that:

- The adapter operates in `connection_type="fake"` mode without `mtjk`.
- `start()`, `health_check()`, and `stop()` all work correctly in fake mode.
- Importing Meshtastic adapter submodules (config, codec, classifier) works
  without `mtjk` — only creating real client instances fails.
- The adapter gracefully handles the no-SDK path without importing errors.

**These tests prove the adapter is safe to develop and test without any
Meshtastic hardware or SDK dependency.**

### Category D: Bounded Live Tests (`TestMeshtasticBoundedLiveTests`)

These tests exercise live radio functionality **only when** the
`MESHTASTIC_LIVE_SEND=1` transmit guard is set. They validate that:

- The adapter connects and health-checks against real hardware.
- RF transmission is guarded by the `MESHTASTIC_LIVE_SEND` flag.
- Without the flag, the adapter may connect but **MUST NOT transmit**.

### No-SDK Behavior (`connection_type="fake"`)

The adapter's fake mode is designed for zero-dependency development and testing:

- **No `mtjk` package required.** Fake mode creates no real client (`_client = None`).
- **Default for all tests.** All pytest runs use fake mode unless explicitly
  overridden with live environment variables.
- **Import behavior.** Meshtastic adapter submodules (`config`, `codec`,
  `packet_classifier`, `outbound_queue`) import successfully without `mtjk`.
  The import guard (`HAS_MESHTASTIC`) is checked only when creating real
  client instances. Importing `meshtastic.tcp_interface` as a module-level
  import will fail without `mtjk`, but the adapter defers all such imports
  behind runtime guards.
- **Concrete limitation.** Without `mtjk`, calling `start()` with
  `connection_type="tcp"` (or `"serial"`, `"ble"`) raises
  `MeshtasticConnectionError`. Only `connection_type="fake"` works.

### New Test Files

| File                                            | Description                                                                                         |
| ----------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| `tests/test_meshtastic_live.py`                 | Live smoke tests (Categories A & B), no-SDK lifecycle (C), bounded live (D)                         |
| `tests/test_meshtastic_storage_roundtrip.py`    | Storage roundtrip tests — validate Meshtastic events survive the full encode → store → decode cycle |
| `tests/test_meshtastic_evidence_diagnostics.py` | Evidence diagnostics tests — validate diagnostic metadata collection and reporting                  |
| `tests/test_meshtastic_nosdk.py`                | Drain lifecycle, queue metrics, delivery lifecycle, and failure classification tests                |

## Dependency Installation

The Meshtastic live tests require the `mtjk` package:

```bash
pip install mtjk
```

**Important notes:**

- **Distribution name:** `mtjk` on PyPI.
- **Import namespace:** `meshtastic` (not `mtjk`). The package is a
  drop-in fork of the upstream Meshtastic Python library.
- **Source:** Fork maintained at `github.com/jeremiah-k/mtjk`.
- **Version:** 2.7.8.post2+ verified. The upstream/fork master `pyproject`
  name is `meshtastic` v2.7.8.
- **Optional:** Core MEDRE tests pass without `mtjk`. Only live smoke
  tests require it.

All Meshtastic dependencies (including `PyPubSub` for callbacks) are pulled
automatically by the `[meshtastic]` extra:

```bash
pip install -e ".[meshtastic]"
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

| Variable                     | Required for | Example             | Description                                                                                                                             |
| ---------------------------- | ------------ | ------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `MESHTASTIC_CONNECTION_TYPE` | All          | `tcp`               | Connection mode: `tcp`, `serial`, `ble`                                                                                                 |
| `MESHTASTIC_HOST`            | TCP          | `meshtastic.local`  | Node hostname or IP address                                                                                                             |
| `MESHTASTIC_PORT`            | TCP          | `4403`              | TCP port (default `4403`)                                                                                                               |
| `MESHTASTIC_SERIAL_PORT`     | Serial       | `/dev/ttyUSB0`      | Serial device path                                                                                                                      |
| `MESHTASTIC_BLE_ADDRESS`     | BLE          | `AA:BB:CC:DD:EE:FF` | BLE MAC address                                                                                                                         |
| `MESHTASTIC_CHANNEL_INDEX`   | All          | `0`                 | Channel for test messages (default `0`)                                                                                                 |
| `MESHTASTIC_NODE_ID`         | All          | `!25d6e474`         | Meshtastic node ID for identifying the local node                                                                                       |
| `MESHTASTIC_LIVE_SEND`       | Live TX      | `1`                 | **Transmit guard.** Must be `1` for RF transmission. Without this flag, the adapter may connect and health-check but MUST NOT transmit. |

If any required variable is unset, all live tests skip with a descriptive
message.

## MESHTASTIC_LIVE_SEND Transmit Guard

The `MESHTASTIC_LIVE_SEND` environment variable is a **transmit guard** that
prevents accidental RF transmission during testing and development.

### Behavior

| `MESHTASTIC_LIVE_SEND` | Connection | Health-Check | Transmit (RF) |
| ---------------------- | ---------- | ------------ | ------------- |
| Not set or empty       | Allowed    | Allowed      | **BLOCKED**   |
| `1`                    | Allowed    | Allowed      | Allowed       |
| Any other value        | Allowed    | Allowed      | **BLOCKED**   |

### Why This Exists

- **Safety.** Prevents accidental radio transmissions when running tests
  against real hardware. Connecting and health-checking is benign; transmitting
  is not.
- **Default-safe.** Tests that only verify connection lifecycle (start →
  health → stop) run without the flag. Tests that transmit RF require the
  flag to be explicitly set.
- **CI-safe.** CI environments never set `MESHTASTIC_LIVE_SEND`, so even if
  a real connection is somehow configured, no RF transmission occurs.

### How Tests Use the Guard

- `TestMeshtasticNoSdkLifecycle` — Always uses fake mode, no guard needed.
- `TestMeshtasticBoundedLiveTests` — Checks `MESHTASTIC_LIVE_SEND` before
  any RF transmission test. Tests that transmit skip if the flag is not set.
- `TestMeshtasticLiveSmoke` (Categories A & B) — Uses the guard for tests
  that call `sendText` or `sendData`.

## Running the Tests

```bash
# Install the Meshtastic dependency
pip install mtjk

# Set environment variables (TCP example)
export MESHTASTIC_CONNECTION_TYPE="tcp"
export MESHTASTIC_HOST="meshtastic.local"

# Run live tests only (connection + health-check, NO RF transmission)
pytest tests/test_meshtastic_live.py -m live -v

# Run live tests WITH RF transmission (requires transmit guard)
export MESHTASTIC_LIVE_SEND=1
pytest tests/test_meshtastic_live.py -m live -v

# Run all tests EXCEPT live (default behavior)
pytest

# Run everything including live
pytest -m ""

# Run no-SDK lifecycle tests (no mtjk required)
pytest tests/test_meshtastic_live.py -k "NoSdkLifecycle" -v

# Run bounded live tests
pytest tests/test_meshtastic_live.py -k "BoundedLive" -v

# Run storage roundtrip tests
pytest tests/test_meshtastic_storage_roundtrip.py -v

# Run evidence diagnostics tests
pytest tests/test_meshtastic_evidence_diagnostics.py -v
```

### Expected Output (successful run)

```text
tests/test_meshtastic_live.py::TestMeshtasticLiveSmoke::test_tcp_interface_connects PASSED
tests/test_meshtastic_live.py::TestMeshtasticLiveSmoke::test_adapter_starts_and_reports_healthy PASSED
tests/test_meshtastic_live.py::TestMeshtasticLiveSmoke::test_send_text_via_raw_interface PASSED
tests/test_meshtastic_live.py::TestMeshtasticLiveSmoke::test_send_data_via_raw_interface PASSED
tests/test_meshtastic_live.py::TestMeshtasticLiveSmoke::test_pubsub_callback_receives_packets PASSED
tests/test_meshtastic_live.py::TestMeshtasticLiveSmoke::test_backlog_suppression_not_implemented_note PASSED
tests/test_meshtastic_live.py::TestMeshtasticLiveSmoke::test_inbound_dm_not_supported_note PASSED
tests/test_meshtastic_live.py::TestMeshtasticLiveSmoke::test_sendtext_returns_packet_with_id_note PASSED
```

### Expected Output (no-SDK lifecycle tests)

```text
tests/test_meshtastic_live.py::TestMeshtasticNoSdkLifecycle::test_fake_mode_start_stop PASSED
tests/test_meshtastic_live.py::TestMeshtasticNoSdkLifecycle::test_fake_mode_health_check PASSED
tests/test_meshtastic_live.py::TestMeshtasticNoSdkLifecycle::test_submodules_import_without_sdk PASSED
tests/test_meshtastic_live.py::TestMeshtasticNoSdkLifecycle::test_real_connection_raises_without_sdk PASSED
```

### Expected Output (bounded live tests)

```text
tests/test_meshtastic_live.py::TestMeshtasticBoundedLiveTests::test_connect_and_health_check PASSED
tests/test_meshtastic_live.py::TestMeshtasticBoundedLiveTests::test_transmit_requires_live_send_flag SKIPPED
```

### Expected Output (missing env vars — skip behavior)

```text
tests/test_meshtastic_live.py::TestMeshtasticLiveSmoke::test_tcp_interface_connects SKIPPED
tests/test_meshtastic_live.py::TestMeshtasticLiveSmoke::test_adapter_starts_and_reports_healthy SKIPPED
...
8 skipped in X.XXs
```

With reason: _"Set MESHTASTIC_CONNECTION_TYPE (tcp/serial/ble) to run
live Meshtastic tests"_

## Common Failures

| Symptom                                                 | Cause                                              | Fix                                                                             |
| ------------------------------------------------------- | -------------------------------------------------- | ------------------------------------------------------------------------------- |
| `ImportError: No module named 'meshtastic'`             | `mtjk` not installed                               | `pip install mtjk`                                                              |
| `ConnectionRefusedError` or timeout                     | Node unreachable, wrong host/port                  | Verify hostname/IP; check node is powered on; try `ping meshtastic.local`       |
| `sendText` returns `None` or empty packet               | Node firmware issue                                | Update node firmware; try with `meshtastic` CLI tool first                      |
| All tests SKIP                                          | Env vars not set                                   | Set `MESHTASTIC_CONNECTION_TYPE` and corresponding connection params            |
| `OSError: [Errno 13] Permission denied` on serial port  | User not in `dialout` group                        | `sudo usermod -aG dialout $USER`; re-login                                      |
| BLE connection fails                                    | BlueZ not running or address wrong                 | Verify `bluetoothctl scan on` sees the device; check MAC format                 |
| Pubsub callback never fires                             | Mesh is silent                                     | Send a message from another node or use self-receive test                       |
| `MeshtasticConnectionError: mtjk library not installed` | `mtjk` missing but `connection_type != "fake"`     | `pip install mtjk` or use `connection_type="fake"`                              |
| Bounded live test skips transmit                        | `MESHTASTIC_LIVE_SEND` not set                     | Set `export MESHTASTIC_LIVE_SEND=1` to enable RF transmission                   |
| No-SDK test fails on real client creation               | `mtjk` not installed but test uses real connection | Use fake mode or install `mtjk`; no-SDK tests must use `connection_type="fake"` |

## Safety Notes

1. **Transmit guard (`MESHTASTIC_LIVE_SEND`).** RF transmission is gated by
   the `MESHTASTIC_LIVE_SEND=1` environment variable. Without this flag, the
   adapter may connect and health-check but **MUST NOT transmit**. This is the
   primary safety mechanism against accidental radio transmissions. Always
   verify this flag is unset when running in environments where RF transmission
   is unwanted.

2. **Radio traffic.** Tests send a small number of text messages (2-3) on
   the configured channel. Ensure the channel is not used for critical or
   emergency communications during testing.

3. **Message identification.** All test messages are prefixed with
   `MEDRE live smoke` for easy identification and cleanup.

4. **Frequency regulations.** Meshtastic operates on license-free bands
   (primarily 868 MHz EU / 915 MHz US). Ensure your node is configured
   for your regional regulations. The tests do not modify radio settings.

5. **Duty cycle.** Tests send a minimal number of packets. No stress
   testing or high-volume transmission is performed.

6. **Firmware compatibility.** `mtjk` v2.7.8.post2 has been verified
   against the source code. Actual firmware compatibility depends on
   the node's firmware version. If you encounter protocol errors,
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
  tested. It is documented as receiving the routing response packet.
- `hopLimit` behavior (default value, interaction with firmware defaults)
  is not verified beyond the source code.

## What It Proves / Does Not Prove

### Proves

- `mtjk` installs correctly and imports as `meshtastic`. (Category A)
- TCP/serial connection to a real node works. (Category A)
- `sendText` and `sendData` return packets with IDs. (Category A)
- Pubsub callbacks fire for received packets. (Category A)
- Packet shape includes expected fields (`decoded`, `id`, `portnum`). (Category A)
- MEDRE `MeshtasticAdapter.start()` connects to a real node. (Category B)
- MEDRE `health_check()` reports `"healthy"` after start. (Category B)
- MEDRE `stop()` disconnects cleanly. (Category B)
- Adapter lifecycle works without `mtjk` installed (fake mode). (Category C)
- Adapter submodules import without `mtjk` dependency. (Category C)
- Real client creation raises `MeshtasticConnectionError` without `mtjk`. (Category C)
- RF transmission is gated by `MESHTASTIC_LIVE_SEND` flag. (Category D)
- Adapter connects and health-checks with transmit guard in place. (Category D)

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

## Recorded Validation Template

Use this template when running live tests against real hardware. Fill in values
and remove any lines that contain secrets before committing.

| Field                        | Value                                             |
| ---------------------------- | ------------------------------------------------- |
| Date                         | YYYY-MM-DD                                        |
| Connection type              | `tcp` / `serial` / `ble`                          |
| Device type                  | e.g. LilyGO T-LORA V2.1, RAK4631, Heltec v3       |
| Test command                 | `pytest tests/test_meshtastic_live.py -m live -v` |
| MESHTASTIC_CONNECTION_TYPE   | `<redacted>`                                      |
| MESHTASTIC_HOST              | `<redacted>` (if TCP)                             |
| MESHTASTIC_SERIAL_PORT       | `<redacted>` (if serial)                          |
| MESHTASTIC_CHANNEL_INDEX     | `<integer>`                                       |
| MESHTASTIC_LIVE_SEND         | `1` (only if transmit tested)                     |
| Node firmware version        | e.g. 2.7.19                                       |
| Health result                | `healthy` / `degraded`                            |
| Send result                  | `passed` / `blocked` / `not tested`               |
| Native packet ID behavior    | e.g. `sequential integers, unique per send`       |
| Known failure modes observed | e.g. None / describe                              |
| Architecture report          | `73 passed, 0 failed`                             |
| Notes                        |                                                   |

If no hardware was available during this tranche, leave this template
unfilled as a checklist for the next operator.

## Diagnostics Reference

The Meshtastic adapter exposes the following fields in `diagnostics()`:

| Field                  | Type | Description                                            |
| ---------------------- | ---- | ------------------------------------------------------ |
| `adapter_id`           | str  | Adapter identifier                                     |
| `platform`             | str  | Always `"meshtastic"`                                  |
| `started`              | bool | Whether the adapter has been started                   |
| `connection_type`      | str  | `fake`, `tcp`, `serial`, or `ble`                      |
| `queue_pending`        | int  | Items currently in the outbound queue                  |
| `queue_total_sent`     | int  | Cumulative successful sends                            |
| `queue_total_failed`   | int  | Cumulative send failures                               |
| `queue_total_rejected` | int  | Cumulative enqueue attempts rejected due to full queue |
| `drain_task_running`   | bool | Whether the background queue-drain task is active      |
| `background_tasks`     | int  | Number of tracked background tasks                     |

> **Queue counter semantics:** `queue_total_sent` counts items where the local SDK/client `sendText` returned a success result — this is **local send confirmation only**, not RF delivery or remote-node receipt. `queue_pending` counts items waiting in the adapter-local in-memory queue. Both counters reset on process restart; the queue is non-durable.
>
> **`outbound_mode = "listen_only"` effect on diagnostics:** When the adapter is configured with `outbound_mode = "listen_only"`, outbound delivery is suppressed before RF transmission. Suppressed deliveries appear as non-retryable adapter failures with a detail like `outbound suppressed: listen_only mode`. `queue_total_sent` does not increment for suppressed deliveries. Inbound reception and inbound diagnostics counters are unaffected.

**Session diagnostics** (present when adapter has been started):

| Field                         | Type          | Description                                  |
| ----------------------------- | ------------- | -------------------------------------------- |
| `connected`                   | bool          | SDK client is created and session is started |
| `reconnecting`                | bool          | Session is in reconnect backoff              |
| `reconnect_attempts`          | int           | Consecutive reconnect attempts               |
| `last_packet_time`            | float or null | Monotonic time of last received packet       |
| `node_id`                     | str or null   | Our node ID                                  |
| `channel_count`               | int           | Number of known channels                     |
| `transient_delivery_failures` | int           | Count of transient send failures             |
| `permanent_delivery_failures` | int           | Count of permanent send failures             |
| `last_error`                  | str or null   | Most recent error description                |

**Secret safety:** Diagnostics intentionally excludes serial port paths,
hostnames, IP addresses, BLE MAC addresses, tokens, passwords, and keys.
See `test_meshtastic_nosdk.py::TestMeshtasticDiagnostics::test_diagnostics_no_secrets_after_start`
for the recursive no-leak assertion.

## Cleanup

After running tests:

1. **No persistent state is created.** Test messages are sent to the
   radio channel but no files, databases, or configuration are written.

2. **Test messages remain on the mesh.** Meshtastic does not support
   message deletion. Messages are prefixed with `MEDRE live smoke` for
   identification.

3. **Unset environment variables** if running in a shared environment:

   ```bash
   unset MESHTASTIC_CONNECTION_TYPE MESHTASTIC_HOST MESHTASTIC_PORT
   unset MESHTASTIC_SERIAL_PORT MESHTASTIC_BLE_ADDRESS MESHTASTIC_CHANNEL_INDEX
   unset MESHTASTIC_NODE_ID MESHTASTIC_LIVE_SEND
   ```

4. **Disconnect the node** if it was powered on only for testing.

## Live Validation Evidence

### Test Results

- **File:** `tests/test_meshtastic_live.py`, `tests/test_soak.py::TestMeshtasticSoak`, `tests/test_meshtastic_storage_roundtrip.py`, `tests/test_meshtastic_evidence_diagnostics.py`
- **Last run:** 2026-05-10
- **Executor:** Live agent (automated)
- **Command:** `pytest tests/test_meshtastic_live.py -m live -v`
- **MEDRE commit:** Pre-beta HEAD (2026-05-10)
- **Python version:** 3.12
- **mtjk version:** 2.7.8.post2+ (imported as `meshtastic`)
- **Connection type:** Serial
- **Node hardware:** LilyGO T-LORA V2.1, node `!25d6e474`
- **Serial port:** `/dev/ttyACM0`
- **Firmware version:** 2.7.19
- **Channel:** Test (PRIMARY, LONG_FAST)
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
- **Caveats observed:** Initial harness had two bugs fixed in-tree before final pass: (1) `isConnected` TypeError — attribute used instead of correct connection-check API; (2) `pypubsub` ListenerMismatchError — callback signature mismatch (`pub.sendMessage` vs `pypubsub.subscribe` parameter). Final 10/10 reflects corrected harness.
- **Destructive operations:** None. No admin packets, firmware changes, or config writes.
- **Second-node inbound:** **NOT EXECUTED** — requires a second Meshtastic node not present.
- **Soak test result:** **NOT EXECUTED** (see `tests/test_soak.py::TestMeshtasticSoak`)

## Multi-Instance Operation

MEDRE supports multiple Meshtastic adapters per runtime. There are two
ways to configure them:

### Mode A: Override an existing TOML adapter

If the adapter is already defined in the TOML config, override any field
at runtime with the per-instance env var pattern:

```bash
export MEDRE_ADAPTER__RADIO_A__SERIAL_PORT=/dev/ttyUSB0
```

The adapter must exist in the TOML file (`[adapters.meshtastic.radio-a]`).
The env var only modifies the specified field — all other fields come from
the config file.

### Mode B: Create an adapter entirely from env vars

When `TRANSPORT=meshtastic` is set and the token does not match any
TOML adapter, a new adapter is created from env vars alone:

```bash
# Radio A — serial
export MEDRE_ADAPTER__RADIO_A__TRANSPORT=meshtastic
export MEDRE_ADAPTER__RADIO_A__CONNECTION_TYPE=serial
export MEDRE_ADAPTER__RADIO_A__SERIAL_PORT=/dev/ttyUSB0

# Radio B — TCP
export MEDRE_ADAPTER__RADIO_B__TRANSPORT=meshtastic
export MEDRE_ADAPTER__RADIO_B__CONNECTION_TYPE=tcp
export MEDRE_ADAPTER__RADIO_B__HOST=192.168.1.25
export MEDRE_ADAPTER__RADIO_B__PORT=4403
```

Required fields for Meshtastic env-created adapters:

- `TRANSPORT` — must be `meshtastic`
- `CONNECTION_TYPE` — `serial`, `tcp`, `ble`, or `fake` (default)

Conditionally required fields per connection type:

- TCP: `HOST` (required), `PORT` (optional, default `4403`)
- Serial: `SERIAL_PORT` (required)
- BLE: `BLE_ADDRESS` (required)

All other `MeshtasticConfig` fields are optional and use their dataclass
defaults when omitted.

### Override vs. create behaviour

- If a TOML adapter with the same `adapter_id` exists: env vars with
  matching fields override the TOML values. The `TRANSPORT` field is
  ignored (the TOML adapter's transport is already known).
- If no TOML adapter matches: `TRANSPORT` must be set to `meshtastic`,
  or the env vars are rejected with `ConfigValidationError`.

### Routes

Routes can be defined in the TOML config file or created from
environment variables (see below). Env-created adapter IDs can be
referenced in route definitions normally:

```toml
[routes.a_to_bridge]
source_adapters = ["radio-a"]
dest_adapters = ["radio-b"]
directionality = "source_to_dest"
enabled = true
```

Simple routes can also be created from environment variables using
`MEDRE_ROUTE__<TOKEN>__<FIELD>`. Route tokens may contain only letters,
numbers, and underscores. Advanced route features (e.g. complex directionality,
conditional routing) may still require TOML configuration.

Route adapter references are adapter IDs (e.g. `radio-a`), not env tokens.

### Legacy vars

- `MESHTASTIC_*` env vars are `pytest` live-test **convenience vars**
  only (see `Required environment variables`\_ above). They are **not**
  runtime config overrides.
- `MEDRE_MESHTASTIC_*` is a **legacy** pattern and remains **unsupported** —
  it triggers `ConfigValidationError` with a migration message pointing to
  `MEDRE_ADAPTER__<TOKEN>__<FIELD>`.
- All runtime config uses `MEDRE_ADAPTER__<TOKEN>__<FIELD>`.

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
- Matrix, MeshCore, or LXMF transport testing (out of scope for Meshtastic runbooks)
