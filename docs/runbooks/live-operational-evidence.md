# Live Operational Evidence Runbook

> Last updated: 2026-05-12
> Tracks: 2, 3, 7
> Status: Procedures documented. Live execution: **NOT EXECUTED** on this machine (no Matrix homeserver credentials or Meshtastic hardware connected).
> Evidence schema: `docs/contracts/61-operational-evidence-contract.md`
> Primary evidence recording: `docs/runbooks/operational-evidence.md`

This runbook provides detailed live operational procedures for Matrix and Meshtastic transports. Each procedure specifies exact environment variables, expected durations, observations to record, and NOT EXECUTED sections when hardware or credentials are absent.

**Evidence tier:** All procedures in this document, if executed against real endpoints, produce R-tier (real-live-runtime) evidence per Contract 61. If not executed, fields remain NOT EXECUTED with documented reasons.


## 1. Matrix Live Procedures

### 1.1 Environment Variables

| Variable | Required | Description | Example |
|----------|----------|-------------|---------|
| `MATRIX_HOMESERVER` | Yes | Full URL of Matrix homeserver | `https://matrix.org` |
| `MATRIX_USER_ID` | Yes | Fully-qualified Matrix user ID | `@bot:matrix.org` |
| `MATRIX_ACCESS_TOKEN` | Yes | Access token for the bot account | `syt_xxx...` |
| `MATRIX_ROOM_ID` | Yes | Room ID to send test messages to | `!abc123:matrix.org` |
| `MATRIX_ENCRYPTION_MODE` | No | Encryption mode (default: `plaintext`) | `e2ee_required` |
| `MATRIX_DEVICE_ID` | E2EE only | Device ID for crypto store | `DEVICEABC` |
| `MATRIX_STORE_PATH` | E2EE only | Crypto store directory path | `/tmp/nio-store` |
| `MATRIX_INBOUND_SENDER` | Inbound test | Expected third-party sender MXID | `@alice:matrix.org` |

**If any required variable is unset, all live Matrix tests skip with a descriptive message.**


### 1.2 Plaintext Smoke Procedure

**Test file:** `tests/test_matrix_live.py`
**Command:** `pytest tests/test_matrix_live.py -m live -v`
**Expected duration:** 10–30 seconds

#### Observations to Record

| Field | What to observe | Where to find it |
|-------|-----------------|------------------|
| Start/connect | Adapter starts, `restore_login` succeeds, sync task begins | Test output: `test_adapter_starts_and_connects` |
| Health → healthy | `health_check()` returns `health == "healthy"`, `platform == "matrix"` | Test output: `test_health_check_healthy` |
| Room join | Room joined successfully | Test output: `test_join_room` |
| Outbound send | `room_send` returns event_id starting with `$` | Test output: `test_send_text_message` |
| Self-echo suppression | Own messages suppressed by sender match | Test output: `test_self_message_suppressed` |
| Stop → clean teardown | `stop()` completes, no leaked tasks | Test output: `test_stop_clean_teardown` |
| Restart idempotency | Stop → start cycle re-establishes sync | Test output: `test_restart_idempotent` |

#### NOT EXECUTED (current machine)

| Field | Value |
|-------|-------|
| **Execution date** | NOT EXECUTED |
| **Reason** | No Matrix homeserver credentials configured on this machine. `MATRIX_HOMESERVER`, `MATRIX_USER_ID`, `MATRIX_ACCESS_TOKEN`, and `MATRIX_ROOM_ID` are not set in the environment. |
| **Resolution** | Set the four required environment variables and re-run `pytest tests/test_matrix_live.py -m live -v`. Record results in `operational-evidence.md` §1.1 with tier `R`. |


### 1.3 Matrix Sync Timing and Diagnostics

Matrix sync is a long-poll operation. The adapter's `_sync_forever` loop maintains a persistent connection to the homeserver. Key timing and diagnostic fields:

| Field | Source | Expected behavior |
|-------|--------|-------------------|
| `sync_task_running` | `diagnostics()` | `True` after start, `False` after stop |
| `sync_running` | `diagnostics()` | `True` while sync loop is active |
| `last_successful_sync` | `diagnostics()` | Timestamp of last successful sync response |
| `reconnecting` | `diagnostics()` | `True` during reconnect backoff |
| `reconnect_attempts` | `diagnostics()` | Count of consecutive failed attempts (max 10) |
| `last_sync_error` | `diagnostics()` | String of last sync error, or `None` |
| `connected` | `diagnostics()` | `True` when nio client is connected |
| `logged_in` | `diagnostics()` | `True` after successful `restore_login` |

**Sync reconnect budget:** Maximum 10 consecutive reconnect attempts (source: `_MAX_RECONNECT_ATTEMPTS` in `session.py`). Exponential backoff: base 1.0s, cap 60.0s, jitter 25% (source: `_BACKOFF_BASE`, `_BACKOFF_CAP`, `_BACKOFF_JITTER_FRACTION`).

**Health transitions during reconnect:**
- Healthy → Degraded: when reconnect begins
- Degraded → Healthy: when sync recovers
- Degraded → Failed: when reconnect budget exhausted

#### NOT EXECUTED (current machine)

| Field | Value |
|-------|-------|
| **sync_start_latency_ms** | NOT EXECUTED — no live session available |
| **outbound_send_latency_ms** | NOT EXECUTED — no live session available |
| **reconnect_behavior** | NOT EXECUTED — no network interruption available |

**Historical observation (H-tier, 2026-05-10):** Health stays `degraded` during reconnect, `healthy` after recovery. Budget exhaustion → `failed`. Initial harness had a bug where `health_check()` was awaited as a coroutine instead of called as a regular method. Fixed before final run.


### 1.4 Matrix Restart/Replay Procedure

Matrix adapter restart tests validate state preservation across stop/start cycles:

1. Start adapter → connect to homeserver → verify healthy
2. Send a message → record event_id
3. Stop adapter → verify clean teardown
4. Start adapter again → verify sync re-establishes
5. Verify `restore_login` succeeds on second start
6. Verify diagnostics fields reflect fresh session

**Crypto store reuse (E2EE mode):** When `encryption_mode` is `e2ee_required`:
- `crypto_store_loaded` should be `True` after first `restore_login`
- On restart, `restore_login` reuses the existing crypto store at `{state}/adapters/{id}/matrix/store/`
- `crypto_enabled` should be `True` if `ENCRYPTION_ENABLED` sentinel is `True` in nio
- `undecryptable_event_count` tracks events that could not be decrypted

**Test file:** `tests/test_matrix_e2ee_live.py`
**Command:** `pytest tests/test_matrix_e2ee_live.py -m live -v`
**Expected duration:** 5–15 seconds

#### NOT EXECUTED (current machine)

| Field | Value |
|-------|-------|
| **Execution date** | NOT EXECUTED |
| **Reason** | E2EE-specific environment variables (`MATRIX_DEVICE_ID`, `MATRIX_STORE_PATH`) not configured. No encrypted room available. |
| **Resolution** | Install `pip install -e ".[matrix-e2e]"`, set E2EE env vars, run E2EE live tests. |

**Historical E2EE evidence (H-tier, 2026-05-10):**
- E2EE tests: 7/7 passed in 3.73s
- Crypto store loaded: confirmed
- Encrypted send → event_id: confirmed (after fix for `OlmUnverifiedDeviceError`)
- Fix: adapter set `ignore_unverified_devices=True` (required by upstream nio, no cross-signing support)
- Undecryptable events: 0 observed


### 1.5 Matrix Third-Party Inbound Procedure

This procedure validates that the adapter correctly processes inbound messages from a third-party Matrix user (not the bot itself).

**Prerequisites:**
1. Core Matrix env vars set
2. A second Matrix account (not the bot) with access to `MATRIX_ROOM_ID`
3. `MATRIX_INBOUND_SENDER` set to the second account's MXID

**Procedure:**
```bash
# 1. Set env vars
export MATRIX_HOMESERVER="http://localhost:8008"
export MATRIX_USER_ID="@bot:localhost"
export MATRIX_ACCESS_TOKEN="syt_..."
export MATRIX_ROOM_ID="!test:localhost"
export MATRIX_INBOUND_SENDER="@alice:localhost"

# 2. Run the inbound test
pytest tests/test_matrix_live.py::TestMatrixLiveSmoke::test_inbound_message_received -m live -v

# 3. During the 30-second wait window, send a message from @alice:localhost
#    into the test room.

# 4. Expected: test passes if message received within window.
#    If no message received, test xfails (acceptable).
```

#### NOT EXECUTED (current machine)

| Field | Value |
|-------|-------|
| **Third-party inbound** | NOT EXECUTED |
| **Reason** | No second Matrix account configured. Requires manual coordination: a different user must send a message during the 30-second test window. |
| **Blocker** | (1) Second Matrix account credentials not available in repo. (2) Manual coordination required. (3) No automated sender harness exists. |

**Deterministic validation status (S-tier):** Unit tests confirm the full inbound pipeline logic (nio sync → `_on_room_message` → codec decode → `publish_inbound()` → canonical event shape → diagnostics counters). See `TestThirdPartyInboundCanonicalEventShape` (8 tests) in `tests/test_matrix_adapter.py`.


### 1.6 Matrix Diagnostics Snapshot Fields

When a Matrix adapter is running, `diagnostics()` returns these fields:

```
connected, logged_in, sync_task_running, last_sync_error,
store_path_configured, device_id_configured, encryption_mode,
crypto_enabled, last_crypto_error, encrypted_room_seen,
undecryptable_event_count, sync_running, reconnecting,
reconnect_attempts, last_successful_sync, crypto_store_loaded,
encrypted_room_count, plaintext_room_count,
transient_delivery_failures, permanent_delivery_failures,
inbound_published, inbound_suppressed_self,
inbound_suppressed_envelope, inbound_filtered_allowlist
```

No secrets, access tokens, room keys, session IDs, or user secrets are exposed. Verified by Contract 27 (Diagnostics Consistency Audit) and Contract 48 (Observability Contract).


## 2. Meshtastic Live Procedures

### 2.1 Environment Variables

| Variable | Required | Description | Example |
|----------|----------|-------------|---------|
| `MESHTASTIC_CONNECTION_TYPE` | Yes | Connection mode: `tcp`, `serial`, or `ble` | `serial` |
| `MESHTASTIC_HOST` | TCP only | Hostname or IP for TCP connections | `192.168.1.100` |
| `MESHTASTIC_PORT` | TCP optional | Port for TCP (default `4403`) | `4403` |
| `MESHTASTIC_SERIAL_PORT` | Serial only | Serial device path | `/dev/ttyACM0` |
| `MESHTASTIC_BLE_ADDRESS` | BLE only | BLE MAC address | `AA:BB:CC:DD:EE:FF` |
| `MESHTASTIC_CHANNEL_INDEX` | No | Channel index (default `0`) | `0` |

**If required variables for the chosen connection type are unset, all live Meshtastic tests skip.**


### 2.2 Smoke Procedure (Raw mtjk + MEDRE Adapter)

**Test file:** `tests/test_meshtastic_live.py`
**Command:** `pytest tests/test_meshtastic_live.py -m live -v`
**Expected duration:** 20–60 seconds (includes serial/TCP connection establishment)

#### Observations to Record

| Field | What to observe | Where to find it |
|-------|-----------------|------------------|
| Connection established | TCP/Serial interface connects to node | Test output: Category A tests |
| `sendText()` → MeshPacket | Returns packet with populated `id` | Test output: `test_raw_send_text` |
| `sendData()` → MeshPacket | Returns packet with populated `id` | Test output: `test_raw_send_data` |
| Pubsub callback fires | Received packets have expected shape | Test output: `test_raw_receive_callback` |
| Outbound packet IDs unique | IDs differ across multiple sends | Test output: Category A send tests |
| MEDRE adapter start | `_create_client()` connects, subscribes | Test output: Category B tests |
| Health → healthy | `health_check()` returns `"healthy"` | Test output: `test_adapter_health_healthy` |
| Stop → clean teardown | Client closed, unsubscribed | Test output: `test_adapter_stop_clean` |

#### NOT EXECUTED (current machine)

| Field | Value |
|-------|-------|
| **Execution date** | NOT EXECUTED |
| **Reason** | No Meshtastic radio hardware connected to this machine. No serial device at `/dev/ttyACM0`. No TCP-accessible Meshtastic node on the network. |
| **Resolution** | Connect a Meshtastic radio (e.g. LilyGO T-Beam, Heltec v3, RAK WisBlock) via USB or TCP. Set `MESHTASTIC_CONNECTION_TYPE` and corresponding host/serial variables. Re-run live tests. |

**Historical evidence (H-tier, 2026-05-10):**
- Connection: Serial to `/dev/ttyACM0`, LilyGO T-LORA V2.1, node `!25d6e474`
- Firmware: 2.7.19, mtjk 2.7.8.post2+
- Results: 10/10 passed, 34.47s wall time
- Raw sendText: confirmed (unique packet IDs)
- Raw sendData: confirmed
- Pubsub callback: confirmed (inbound telemetry packet observed)
- MEDRE adapter lifecycle: confirmed (start, health, stop)
- Bugs found and fixed: `isConnected` TypeError, `pypubsub` ListenerMismatchError


### 2.3 Serial Reconnect Procedure

The Meshtastic session implements bounded reconnection with exponential backoff:

- **Maximum reconnect attempts:** 10 (source: `_MAX_RECONNECT_ATTEMPTS` in `session.py`)
- **Backoff base:** 1.0s, **cap:** 30.0s, **jitter:** 25% (source: `_BACKOFF_BASE`, `_BACKOFF_CAP`, `_BACKOFF_JITTER_FRACTION`)
- **Reconnect triggers:** Serial disconnect, TCP connection loss, client error

**Procedure to test serial reconnect:**
```bash
# 1. Set serial env vars
export MESHTASTIC_CONNECTION_TYPE="serial"
export MESHTASTIC_SERIAL_PORT="/dev/ttyACM0"

# 2. Start a live smoke test
pytest tests/test_meshtastic_live.py -m live -v -s

# 3. During execution, physically disconnect the USB cable
# 4. Observe: reconnecting=True, reconnect_attempts incrementing
# 5. Reconnect the USB cable
# 6. Observe: reconnecting=False, connected=True, reconnect_attempts resets to 0
```

#### NOT EXECUTED (current machine)

| Field | Value |
|-------|-------|
| **Serial reconnect** | NOT EXECUTED |
| **Reason** | No serial-connected Meshtastic hardware available. |
| **Expected behavior** | Session enters reconnect loop (max 10 attempts, exponential backoff 1–30s). Health transitions: healthy → degraded → healthy on recovery, or healthy → degraded → failed on budget exhaustion. |
| **Diagnostics fields** | `reconnecting`, `reconnect_attempts` in session diagnostics |


### 2.4 Outbound `send_one` Procedure

The MEDRE adapter's `send_one()` method is the primary outbound delivery path. It:
1. Dequeues the next pending outbound from the internal queue
2. Calls the Meshtastic `sendText` API via the session
3. Handles transient failures with up to 3 retries (source: `_MAX_SEND_RETRIES`)
4. Returns `AdapterDeliveryResult` with success/failure state

**Important:** The existing live smoke harness exercises raw `mtjk` `sendText` (Category A) and adapter lifecycle (Category B), but does **not** exercise the full MEDRE `send_one` path against real hardware. The `send_one` path is unit-tested with monkeypatched clients.

#### NOT EXECUTED (current machine)

| Field | Value |
|-------|-------|
| **Outbound send_one** | NOT EXECUTED |
| **Reason** | The live smoke harness does not exercise `send_one` against real hardware. No dedicated `send_one` live test exists. |
| **Gap** | `send_one` is tested via `monkeypatch` in `tests/test_meshtastic_adapter.py`. A live test would require: (1) real node connected, (2) adapter started, (3) `deliver()` called with a real canonical event, (4) confirmation that `sendText` completes via the MEDRE queue path. |


### 2.5 Meshtastic Diagnostics Snapshot Fields

When a Meshtastic adapter is running, `diagnostics()` returns:

**Adapter-level:**
```
adapter_id, platform, started, connection_type,
queue_pending, queue_total_sent, queue_total_failed, queue_total_dropped,
background_tasks
```

**Session-level (nested under `session`):**
```
connected, reconnecting, reconnect_attempts, last_packet_time,
node_id, channel_count, transient_delivery_failures,
permanent_delivery_failures, last_error
```

No secrets, private keys, raw protobuf dumps, or sensitive radio identifiers beyond public fields are exposed.


### 2.6 Meshtastic Hardware Identification Fields

When recording Meshtastic live evidence, capture these hardware fields:

| Field | Example | Source |
|-------|---------|--------|
| Node hardware | LilyGO T-LORA V2.1 | Physical inspection or `meshtastic --info` |
| Firmware version | 2.7.19 | `node.getMetadata()` or `meshtastic --info` |
| Node ID | `!25d6e474` | `diagnostics().session.node_id` |
| Channel index | 0 | Config `MESHTASTIC_CHANNEL_INDEX` |
| Channel name | LONG_FAST | `meshtastic --info` or node config |
| Connection type | serial | `MESHTASTIC_CONNECTION_TYPE` |
| Serial port | `/dev/ttyACM0` | `MESHTASTIC_SERIAL_PORT` |
| mtjk version | 2.7.8.post2+ | `pip show mtjk` |


## 3. Boundedness and Recovery Evidence

### 3.1 Matrix Boundedness

| Resource | Bound | Source |
|----------|-------|--------|
| Sync reconnect attempts | Max 10 consecutive | `_MAX_RECONNECT_ATTEMPTS` in `session.py` |
| Backoff cap | 60 seconds | `_BACKOFF_CAP` in `session.py` |
| Room state tracking | Max 10,000 rooms | `_MAX_ROOM_STATES` in `session.py` |
| Delivery failures (transient/permanent) | Tracked in diagnostics, no hard cap | `diagnostics()` output |

### 3.2 Meshtastic Boundedness

| Resource | Bound | Source |
|----------|-------|--------|
| Reconnect attempts | Max 10 consecutive | `_MAX_RECONNECT_ATTEMPTS` in `session.py` |
| Backoff cap | 30 seconds | `_BACKOFF_CAP` in `session.py` |
| Send retries | Max 3 transient | `_MAX_SEND_RETRIES` in `session.py` |
| Queue depth | Configurable (default 100) | `MeshtasticQueue` constructor |

### 3.3 Recovery Evidence Status

| Transport | Recovery scenario | Observed? | Evidence |
|-----------|------------------|-----------|----------|
| Matrix | Sync failure → reconnect → healthy | Yes (H-tier, 2026-05-10) | Health transitions confirmed in live smoke |
| Matrix | Reconnect budget exhaustion → failed | Yes (H-tier, 2026-05-10) | Budget exhaustion → `failed` confirmed |
| Matrix | Stop → restart → sync re-establishes | Yes (H-tier, 2026-05-10) | Restart idempotency test passed |
| Matrix | E2EE crypto store reuse across restart | Yes (H-tier, 2026-05-10) | `crypto_store_loaded` confirmed on restart |
| Meshtastic | Serial disconnect → reconnect | NOT EXECUTED | No hardware available for disconnect test |
| Meshtastic | Send failure → transient retry | NOT EXECUTED | No hardware for `send_one` live test |
| Meshtastic | Reconnect budget exhaustion → failed | NOT EXECUTED | No hardware available |


## 4. Remaining Risks

### 4.1 Matrix Risks

1. **Long sync replay on restart:** After a restart, nio's `sync_forever` replays missed events. Duration depends on homeserver backlog and room count. Not measured against a real homeserver with significant backlog.
2. **E2EE key material loss:** If the crypto store directory is deleted between restarts, previously encrypted messages become undecryptable. This is an operational concern, not a code bug.
3. **Access token expiry:** No token rotation mechanism. Long-running sessions may fail if the token expires.
4. **Third-party inbound timing:** The 30-second window for the inbound test may be insufficient on slow homeservers or congested networks.

### 4.2 Meshtastic Risks

1. **Serial flakiness:** USB serial connections are susceptible to physical disconnect, USB power management, and kernel driver issues. The reconnect loop mitigates but cannot prevent all serial failures.
2. **Radio delivery is best-effort:** Meshtastic radio transmission is inherently unreliable. Packet delivery depends on antenna, distance, interference, and mesh topology. No delivery guarantee exists at the radio level.
3. **Send_one path unvalidated live:** The full MEDRE outbound queue → `send_one` → `sendText` path has not been exercised against real hardware. Transient retry behavior is unit-tested only.
4. **BLE connectivity untested:** BLE mode is documented but has never been tested against real hardware.
5. **Firmware version sensitivity:** The mtjk library (v2.7.8.post2+) targets specific firmware versions. Behavior may differ on newer or older firmware.


## 5. Cross-References

| Document | Relationship |
|----------|-------------|
| `docs/contracts/61-operational-evidence-contract.md` | Evidence schema and classification |
| `docs/runbooks/operational-evidence.md` | Primary evidence recording location |
| `docs/runbooks/longrun-validation.md` | Extended operation evidence procedures |
| `docs/runbooks/matrix-live-smoke.md` | Matrix live smoke test details |
| `docs/runbooks/meshtastic-live-smoke.md` | Meshtastic live smoke test details |
| `docs/runbooks/matrix-alpha-operation.md` | Full Matrix alpha operation procedures |
| `docs/runbooks/meshtastic-alpha-operation.md` | Full Meshtastic alpha operation procedures |
| `docs/contracts/37-transport-maturity-classification.md` | Transport maturity tiers |
| `docs/contracts/39-operational-risk-register.md` | Risk register |
| `docs/contracts/48-runtime-observability-contract.md` | Diagnostics contract |
