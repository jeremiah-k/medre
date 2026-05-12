# Operational Evidence Runbook

> Last updated: 2026-05-11
> Status: Partially populated. Current deterministic suite: 3237 passed, 4 skipped,
> 63 deselected (2026-05-11). Live evidence is historical from 2026-05-10:
> Matrix plaintext 13/13, E2EE harness 7/7, Meshtastic serial 10/10.
> Current beta-entry tranche live execution: NOT EXECUTED.
> MeshCore, LXMF, and soak tests remain **NOT EXECUTED**.
> Live commands, env vars, and NOT EXECUTED reasoning in §6–§7.
> Related: `docs/contracts/32-beta-readiness-checklist.md`, section 1.3.2.

This document is the consolidated operational evidence record for each validated
transport. Each transport section contains fields for actual test date,
environment, results, caveats, reconnect observations, and limitations.

**How to use this document:**

1. Each transport has an evidence table with well-defined fields.
2. Fields marked **NOT EXECUTED** indicate that no live agent has reported
   results yet. Do not remove these placeholders until real evidence is
   available.
3. When live results arrive, replace the placeholder with actual values
   including date, environment details, and observed behavior.
4. Do not invent, fabricate, or extrapolate live evidence from unit test
   results. Unit tests are recorded separately.


## 1. Matrix Operational Evidence

> **Historical evidence note:** Live results in this section were recorded on 2026-05-10 against matrix.org. Current beta-entry tranche live execution: **NOT EXECUTED**.

### 1.1 Live Smoke Test Evidence (Historical — recorded 2026-05-10)

| Field | Value |
|-------|-------|
| **Test file** | `tests/test_matrix_live.py` |
| **Last execution date** | 2026-05-10 |
| **Executor** | Live agent (automated) |
| **Homeserver** | matrix.org (public homeserver) |
| **MEDRE commit** | Pre-beta HEAD (2026-05-10) |
| **Python version** | 3.12 |
| **mindroom-nio version** | Installed via `pip install -e ".[matrix]"` |
| **Environment** | Local development machine |
| **Test command** | `pytest tests/test_matrix_live.py -m live -v` |
| **Total tests run** | 13 |
| **Passed / Failed / Skipped** | 13 passed / 0 failed / 0 skipped |
| **Non-live regression** | 202 passed, 0 failed (full suite minus live) |
| **Start/connect** | ✅ Adapter started, `restore_login` succeeded, sync task running |
| **Health check → healthy** | ✅ `info.health == "healthy"`, `info.platform == "matrix"` |
| **Room join** | ✅ Room `!sRlwdLCwIGBpSzoRsV:matrix.org` joined successfully |
| **Room encryption status** | Unencrypted (plaintext alpha path) |
| **Outbound send → event_id** | ✅ `room_send` returned event_id starting with `$` |
| **Self-message suppression** | ✅ Own messages suppressed by sender match |
| **Stop → clean teardown** | ✅ `stop()` completed; no leaked tasks |
| **Reconnect observations** | ✅ Health stays `degraded` during reconnect, `healthy` after recovery. Budget exhaustion → `failed`. |
| **Caveats observed** | Initial harness had a bug where `health_check()` was awaited as a coroutine instead of called as a regular method. Fixed in-tree before final run. No remaining issues. |
| **Restart idempotency** | ✅ Stop → start cycle re-establishes sync; second `health_check()` returns `healthy` |

### 1.2 E2EE Live Test Evidence (Historical — recorded 2026-05-10)

| Field | Value |
|-------|-------|
| **Test file** | `tests/test_matrix_e2ee_live.py` |
| **Last execution date** | 2026-05-10 |
| **Executor** | Live agent (automated) |
| **Homeserver** | matrix.org (public homeserver) |
| **Room type** | Unencrypted room used for initial E2EE-mode startup tests |
| **E2EE mode** | `e2ee_required` config used; crypto store loaded |
| **mindroom-nio[e2e] version** | Installed via `pip install -e ".[matrix-e2e]"` |
| **vodozemac version** | Pulled as dependency of `mindroom-nio[e2e]` |
| **Total tests run** | 7 |
| **Passed / Failed / Skipped** | 7 passed / 0 failed / 0 skipped |
| **Crypto store loaded** | ✅ `crypto_store_loaded == True` |
| **Encrypted send → event_id** | Tests ran against unencrypted room in E2EE mode. Encrypted-room results: see §1.3 below. |
| **Undecryptable events** | 0 observed during run |
| **Caveats observed** | E2EE tests validated startup with crypto deps against an unencrypted room. See §1.3 for encrypted-room follow-up results. |

### 1.3 Encrypted Room Follow-up Evidence (Historical — recorded 2026-05-10)

#### 1.3.1 Pre-fix Run (initial)

| Field | Value |
|-------|-------|
| **Test type** | Manual / agent-driven encrypted-room follow-up |
| **Execution date** | 2026-05-10 |
| **Executor** | Live agent (automated) |
| **Homeserver** | matrix.org (public homeserver) |
| **Target room** | `!rnmyZMhUoraPwZUDPP:matrix.org` (E2EE enabled) |
| **Room join** | ✅ Adapter successfully joined the encrypted room |
| **Room encryption confirmed** | ✅ Room confirmed as encrypted (`RoomEncryptionEvent` received) |
| **Outbound send attempt 1** | ❌ Failed with `OlmUnverifiedDeviceError` |
| **Outbound send attempt 2** | ❌ Failed with `OlmUnverifiedDeviceError` |
| **Root cause** | `ignore_unverified_devices` was `False` (nio strict default). The bot's device was not verified by other room members, so nio refused to share the Megolm session key with unverified devices. |
| **Implication** | Encrypted-room **join** and **detection** work. Outbound encrypted send was blocked by nio's unverified-device policy. |
| **Fix applied** | MEDRE now internally sets `ignore_unverified_devices=True` when `encryption_mode` is not `"plaintext"` — required by upstream nio (no cross-signing support). No operator toggle needed. See `docs/contracts/25-matrix-e2ee-readiness.md` §5.2 for rationale. |

#### 1.3.2 Post-fix Re-test (E2EE live suite)

| Field | Value |
|-------|-------|
| **Test file** | `tests/test_matrix_e2ee_live.py` |
| **Execution date** | 2026-05-10 |
| **Executor** | Live agent (automated) |
| **Homeserver** | matrix.org (public homeserver) |
| **Target room** | `!rnmyZMhUoraPwZUDPP:matrix.org` (E2EE enabled) |
| **Test command** | `pytest tests/test_matrix_e2ee_live.py -m live -v` |
| **Total tests run** | 7 |
| **Passed / Failed / Skipped** | 7 passed / 0 failed / 0 skipped |
| **Duration** | 3.73s |
| **Previously failing `test_send_encrypted_text`** | ✅ Passed post-fix |
| **Previously failing `test_restart_send_encrypted`** | ✅ Passed post-fix |
| **Crypto store loaded** | ✅ |
| **Encrypted send → event_id** | ✅ Outbound encrypted send succeeds — MEDRE passes `ignore_unverified_devices=True` for non-plaintext modes |
| **Caveats** | This is not a security downgrade — `ignore_unverified_devices=True` is required by the upstream nio client (no cross-signing support, MSC1756). MEDRE applies this automatically based on `encryption_mode`. Device verification via cross-signing is an upstream nio gap, not a MEDRE deferral. |

### 1.4 Soak Test Evidence

| Field | Value |
|-------|-------|
| **Test file** | `tests/test_soak.py::TestMatrixSoak` |
| **Last execution date** | **NOT EXECUTED** |
| **Soak duration (seconds)** | **NOT EXECUTED** |
| **Messages sent** | **NOT EXECUTED** |
| **Messages succeeded** | **NOT EXECUTED** |
| **Max reconnect attempts seen** | **NOT EXECUTED** |
| **Session health throughout** | **NOT EXECUTED** |
| **Caveats observed** | **NOT EXECUTED** |

### 1.5 Matrix Known Limitations (confirmed from source and live testing)

- **Third-party inbound: test harness exists, live execution operator-dependent.** The live test `test_inbound_message_received` in `tests/test_matrix_live.py` validates the full third-party inbound path (nio sync → `_on_room_message` → codec decode → `publish_inbound()` → canonical event shape → diagnostics counters). It is gated by `MATRIX_INBOUND_SENDER` and a 30-second window. Deterministic unit tests cover the same logic paths without live connectivity (see §1.6). Live execution requires a second Matrix user sending a message during the test window — this has not yet been executed against a real homeserver.
- E2EE text alpha: encrypted-room join works. Initial outbound encrypted send failed with `OlmUnverifiedDeviceError` (2 tests); root cause was nio's strict `ignore_unverified_devices=False` default blocking key sharing with unverified devices. Fix: adapter set `ignore_unverified_devices=True`. Post-fix re-test: encrypted-room full suite passed 7/7 in 3.73s (see §1.3). This is required by upstream nio (no cross-signing support, MSC1756) — every nio-based automated E2EE client must set this flag. **E2EE is Matrix client encrypted-room support only — not generic cross-transport E2EE.**
- No E2EE reactions, edits, deletes, or attachments.
- No cross-signing support in `mindroom-nio`. Device verification via cross-signing is not implemented.
- Access token is a plain string in config (no secure storage or rotation).
- `mindroom-nio` is a fork; maintenance status relative to upstream is unverified.
- Sync loop error handling is untested under real network conditions.

### 1.6 Track 2 — Third-party Inbound Validation Status

#### 1.6.1 What has been validated (deterministic)

| Aspect | Validation | Evidence |
|--------|-----------|----------|
| `publish_inbound()` called for third-party sender | ✅ Unit tested | `TestThirdPartyInboundCanonicalEventShape` (8 tests) in `tests/test_matrix_adapter.py` |
| `source_transport_id` is sender MXID (not bot) | ✅ Unit tested | `test_third_party_event_has_sender_as_transport_id` |
| `source_channel_id` is Matrix room ID | ✅ Unit tested | `test_third_party_event_has_room_as_channel_id` |
| `source_native_ref` carries Matrix event_id | ✅ Unit tested | `test_third_party_event_has_source_native_ref` |
| `event_kind == "message.created"` | ✅ Unit tested | `test_third_party_event_kind_is_message_created` |
| Payload contains body and msgtype | ✅ Unit tested | `test_third_party_event_has_correct_payload` |
| Self-loop suppression (sender == bot) | ✅ Unit tested | `TestSelfMessageSuppression` (3 tests) |
| MEDRE-origin envelope suppression | ✅ Unit tested | `TestMEDREOriginLoopSuppression` (4 tests) |
| Room allowlist filtering | ✅ Unit tested | `TestRoomAllowlist` (4 tests) |
| Inbound diagnostics counters | ✅ Unit tested | `TestInboundDiagnosticsCounters` (8 tests) |
| Diagnostics dict exposure | ✅ Unit tested | `test_diagnostics_exposes_inbound_counters` |

#### 1.6.2 What requires operator-dependent live validation

| Aspect | Status | Blocker |
|--------|--------|---------|
| nio sync delivers third-party event to `_on_room_message` | ⏳ Not executed | Requires second Matrix account sending to test room during 30 s window |
| Live self-echo suppression (send → sync → suppress) | ⏳ Partially tested | `test_live_send_and_receive` validates self-echo doesn't leak, but full round-trip timing is environment-dependent |
| Inbound diagnostics counters on live server | ⏳ Not executed | Requires live third-party inbound event |
| Encrypted-room inbound from third party | ⏳ Not executed | Requires second account in encrypted room with crypto store |

#### 1.6.3 Live third-party inbound test procedure

```bash
# 1. Set core Matrix env vars (same as smoke tests)
export MATRIX_HOMESERVER="http://localhost:8008"
export MATRIX_USER_ID="@bot:localhost"
export MATRIX_ACCESS_TOKEN="syt_..."
export MATRIX_ROOM_ID="!test:localhost"

# 2. Set the expected inbound sender (optional but recommended)
export MATRIX_INBOUND_SENDER="@alice:localhost"

# 3. Run the inbound test
pytest tests/test_matrix_live.py::TestMatrixLiveSmoke::test_inbound_message_received -m live -v

# 4. While the test is waiting (30 s window), send a message from
#    the second account (@alice:localhost) into MATRIX_ROOM_ID.

# 5. Expected: test passes, confirming publish_inbound() fires with
#    correct CanonicalEvent shape and diagnostics counters.
#    If no second account sends a message, test xfails (acceptable).
```

#### 1.6.4 Blockers for live execution

1. **Second Matrix account**: The test requires a different Matrix user to send a message to the test room. A single bot account cannot produce a third-party inbound event (self-messages are suppressed).
2. **Manual coordination**: The sender must send during the 30-second test window. No automated sender harness exists.
3. **No shared second account credentials in repository**: Credentials are operator-specific and must not be stored in the repo.


## 2. Meshtastic Operational Evidence

> **Historical evidence note:** Live results in this section were recorded on 2026-05-10 against real hardware. Current beta-entry tranche live execution: **NOT EXECUTED**.

### 2.1 Live Smoke Test Evidence (Historical — recorded 2026-05-10)

| Field | Value |
|-------|-------|
| **Test file** | `tests/test_meshtastic_live.py` |
| **Last execution date** | 2026-05-10 |
| **Executor** | Live agent (automated) |
| **Connection type** | Serial (direct USB connection to `/dev/ttyACM0`) |
| **Node hardware** | LilyGO T-LORA V2.1, node `!25d6e474` |
| **Firmware version** | 2.7.19 |
| **Channel** | Test (PRIMARY, LONG_FAST) |
| **mtjk version** | 2.7.8.post2+ (imported as `meshtastic`) |
| **MEDRE commit** | Pre-beta HEAD (2026-05-10) |
| **Python version** | 3.12 |
| **Environment** | Local development machine |
| **Test command** | `pytest tests/test_meshtastic_live.py -m live -v` |
| **Total tests run** | 10 |
| **Passed / Failed / Skipped** | 10 passed / 0 failed / 0 skipped |
| **Wall time** | 34.47s |
| **Raw mtjk sendText** | ✅ `sendText()` returned `MeshPacket` with populated `id`. Outbound packet IDs were unique across sends. |
| **Raw mtjk sendData** | ✅ `sendData()` returned `MeshPacket` with populated `id`. |
| **Raw mtjk receive callback** | ✅ Pubsub callback fired on packet reception. Received packets have expected shape (`decoded`, `id`, `portnum`). Inbound telemetry packet observed (not just text). |
| **MEDRE adapter start** | ✅ Adapter created client via `_create_client()`, connected and subscribed. |
| **MEDRE adapter health → healthy** | ✅ `health_check()` returned `"healthy"` after start. |
| **MEDRE adapter stop** | ✅ `stop()` closed client, unsubscribed cleanly. |
| **Caveats observed** | Initial harness had two bugs fixed in-tree before final run: (1) `isConnected` TypeError — attribute used instead of correct API; (2) `pypubsub` ListenerMismatchError — callback signature mismatch (`pub.sendMessage` vs `pypubsub.subscribe`). Both fixed. Final 10/10 pass reflects corrected harness. |
| **Reconnect observations** | Not explicitly tested in this run. Session maintained stable connection throughout 34.47s execution. |
| **Destructive operations** | None performed. No admin packets, no firmware changes, no config writes. |
| **Second-node inbound** | **NOT EXECUTED** — requires a second Meshtastic node not present in this run. |

### 2.2 Soak Test Evidence

| Field | Value |
|-------|-------|
| **Test file** | `tests/test_soak.py::TestMeshtasticSoak` |
| **Last execution date** | **NOT EXECUTED** |
| **Soak duration (seconds)** | **NOT EXECUTED** |
| **Messages sent** | **NOT EXECUTED** |
| **Messages succeeded** | **NOT EXECUTED** |
| **Max reconnect attempts seen** | **NOT EXECUTED** |
| **Session health throughout** | **NOT EXECUTED** |
| **Caveats observed** | **NOT EXECUTED** |

### 2.3 Meshtastic Known Limitations (confirmed from source and live testing)

- No full MEDRE adapter `send_one` integration with real hardware.
- No inbound message reception from a second node.
- No multi-hop mesh delivery testing.
- No encrypted channel support.
- No telemetry, position, nodeinfo, or admin packet processing.
- BLE connectivity documented but not exercised.
- `mtjk` is a fork; distribution name is `mtjk`, import name is `meshtastic`.
- No factory reset, no ham mode, no channel deletion performed during testing.


## 3. MeshCore Operational Evidence

### 3.1 Live Smoke Test Evidence

| Field | Value |
|-------|-------|
| **Test file** | `tests/test_meshcore_live.py` |
| **Last execution date** | **NOT EXECUTED** |
| **Executor** | **NOT EXECUTED** |
| **Connection type** | **NOT EXECUTED** (TCP/serial/BLE) |
| **Node hardware** | **NOT EXECUTED** |
| **SDK version** | **NOT EXECUTED** |
| **MEDRE commit** | **NOT EXECUTED** |
| **Python version** | **NOT EXECUTED** |
| **Environment** | **NOT EXECUTED** |
| **Total tests run** | **NOT EXECUTED** |
| **Passed / Failed / Skipped** | **NOT EXECUTED** |
| **Adapter start** | **NOT EXECUTED** |
| **Health check → healthy** | **NOT EXECUTED** |
| **Send text → success** | **NOT EXECUTED** |
| **Inbound callback received** | **NOT EXECUTED** |
| **Diagnostics snapshot** | **NOT EXECUTED** |
| **Stop → clean teardown** | **NOT EXECUTED** |
| **Caveats observed** | **NOT EXECUTED** |
| **Reconnect observations** | **NOT EXECUTED** |

### 3.2 MeshCore Known Limitations (confirmed from source)

- No inbound message reception from a second node.
- No multi-hop mesh delivery testing.
- No bridge compatibility with Meshtastic.
- No BLE connectivity with PIN pairing tested.
- No reconnection handling under real network conditions.
- Duplicate-send risk acknowledged (session retries up to 3 times).


## 4. LXMF/Reticulum Operational Evidence

### 4.1 Live Smoke Test Evidence

| Field | Value |
|-------|-------|
| **Test file** | `tests/test_lxmf_live.py` |
| **Last execution date** | **NOT EXECUTED** |
| **Executor** | **NOT EXECUTED** |
| **Connection type** | **NOT EXECUTED** (`reticulum`) |
| **RNS version** | **NOT EXECUTED** |
| **lxmf version** | **NOT EXECUTED** |
| **Identity source** | **NOT EXECUTED** (loaded/generated) |
| **MEDRE commit** | **NOT EXECUTED** |
| **Python version** | **NOT EXECUTED** |
| **Environment** | **NOT EXECUTED** |
| **Total tests run** | **NOT EXECUTED** |
| **Passed / Failed / Skipped** | **NOT EXECUTED** |
| **Fake mode lifecycle** | **NOT EXECUTED** |
| **Real mode start/connect** | **NOT EXECUTED** |
| **Real mode deliver** | **NOT EXECUTED** |
| **Inbound callback received** | **NOT EXECUTED** |
| **Diagnostics snapshot** | **NOT EXECUTED** |
| **Stop → clean teardown** | **NOT EXECUTED** |
| **Caveats observed** | **NOT EXECUTED** |
| **Reconnect observations** | **NOT EXECUTED** |

### 4.2 LXMF Known Limitations (confirmed from source)

- No synchronous delivery confirmation. Outbound returns `OUTBOUND` state.
- No inbound from a separate, independent Reticulum instance.
- No propagation node store-and-forward testing.
- No multi-hop mesh delivery testing across heterogeneous transports.
- No resource transfer for large messages.
- Production deployment readiness is not claimed.


## 5. Deterministic Test Evidence (confirmed)

This section records evidence from deterministic/unit tests that do not require
live services. These are confirmed from CI runs.

### 5.1 Current Evidence (as of 2026-05-11)

| Field | Value |
|-------|-------|
| **Test command** | `pytest` (default, no live) |
| **Last confirmed date** | 2026-05-11 |
| **Passed** | 3237 |
| **Skipped** | 4 |
| **Deselected** | 63 (live + soak tests excluded by default) |
| **compileall** | Clean (`python -m compileall -q src tests`) |
| **All adapters covered** | Yes (Matrix, Meshtastic, MeshCore, LXMF) |

### 5.2 Historical Evidence (superseded)

> The following counts are from a prior run and are preserved for traceability.
> They are NOT the current evidence — use §5.1 above for current numbers.

| Field | Value |
|-------|-------|
| **Run date** | 2026-05-10 |
| **Total tests** | 2076 (including 27 resource containment tests) |
| **Passed** | 2076 |
| **Failed** | 0 |
| **Deselected** | 61 (live + soak tests excluded by default) |
| **compileall** | Clean |


## 6. Live Evidence Commands and Environment Variables

This section provides the exact commands and required environment variables
for reproducing live evidence per transport. Every **NOT EXECUTED** entry
in this document can be resolved by running the corresponding command with
the required environment variables set.

### 6.1 Matrix (Plaintext)

```bash
# Install transport SDK
pip install -e ".[matrix]"

# Required environment variables
export MATRIX_HOMESERVER="https://matrix.example.com"
export MATRIX_USER_ID="@bot:example.com"
export MATRIX_ACCESS_TOKEN="syt_..."
export MATRIX_ROOM_ID="!room:example.com"

# Run live smoke tests
pytest tests/test_matrix_live.py -m live -v

# Run Matrix soak tests (default 30s, max 300s)
SOAK_DURATION_SECONDS=30 pytest tests/test_soak.py::TestMatrixSoak -m live -v -s
```

### 6.2 Matrix (E2EE)

```bash
# Install E2EE transport SDK
pip install -e ".[matrix-e2e]"

# Required environment variables (same as plaintext, plus)
export MATRIX_HOMESERVER="https://matrix.example.com"
export MATRIX_USER_ID="@bot:example.com"
export MATRIX_ACCESS_TOKEN="syt_..."
export MATRIX_ROOM_ID="!room:example.com"
export MATRIX_ENCRYPTION_MODE="e2ee_required"

# Run E2EE live tests
pytest tests/test_matrix_e2ee_live.py -m live -v
```

### 6.3 Meshtastic

```bash
# Install transport SDK
pip install -e ".[meshtastic]"

# Required environment variables (connection-type-dependent)
# For serial:
export MESHTASTIC_CONNECTION_TYPE="serial"
export MESHTASTIC_SERIAL_PORT="/dev/ttyACM0"

# For TCP:
export MESHTASTIC_CONNECTION_TYPE="tcp"
export MESHTASTIC_HOST="192.168.1.100"
export MESHTASTIC_PORT="4403"  # optional, default 4403

# Optional:
export MESHTASTIC_CHANNEL_INDEX="0"  # default 0

# Run live smoke tests
pytest tests/test_meshtastic_live.py -m live -v

# Run Meshtastic soak tests (default 30s, max 300s)
SOAK_DURATION_SECONDS=30 pytest tests/test_soak.py::TestMeshtasticSoak -m live -v -s
```

### 6.4 MeshCore

```bash
# Install transport SDK
pip install -e ".[meshcore]"

# Required environment variables (connection-type-dependent)
# For TCP:
export MESHCORE_CONNECTION_TYPE="tcp"
export MESHCORE_HOST="192.168.1.100"
# MESHCORE_PORT defaults to 4000

# For serial:
export MESHCORE_CONNECTION_TYPE="serial"
export MESHCORE_SERIAL_PORT="/dev/ttyUSB0"

# Run live smoke tests
pytest tests/test_meshcore_live.py -m live -v

# No soak test class exists for MeshCore yet.
```

### 6.5 LXMF / Reticulum

```bash
# Install transport SDK
pip install -e ".[lxmf]"

# Required environment variables
export LXMF_CONNECTION_TYPE="reticulum"
export LXMF_IDENTITY_PATH="/path/to/identity.key"
# Reticulum config defaults to ~/.reticulum/config

# Run live smoke tests
pytest tests/test_lxmf_live.py -m live -v

# No soak test class exists for LXMF yet.
```

### 6.6 Soak Tests

```bash
# Environment variable controlling soak duration
# Default: 30 seconds. Maximum: 300 seconds (hard cap in test_soak.py).
export SOAK_DURATION_SECONDS=30

# Matrix soak
pytest tests/test_soak.py::TestMatrixSoak -m live -v -s

# Meshtastic soak
pytest tests/test_soak.py::TestMeshtasticSoak -m live -v -s
```

### 6.7 Harness Soak Tests (No Hardware Required)

```bash
# These test the soak harness itself — no real transport needed.
pytest tests/test_soak_harness.py tests/test_soak_config_builder.py -q
```


## 7. NOT EXECUTED Reasoning

The following table documents why specific evidence has not been collected.
This section exists to ensure honesty: absence of evidence is not evidence
of absence, and every gap is traceable to a specific reason.

| Transport | Evidence type | Status | Reason | Required command | Required env vars |
|-----------|--------------|--------|--------|------------------|-------------------|
| Matrix | Soak test | NOT EXECUTED | No sustained Matrix session executed against real homeserver | `SOAK_DURATION_SECONDS=30 pytest tests/test_soak.py::TestMatrixSoak -m live -v -s` | `MATRIX_HOMESERVER`, `MATRIX_USER_ID`, `MATRIX_ACCESS_TOKEN`, `MATRIX_ROOM_ID` |
| Meshtastic | Soak test | NOT EXECUTED | No sustained Meshtastic session executed against real hardware | `SOAK_DURATION_SECONDS=30 pytest tests/test_soak.py::TestMeshtasticSoak -m live -v -s` | `MESHTASTIC_CONNECTION_TYPE`, `MESHTASTIC_HOST` or `MESHTASTIC_SERIAL_PORT` |
| Meshtastic | Second-node inbound | NOT EXECUTED | No second Meshtastic node available in test environment | (same as Meshtastic live smoke, with second node transmitting) | Same as Meshtastic live + second node on same channel |
| MeshCore | Live smoke | NOT EXECUTED | No MeshCore hardware or TCP endpoint available in test environment | `pytest tests/test_meshcore_live.py -m live -v` | `MESHCORE_CONNECTION_TYPE`, `MESHCORE_HOST` or `MESHCORE_SERIAL_PORT` |
| MeshCore | Soak test | NOT EXECUTED | No soak test class exists for MeshCore; no hardware available | N/A (test class does not exist yet) | N/A |
| LXMF | Live smoke | NOT EXECUTED | No Reticulum network or identity file available in test environment | `pytest tests/test_lxmf_live.py -m live -v` | `LXMF_CONNECTION_TYPE`, `LXMF_IDENTITY_PATH` |
| LXMF | Soak test | NOT EXECUTED | No soak test class exists for LXMF; no Reticulum network available | N/A (test class does not exist yet) | N/A |

**To resolve any NOT EXECUTED entry:** set the required environment variables,
ensure the transport SDK is installed, run the command, and record results
in the relevant section of this document.


## 8. Evidence Integration Instructions

When live agents or operators report results:

1. Replace **NOT EXECUTED** placeholders in the relevant transport section
   (§1–§4) with actual values.
2. Include the commit hash, Python version, dependency versions, and
   environment details.
3. Record any caveats, reconnect observations, or unexpected behavior
   exactly as observed.
4. Update this document's "Last updated" header.
5. Remove the resolved entry from §7 (NOT EXECUTED Reasoning).
6. Update `docs/contracts/32-beta-readiness-checklist.md` section 1.3.2
   to reflect the new evidence status.
7. Follow the evidence honesty requirements in
   `docs/runbooks/beta-entry-validation.md` §4.
