# Live Operational Evidence Runbook

> Last updated: 2026-05-25 (Tranche 6 truth-surface update)
> Tranche 6 session (2026-05-25): **Did NOT execute live hardware/server tests.**
> No Matrix homeserver credentials, no second Matrix account token, no Meshtastic
> physical radio interaction, no MeshCore BLE connection, no LXMF/Reticulum instance
> were provided or available. All live procedure sections remain as previously
> recorded or NOT EXECUTED. This update adds dependency/version capture commands
> (§6A), Docker Synapse second-bot inbound procedure template (§1.5b), and clarifies
> evidence artifact locations (§7A). No statuses were promoted.
> Baseline: HEAD 41a07c7, Python 3.12.3, medre 0.1.0.
> Tracks: 1, 2, 7, 8 (v2 consolidation + hardware probe)
> Status: Procedures documented. Meshtastic serial live validation: **EXECUTED 2026-05-12** (CLI-level: device discovery, hardware/firmware capture, one outbound send on channel 0, 2 reconnect cycles). MEDRE adapter lifecycle and Matrix live tests: **NOT EXECUTED** (2026-05-12: sk.community access token rejected `M_UNKNOWN_TOKEN`; matrix.org password login rejected `M_FORBIDDEN Invalid username/password` — see §1.7). M14 third-party inbound validation attempted 2026-05-12: `matrix.sk.community` homeserver confirmed reachable and healthy, but no `MATRIX_*` env vars are set in the current session. 13 live tests skip cleanly. Test infrastructure (`test_inbound_message_received`) is complete and validates all M14 requirements (sender attribution, room attribution, canonical event shape, source_native_ref, diagnostics). Blocker is purely operational: need valid `MATRIX_ACCESS_TOKEN` for `@forxrelay:sk.community` (password-to-token exchange required). mtjk not in project venv. **Hardware probe (2026-05-12):** CP2104 `/dev/ttyUSB0` (stable by-id, likely T-Beam) — no serial chatter observed; CH9102F `/dev/ttyACM0` (stable by-id, confirmed T-LoRa V2.1-1.6). MeshCore serial path confirmed NOT VIABLE (companion heartbeat protocol). BLE preconditions met, connection NOT ATTEMPTED. RNode KISS probe to ttyUSB0 returned NO RESPONSE. LXMF/Reticulum live path setup pending.
> **Meshtastic queue note:** Per Contract 61 §3.8.3, Meshtastic `queued`/`sent` statuses mean local queue acceptance and local SDK send return, not RF confirmation, remote-node receipt, or ACK. This applies to all Meshtastic maturity evidence in this document.
> Evidence schema: `docs/contracts/61-operational-evidence-contract.md`
> Maturity matrix: `docs/contracts/62-adapter-operational-maturity-matrix.md`
> Primary evidence recording: `docs/runbooks/operational-evidence.md`
> Capability status anchor: `docs/STATUS.md`
> Boundary tests: `tests/test_deployment_boundaries.py`, `tests/test_runtime_deployment_boundaries.py`

This runbook provides detailed live operational procedures for Matrix and Meshtastic transports. Each procedure specifies exact environment variables, expected durations, observations to record, and NOT EXECUTED sections when hardware or credentials are absent. Hardware probe findings inform MeshCore and LXMF follow-up procedures.

**Evidence tier:** All procedures in this document, if executed against real endpoints, produce R-tier (real-live-runtime) evidence per Contract 61. If not executed, fields remain NOT EXECUTED with documented reasons.

**v2 scope (Tracks 1/2/7/8/9):** This revision consolidates start/stop cycle, replay/restart, reconnect, long-running sync/runtime observation, diagnostics snapshot, E2EE store reuse, room-state boundedness, serial reconnect/outbound/degraded behavior, hardware/firmware field capture, actual runtime duration, restart/recovery/boundedness observation, deployment boundary enforcement evidence, and unresolved risks documentation. Every new procedure includes a NOT EXECUTED section for environments without live endpoints. Deployment boundary enforcement is verified by `tests/test_deployment_boundaries.py` and `tests/test_runtime_deployment_boundaries.py`.

**Hardware probe facts incorporated (2026-05-12):** Hardware probe identified two serial devices: CP2104 at `/dev/ttyUSB0` (stable by-id path, likely T-Beam) with no serial chatter observed, and CH9102F at `/dev/ttyACM0` (stable by-id path, confirmed T-LoRa V2.1-1.6 running Meshtastic firmware). Local source repos available at `/home/jeremiah/dev` for LXMF, Reticulum, MeshCore firmware, and MeshCore Python library. `esptool` available via pipx. Docs cleanup resolved stale GPL/license claims. Operational maturity matrix (Contract 62) created. Follow-up validation required for: MeshCore firmware flash attempt on CP2104 device, LXMF/Reticulum live path setup.

## 1. Matrix Live Procedures

### 1.1 Environment Variables

| Variable                 | Required     | Description                            | Example              |
| ------------------------ | ------------ | -------------------------------------- | -------------------- |
| `MATRIX_HOMESERVER`      | Yes          | Full URL of Matrix homeserver          | `https://matrix.org` |
| `MATRIX_USER_ID`         | Yes          | Fully-qualified Matrix user ID         | `@bot:matrix.org`    |
| `MATRIX_ACCESS_TOKEN`    | Yes          | Access token for the bot account       | `syt_xxx...`         |
| `MATRIX_ROOM_ID`         | Yes          | Room ID to send test messages to       | `!abc123:matrix.org` |
| `MATRIX_ENCRYPTION_MODE` | No           | Encryption mode (default: `plaintext`) | `e2ee_required`      |
| `MATRIX_DEVICE_ID`       | E2EE only    | Device ID for crypto store             | `DEVICEABC`          |
| `MATRIX_STORE_PATH`      | E2EE only    | Crypto store directory path            | `/tmp/nio-store`     |
| `MATRIX_INBOUND_SENDER`  | Inbound test | Expected third-party sender MXID       | `@alice:matrix.org`  |

**If any required variable is unset, all live Matrix tests skip with a descriptive message.**

### 1.2 Plaintext Smoke Procedure

**Test file:** `tests/test_matrix_live.py`
**Command:** `pytest tests/test_matrix_live.py -m live -v`
**Expected duration:** 10–30 seconds

#### §1.2 Observations to Record

| Field                 | What to observe                                                        | Where to find it                                |
| --------------------- | ---------------------------------------------------------------------- | ----------------------------------------------- |
| Start/connect         | Adapter starts, `restore_login` succeeds, sync task begins             | Test output: `test_adapter_starts_and_connects` |
| Health → healthy      | `health_check()` returns `health == "healthy"`, `platform == "matrix"` | Test output: `test_health_check_healthy`        |
| Room join             | Room joined successfully                                               | Test output: `test_join_room`                   |
| Outbound send         | `room_send` returns event_id starting with `$`                         | Test output: `test_send_text_message`           |
| Self-echo suppression | Own messages suppressed by sender match                                | Test output: `test_self_message_suppressed`     |
| Stop → clean teardown | `stop()` completes, no leaked tasks                                    | Test output: `test_stop_clean_teardown`         |
| Restart idempotency   | Stop → start cycle re-establishes sync                                 | Test output: `test_restart_idempotent`          |

#### §1.2 NOT EXECUTED (current machine)

| Field              | Value                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Execution date** | NOT EXECUTED (2026-05-12 attempt: 13 tests skipped — no `MATRIX_*` env vars set)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| **Reason**         | No Matrix homeserver credentials configured in the current session. `MATRIX_HOMESERVER`, `MATRIX_USER_ID`, `MATRIX_ACCESS_TOKEN`, and `MATRIX_ROOM_ID` are not set. `matrix.sk.community` homeserver IS reachable and healthy (well-known discovery confirmed, versions endpoint returns v1.12). Previous attempt (2026-05-12) used a stale token that produced `M_UNKNOWN_TOKEN`. The adapter uses `restore_login()` with an access token — password auth is not supported by the adapter directly. A password-to-token exchange via `curl -X POST https://matrix.sk.community/_matrix/client/v3/login` is required to obtain a fresh `MATRIX_ACCESS_TOKEN`. |
| **Resolution**     | 1. Obtain access token: `curl -s -X POST https://matrix.sk.community/_matrix/client/v3/login -d '{"type":"m.login.password","user":"forxrelay","password":"<PASSWORD>"}'`. 2. Set `MATRIX_HOMESERVER="https://matrix.sk.community"`, `MATRIX_USER_ID="@forxrelay:sk.community"`, `MATRIX_ACCESS_TOKEN="<token>"`, `MATRIX_ROOM_ID="<room>"`. 3. Run `pytest tests/test_matrix_live.py -m live -v`. 4. For M14: have a second user send a message to the room during the 30s test window.                                                                                                                                                                      |

### 1.3 Matrix Sync Timing and Diagnostics

Matrix sync is a long-poll operation. The adapter's `_sync_forever` loop maintains a persistent connection to the homeserver. Key timing and diagnostic fields:

| Field                  | Source          | Expected behavior                             |
| ---------------------- | --------------- | --------------------------------------------- |
| `sync_task_running`    | `diagnostics()` | `True` after start, `False` after stop        |
| `sync_running`         | `diagnostics()` | `True` while sync loop is active              |
| `last_successful_sync` | `diagnostics()` | Timestamp of last successful sync response    |
| `reconnecting`         | `diagnostics()` | `True` during reconnect backoff               |
| `reconnect_attempts`   | `diagnostics()` | Count of consecutive failed attempts (max 10) |
| `last_sync_error`      | `diagnostics()` | String of last sync error, or `None`          |
| `connected`            | `diagnostics()` | `True` when nio client is connected           |
| `logged_in`            | `diagnostics()` | `True` after successful `restore_login`       |

**Sync reconnect budget:** Maximum 10 consecutive reconnect attempts (source: `_MAX_RECONNECT_ATTEMPTS` in `session.py`). Exponential backoff: base 1.0s, cap 60.0s, jitter 25% (source: `_BACKOFF_BASE`, `_BACKOFF_CAP`, `_BACKOFF_JITTER_FRACTION`).

**Health transitions during reconnect:**

- Healthy → Degraded: when reconnect begins
- Degraded → Healthy: when sync recovers
- Degraded → Failed: when reconnect budget exhausted

#### §1.3 NOT EXECUTED (current machine)

| Field                        | Value                                            |
| ---------------------------- | ------------------------------------------------ |
| **sync_start_latency_ms**    | NOT EXECUTED — no live session available         |
| **outbound_send_latency_ms** | NOT EXECUTED — no live session available         |
| **reconnect_behavior**       | NOT EXECUTED — no network interruption available |

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

#### §1.4 NOT EXECUTED (current machine)

| Field | Value | (`MATRIX_DEVICE_ID`, `MATRIX_STORE_PATH`) not configured. No encrypted room available. |
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

#### §1.5 NOT EXECUTED (current machine)

| Field                   | Value                                                                                                                                     |
| ----------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| **Third-party inbound** | NOT EXECUTED                                                                                                                              |
| **Reason**              | No second Matrix account configured. Requires manual coordination: a different user must send a message during the 30-second test window. |
| **Blocker**             | (1) Second Matrix account credentials not available in repo. (2) Manual coordination required. (3) No automated sender harness exists.    |

**Deterministic validation status (S-tier):** Unit tests confirm the full inbound pipeline logic (nio sync → `_on_room_message` → codec decode → `publish_inbound()` → canonical event shape → diagnostics counters). See `TestThirdPartyInboundCanonicalEventShape` (8 tests) in `tests/test_matrix_adapter.py`.

### 1.6 Matrix Diagnostics Snapshot Fields

When a Matrix adapter is running, `diagnostics()` returns these fields:

```text
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

### 1.7 Matrix Repeated Start/Stop Cycle Procedure (v2)

Validates that the Matrix adapter can cleanly start and stop multiple times without leaking resources, stale state, or orphaned sync tasks.

**Existing test coverage:**

- `tests/test_matrix_e2ee_live.py::TestLiveE2EEStartStopCycles::test_repeated_start_stop_cycles` — 3 start/stop cycles, verifies `connected`, `sync_running`, `reconnecting == False`, `reconnect_attempts == 0` after each start, `_session is None` after each stop.
- `tests/test_matrix_e2ee_live.py::TestLiveE2EEStartStopCycles::test_disconnect_reconnect` — start → stop (disconnect) → start (reconnect), verifies `connected`, `sync_running`, `crypto_enabled`.

**Manual procedure:**

```bash
# 1. Set core Matrix env vars
export MATRIX_HOMESERVER="https://matrix.example.com"
export MATRIX_USER_ID="@bot:example.com"
export MATRIX_ACCESS_TOKEN="syt_..."
export MATRIX_ROOM_ID="!room:example.com"

# For E2EE mode, also set:
export MATRIX_DEVICE_ID="DEVICEABC"
export MATRIX_STORE_PATH="/tmp/nio-store"

# 2. Run the start/stop cycle tests
pytest tests/test_matrix_e2ee_live.py::TestLiveE2EEStartStopCycles -m live -v

# 3. Observations to record per cycle:
#    - After start:  diagnostics()["connected"] == True
#                    diagnostics()["sync_running"] == True
#                    diagnostics()["sync_task_running"] == True
#                    diagnostics()["reconnecting"] == False
#                    diagnostics()["reconnect_attempts"] == 0
#    - After stop:   adapter._session is None
#                    diagnostics()["connected"] == False
#                    No leaked asyncio tasks
```

**Expected duration:** 15–45 seconds (3 cycles × ~5–15s each)

#### §1.7 Observations to Record

| Field                          | What to observe                                                                  | Source                     |
| ------------------------------ | -------------------------------------------------------------------------------- | -------------------------- |
| Cycle count                    | Number of start/stop cycles completed                                            | Test parameter (default 3) |
| Per-cycle start success        | `connected == True` after each start                                             | `diagnostics()`            |
| Per-cycle stop success         | `_session is None` after each stop                                               | `adapter._session`         |
| State reset between cycles     | `reconnect_attempts == 0`, `reconnecting == False` after each start              | `diagnostics()`            |
| No task leaks                  | No orphaned asyncio tasks after final stop                                       | `asyncio.all_tasks()`      |
| E2EE crypto state preservation | `crypto_enabled` and `crypto_store_loaded` stable across cycles (E2EE mode only) | `diagnostics()`            |

#### §1.7 NOT EXECUTED (current machine)

| Field              | Value                                                                                                                                     |
| ------------------ | ----------------------------------------------------------------------------------------------------------------------------------------- |
| **Execution date** | NOT EXECUTED                                                                                                                              |
| **Reason**         | No Matrix homeserver credentials configured. `MATRIX_HOMESERVER`, `MATRIX_USER_ID`, `MATRIX_ACCESS_TOKEN` not set.                        |
| **Resolution**     | Set required env vars, run `pytest tests/test_matrix_e2ee_live.py::TestLiveE2EEStartStopCycles -m live -v`. Record results with tier `R`. |

**Historical observation (H-tier, 2026-05-10):** E2EE start/stop cycle tests passed 7/7 in 3.73s. `connected`, `sync_running`, `crypto_enabled` all stable across 3 cycles. No task leaks observed.

### 1.8 Matrix Replay/Restart/Recovery Observation Procedure (v2)

Validates that the Matrix adapter correctly handles the sync replay that occurs after a restart, including missed event recovery and diagnostics field accuracy.

**Background:** When the Matrix adapter stops and restarts, nio's `sync_forever` uses the stored sync token to replay events that arrived while offline. The duration and volume of replay depend on:

- Time offline (longer offline → more missed events)
- Room count and activity level
- Homeserver performance

**Existing test coverage:**

- `tests/test_matrix_live.py::TestMatrixLiveSmoke::test_full_lifecycle_start_send_stop` — single start→send→stop cycle.
- `tests/test_matrix_e2ee_live.py::TestLiveE2EERestart` — restart with same store/device, verify connected/crypto state preserved.
- `tests/test_matrix_e2ee_live.py::TestLiveE2EERestart::test_restart_preserves_crypto_state` — verifies `crypto_enabled` and `crypto_store_loaded` identical across restart.
- `tests/test_matrix_e2ee_live.py::TestLiveE2EERestart::test_restart_send_encrypted` — restart → send encrypted message → verify delivery.

**Manual procedure for extended replay observation:**

```bash
# 1. Start adapter, let it run for 60s to accumulate sync state
# 2. Send a message, record event_id
# 3. Stop adapter, wait 30s (simulating offline period)
# 4. During offline, have a second user send messages to the room
# 5. Start adapter again
# 6. Observe: diagnostics()["last_successful_sync"] updates
#             self-echo from previously sent message is suppressed
#             third-party messages from offline period arrive via replay
# 7. Record: replay_duration_ms (time from start to first sync completion)
#            replay_event_count (number of events processed during replay)
#            health transitions during replay
```

**Expected duration:** 2–5 minutes (including 30s offline wait)

#### §1.8 Observations to Record

| Field                               | What to observe                                                   | Source                                       |
| ----------------------------------- | ----------------------------------------------------------------- | -------------------------------------------- |
| Offline duration                    | Seconds between stop and restart                                  | Wall clock                                   |
| Replay duration (ms)                | Time from second `start()` to first `last_successful_sync` update | `diagnostics()` timestamps                   |
| Replay event count                  | Number of events processed during replay                          | `diagnostics()["inbound_published"]` delta   |
| Self-echo suppression during replay | Own pre-stop message not re-published                             | `publish_inbound` mock                       |
| Health during replay                | `healthy` throughout (no transient `degraded`)                    | `health_check()`                             |
| Sync token advance                  | `last_successful_sync` advances after replay                      | `diagnostics()`                              |
| E2EE crypto store reuse             | `crypto_store_loaded == True` on restart                          | `diagnostics()` (E2EE mode)                  |
| Undecryptable events during replay  | Count of events that fail decryption                              | `diagnostics()["undecryptable_event_count"]` |

#### §1.8 NOT EXECUTED (current machine)

| Field                    | Value                                                                                                      |
| ------------------------ | ---------------------------------------------------------------------------------------------------------- |
| **Replay duration**      | NOT EXECUTED — no live session                                                                             |
| **Replay event count**   | NOT EXECUTED — no live session                                                                             |
| **Health during replay** | NOT EXECUTED — no live session                                                                             |
| **Reason**               | No Matrix homeserver credentials. Extended replay observation requires sustained session with offline gap. |
| **Resolution**           | Set env vars, run adapter for 60s, stop for 30s, restart. Record all fields.                               |

**Historical observation (H-tier, 2026-05-10):** Restart idempotency test confirmed: stop→start re-establishes sync, `restore_login` succeeds, health returns `healthy`. Crypto store loads on restart (E2EE mode). Exact replay duration not measured — test infrastructure did not include offline-gap replay scenarios.

### 1.9 Matrix Long-Running Sync Observation Procedure (v2)

Validates sustained Matrix adapter operation over an extended period, observing sync stability, health transitions, diagnostics field drift, and reconnect behavior under real network conditions.

**Existing test infrastructure:**

- `tests/test_soak.py::TestMatrixSoak` — configurable duration soak test (default 30s, max 300s)
- `SOAK_DURATION_SECONDS` env var controls duration

**Manual procedure:**

```bash
# 1. Set core Matrix env vars (see §1.1)
export MATRIX_HOMESERVER="https://matrix.example.com"
export MATRIX_USER_ID="@bot:example.com"
export MATRIX_ACCESS_TOKEN="syt_..."
export MATRIX_ROOM_ID="!room:example.com"

# 2. Run soak test with desired duration
SOAK_DURATION_SECONDS=120 pytest tests/test_soak.py::TestMatrixSoak -m live -v -s

# 3. During execution, observe:
#    - Periodic diagnostics snapshots (every 30s recommended)
#    - Health transitions (should remain "healthy" throughout)
#    - Sync loop stability (no unexpected reconnects)
#    - Memory stability (no unbounded growth)
#    - Inbound/outbound message counts
```

**Expected duration:** User-configured via `SOAK_DURATION_SECONDS` (30–300s). Recommended: 120s for initial observation.

#### §1.9 Observations to Record

| Field                              | What to observe                         | Source                                |
| ---------------------------------- | --------------------------------------- | ------------------------------------- |
| Soak duration (seconds)            | Actual wall-clock duration              | Soak test output                      |
| Messages sent                      | Number of outbound messages during soak | Soak test output                      |
| Messages succeeded                 | Number of successful deliveries         | Soak test output                      |
| Messages failed                    | Number of failed deliveries             | Soak test output                      |
| Reconnect count                    | Number of reconnect events during soak  | `diagnostics()["reconnect_attempts"]` |
| Health throughout                  | Min/max health states observed          | Periodic `health_check()`             |
| Diagnostics snapshot at start      | All fields at t=0                       | `diagnostics()`                       |
| Diagnostics snapshot at end        | All fields at t=duration                | `diagnostics()`                       |
| Max `reconnect_attempts` seen      | Peak reconnect attempt count            | Periodic `diagnostics()`              |
| `undecryptable_event_count` drift  | Count at end minus count at start       | `diagnostics()`                       |
| `last_successful_sync` progression | Timestamp advances throughout           | `diagnostics()`                       |
| Memory usage                       | Process RSS at start and end            | `psutil.Process().memory_info().rss`  |

#### §1.9 NOT EXECUTED (current machine)

| Field              | Value                                                                                                                                   |
| ------------------ | --------------------------------------------------------------------------------------------------------------------------------------- |
| **Execution date** | NOT EXECUTED                                                                                                                            |
| **Reason**         | No Matrix homeserver credentials. Long-running observation requires sustained session.                                                  |
| **Resolution**     | Set env vars, run `SOAK_DURATION_SECONDS=120 pytest tests/test_soak.py::TestMatrixSoak -m live -v -s`. Record all fields with tier `R`. |

**Deterministic validation status (S-tier):** Soak harness infrastructure tested via `tests/test_soak_harness.py` and `tests/test_soak_config_builder.py` (no live endpoints required). Unit tests confirm health transitions, diagnostics stability, and boundedness under mocked conditions.

### 1.10 Matrix Diagnostics Snapshot Capture Procedure (v2)

Defines the procedure for capturing and recording a full `diagnostics()` snapshot from a running Matrix adapter. Snapshots are the primary evidence format for runtime observations.

**Procedure:**

```bash
# 1. Start the adapter (live or simulated)
# 2. At desired observation point, capture:
adapter = MatrixAdapter(config)
await adapter.start(ctx)
snapshot = adapter.diagnostics()

# 3. Record every field from the snapshot:
# connected, logged_in, sync_task_running, last_sync_error,
# store_path_configured, device_id_configured, encryption_mode,
# crypto_enabled, last_crypto_error, encrypted_room_seen,
# undecryptable_event_count, sync_running, reconnecting,
# reconnect_attempts, last_successful_sync, crypto_store_loaded,
# encrypted_room_count, plaintext_room_count,
# transient_delivery_failures, permanent_delivery_failures,
# inbound_published, inbound_suppressed_self,
# inbound_suppressed_envelope, inbound_filtered_allowlist

# 4. At minimum, capture snapshots at:
#    - t=0 (immediately after start)
#    - t=steady (after first successful sync)
#    - t=pre-stop (just before stop)
#    - t=post-stop (after stop)
```

**Expected duration:** Negligible (diagnostics() is synchronous, no network calls)

#### Snapshot Format

```json
{
  "snapshot_timestamp": "2026-05-12T10:30:00Z",
  "snapshot_trigger": "manual | soak-tick | reconnect-event | pre-stop",
  "adapter_id": "matrix-live-smoke",
  "connected": true,
  "logged_in": true,
  "sync_task_running": true,
  "sync_running": true,
  "last_sync_error": null,
  "reconnecting": false,
  "reconnect_attempts": 0,
  "last_successful_sync": "2026-05-12T10:29:58Z",
  "encryption_mode": "plaintext",
  "crypto_enabled": false,
  "crypto_store_loaded": false,
  "encrypted_room_seen": false,
  "undecryptable_event_count": 0,
  "encrypted_room_count": 0,
  "plaintext_room_count": 1,
  "transient_delivery_failures": 0,
  "permanent_delivery_failures": 0,
  "inbound_published": 0,
  "inbound_suppressed_self": 0,
  "inbound_suppressed_envelope": 0,
  "inbound_filtered_allowlist": 0,
  "store_path_configured": false,
  "device_id_configured": false,
  "last_crypto_error": null
}
```

#### §1.10 NOT EXECUTED (current machine)

| Field             | Value                                                                                 |
| ----------------- | ------------------------------------------------------------------------------------- |
| **Live snapshot** | NOT EXECUTED                                                                          |
| **Reason**        | No running Matrix adapter session.                                                    |
| **Resolution**    | Start adapter against live homeserver, capture `diagnostics()` at observation points. |

**Deterministic validation (S-tier):** Unit tests verify all snapshot fields are present and correctly typed in `tests/test_matrix_session.py` and `tests/test_matrix_adapter.py`. No secrets are exposed in any snapshot (Contract 27, Contract 48).

### 1.11 Matrix Room-State Boundedness Observation Procedure (v2)

Validates that the Matrix adapter's internal room-state tracking does not grow unbounded as the adapter observes large numbers of rooms.

**Source constant:** `_MAX_ROOM_STATES = 10_000` in `src/medre/adapters/matrix/session.py`

**Behavior:** When the adapter processes sync events, it tracks room state (encryption status, membership) in an internal dict. If the number of tracked rooms reaches `_MAX_ROOM_STATES`, the oldest room state is evicted to make room for the new one. This prevents unbounded memory growth on accounts that have joined many rooms.

**Deterministic validation (S-tier):**

- `tests/test_matrix_session.py` tests room-state eviction at `_MAX_ROOM_STATES` boundary.
- `tests/test_resource_containment.py` verifies `_MAX_ROOM_STATES == 10_000`.

**Live observation procedure:**

```bash
# 1. Use a Matrix account that has joined 10+ rooms (ideally 100+)
# 2. Start the adapter and observe:
#    - diagnostics()["encrypted_room_count"] + diagnostics()["plaintext_room_count"]
#      should be <= _MAX_ROOM_STATES (10,000)
#    - After running for several sync cycles, verify the counts don't
#      exceed the bound
# 3. If account has >10,000 rooms, observe eviction behavior:
#    - Room count stays at 10,000
#    - Eviction logged at INFO level
```

#### §1.11 NOT EXECUTED (current machine)

| Field                                       | Value                                                                                                            |
| ------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| **Room-state boundedness live observation** | NOT EXECUTED                                                                                                     |
| **Reason**                                  | No Matrix homeserver credentials. Test account room count unknown.                                               |
| **Expected behavior**                       | `encrypted_room_count + plaintext_room_count <= 10,000`. Eviction of oldest room state when cap reached.         |
| **Source**                                  | `_MAX_ROOM_STATES` in `session.py`, S-tier tests in `test_matrix_session.py` and `test_resource_containment.py`. |

### 1.12 Matrix E2EE Store Reuse Observation Procedure (v2)

Validates that the Matrix crypto store is correctly reused across adapter restarts, preserving key material and session state.

**Existing test coverage:**

- `tests/test_matrix_e2ee_live.py::TestLiveE2EERestart::test_restart_same_store_device` — stop → start with same `store_path` and `device_id`, verify `connected == True`.
- `tests/test_matrix_e2ee_live.py::TestLiveE2EERestart::test_restart_preserves_crypto_state` — verifies `crypto_enabled` and `crypto_store_loaded` are identical across restart.
- `tests/test_matrix_e2ee_live.py::TestLiveE2EERestart::test_restart_send_encrypted` — restart → send encrypted message, verify delivery.

**Crypto store location:** `{state_dir}/adapters/{adapter_id}/matrix/store/` (managed by adapter, configured via `MATRIX_STORE_PATH` env var).

**Procedure:**

```bash
# 1. Set E2EE env vars
export MATRIX_HOMESERVER="https://matrix.example.com"
export MATRIX_USER_ID="@bot:example.com"
export MATRIX_ACCESS_TOKEN="syt_..."
export MATRIX_ROOM_ID="!room:example.com"
export MATRIX_DEVICE_ID="DEVICEABC"
export MATRIX_STORE_PATH="/tmp/nio-store-test"

# 2. Run E2EE restart tests
pytest tests/test_matrix_e2ee_live.py::TestLiveE2EERestart -m live -v

# 3. Observations:
#    - First start: crypto_store_loaded == True (store created)
#    - After restart: crypto_store_loaded == True (store reused)
#    - crypto_enabled identical across restarts
#    - Encrypted send works after restart
#    - undecryptable_event_count == 0 (or explain why non-zero)
```

#### §1.12 Observations to Record

| Field                            | What to observe                              | Source             |
| -------------------------------- | -------------------------------------------- | ------------------ |
| Store path                       | Configured `MATRIX_STORE_PATH`               | Config             |
| First start: crypto_store_loaded | `True` after first start                     | `diagnostics()`    |
| Restart: crypto_store_loaded     | `True` after restart (reuses existing store) | `diagnostics()`    |
| crypto_enabled stability         | Same value across restarts                   | `diagnostics()`    |
| Encrypted send post-restart      | Delivery returns `event_id`                  | `deliver()` result |
| undecryptable_event_count        | 0 or explain non-zero                        | `diagnostics()`    |

#### §1.12 NOT EXECUTED (current machine)

| Field                                 | Value                                                                                                                                     |
| ------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| **E2EE store reuse live observation** | NOT EXECUTED                                                                                                                              |
| **Reason**                            | E2EE env vars (`MATRIX_DEVICE_ID`, `MATRIX_STORE_PATH`) not configured. No encrypted room available.                                      |
| **Resolution**                        | Install `pip install -e ".[matrix-e2e]"`, set E2EE env vars, run `pytest tests/test_matrix_e2ee_live.py::TestLiveE2EERestart -m live -v`. |

**Historical observation (H-tier, 2026-05-10):** Crypto store loaded confirmed across restarts. Encrypted send succeeds post-restart. `undecryptable_event_count == 0`.

## 2. Meshtastic Live Procedures

### 2.1 Environment Variables

| Variable                     | Required     | Description                                | Example             |
| ---------------------------- | ------------ | ------------------------------------------ | ------------------- |
| `MESHTASTIC_CONNECTION_TYPE` | Yes          | Connection mode: `tcp`, `serial`, or `ble` | `serial`            |
| `MESHTASTIC_HOST`            | TCP only     | Hostname or IP for TCP connections         | `192.168.1.100`     |
| `MESHTASTIC_PORT`            | TCP optional | Port for TCP (default `4403`)              | `4403`              |
| `MESHTASTIC_SERIAL_PORT`     | Serial only  | Serial device path                         | `/dev/ttyACM0`      |
| `MESHTASTIC_BLE_ADDRESS`     | BLE only     | BLE MAC address                            | `AA:BB:CC:DD:EE:FF` |
| `MESHTASTIC_CHANNEL_INDEX`   | No           | Channel index (default `0`)                | `0`                 |

**If required variables for the chosen connection type are unset, all live Meshtastic tests skip.**

### 2.2 Smoke Procedure (Raw mtjk + MEDRE Adapter)

**Test file:** `tests/test_meshtastic_live.py`
**Command:** `pytest tests/test_meshtastic_live.py -m live -v`
**Expected duration:** 20–60 seconds (includes serial/TCP connection establishment)

**Meshtastic queue local-acceptance note:** Per Contract 61 §3.8.3, `deliver()` returns `delivery_status="enqueued"` (receipt `status="queued"`) when the adapter-local queue accepts the payload. The subsequent `"sent"` receipt means the SDK send returned success. Neither means RF confirmation, remote-node receipt, or ACK. If the process crashes between enqueue and send, evidence correctly shows `queued` with `native_message_id=None`.

#### §2.2 Observations to Record

| Field                      | What to observe                         | Where to find it                           |
| -------------------------- | --------------------------------------- | ------------------------------------------ |
| Connection established     | TCP/Serial interface connects to node   | Test output: Category A tests              |
| `sendText()` → MeshPacket  | Returns packet with populated `id`      | Test output: `test_raw_send_text`          |
| `sendData()` → MeshPacket  | Returns packet with populated `id`      | Test output: `test_raw_send_data`          |
| Pubsub callback fires      | Received packets have expected shape    | Test output: `test_raw_receive_callback`   |
| Outbound packet IDs unique | IDs differ across multiple sends        | Test output: Category A send tests         |
| MEDRE adapter start        | `_create_client()` connects, subscribes | Test output: Category B tests              |
| Health → healthy           | `health_check()` returns `"healthy"`    | Test output: `test_adapter_health_healthy` |
| Stop → clean teardown      | Client closed, unsubscribed             | Test output: `test_adapter_stop_clean`     |

#### Executed 2026-05-12 (CLI-level serial validation)

> **Note:** This was a manual CLI-level validation using `meshtastic` CLI 2.7.8, NOT the MEDRE adapter `pytest` live test suite. The live pytest suite requires `mtjk` in the project venv. See `operational-evidence.md` §2.0 for full R-tier evidence.

| Field                      | Value                                                                                                                                                |
| -------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Execution date**         | 2026-05-12                                                                                                                                           |
| **Executor**               | Manual operator (serial CLI)                                                                                                                         |
| **Connection type**        | Serial, `/dev/ttyACM0` (1a86 USB Single Serial / CDC ACM)                                                                                            |
| **Node hardware**          | LilyGO T-LoRa V2.1.1.6 (TLORA_V2_1_1P6)                                                                                                              |
| **Firmware version**       | 2.7.19.bb3d6d5 (VANILLA)                                                                                                                             |
| **Node ID**                | `!25d6e474`                                                                                                                                          |
| **meshtastic CLI version** | 2.7.8 (platformio penv)                                                                                                                              |
| **pyserial version**       | 3.5                                                                                                                                                  |
| **Connection established** | ✅ All 3 CLI connections succeeded within 15s timeout                                                                                                |
| **sendText on channel 0**  | ✅ CLI completed without error. Output: `Sending text message ... to ^all on channelIndex:0`. No explicit ACK printed.                               |
| **Explicit ACK**           | **Not observed** — meshtastic 2.7.8 CLI does not print ACK for broadcast sends.                                                                      |
| **Second node observed**   | `!ee4a65b1` "Meshtastic 65b1" appeared in node DB (UNSET hardware, SNR -0 dB, 1 hop, channel 0). Presence confirmed, message delivery NOT confirmed. |
| **Reconnect cycles**       | ✅ 2 CLI-level disconnect/reconnect cycles succeeded (independent serial sessions, not MEDRE adapter reconnect).                                     |
| **Duplicate-send caveat**  | Not assessed (only one send performed).                                                                                                              |
| **Destructive operations** | None. No admin, config writes, or firmware changes.                                                                                                  |
| **Full pytest live suite** | **NOT EXECUTED** — requires `mtjk` in project venv. Only CLI-level validation performed.                                                             |

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

#### Partially executed 2026-05-12 (CLI-level)

> **Note:** CLI-level reconnect tested (3 independent serial sessions to `/dev/ttyACM0`, all succeeded). This is NOT the same as MEDRE adapter session reconnect with exponential backoff and health transitions. The MEDRE adapter session reconnect remains untested.

| Field                                 | Value                                                                                                                                                                                          |
| ------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Serial reconnect (CLI-level)**      | ✅ 3 independent serial connections to `/dev/ttyACM0` succeeded with no errors                                                                                                                 |
| **MEDRE adapter session reconnect**   | NOT EXECUTED — requires `mtjk` in project venv and MEDRE adapter running                                                                                                                       |
| **Reason for partial**                | Only CLI-level validation performed. Physical USB disconnect not tested (would interrupt active CLI session).                                                                                  |
| **Expected behavior (MEDRE adapter)** | Session enters reconnect loop (max 10 attempts, exponential backoff 1–30s). Health transitions: healthy → degraded → healthy on recovery, or healthy → degraded → failed on budget exhaustion. |
| **Diagnostics fields**                | `reconnecting`, `reconnect_attempts` in session diagnostics                                                                                                                                    |

### 2.4 Outbound `send_one` Procedure

The MEDRE adapter's `send_one()` method is the primary outbound delivery path. It:

1. Dequeues the next pending outbound from the internal queue
2. Calls the Meshtastic `sendText` API via the session
3. Handles transient failures with up to 3 retries (source: `_MAX_SEND_RETRIES`)
4. Returns `AdapterDeliveryResult` with success/failure state

**Important:** The existing live smoke harness exercises raw `mtjk` `sendText` (Category A) and adapter lifecycle (Category B), but does **not** exercise the full MEDRE `send_one` path against real hardware. The `send_one` path is unit-tested with monkeypatched clients.

#### §2.4 NOT EXECUTED (current session)

> **Note:** CLI-level `sendText` on channel 0 was confirmed working (see §2.2). The MEDRE adapter `send_one` path via the queued delivery pipeline remains untested.

| Field                 | Value                                                                                                                                                                                                                                                                           |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Outbound send_one** | NOT EXECUTED                                                                                                                                                                                                                                                                    |
| **Reason**            | Requires MEDRE adapter running against real hardware with `mtjk` in project venv. CLI-level `sendText` confirmed (§2.2), but `send_one` is a different code path (adapter queue → pacing → real send).                                                                          |
| **Gap**               | `send_one` is tested via `monkeypatch` in `tests/test_meshtastic_adapter.py`. A live test would require: (1) real node connected, (2) adapter started, (3) `deliver()` called with a real canonical event, (4) confirmation that `sendText` completes via the MEDRE queue path. |

### 2.5 Meshtastic Diagnostics Snapshot Fields

When a Meshtastic adapter is running, `diagnostics()` returns:

**Adapter-level:**

```text
adapter_id, platform, started, connection_type,
queue_pending, queue_total_sent, queue_total_failed, queue_total_rejected,
queue_total_requeued, queue_total_exhausted, queue_total_permanent_failed,
queue_send_max_attempts, outbound_mode, outbound_gate_suppressed,
background_tasks
```

> **Queue counter semantics (per Contract 61 §3.8.3, §3.8.6):** `queue_total_sent` counts local SDK send confirmations only, not RF delivery or remote-node receipt. `queue_pending` reflects items in the adapter-local in-memory queue. Both counters reset on process restart. `queue_total_rejected` counts items rejected because the queue was full (not silently evicted). `queued`/`sent` receipt statuses mean local acceptance and local SDK send return, not RF confirmation. When `outbound_mode = "listen_only"` is set, suppressed deliveries are rejected before enqueue and do not increment `queue_total_sent`. See `docs/runbooks/meshtastic-alpha-operation.md` section 9.1a.

**Session-level (nested under `session`):**

```text
connected, reconnecting, reconnect_attempts, last_packet_time,
node_id, channel_count, transient_delivery_failures,
permanent_delivery_failures, last_error
```

No secrets, private keys, raw protobuf dumps, or sensitive radio identifiers beyond public fields are exposed.

### 2.6 Meshtastic Hardware Identification Fields

When recording Meshtastic live evidence, capture these hardware fields:

| Field            | Example            | Source                                      |
| ---------------- | ------------------ | ------------------------------------------- |
| Node hardware    | LilyGO T-LORA V2.1 | Physical inspection or `meshtastic --info`  |
| Firmware version | 2.7.19             | `node.getMetadata()` or `meshtastic --info` |
| Node ID          | `!25d6e474`        | `diagnostics().session.node_id`             |
| Channel index    | 0                  | Config `MESHTASTIC_CHANNEL_INDEX`           |
| Channel name     | LONG_FAST          | `meshtastic --info` or node config          |
| Connection type  | serial             | `MESHTASTIC_CONNECTION_TYPE`                |
| Serial port      | `/dev/ttyACM0`     | `MESHTASTIC_SERIAL_PORT`                    |
| mtjk version     | 2.7.8.post2+       | `pip show mtjk`                             |

### 2.7 Meshtastic Repeated Start/Stop Cycle Procedure (v2)

Validates that the Meshtastic adapter can cleanly start and stop multiple times without leaking serial/TCP connections, orphaned pubsub subscriptions, or stale session state.

**Existing test coverage:**

- `tests/test_meshtastic_live.py::TestMeshtasticLiveSmoke::test_repeated_start_stop_cycle` — 3 cycles of `MeshtasticAdapter(config)` → `start()` → `health_check()` → `stop()`. Each cycle creates a fresh adapter instance.

**Manual procedure:**

```bash
# 1. Set Meshtastic env vars (see §2.1)
export MESHTASTIC_CONNECTION_TYPE="serial"
export MESHTASTIC_SERIAL_PORT="/dev/ttyACM0"

# 2. Run the start/stop cycle test
pytest tests/test_meshtastic_live.py::TestMeshtasticLiveSmoke::test_repeated_start_stop_cycle -m live -v

# 3. Observations to record per cycle:
#    - After start:  health in ("healthy", "unknown")
#                    diagnostics()["session"]["connected"] == True
#    - After stop:   adapter stopped cleanly, no leaked interfaces
#    - Across cycles: no resource leaks, stable connection establishment time
```

**Expected duration:** 30–90 seconds (3 cycles × ~10–30s each, depending on connection type)

#### §2.7 Observations to Record

| Field                            | What to observe                                      | Source                     |
| -------------------------------- | ---------------------------------------------------- | -------------------------- |
| Cycle count                      | Number of start/stop cycles completed                | Test parameter (default 3) |
| Per-cycle health                 | `healthy` or `unknown` after each start              | `health_check()`           |
| Per-cycle connected              | `session.connected == True` after each start         | `diagnostics()`            |
| Resource cleanup                 | No leaked serial/TCP interfaces after each stop      | Process inspection         |
| Connection re-establishment time | Time from `start()` to `connected == True` per cycle | Wall clock                 |

#### §2.7 NOT EXECUTED (current session)

| Field              | Value                                                                                                                                                                                                            |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Execution date** | NOT EXECUTED                                                                                                                                                                                                     |
| **Reason**         | `mtjk` not installed in project venv. MEDRE adapter live tests cannot run.                                                                                                                                       |
| **Resolution**     | Install mtjk (`pip install mtjk`), set `MESHTASTIC_CONNECTION_TYPE` and connection-specific var, run `pytest tests/test_meshtastic_live.py::TestMeshtasticLiveSmoke::test_repeated_start_stop_cycle -m live -v`. |

**Historical observation (H-tier, 2026-05-10):** Single start/stop cycle confirmed in smoke test (10/10 passed, 34.47s). Multi-cycle test added after initial run; not part of historical evidence.

**CLI-level observation (R-tier, 2026-05-12):** 3 independent serial connections to the same device succeeded without errors (see §2.2, §2.3). This demonstrates serial port reliability but is NOT the MEDRE adapter start/stop cycle.

### 2.8 Meshtastic Serial Reconnect/Outbound/Degraded Behavior Procedure (v2)

Observes the Meshtastic session's behavior under degraded conditions: serial disconnect, reconnect loop, outbound send failures, and health transitions.

**Source constants:**

- `_MAX_RECONNECT_ATTEMPTS = 10` in `session.py`
- `_BACKOFF_BASE = 1.0s`, `_BACKOFF_CAP = 30.0s`, `_BACKOFF_JITTER_FRACTION = 0.25`
- `_MAX_SEND_RETRIES = 3` for transient send failures

**Health transitions:**

- `healthy` → `degraded`: when reconnect begins or send fails transiently
- `degraded` → `healthy`: when connection recovers and send succeeds
- `degraded` → `failed`: when reconnect budget exhausted (10 consecutive failures)

**Deterministic validation (S-tier):**

- `tests/test_meshtastic_session.py` — reconnect loop with exponential backoff, budget exhaustion, health transitions.
- `tests/test_meshtastic_adapter.py` — send retry with `_MAX_SEND_RETRIES`, transient/permanent failure classification.
- `tests/test_resource_containment.py` — `_MAX_RECONNECT_ATTEMPTS == 10`, `_MAX_SEND_RETRIES == 3`.

**Manual procedure (serial disconnect):**

```bash
# 1. Connect via serial
export MESHTASTIC_CONNECTION_TYPE="serial"
export MESHTASTIC_SERIAL_PORT="/dev/ttyACM0"

# 2. Start adapter and verify healthy
pytest tests/test_meshtastic_live.py -m live -v -s

# 3. During execution, physically disconnect USB cable
# 4. Observe diagnostics:
#    reconnecting → True
#    reconnect_attempts → 1, 2, 3, ... (up to 10)
#    health → degraded
#    last_error → serial disconnect message
#    Backoff: 1.0s, 2.0s, 4.0s, 8.0s, 16.0s, 30.0s (capped), 30.0s, ...

# 5. Reconnect cable within 10 attempts
# 6. Observe:
#    reconnecting → False
#    reconnect_attempts → 0
#    connected → True
#    health → healthy
```

**Manual procedure (outbound failure):**

```bash
# 1. Connect via TCP to a node
# 2. Start adapter
# 3. Call adapter.deliver() with a message
# 4. During send, simulate network disruption (disable network interface)
# 5. Observe:
#    transient_delivery_failures increments
#    Up to 3 retry attempts with backoff
#    If all 3 fail → permanent_delivery_failures increments
#    health → degraded during retries
```

#### §2.8 Observations to Record

| Field                                | What to observe                                                     | Source           |
| ------------------------------------ | ------------------------------------------------------------------- | ---------------- |
| Disconnect trigger                   | Physical disconnect / network disruption                            | Manual           |
| Reconnect start                      | `reconnecting == True`, `reconnect_attempts == 1`                   | `diagnostics()`  |
| Reconnect attempt sequence           | 1, 2, 3, ..., up to 10                                              | `diagnostics()`  |
| Backoff timing                       | Approximate: 1s, 2s, 4s, 8s, 16s, 30s, ...                          | Wall clock       |
| Health during reconnect              | `degraded`                                                          | `health_check()` |
| Recovery (if reconnected)            | `reconnecting == False`, `connected == True`, `health == "healthy"` | `diagnostics()`  |
| Budget exhaustion (if not recovered) | `reconnect_attempts == 10`, `health == "failed"`                    | `diagnostics()`  |
| Outbound failure count               | `transient_delivery_failures`, `permanent_delivery_failures`        | `diagnostics()`  |
| Send retry count                     | Up to 3 transient retries observed                                  | `diagnostics()`  |

#### §2.8 NOT EXECUTED (current session)

| Field                             | Value                                                                                                                                                                        |
| --------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Serial reconnect observation**  | NOT EXECUTED (MEDRE adapter level). CLI-level reconnect confirmed (3/3 success).                                                                                             |
| **Outbound failure observation**  | NOT EXECUTED                                                                                                                                                                 |
| **Degraded behavior observation** | NOT EXECUTED                                                                                                                                                                 |
| **Reason**                        | Physical disconnect and MEDRE adapter session reconnect not tested. Requires MEDRE adapter running with mtjk in project venv.                                                |
| **Expected behavior**             | Reconnect loop: max 10 attempts, exponential backoff 1–30s, health transitions `healthy → degraded → healthy/failed`. Send retries: max 3 transient, then permanent failure. |
| **Source**                        | S-tier deterministic tests confirm constants and logic. CLI-level R-tier evidence confirms serial port reliability. No MEDRE adapter R-tier evidence.                        |

### 2.9 Meshtastic Long-Running Runtime Observation Procedure (v2)

Validates sustained Meshtastic adapter operation over an extended period, observing connection stability, diagnostics field drift, and pubsub callback reliability.

**Existing test infrastructure:**

- `tests/test_soak.py::TestMeshtasticSoak` — configurable duration soak test
- `SOAK_DURATION_SECONDS` env var controls duration (default 30s, max 300s)

**Manual procedure:**

```bash
# 1. Set Meshtastic env vars (see §2.1)
export MESHTASTIC_CONNECTION_TYPE="serial"
export MESHTASTIC_SERIAL_PORT="/dev/ttyACM0"
export MESHTASTIC_CHANNEL_INDEX="0"

# 2. Run soak test
SOAK_DURATION_SECONDS=120 pytest tests/test_soak.py::TestMeshtasticSoak -m live -v -s

# 3. During execution, observe:
#    - Periodic diagnostics snapshots (every 30s recommended)
#    - Health transitions
#    - Connection stability (no unexpected disconnects)
#    - Inbound/outbound counts
#    - Queue depth (queue_pending)
```

**Expected duration:** User-configured via `SOAK_DURATION_SECONDS` (30–300s). Recommended: 120s for initial observation.

#### §2.9 Observations to Record

| Field                                                              | What to observe                       | Source                    |
| ------------------------------------------------------------------ | ------------------------------------- | ------------------------- |
| Runtime duration (seconds)                                         | Actual wall-clock duration            | Soak test output          |
| Connection type                                                    | serial / tcp                          | Config                    |
| Connection stability                                               | Number of disconnect/reconnect events | `diagnostics()` delta     |
| Messages sent                                                      | Outbound count during runtime         | Soak test output          |
| Messages succeeded                                                 | Successful delivery count             | Soak test output          |
| Messages failed                                                    | Failed delivery count                 | Soak test output          |
| `queue_pending` at end                                             | Pending queue depth at end            | `diagnostics()`           |
| `queue_total_sent` / `queue_total_failed` / `queue_total_rejected` | Cumulative queue stats                | `diagnostics()`           |
| Health throughout                                                  | Min/max health states                 | Periodic `health_check()` |
| `transient_delivery_failures`                                      | Transient failure count               | `diagnostics()`           |
| `permanent_delivery_failures`                                      | Permanent failure count               | `diagnostics()`           |
| `last_packet_time`                                                 | Timestamp of last received packet     | `diagnostics()`           |
| Diagnostics snapshot at start                                      | All session fields at t=0             | `diagnostics()`           |
| Diagnostics snapshot at end                                        | All session fields at t=duration      | `diagnostics()`           |

#### §2.9 NOT EXECUTED (current machine)

| Field              | Value                                                                                                                                                      |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Execution date** | NOT EXECUTED                                                                                                                                               |
| **Reason**         | No Meshtastic radio hardware. Long-running observation requires sustained connection.                                                                      |
| **Resolution**     | Connect radio, set env vars, run `SOAK_DURATION_SECONDS=120 pytest tests/test_soak.py::TestMeshtasticSoak -m live -v -s`. Record all fields with tier `R`. |

### 2.10 Meshtastic Diagnostics Snapshot Capture Procedure (v2)

Defines the procedure for capturing a full `diagnostics()` snapshot from a running Meshtastic adapter.

**Adapter-level snapshot fields:**

```text
adapter_id, platform, started, connection_type,
queue_pending, queue_total_sent, queue_total_failed, queue_total_rejected,
background_tasks
```

**Session-level snapshot fields (nested under `session`):**

```text
connected, reconnecting, reconnect_attempts, last_packet_time,
node_id, channel_count, transient_delivery_failures,
permanent_delivery_failures, last_error
```

**Procedure:**

```bash
# 1. Start the adapter against a real node
# 2. At observation points, capture:
snapshot = adapter.diagnostics()
# 3. Record both adapter-level and session-level fields
# 4. Capture at minimum:
#    t=0 (immediately after start)
#    t=steady (after first packet received)
#    t=pre-stop (just before stop)
#    t=post-stop (after stop — should show started=False, connected=False)
```

#### §2.10 NOT EXECUTED (current machine)

| Field             | Value                                                                        |
| ----------------- | ---------------------------------------------------------------------------- |
| **Live snapshot** | NOT EXECUTED                                                                 |
| **Reason**        | No running Meshtastic adapter session.                                       |
| **Resolution**    | Connect radio, start adapter, capture `diagnostics()` at observation points. |

**Deterministic validation (S-tier):** Unit tests verify all snapshot fields present and correctly typed in `tests/test_meshtastic_adapter.py` and `tests/test_meshtastic_session.py`. No secrets exposed.

### 2.11 Meshtastic Hardware/Firmware Field Capture Procedure (v2)

When recording live evidence, capture these hardware and firmware fields. These are critical for reproducibility and firmware-version-sensitive behavior documentation.

**Fields to capture:**

| Field                         | How to obtain                                                   | Example                                                   |
| ----------------------------- | --------------------------------------------------------------- | --------------------------------------------------------- |
| Node hardware model           | Physical inspection or `meshtastic --info` output `model` field | `LilyGO T-LORA V2.1`, `Heltec v3`, `RAK WisBlock RAK4631` |
| Firmware version              | `node.getMetadata().firmware_version` or `meshtastic --info`    | `2.7.19`                                                  |
| Node ID                       | `diagnostics()["session"]["node_id"]` or `meshtastic --info`    | `!25d6e474`                                               |
| Channel index                 | Config `MESHTASTIC_CHANNEL_INDEX`                               | `0`                                                       |
| Channel name                  | `meshtastic --info` channel listing                             | `LONG_FAST`, `PRIMARY`                                    |
| Connection type               | Config `MESHTASTIC_CONNECTION_TYPE`                             | `serial`, `tcp`, `ble`                                    |
| Serial port (if serial)       | Config `MESHTASTIC_SERIAL_PORT`                                 | `/dev/ttyACM0`                                            |
| Host:port (if TCP)            | Config `MESHTASTIC_HOST:PORT`                                   | `192.168.1.100:4403`                                      |
| mtjk version                  | `pip show mtjk`                                                 | `2.7.8.post2+`                                            |
| Role / hw_model protobuf      | `node.localNode.nodeInfo`                                       | `CLIENT`, `ROUTER`                                        |
| Device metrics (if available) | `diagnostics()` or `meshtastic --info`                          | Battery level, SNR, RSSI                                  |

**Historical capture (H-tier, 2026-05-10):**

- Hardware: LilyGO T-LORA V2.1
- Firmware: 2.7.19
- Node ID: `!25d6e474`
- Channel: 0 (LONG_FAST)
- Connection: serial, `/dev/ttyACM0`
- mtjk: 2.7.8.post2+

#### §2.11 NOT EXECUTED (current machine)

No current hardware connected. When hardware is available, run:

```bash
meshtastic --info 2>&1 | tee hardware-capture-$(date +%Y%m%d).txt
pip show mtjk
```

### 2.12 Meshtastic Actual Runtime Duration and Restart/Recovery Observation (v2)

Documents procedures for recording actual runtime durations and observing restart/recovery behavior under controlled conditions.

**Runtime duration observation:**
The Meshtastic adapter tracks `started` in diagnostics. To record actual runtime:

```bash
# 1. Record wall-clock time at adapter start
# 2. Record wall-clock time at adapter stop
# 3. Duration = stop_time - start_time
# 4. Compare with session diagnostics:
#    diagnostics()["session"]["last_packet_time"] indicates time of last activity
```

**Restart/recovery observation:**

```bash
# 1. Start adapter → healthy
# 2. Record: connection establishment time (start to connected)
# 3. Send a message → record delivery result
# 4. Stop adapter
# 5. Wait 5 seconds
# 6. Start adapter again → verify healthy
# 7. Record: reconnection time (second start to connected)
# 8. Send another message → record delivery result
# 9. Compare: first-start vs second-start connection time
#             first-send vs second-send result
```

**Deterministic validation (S-tier):**

- Connection establishment: tested in `tests/test_meshtastic_session.py` with mocked interfaces.
- Queue state reset: tested in `tests/test_meshtastic_adapter.py`.
- Session state reset: `connected`, `reconnect_attempts`, `reconnecting` reset on `start()`.

#### §2.12 NOT EXECUTED (current machine)

| Field                                | Value                                                         |
| ------------------------------------ | ------------------------------------------------------------- |
| **Runtime duration**                 | NOT EXECUTED — no live session                                |
| **Connection establishment time**    | NOT EXECUTED — no hardware                                    |
| **Reconnection time (second start)** | NOT EXECUTED — no hardware                                    |
| **Reason**                           | No Meshtastic hardware available for live session timing.     |
| **Resolution**                       | Connect hardware, run procedure, record wall-clock durations. |

### 2.13 Hardware Probe Evidence (2026-05-12)

> **Hardware probe findings.** Not live-transport evidence (no MEDRE adapter interaction). Documents physical serial device landscape for follow-up planning.

| Field                            | Value                                                                                                                                       |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| **Probe date**                   | 2026-05-12                                                                                                                                  |
| **Device 1: CP2104**             | `/dev/ttyUSB0`, stable by-id path `Silicon_Labs_CP2104_USB_to_UART_Bridge_Controller_*/if00-port0`                                          |
| **Device 1: Likely hardware**    | T-Beam (CP2104 is typical T-Beam USB-UART bridge)                                                                                           |
| **Device 1: Serial chatter**     | None observed. Device present but no spontaneous serial output at 9600 or 115200 baud. May be unflashed or running non-Meshtastic firmware. |
| **Device 1: esptool**            | `esptool` available via pipx. `esptool chip_id` not yet run — pending follow-up.                                                            |
| **Device 2: CH9102F**            | `/dev/ttyACM0`, stable by-id path `1a86_USB_Serial_5435017226/if00`                                                                         |
| **Device 2: Confirmed hardware** | LilyGO T-LoRa V2.1-1.6 (TLORA_V2_1_1P6), node `!25d6e474`, running Meshtastic firmware 2.7.19.bb3d6d5                                       |
| **Device 2: Status**             | Active Meshtastic node. CLI-level R-tier evidence recorded (§2.2, §2.3).                                                                    |
| **MeshCore firmware source**     | Available at `/home/jeremiah/dev` (local source repo)                                                                                       |
| **MeshCore Python library**      | Available at `/home/jeremiah/dev` (local source repo)                                                                                       |
| **LXMF source**                  | Available at `/home/jeremiah/dev` (local source repo)                                                                                       |
| **Reticulum source**             | Available at `/home/jeremiah/dev` (local source repo)                                                                                       |
| **esptool**                      | Available via pipx (for ESP32 firmware flash operations)                                                                                    |
| **pipx preference**              | User prefers pipx for PyPI CLI tools                                                                                                        |

#### Pending Follow-Up Operations

| Operation                       | Target Device                  | Prerequisite                                             | Status      |
| ------------------------------- | ------------------------------ | -------------------------------------------------------- | ----------- |
| `esptool chip_id` on CP2104     | `/dev/ttyUSB0` (likely T-Beam) | Physical access                                          | **Pending** |
| MeshCore firmware flash attempt | `/dev/ttyUSB0` (likely T-Beam) | Confirm chip type, obtain MeshCore firmware binary       | **Pending** |
| MeshCore live smoke test        | TBD (depends on flash)         | MeshCore firmware running on device                      | **Pending** |
| LXMF/Reticulum live path setup  | N/A (software-only)            | Install Reticulum from local source, configure transport | **Pending** |
| LXMF live smoke test            | N/A (Reticulum instance)       | Running Reticulum instance with identity file            | **Pending** |

### 2.14 MeshCore Live Procedure Placeholder

> **Status:** NOT EXECUTED. Hardware probe identified CP2104 device at `/dev/ttyUSB0` (likely T-Beam). Serial path confirmed NOT VIABLE (companion heartbeat protocol, not MeshCore SDK serial). BLE preconditions met but connection NOT ATTEMPTED. Firmware flash and live validation deferred to follow-up validation.
> **Maturity:** Experimental / SDK-validated, hardware live validation pending per Contract 62 §3.3. Cannot promote hardware path beyond experimental until BLE connection succeeds and appstart returns valid MeshCore instance.

**Test file:** `tests/test_meshcore_live.py`
**Required env vars:** `MESHCORE_CONNECTION_TYPE`, `MESHCORE_HOST` or `MESHCORE_SERIAL_PORT`

#### §2.14 NOT EXECUTED

| Field                     | Value                                                                                                                                                                                                                                                                            |
| ------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Execution date**        | NOT EXECUTED                                                                                                                                                                                                                                                                     |
| **Reason**                | No MeshCore firmware running on CP2104 device. Hardware probe identified `/dev/ttyUSB0` (likely T-Beam) but no serial chatter observed. Device is present but requires firmware flash. MeshCore firmware source available at `/home/jeremiah/dev`. `esptool` available via pipx. |
| **Next validation steps** | (1) Run `esptool chip_id` on `/dev/ttyUSB0` to confirm chip type. (2) Build/obtain MeshCore firmware binary from local source repo. (3) Flash firmware via `esptool`. (4) Verify serial chatter. (5) Run `pytest tests/test_meshcore_live.py -m live -v`.                        |
| **Resolution**            | Execute follow-up hardware operations, then record R-tier evidence here.                                                                                                                                                                                                         |

#### §2.14 Observations to Record

| Field                   | What to observe                                | Source      |
| ----------------------- | ---------------------------------------------- | ----------- |
| Connection established  | TCP/Serial interface connects to MeshCore node | Test output |
| Adapter start → healthy | `health_check()` returns `"healthy"`           | Test output |
| Send text → success     | `deliver()` completes without error            | Test output |
| Diagnostics snapshot    | Non-empty dict, no secrets                     | Test output |
| Stop → clean teardown   | No leaked connections                          | Test output |

### 2.15 LXMF/Reticulum Live Procedure Placeholder

> **Status:** NOT EXECUTED. Local source repos for LXMF and Reticulum available at `/home/jeremiah/dev`. RNode serial path BLOCKED (KISS probe silent at both baud rates). Reticulum live path setup deferred to follow-up validation.
> **Maturity:** Experimental / SDK-validated, Reticulum live validation pending per Contract 62 §3.4. Cannot promote from Experimental until RNode firmware confirmed and at least one live send/receive cycle observed.

**Test file:** `tests/test_lxmf_live.py`
**Required env vars:** `LXMF_CONNECTION_TYPE`, `LXMF_IDENTITY_PATH`

#### §2.15 NOT EXECUTED

| Field                     | Value                                                                                                                                                                                                                                                                                                                                                                                                     |
| ------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Execution date**        | NOT EXECUTED                                                                                                                                                                                                                                                                                                                                                                                              |
| **Reason**                | No Reticulum instance configured. Local source repos for Reticulum and LXMF available at `/home/jeremiah/dev` but not yet installed/configured for live testing.                                                                                                                                                                                                                                          |
| **Next validation steps** | (1) Install Reticulum from local source: `pip install -e /home/jeremiah/dev/rns` (or equivalent). (2) Install LXMF from local source: `pip install -e /home/jeremiah/dev/lxmf` (or equivalent). (3) Configure Reticulum transport (local TCP or serial). (4) Generate/create identity file. (5) Set `LXMF_CONNECTION_TYPE` and `LXMF_IDENTITY_PATH`. (6) Run `pytest tests/test_lxmf_live.py -m live -v`. |
| **Resolution**            | Execute follow-up Reticulum setup, then record R-tier evidence here.                                                                                                                                                                                                                                                                                                                                      |

#### §2.15 Observations to Record

| Field                      | What to observe                                        | Source      |
| -------------------------- | ------------------------------------------------------ | ----------- |
| Connection established     | Reticulum transport initialized                        | Test output |
| Adapter start → healthy    | `health_check()` returns `"healthy"`                   | Test output |
| Send text → success        | `deliver()` completes, delivery state model progresses | Test output |
| Delivery state progression | `OUTBOUND → SENDING → SENT → DELIVERED` observed       | Test output |
| Inbound callback received  | LXMFRouter callback fires                              | Test output |
| Diagnostics snapshot       | Non-empty dict, no secrets                             | Test output |
| Stop → clean teardown      | No leaked Reticulum resources                          | Test output |

### 3.1 Matrix Boundedness

| Resource                                | Bound                               | Source                                    | Deterministic test                                                  |
| --------------------------------------- | ----------------------------------- | ----------------------------------------- | ------------------------------------------------------------------- |
| Sync reconnect attempts                 | Max 10 consecutive                  | `_MAX_RECONNECT_ATTEMPTS` in `session.py` | `test_resource_containment.py`                                      |
| Backoff cap                             | 60 seconds                          | `_BACKOFF_CAP` in `session.py`            | `test_matrix_session.py`                                            |
| Room state tracking                     | Max 10,000 rooms                    | `_MAX_ROOM_STATES` in `session.py`        | `test_matrix_session.py` (eviction), `test_resource_containment.py` |
| Delivery backoff                        | Base 0.5s, exponential              | `_DELIVERY_BACKOFF_BASE` in `adapter.py`  | `test_matrix_adapter.py`                                            |
| Delivery failures (transient/permanent) | Tracked in diagnostics, no hard cap | `diagnostics()` output                    | `test_matrix_adapter.py`                                            |

### 3.2 Meshtastic Boundedness

| Resource           | Bound                      | Source                                    | Deterministic test                                           |
| ------------------ | -------------------------- | ----------------------------------------- | ------------------------------------------------------------ |
| Reconnect attempts | Max 10 consecutive         | `_MAX_RECONNECT_ATTEMPTS` in `session.py` | `test_resource_containment.py`                               |
| Backoff cap        | 30 seconds                 | `_BACKOFF_CAP` in `session.py`            | `test_meshtastic_session.py`                                 |
| Send retries       | Max 3 transient            | `_MAX_SEND_RETRIES` in `session.py`       | `test_resource_containment.py`, `test_meshtastic_session.py` |
| Queue depth        | Configurable (default 100) | `MeshtasticQueue` constructor             | `test_meshtastic_adapter.py`                                 |

### 3.3 Recovery and Restart Evidence Status (v2)

| Transport  | Recovery scenario                      | Observed?                       | Evidence tier  | Procedure  |
| ---------- | -------------------------------------- | ------------------------------- | -------------- | ---------- |
| Matrix     | Sync failure → reconnect → healthy     | Yes                             | H (2026-05-10) | §1.3       |
| Matrix     | Reconnect budget exhaustion → failed   | Yes                             | H (2026-05-10) | §1.3       |
| Matrix     | Stop → restart → sync re-establishes   | Yes                             | H (2026-05-10) | §1.4, §1.8 |
| Matrix     | E2EE crypto store reuse across restart | Yes                             | H (2026-05-10) | §1.12      |
| Matrix     | Repeated start/stop cycles (3x)        | Yes                             | H (2026-05-10) | §1.7       |
| Matrix     | Long sync replay after offline gap     | NOT EXECUTED                    | —              | §1.8       |
| Matrix     | Long-running sync stability (>60s)     | NOT EXECUTED                    | —              | §1.9       |
| Matrix     | Room-state boundedness (10K cap)       | NOT EXECUTED (S-tier confirmed) | S              | §1.11      |
| Meshtastic | Serial disconnect → reconnect          | NOT EXECUTED                    | —              | §2.8       |
| Meshtastic | Send failure → transient retry (3x)    | NOT EXECUTED (S-tier confirmed) | S              | §2.8       |
| Meshtastic | Reconnect budget exhaustion → failed   | NOT EXECUTED (S-tier confirmed) | S              | §2.8       |
| Meshtastic | Repeated start/stop cycles (3x)        | NOT EXECUTED                    | —              | §2.7       |
| Meshtastic | Long-running runtime stability (>60s)  | NOT EXECUTED                    | —              | §2.9       |
| Meshtastic | Outbound degraded behavior             | NOT EXECUTED                    | —              | §2.8       |

## 4. Remaining Risks (v2)

### 4.1 Matrix Risks

1. **Long sync replay on restart:** After a restart, nio's `sync_forever` replays missed events. Duration depends on homeserver backlog and room count. Not measured against a real homeserver with significant backlog. Procedure in §1.8.
2. **E2EE key material loss:** If the crypto store directory is deleted between restarts, previously encrypted messages become undecryptable. This is an operational concern, not a code bug. Procedure in §1.12.
3. **Access token expiry:** No token rotation mechanism. Long-running sessions may fail if the token expires. Long-running observation procedure in §1.9.
4. **Third-party inbound timing:** The 30-second window for the inbound test may be insufficient on slow homeservers or congested networks.
5. **Room-state memory at scale:** The 10,000-room cap has not been tested against an account with that many rooms. Eviction behavior is deterministic-tested only (§1.11).
6. **Repeated start/stop edge cases:** 3-cycle testing passed historically. Larger cycle counts, rapid cycling, or cycling under load have not been tested (§1.7).

### 4.2 Meshtastic Risks

1. **Serial flakiness:** USB serial connections are susceptible to physical disconnect, USB power management, and kernel driver issues. The reconnect loop mitigates but cannot prevent all serial failures. Procedure in §2.8.
2. **Radio delivery is best-effort:** Meshtastic radio transmission is inherently unreliable. Packet delivery depends on antenna, distance, interference, and mesh topology. No delivery guarantee exists at the radio level.
3. **Send_one path unvalidated live:** The full MEDRE outbound queue → `send_one` → `sendText` path has not been exercised against real hardware. Transient retry behavior is unit-tested only. Procedure in §2.8.
4. **BLE connectivity untested:** BLE mode is documented but has never been tested against real hardware.
5. **Firmware version sensitivity:** The mtjk library (v2.7.8.post2+) targets specific firmware versions. Behavior may differ on newer or older firmware. Hardware/firmware capture procedure in §2.11.
6. **Degraded/outbound behavior unobserved:** The `degraded` health state and transient send retry path have not been observed against real hardware. S-tier tests confirm logic; R-tier observation pending. Procedure in §2.8.
7. **Long-running stability unknown:** No sustained Meshtastic session (>60s) has been executed against real hardware. Runtime observation procedure in §2.9.

## 6A. Dependency and Version Capture Commands (Tranche 6)

Before running any live validation, capture the exact dependency and environment
metadata. This ensures evidence is reproducible and traceable.

### 6A.1 Matrix Dependency Capture

```bash
# Project metadata
python3 --version
grep 'version = ' pyproject.toml
git log --oneline -1

# Matrix SDK
pip show mindroom-nio 2>/dev/null || echo "mindroom-nio: NOT INSTALLED"
pip show matrix-nio 2>/dev/null || echo "matrix-nio: NOT INSTALLED"

# E2EE dependencies (if applicable)
pip show vodozemac 2>/dev/null || echo "vodozemac: NOT INSTALLED (required for E2EE)"
pip show peewee 2>/dev/null || echo "peewee: NOT INSTALLED"

# Homeserver connectivity (non-secret)
curl -s https://matrix.example.com/_matrix/client/versions 2>/dev/null | python3 -m json.tool
```

### 6A.2 Meshtastic Dependency Capture

```bash
# Project metadata
python3 --version
grep 'version = ' pyproject.toml
git log --oneline -1

# Meshtastic SDK
pip show mtjk 2>/dev/null || echo "mtjk: NOT INSTALLED"
python3 -c "import meshtastic; print(f'meshtastic import: {meshtastic.__file__}')" 2>/dev/null || echo "meshtastic: NOT IMPORTABLE"

# Serial
pip show pyserial 2>/dev/null || echo "pyserial: NOT INSTALLED"

# Hardware detection
ls -la /dev/ttyACM* /dev/ttyUSB* /dev/serial/by-id/* 2>/dev/null || echo "No serial devices found"
groups | grep -q dialout && echo "dialout: YES" || echo "dialout: NO (serial access may fail)"
```

### 6A.3 MeshCore Dependency Capture

```bash
pip show meshcore-py 2>/dev/null || echo "meshcore-py: NOT INSTALLED"
pip show bleak 2>/dev/null || echo "bleak: NOT INSTALLED (required for BLE)"
ls -la /dev/ttyACM* /dev/ttyUSB* 2>/dev/null || echo "No serial devices found"
```

### 6A.4 LXMF/Reticulum Dependency Capture

```bash
pip show Reticulum 2>/dev/null || echo "Reticulum: NOT INSTALLED"
pip show LXMF 2>/dev/null || echo "LXMF: NOT INSTALLED"
```

## 7A. Evidence Artifact Locations (Tranche 6)

When live evidence is recorded, the following locations store the artifacts:

| Artifact                     | Location                                                    | Format            |
| ---------------------------- | ----------------------------------------------------------- | ----------------- |
| Operational evidence record  | `docs/runbooks/operational-evidence.md`                     | Markdown tables   |
| Live procedure observations  | `docs/runbooks/live-operational-evidence.md`                | Markdown tables   |
| Docker Synapse evidence      | `docs/runbooks/matrix-local-bringup.md` §Live Validation    | Markdown table    |
| Matrix alpha live evidence   | `docs/runbooks/matrix-alpha-operation.md` §Live Validation  | Markdown section  |
| Meshtastic alpha evidence    | `docs/runbooks/meshtastic-alpha-operation.md` §Live Validation | Markdown section |
| Capability status            | `docs/STATUS.md`                                            | Markdown table    |
| Maturity classification      | `docs/contracts/37-transport-maturity-classification.md`    | Markdown tables   |
| Maturity matrix              | `docs/contracts/62-adapter-operational-maturity-matrix.md`  | Markdown tables   |
| Evidence schema              | `docs/contracts/61-operational-evidence-contract.md`        | Markdown contract |
| Longrun evidence             | `docs/runbooks/longrun-validation.md`                       | Markdown tables   |

## 7B. Docker Synapse Second-Bot Inbound Procedure Template (Tranche 6)

This procedure template resolves the M14 third-party inbound blocker using
Docker Synapse (local, no external server required).

**Prerequisites:**

1. Docker Synapse running (see `docs/runbooks/matrix-local-bringup.md`)
2. Bot user registered and access token obtained
3. A second user registered on the same Docker Synapse

**Setup:**

```bash
# 1. Register a second user on local Synapse
docker exec -it medre-synapse register_new_matrix_user \
  -u alice -p alice_password -c /data/homeserver.yaml \
  http://localhost:8008

# 2. Obtain second user's access token
curl -s -X POST http://localhost:8008/_matrix/client/v3/login \
  -H "Content-Type: application/json" \
  -d '{"type":"m.login.password","user":"alice","password":"alice_password"}'

# 3. Have both users join the same room (via Element or API)
# Bot: POST /_matrix/client/v3/join/{room_id_or_alias}
# Alice: POST /_matrix/client/v3/join/{room_id_or_alias}
```

**Execute inbound test:**

```bash
# Set bot env vars
export MATRIX_HOMESERVER=http://localhost:8008
export MATRIX_USER_ID=@bot_user:matrix.local
export MATRIX_ACCESS_TOKEN=syt_<bot_token>
export MATRIX_ROOM_ID=!<room_id>:matrix.local
export MATRIX_LOCAL_SYNAPSE=1
export MATRIX_INBOUND_SENDER=@alice:matrix.local

# Run inbound test (30s window)
pytest tests/test_matrix_live.py::TestMatrixLiveSmoke::test_inbound_message_received -m live -v

# During the 30s window, send a message from Alice's account:
curl -s -X POST "http://localhost:8008/_matrix/client/v3/rooms/${MATRIX_ROOM_ID}/send/m.room.message" \
  -H "Authorization: Bearer syt_<alice_token>" \
  -H "Content-Type: application/json" \
  -d '{"msgtype":"m.text","body":"MEDRE inbound validation test"}'
```

**Status:** NOT EXECUTED in Tranche 6 session. No Docker Synapse instance running,
no second user registered. This procedure template is provided for operator execution.

## 5. Cross-References

| Document                                                   | Relationship                                          |
| ---------------------------------------------------------- | ----------------------------------------------------- |
| `docs/contracts/61-operational-evidence-contract.md`       | Evidence schema and classification                    |
| `docs/runbooks/operational-evidence.md`                    | Primary evidence recording location                   |
| `docs/runbooks/longrun-validation.md`                      | Extended operation evidence procedures                |
| `docs/runbooks/matrix-live-smoke.md`                       | Matrix live smoke test details                        |
| `docs/runbooks/meshtastic-live-smoke.md`                   | Meshtastic live smoke test details                    |
| `docs/runbooks/matrix-alpha-operation.md`                  | Full Matrix alpha operation procedures                |
| `docs/runbooks/meshtastic-alpha-operation.md`              | Full Meshtastic alpha operation procedures            |
| `docs/contracts/37-transport-maturity-classification.md`   | Transport maturity tiers                              |
| `docs/contracts/39-operational-risk-register.md`           | Risk register                                         |
| `docs/contracts/62-adapter-operational-maturity-matrix.md` | Cross-adapter maturity assessment with evidence tiers |
| `docs/contracts/48-runtime-observability-contract.md`      | Diagnostics contract                                  |
| `docs/runbooks/deployment-validation.md`                   | Deployment boundary validation (Track 8/9)            |
| `docs/runbooks/container-operation.md`                     | Container deployment procedures (Track 8/9)           |
| `tests/test_deployment_boundaries.py`                      | Deployment boundary enforcement tests                 |
| `tests/test_runtime_deployment_boundaries.py`              | Runtime-level boundary enforcement tests              |

## 6. Evidence Separation Summary

This document contains the following evidence categories, clearly separated:

| Category                       | Section                                                          | Status                                                                                                              |
| ------------------------------ | ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| **R-tier (hardware)**          | §2.2 (Meshtastic CLI-level serial validation 2026-05-12)         | Meshtastic CLI-level hardware evidence recorded                                                                     |
| **R-tier (Docker SDK-boundary)** | (recorded in matrix-local-bringup.md, not in this document)   | Matrix local Docker Synapse 2026-05-22, 15 passed                                                                   |
| **H-tier (historical)**        | §1.3, §1.7, §1.8, §1.12 (Matrix); §2.2, §2.7, §2.11 (Meshtastic) | Historical evidence from 2026-05-10. May be stale.                                                                  |
| **Hardware probe**             | §2.13                                                            | CP2104/ttyUSB0 (likely T-Beam, no serial chatter), CH9102F/ttyACM0 (confirmed T-LoRa). Not live-transport evidence. |
| **Follow-up placeholders**     | §2.14 (MeshCore), §2.15 (LXMF)                                   | Pending follow-up hardware/Reticulum operations.                                                                    |
| **S-tier (simulated/fake)**    | §1.11, §1.10, §2.10, §3.1, §3.2, §3.3                            | Deterministic unit test coverage confirmed                                                                          |
| **NOT EXECUTED**               | All live procedure NOT EXECUTED sections                         | Live endpoints unavailable or pending follow-up validation                                                          |

**No overclaims:** This document does not claim any transport is production-ready, reliable, or performs at any specific latency. All live procedures are documented as NOT EXECUTED unless explicitly marked with R-tier evidence and an execution date.

## 7. Unresolved Risks (Track 9 Consolidation)

| Risk                                         | Status                                                                                 | Affects            | Mitigation                                                                                                                                           |
| -------------------------------------------- | -------------------------------------------------------------------------------------- | ------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| No current-tranche live evidence             | All historical (H-tier) from 2026-05-10                                                | All transports     | Re-run live procedures at current commit to produce C-tier evidence.                                                                                 |
| MeshCore has no live evidence                | S-tier only; hardware probe identified CP2104 device at `/dev/ttyUSB0` (likely T-Beam) | MeshCore           | Next: flash MeshCore firmware, run live smoke test. Alpha (Tier 2) per Contract 62.                                                                  |
| LXMF has no live evidence                    | S-tier only; local source repos available at `/home/jeremiah/dev`                      | LXMF               | Next: set up Reticulum from local source, configure live path, run live smoke test. Alpha (Tier 2) with experimental downgrade risk per Contract 62. |
| CP2104 device may not be MeshCore-compatible | Unknown chip type, no serial chatter                                                   | MeshCore           | Next: run `esptool chip_id` to confirm. If incompatible, document as hardware gap.                                                                   |
| No soak/longrun evidence                     | NOT EXECUTED                                                                           | All transports     | Run soak procedures with live endpoints. Record evidence per Contract 61 §3.6.                                                                       |
| Container deployment unvalidated             | NOT EXECUTED                                                                           | All transports     | Build container image and run validation per deployment-validation.md.                                                                               |
| Historical evidence may be stale             | H-tier from 2026-05-10                                                                 | Matrix, Meshtastic | Adapter code may have changed since recording. Re-run live tests at current commit to confirm.                                                       |
| No third-party inbound evidence              | NOT EXECUTED                                                                           | Matrix             | Requires second Matrix account and manual coordination.                                                                                              |
| No multi-node inbound evidence               | NOT EXECUTED                                                                           | Meshtastic         | Requires second Meshtastic node on same channel.                                                                                                     |
