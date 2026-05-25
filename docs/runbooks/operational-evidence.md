# Operational Evidence Runbook

> Last updated: 2026-05-25 (Tranche 6 truth-surface update)
> Baseline: HEAD 41a07c7, Python 3.12.3, medre 0.1.0
> Tranche 6 session: **Did NOT execute live hardware/server tests.** No Matrix
> homeserver credentials, no second Matrix account token, no Meshtastic physical
> radio interaction, no MeshCore BLE connection, no LXMF/Reticulum instance were
> provided or available in this session. All live procedure sections remain as
> previously recorded or NOT EXECUTED. This update adds evidence sub-classification
> (fake / Docker SDK-boundary / external live / hardware), procedure templates,
> dependency/version capture commands, and clarifies evidence artifact locations.
> No statuses were promoted.
>
> Status: Partially populated. Current deterministic suite: 3237 passed, 4 skipped,
> 63 deselected (2026-05-11, §5.1). A larger run of 4596 passed was recorded 2026-05-12
> (Contract 62 §2), but the primary evidence anchor in §5.1 remains 3237 from 2026-05-11.
> Live evidence: Matrix historical H-tier 2026-05-10
> (plaintext 13/13, E2EE 7/7). Matrix Docker SDK-boundary: 2026-05-22 local Docker
> Synapse 15 passed, 1 xfailed (see §1.1b). Matrix sk.community live attempt 2026-05-12:
> NOT EXECUTED (access token rejected `M_UNKNOWN_TOKEN`; see §1.4).
> Matrix matrix.org live attempt 2026-05-12:
> NOT EXECUTED (password login rejected `M_FORBIDDEN Invalid username/password`; see §1.4b).
> Meshtastic serial CLI validation: R-tier (hardware)
> 2026-05-12 (device discovery, hardware/firmware, one outbound on ch0, reconnect).
> Track 2 follow-up: R-tier (hardware) 2026-05-12 (additional diagnostics cycle, one reconnect,
> node DB verification, device metrics capture). ACK classified UNRELIABLE,
> delivery classified BEST EFFORT.
> Meshtastic MEDRE adapter live tests: NOT EXECUTED (mtjk not in project venv).
> Meshtastic `queued`/`sent` = local queue acceptance / local SDK send return, not RF confirmation (Contract 61 §3.8.3).
> MeshCore: NOT EXECUTED. Hardware probe (2026-05-12) identified CP2104 `/dev/ttyUSB0`
> (stable by-id, likely T-Beam, no serial chatter). MeshCore firmware flash pending follow-up validation.
> LXMF: NOT EXECUTED. Local source repos available at `/home/jeremiah/dev` for Reticulum
> and LXMF. Reticulum live path setup pending follow-up validation.
> Soak tests remain **NOT EXECUTED**.
> Live commands, env vars, and NOT EXECUTED reasoning in §6–§7.
> Related: `docs/contracts/32-beta-readiness-checklist.md`, section 1.3.2.
> Evidence schema: `docs/contracts/61-operational-evidence-contract.md`.
> Maturity matrix: `docs/contracts/62-adapter-operational-maturity-matrix.md`.
> Live procedures: `docs/runbooks/live-operational-evidence.md`.
> Longrun validation: `docs/runbooks/longrun-validation.md`.
> Capability status anchor: `docs/STATUS.md`.

This document is the consolidated operational evidence record for each validated
transport. Each transport section contains fields for actual test date,
environment, results, caveats, reconnect observations, and limitations.

**Evidence classification (per Contract 61):**

| Tier  | Label                    | Meaning                                                  |
| ----- | ------------------------ | -------------------------------------------------------- |
| **H** | Historical               | Recorded during a prior phase. May be stale.             |
| **C** | Current-tranche          | Recorded against current codebase during active tranche. |
| **S** | Simulated / Fake-runtime | Recorded using mocks/fakes. No real endpoint.            |
| **R** | Real-live-runtime        | Recorded against a real transport endpoint.              |

**Evidence sub-classification (Tranche 6 addition):**

R-tier evidence should be annotated with the *environment boundary* where it was collected:

| Sub-class             | Meaning                                                                                                      | Examples                                                                        |
| --------------------- | ------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------- |
| **Docker SDK-boundary** | Local Docker container running the transport server (e.g. Synapse). SDK boundary test — no external network. | Matrix: local Docker Synapse on localhost:8008 (2026-05-22, 15 passed)           |
| **External live**     | Real external server over the network. Credentials to a third-party or self-hosted service.                  | Matrix: matrix.org or sk.community (H-tier 2026-05-10, NOT EXECUTED 2026-05-12) |
| **Hardware**          | Physical radio hardware connected via serial/TCP/BLE. Real RF transmission or reception.                     | Meshtastic: serial CLI validation on /dev/ttyACM0 (R-tier 2026-05-12)           |

When sub-class is not specified, treat the evidence as **UNSPECIFIED** and do not use it for boundary-specific claims until explicitly classified. **Do not treat Docker SDK-boundary evidence as equivalent to external live or hardware evidence.** Each boundary validates different properties: SDK-boundary validates SDK integration and adapter wiring; external live validates network connectivity and real server behavior; hardware validates physical radio operation.

**How to use this document:**

1. Each transport has an evidence table with well-defined fields.
2. Fields marked **NOT EXECUTED** indicate that no live agent has reported
   results yet. Do not remove these placeholders until real evidence is
   available.
3. When live results arrive, replace the placeholder with actual values
   including date, environment details, and observed behavior.
4. Do not invent, fabricate, or extrapolate live evidence from unit test
   results. Unit tests are recorded separately.
5. Every evidence entry must include a `tier` field per Contract 61 §2.

## 1. Matrix Operational Evidence

> **Evidence tier:** H (historical, recorded 2026-05-10 against matrix.org). Current beta-entry tranche live execution: **NOT EXECUTED** (2026-05-12 attempts: `sk.community` access token rejected `M_UNKNOWN_TOKEN` (§1.4); `matrix.org` password login rejected `M_FORBIDDEN Invalid username/password` (§1.4b)).
> **Live procedures:** `docs/runbooks/live-operational-evidence.md` §1.

### 1.1 Live Smoke Test Evidence (Tier: H — recorded 2026-05-10)

| Field                         | Value                                                                                                                                                                   |
| ----------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Test file**                 | `tests/test_matrix_live.py`                                                                                                                                             |
| **Evidence tier**             | H (historical)                                                                                                                                                          |
| **Last execution date**       | 2026-05-10                                                                                                                                                              |
| **Executor**                  | Live agent (automated)                                                                                                                                                  |
| **Homeserver**                | matrix.org (public homeserver)                                                                                                                                          |
| **MEDRE commit**              | Pre-beta HEAD (2026-05-10)                                                                                                                                              |
| **Python version**            | 3.12                                                                                                                                                                    |
| **mindroom-nio version**      | Installed via `pip install -e ".[matrix]"`                                                                                                                              |
| **Environment**               | Local development machine                                                                                                                                               |
| **Test command**              | `pytest tests/test_matrix_live.py -m live -v`                                                                                                                           |
| **Total tests run**           | 13                                                                                                                                                                      |
| **Passed / Failed / Skipped** | 13 passed / 0 failed / 0 skipped                                                                                                                                        |
| **Non-live regression**       | 202 passed, 0 failed (full suite minus live)                                                                                                                            |
| **Start/connect**             | ✅ Adapter started, `restore_login` succeeded, sync task running                                                                                                        |
| **Health check → healthy**    | ✅ `info.health == "healthy"`, `info.platform == "matrix"`                                                                                                              |
| **Room join**                 | ✅ Room `!sRlwdLCwIGBpSzoRsV:matrix.org` joined successfully                                                                                                            |
| **Room encryption status**    | Unencrypted (plaintext alpha path)                                                                                                                                      |
| **Outbound send → event_id**  | ✅ `room_send` returned event_id starting with `$`                                                                                                                      |
| **Self-message suppression**  | ✅ Own messages suppressed by sender match                                                                                                                              |
| **Stop → clean teardown**     | ✅ `stop()` completed; no leaked tasks                                                                                                                                  |
| **Reconnect observations**    | ✅ Health stays `degraded` during reconnect, `healthy` after recovery. Budget exhaustion → `failed`.                                                                    |
| **Caveats observed**          | Initial harness had a bug where `health_check()` was awaited as a coroutine instead of called as a regular method. Fixed in-tree before final run. No remaining issues. |
| **Restart idempotency**       | ✅ Stop → start cycle re-establishes sync; second `health_check()` returns `healthy`                                                                                    |

### 1.1b Docker SDK-boundary Live Evidence (Tier: R — Docker SDK-boundary, recorded 2026-05-22)

> **Sub-classification:** Docker SDK-boundary (local Docker Synapse on localhost:8008).
> This validates SDK integration, adapter wiring, and lifecycle against a real
> (containerized) Synapse. It does NOT validate external network connectivity,
> federation, or production server behavior.

| Field                         | Value                                                                                                     |
| ----------------------------- | --------------------------------------------------------------------------------------------------------- |
| **Test file**                 | `tests/test_matrix_live.py`                                                                               |
| **Evidence tier**             | R (Docker SDK-boundary)                                                                                   |
| **Last execution date**       | 2026-05-22                                                                                                |
| **Executor**                  | Live agent (automated)                                                                                    |
| **Homeserver**                | Local Docker Synapse (`matrix.local`, `localhost:8008`)                                                   |
| **Gate**                      | `MATRIX_LOCAL_SYNAPSE=1`                                                                                  |
| **Total tests run**           | 16 (15 passed, 1 xfailed)                                                                                |
| **Duration**                  | 40.37s                                                                                                    |
| **Start/connect**             | ✅ Adapter started, connected to local Synapse                                                            |
| **Health check → healthy**    | ✅                                                                                                        |
| **Outbound send → event_id**  | ✅ `room_send` returned event_id                                                                          |
| **Synapse-specific test**     | ✅ `test_synapse_send_captures_event_id` passed                                                           |
| **Third-party inbound**       | xfailed (expected — requires second Matrix user sending during 30s window)                                |
| **E2EE**                      | NOT EXECUTED (no E2EE env vars configured)                                                                |
| **Artifact location**         | Evidence recorded in `docs/runbooks/matrix-local-bringup.md` §Live Validation Evidence                    |
| **Limitations**               | Local Docker network only. No federation, no external latency, no token expiry, no real-world rate limits |

| Field                         | Value                                                                                                                     |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| **Test file**                 | `tests/test_matrix_e2ee_live.py`                                                                                          |
| **Evidence tier**             | H (historical)                                                                                                            |
| **Last execution date**       | 2026-05-10                                                                                                                |
| **Executor**                  | Live agent (automated)                                                                                                    |
| **Homeserver**                | matrix.org (public homeserver)                                                                                            |
| **Room type**                 | Unencrypted room used for initial E2EE-mode startup tests                                                                 |
| **E2EE mode**                 | `e2ee_required` config used; crypto store loaded                                                                          |
| **mindroom-nio[e2e] version** | Installed via `pip install -e ".[matrix-e2e]"`                                                                            |
| **vodozemac version**         | Pulled as dependency of `mindroom-nio[e2e]`                                                                               |
| **Total tests run**           | 7                                                                                                                         |
| **Passed / Failed / Skipped** | 7 passed / 0 failed / 0 skipped                                                                                           |
| **Crypto store loaded**       | ✅ `crypto_store_loaded == True`                                                                                          |
| **Encrypted send → event_id** | Tests ran against unencrypted room in E2EE mode. Encrypted-room results: see §1.3 below.                                  |
| **Undecryptable events**      | 0 observed during run                                                                                                     |
| **Caveats observed**          | E2EE tests validated startup with crypto deps against an unencrypted room. See §1.3 for encrypted-room follow-up results. |

### 1.3 Encrypted Room Follow-up Evidence (Tier: H — recorded 2026-05-10)

#### 1.3.1 Pre-fix Run (initial)

| Field                         | Value                                                                                                                                                                                                                                                         |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Test type**                 | Manual / agent-driven encrypted-room follow-up                                                                                                                                                                                                                |
| **Execution date**            | 2026-05-10                                                                                                                                                                                                                                                    |
| **Executor**                  | Live agent (automated)                                                                                                                                                                                                                                        |
| **Homeserver**                | matrix.org (public homeserver)                                                                                                                                                                                                                                |
| **Target room**               | `!rnmyZMhUoraPwZUDPP:matrix.org` (E2EE enabled)                                                                                                                                                                                                               |
| **Room join**                 | ✅ Adapter successfully joined the encrypted room                                                                                                                                                                                                             |
| **Room encryption confirmed** | ✅ Room confirmed as encrypted (`RoomEncryptionEvent` received)                                                                                                                                                                                               |
| **Outbound send attempt 1**   | ❌ Failed with `OlmUnverifiedDeviceError`                                                                                                                                                                                                                     |
| **Outbound send attempt 2**   | ❌ Failed with `OlmUnverifiedDeviceError`                                                                                                                                                                                                                     |
| **Root cause**                | `ignore_unverified_devices` was `False` (nio strict default). The bot's device was not verified by other room members, so nio refused to share the Megolm session key with unverified devices.                                                                |
| **Implication**               | Encrypted-room **join** and **detection** work. Outbound encrypted send was blocked by nio's unverified-device policy.                                                                                                                                        |
| **Fix applied**               | MEDRE now internally sets `ignore_unverified_devices=True` when `encryption_mode` is not `"plaintext"` — required by upstream nio (no cross-signing support). No operator toggle needed. See `docs/contracts/25-matrix-e2ee-readiness.md` §5.2 for rationale. |

#### 1.3.2 Post-fix Re-test (E2EE live suite)

| Field                                                | Value                                                                                                                                                                                                                                                                                            |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Test file**                                        | `tests/test_matrix_e2ee_live.py`                                                                                                                                                                                                                                                                 |
| **Execution date**                                   | 2026-05-10                                                                                                                                                                                                                                                                                       |
| **Executor**                                         | Live agent (automated)                                                                                                                                                                                                                                                                           |
| **Homeserver**                                       | matrix.org (public homeserver)                                                                                                                                                                                                                                                                   |
| **Target room**                                      | `!rnmyZMhUoraPwZUDPP:matrix.org` (E2EE enabled)                                                                                                                                                                                                                                                  |
| **Test command**                                     | `pytest tests/test_matrix_e2ee_live.py -m live -v`                                                                                                                                                                                                                                               |
| **Total tests run**                                  | 7                                                                                                                                                                                                                                                                                                |
| **Passed / Failed / Skipped**                        | 7 passed / 0 failed / 0 skipped                                                                                                                                                                                                                                                                  |
| **Duration**                                         | 3.73s                                                                                                                                                                                                                                                                                            |
| **Previously failing `test_send_encrypted_text`**    | ✅ Passed post-fix                                                                                                                                                                                                                                                                               |
| **Previously failing `test_restart_send_encrypted`** | ✅ Passed post-fix                                                                                                                                                                                                                                                                               |
| **Crypto store loaded**                              | ✅                                                                                                                                                                                                                                                                                               |
| **Encrypted send → event_id**                        | ✅ Outbound encrypted send succeeds — MEDRE passes `ignore_unverified_devices=True` for non-plaintext modes                                                                                                                                                                                      |
| **Caveats**                                          | This is not a security downgrade — `ignore_unverified_devices=True` is required by the upstream nio client (no cross-signing support, MSC1756). MEDRE applies this automatically based on `encryption_mode`. Device verification via cross-signing is an upstream nio gap, not a MEDRE deferral. |

### 1.4 Live Smoke Test Attempt — sk.community (Tier: NOT EXECUTED — 2026-05-12)

| Field                       | Value                                                                                                                                                                                             |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Test file**               | `tests/test_matrix_live.py`                                                                                                                                                                       |
| **Evidence tier**           | NOT EXECUTED (credential failure)                                                                                                                                                                 |
| **Attempt date**            | 2026-05-12                                                                                                                                                                                        |
| **Executor**                | Live agent (automated)                                                                                                                                                                            |
| **Homeserver**              | `sk.community` (reachable, Matrix API v1.12 confirmed via `/_matrix/client/versions`)                                                                                                             |
| **User**                    | `@forxrelay:sk.community`                                                                                                                                                                         |
| **MATRIX_ROOM_ID**          | Not provided; bot has 0 joined rooms                                                                                                                                                              |
| **MATRIX_DEVICE_ID**        | Not set — E2EE NOT EXECUTED                                                                                                                                                                       |
| **MATRIX_STORE_PATH**       | Not set — E2EE NOT EXECUTED                                                                                                                                                                       |
| **Test command**            | Not executed — credentials rejected before pytest invocation                                                                                                                                      |
| **Prerequisite check**      | `curl /_matrix/client/v3/joined_rooms` with provided access token → `{"errcode":"M_UNKNOWN_TOKEN","error":"Token is not active","soft_logout":false}`                                             |
| **Homeserver connectivity** | ✅ `/_matrix/client/versions` → HTTP 200, flows: `m.login.password`, `m.login.sso`, `m.login.token`                                                                                               |
| **Access token validity**   | ❌ Rejected — token not active                                                                                                                                                                    |
| **Resolution**              | Obtain a valid (non-expired) access token for `@forxrelay:sk.community`. Create or join at least one room to provide `MATRIX_ROOM_ID`. Then re-run `pytest tests/test_matrix_live.py -m live -v`. |

### 1.4b Live Smoke Test Attempt — matrix.org (Tier: NOT EXECUTED — 2026-05-12)

| Field                          | Value                                                                                                                                                                                                                                                                                                                                                                                               |
| ------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Test file**                  | `tests/test_matrix_live.py`                                                                                                                                                                                                                                                                                                                                                                         |
| **Evidence tier**              | NOT EXECUTED (credential failure)                                                                                                                                                                                                                                                                                                                                                                   |
| **Attempt date**               | 2026-05-12                                                                                                                                                                                                                                                                                                                                                                                          |
| **Executor**                   | Live agent (automated)                                                                                                                                                                                                                                                                                                                                                                              |
| **Homeserver**                 | `matrix.org` (reachable, login flows: `m.login.password`, `m.login.sso`, `m.login.token`)                                                                                                                                                                                                                                                                                                           |
| **User**                       | `@forxrelay:matrix.org`                                                                                                                                                                                                                                                                                                                                                                             |
| **Target rooms**               | `!sRlwdLCwIGBpSzoRsV:matrix.org` (unencrypted), `!rnmyZMhUoraPwZUDPP:matrix.org` (encrypted — E2EE skipped, no DEVICE_ID/STORE_PATH)                                                                                                                                                                                                                                                                |
| **MATRIX_DEVICE_ID**           | Not set — E2EE NOT EXECUTED                                                                                                                                                                                                                                                                                                                                                                         |
| **MATRIX_STORE_PATH**          | Not set — E2EE NOT EXECUTED                                                                                                                                                                                                                                                                                                                                                                         |
| **Test command**               | Not executed — login rejected before pytest invocation                                                                                                                                                                                                                                                                                                                                              |
| **Login method**               | `POST /_matrix/client/v3/login` with `m.login.password`, user `forxrelay`                                                                                                                                                                                                                                                                                                                           |
| **Login result**               | ❌ HTTP 403 `M_FORBIDDEN: Invalid username/password` (3 attempts: shell curl, Python urllib, full MXID identifier — all identical failure)                                                                                                                                                                                                                                                          |
| **Password encoding verified** | ✅ 14 chars, hex `212a696c30442456753530526426`, matches specification exactly                                                                                                                                                                                                                                                                                                                      |
| **Homeserver connectivity**    | ✅ `/_matrix/client/v3/login` (GET) → HTTP 200, 3 flows listed                                                                                                                                                                                                                                                                                                                                      |
| **Resolution**                 | The provided password was transmitted correctly (verified via hex dump) but is not accepted by matrix.org for user `forxrelay`. The account password may have changed, the account may be locked, or matrix.org may require SSO/captcha for this account. Obtain a valid access token via Element or another Matrix client and set `MATRIX_ACCESS_TOKEN` directly, or confirm the correct password. |

### 1.5 Soak Test Evidence (Tier: NOT EXECUTED)

| Field                           | Value                                |
| ------------------------------- | ------------------------------------ |
| **Test file**                   | `tests/test_soak.py::TestMatrixSoak` |
| **Last execution date**         | **NOT EXECUTED**                     |
| **Soak duration (seconds)**     | **NOT EXECUTED**                     |
| **Messages sent**               | **NOT EXECUTED**                     |
| **Messages succeeded**          | **NOT EXECUTED**                     |
| **Max reconnect attempts seen** | **NOT EXECUTED**                     |
| **Session health throughout**   | **NOT EXECUTED**                     |
| **Caveats observed**            | **NOT EXECUTED**                     |

### 1.6 Matrix Known Limitations (confirmed from source and live testing)

- **Third-party inbound: test harness exists, live execution operator-dependent.** The live test `test_inbound_message_received` in `tests/test_matrix_live.py` validates the full third-party inbound path (nio sync → `_on_room_message` → codec decode → `publish_inbound()` → canonical event shape → diagnostics counters). It is gated by `MATRIX_INBOUND_SENDER` and a 30-second window. Deterministic unit tests cover the same logic paths without live connectivity (see §1.6). Live execution requires a second Matrix user sending a message during the test window — this has not yet been executed against a real homeserver.
- E2EE text alpha: encrypted-room join works. Initial outbound encrypted send failed with `OlmUnverifiedDeviceError` (2 tests); root cause was nio's strict `ignore_unverified_devices=False` default blocking key sharing with unverified devices. Fix: adapter set `ignore_unverified_devices=True`. Post-fix re-test: encrypted-room full suite passed 7/7 in 3.73s (see §1.3). This is required by upstream nio (no cross-signing support, MSC1756) — every nio-based automated E2EE client must set this flag. **E2EE is Matrix client encrypted-room support only — not generic cross-transport E2EE.**
- No E2EE reactions, edits, deletes, or attachments.
- No cross-signing support in `mindroom-nio`. Device verification via cross-signing is not implemented.
- Access token is a plain string in config (no secure storage or rotation).
- `mindroom-nio` is a fork; maintenance status relative to upstream is unverified.
- Sync loop error handling is untested under real network conditions.

### 1.7 Track 2 — Third-party Inbound Validation Status

#### 1.7.1 What has been validated (deterministic)

| Aspect                                            | Validation     | Evidence                                                                               |
| ------------------------------------------------- | -------------- | -------------------------------------------------------------------------------------- |
| `publish_inbound()` called for third-party sender | ✅ Unit tested | `TestThirdPartyInboundCanonicalEventShape` (8 tests) in `tests/test_matrix_adapter.py` |
| `source_transport_id` is sender MXID (not bot)    | ✅ Unit tested | `test_third_party_event_has_sender_as_transport_id`                                    |
| `source_channel_id` is Matrix room ID             | ✅ Unit tested | `test_third_party_event_has_room_as_channel_id`                                        |
| `source_native_ref` carries Matrix event_id       | ✅ Unit tested | `test_third_party_event_has_source_native_ref`                                         |
| `event_kind == "message.created"`                 | ✅ Unit tested | `test_third_party_event_kind_is_message_created`                                       |
| Payload contains body and msgtype                 | ✅ Unit tested | `test_third_party_event_has_correct_payload`                                           |
| Self-loop suppression (sender == bot)             | ✅ Unit tested | `TestSelfMessageSuppression` (3 tests)                                                 |
| MEDRE-origin envelope suppression                 | ✅ Unit tested | `TestMEDREOriginLoopSuppression` (4 tests)                                             |
| Room allowlist filtering                          | ✅ Unit tested | `TestRoomAllowlist` (4 tests)                                                          |
| Inbound diagnostics counters                      | ✅ Unit tested | `TestInboundDiagnosticsCounters` (8 tests)                                             |
| Diagnostics dict exposure                         | ✅ Unit tested | `test_diagnostics_exposes_inbound_counters`                                            |

#### 1.7.2 What requires operator-dependent live validation

| Aspect                                                    | Status              | Blocker                                                                                                            |
| --------------------------------------------------------- | ------------------- | ------------------------------------------------------------------------------------------------------------------ |
| nio sync delivers third-party event to `_on_room_message` | ⏳ Not executed     | Requires second Matrix account sending to test room during 30 s window                                             |
| Live self-echo suppression (send → sync → suppress)       | ⏳ Partially tested | `test_live_send_and_receive` validates self-echo doesn't leak, but full round-trip timing is environment-dependent |
| Inbound diagnostics counters on live server               | ⏳ Not executed     | Requires live third-party inbound event                                                                            |
| Encrypted-room inbound from third party                   | ⏳ Not executed     | Requires second account in encrypted room with crypto store                                                        |

#### 1.7.3 Live third-party inbound test procedure

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

#### 1.7.4 Blockers for live execution

1. **Second Matrix account**: The test requires a different Matrix user to send a message to the test room. A single bot account cannot produce a third-party inbound event (self-messages are suppressed).
2. **Manual coordination**: The sender must send during the 30-second test window. No automated sender harness exists.
3. **No shared second account credentials in repository**: Credentials are operator-specific and must not be stored in the repo.

## 2. Meshtastic Operational Evidence

> **Evidence tier:** R (real-live-runtime, recorded 2026-05-12 against real hardware via serial). Prior H-tier evidence from 2026-05-10 remains valid for historical reference. Track 2 follow-up evidence added 2026-05-12.
> **Live procedures:** `docs/runbooks/live-operational-evidence.md` §2.
> **Queue local-acceptance note:** Per Contract 61 §3.8.3, Meshtastic is the only adapter where `deliver()` returns `native_message_id=None` initially. The delivery lifecycle is two-phase: `queued` (local queue acceptance, not yet sent to radio) then `sent` (queue drain completed radio send). Neither `queued` nor `sent` means RF confirmation, remote-node receipt, or ACK. If the process crashes between phases, evidence correctly shows `queued` with no `sent` receipt. The queue is in-memory and non-durable across process restart. This applies to all Meshtastic maturity evidence in this section.

### 2.0 Serial Live Validation Evidence (Tier: R — recorded 2026-05-12)

> **Validation type:** Manual CLI-driven serial live validation using meshtastic 2.7.8 CLI.
> **Scope:** Device discovery, hardware/firmware capture, one outbound text on channel 0, disconnect/reconnect resilience (3 cycles total: 2 initial + 1 Track 2). Track 2 adds: full diagnostics snapshot, node DB verification, device metrics, Python import confirmation.
> **NOT in scope:** MEDRE adapter lifecycle (start/stop/health), send_one path, soak testing, second-node inbound, encrypted channels, admin operations. These remain NOT EXECUTED in this session.

**Evidence lifecycle** (per Contract 61 §8):

| Field              | Value                                                                                                                                                                                                                |
| ------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| evidence_type      | observed                                                                                                                                                                                                             |
| confidence         | medium                                                                                                                                                                                                               |
| verified_at        | 2026-05-12                                                                                                                                                                                                           |
| verification_scope | Manual CLI serial validation: device discovery, hardware/firmware capture, 1 outbound send on ch0, 3 reconnect cycles. NOT MEDRE adapter lifecycle — CLI-level only. No soak, no second-node, no encrypted channels. |
| environment        | Dev laptop, serial `/dev/ttyACM0` (CH9102F, T-LoRa V2.1-1.6), firmware 2.7.19, meshtastic CLI 2.7.8, Python (platformio penv). `mtjk` not in project venv.                                                           |

| Field                        | Value                                                                        |
| ---------------------------- | ---------------------------------------------------------------------------- |
| **Evidence tier**            | R (real-live-runtime)                                                        |
| **Execution date**           | 2026-05-12                                                                   |
| **Executor**                 | Manual operator (serial CLI validation)                                      |
| **Connection type**          | Serial (USB CDC ACM)                                                         |
| **Serial port**              | `/dev/ttyACM0` (USB ID: `1a86_USB_Single_Serial_5435017226-if00`)            |
| **meshtastic CLI version**   | 2.7.8 (`/home/jeremiah/.platformio/penv/bin/meshtastic`)                     |
| **pyserial version**         | 3.5                                                                          |
| **mtjk package**             | NOT installed (used platformio penv meshtastic 2.7.8 instead)                |
| **User groups**              | `dialout` (serial write access confirmed)                                    |
| **Node hardware**            | LilyGO T-LoRa V2.1.1.6 (`hwModel: TLORA_V2_1_1P6`, `pioEnv: tlora-v2-1-1_6`) |
| **Node ID**                  | `!25d6e474` (num 634840180)                                                  |
| **Node name**                | "Meshtastic e474" (short: "e474")                                            |
| **Firmware version**         | 2.7.19.bb3d6d5 (firmwareEdition: VANILLA)                                    |
| **Device role**              | CLIENT                                                                       |
| **Capabilities**             | hasWifi: true, hasBluetooth: true, hasPKC: true, canShutdown: true           |
| **LoRa config**              | Region: US, Bandwidth: 250, SF: 11, CR: 5, hopLimit: 3, txEnabled: true      |
| **Device serialEnabled**     | false (device pref, but serial CLI connects fine via CDC ACM)                |
| **GPS mode**                 | NOT_PRESENT (no GPS module)                                                  |
| **Battery at first query**   | 97%, voltage 4.157V                                                          |
| **Battery at second query**  | 96% (normal drain)                                                           |
| **Battery at nodes query**   | "Powered" (USB power detected)                                               |
| **Battery at Track 2 query** | 101% / 4.202V ("Powered" — USB power detected)                               |
| **Channel utilization**      | 1.0% initially, 7.51% after test message, 1.68% at Track 2 query             |
| **Air util TX**              | 0.028% initially, 0.06% after test message, 0.06% at Track 2 query           |
| **Uptime at first query**    | 1276 seconds                                                                 |
| **Uptime at Track 2 query**  | 27616 seconds (~7.7 hours)                                                   |
| **Reboot count**             | 26 (unchanged across initial and Track 2 queries)                            |
| **Min app version**          | 30200                                                                        |
| **Device state version**     | 24                                                                           |

#### 2.0.1 Commands Run (no secrets)

```bash
# Device discovery
ls -la /dev/ttyACM0 /dev/ttyUSB* /dev/serial/by-id/*
test -w /dev/ttyACM0

# Dependency checks
python3 -c "import meshtastic; print(meshtastic.__file__)"
python3 -c "import serial; print(serial.__version__)"
pip show mtjk

# Device info (serial connection)
meshtastic --port /dev/ttyACM0 --info

# Node listing
meshtastic --port /dev/ttyACM0 --nodes

# Outbound test (channel 0, one message only)
meshtastic --port /dev/ttyACM0 --ch-index 0 --sendtext "MEDRE serial validation test 2026-05-12 - disregard"

# Reconnect cycle 1 (disconnect + reconnect)
meshtastic --port /dev/ttyACM0 --info

# Reconnect cycle 2 (disconnect + reconnect)
meshtastic --port /dev/ttyACM0 --info

# Track 2 follow-up: device info
meshtastic --port /dev/ttyACM0 --info

# Track 2 follow-up: node listing
meshtastic --port /dev/ttyACM0 --nodes

# Track 2 follow-up: Python import verification
python3 -c "import meshtastic; from meshtastic import serial_interface; print('OK')"

# Track 2 follow-up: reconnect cycle 3 (disconnect 3s + reconnect)
meshtastic --port /dev/ttyACM0 --info
```

#### 2.0.2 Outbound Send Observation

| Field                               | Value                                                                                                                                                                                                           |
| ----------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Command**                         | `meshtastic --port /dev/ttyACM0 --ch-index 0 --sendtext "MEDRE serial validation test 2026-05-12 - disregard"`                                                                                                  |
| **CLI output**                      | `Connected to radio` → `Sending text message MEDRE serial validation test 2026-05-12 - disregard to ^all on channelIndex:0`                                                                                     |
| **No error raised**                 | ✅ CLI completed with exit code 0                                                                                                                                                                               |
| **Explicit ACK received**           | **No** — meshtastic 2.7.8 CLI does not print ACK confirmation for broadcast sends. sendText completed without error, but no delivery acknowledgment was observed.                                               |
| **Second-node reception confirmed** | **No** — a second node (`!ee4a65b1`, "Meshtastic 65b1") appeared in the node DB after the send, but its appearance is due to hearing its periodic announcement, NOT evidence that it received our test message. |
| **Duplicate-send risk**             | Not assessed in this session. Only one send was performed.                                                                                                                                                      |

#### 2.0.3 Second Node Observation

During the validation session, a second node appeared in the mesh:

| Field              | Value                                                                                                                                          |
| ------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| **Node ID**        | `!ee4a65b1`                                                                                                                                    |
| **Short name**     | "65b1"                                                                                                                                         |
| **Long name**      | "Meshtastic 65b1"                                                                                                                              |
| **Hardware model** | UNSET (not yet broadcast or older firmware)                                                                                                    |
| **Public key**     | N/A (PKC not available or not broadcast)                                                                                                       |
| **SNR**            | -0 dB (direct, 1 hop)                                                                                                                          |
| **Channel**        | 0                                                                                                                                              |
| **Battery**        | N/A                                                                                                                                            |
| **Position**       | N/A                                                                                                                                            |
| **When observed**  | Appeared in node DB between first `--info` (nodedbCount: 1) and second `--info` (nodedbCount: 2), approximately 30–60 seconds into the session |

**Honest assessment:** The second node's appearance confirms that at least one other Meshtastic device is active on the same LoRa channel in radio range. We CANNOT confirm this node received our test message, acknowledged it, or processed it in any way. Its node DB entry is evidence of its presence, not evidence of message delivery.

#### 2.0.4 Disconnect/Reconnect Resilience (CLI-level)

| Field                   | Value                                                                                                                                                                                                                                                                                    |
| ----------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Cycle count**         | 3 (after initial connection, then 2 more CLI sessions, plus 1 Track 2 follow-up)                                                                                                                                                                                                         |
| **Cycle 1**             | `--info` connected successfully. nodedbCount: 2. All fields consistent.                                                                                                                                                                                                                  |
| **Cycle 2**             | `--info` connected successfully. nodedbCount: 2. All fields consistent.                                                                                                                                                                                                                  |
| **Cycle 3 (Track 2)**   | `--info` connected successfully after 3s disconnect. nodedbCount: 2. Battery: Powered, uptime: 27616s, channel util: 1.68%, air util TX: 0.06%. All fields consistent.                                                                                                                   |
| **Connection failures** | 0 across all 4 CLI sessions (initial + 3 reconnect cycles)                                                                                                                                                                                                                               |
| **Serial errors**       | 0 across all sessions                                                                                                                                                                                                                                                                    |
| **Observation**         | Each CLI invocation creates a fresh serial connection to `/dev/ttyACM0`, completes its operation, and disconnects. All 4 connections (initial + 3 reconnects) succeeded within 15-second timeouts. Device stable across 7.7+ hours of uptime with no reboot (rebootCount: 26 unchanged). |

**Caveat:** These are CLI-level disconnect/reconnect cycles (each meshtastic CLI invocation opens and closes the serial port). This is NOT the same as MEDRE adapter session reconnect with exponential backoff, health transitions, and pubsub resubscription. MEDRE adapter session reconnect remains NOT EXECUTED (see §2.3).

#### 2.0.5 Startup/Shutdown Observations

- **Startup:** `meshtastic --port /dev/ttyACM0` connects within 1–3 seconds. "Connected to radio" message appears promptly. Consistent across all 4 CLI sessions (initial + 3 reconnects).
- **Shutdown:** CLI disconnects cleanly after command completion. No orphaned serial locks observed (subsequent connections succeed immediately).
- **Serial mode:** Device `serialEnabled` is `false` in preferences, but CDC ACM serial works correctly for CLI access. The `serialEnabled` preference likely refers to the Meshtastic device's own serial console, not the USB serial interface.
- **Long-running stability (Track 2):** Device stable at 27616 seconds (~7.7 hours) uptime with no reboot during the observation window. Battery at "Powered" (USB). Channel utilization low (1.68%). Second node still present in node DB (`!ee4a65b1`, SNR -0.25 dB).

#### 2.0.6 Track 2 — Delivery & ACK Classification

> **Added 2026-05-12 (Track 2 follow-up).** Based on CLI-level serial validation across 4 sessions and 1 outbound send attempt.

| Field                                 | Value                                                                                                                                                                                                                                                                 |
| ------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **ACK reliability**                   | **UNRELIABLE** — meshtastic 2.7.8 CLI does not print ACK confirmation for broadcast sends. `sendText` completed without error, but no delivery acknowledgment was observed. The Meshtastic protocol does not guarantee ACK for broadcast messages on shared channels. |
| **Delivery guarantee**                | **BEST EFFORT** — fire-and-forget LoRa broadcast. No second-node reception confirmed. A second node (`!ee4a65b1`) was present in the node DB at SNR -0.25 dB, 1 hop away, but its presence confirms radio range overlap only, NOT message delivery to that node.      |
| **Reconnect reliability (CLI-level)** | **RELIABLE** — 4/4 serial connections succeeded across ~7.7 hours of device uptime. No serial errors, no connection failures.                                                                                                                                         |
| **Device stability**                  | **STABLE** — device ran continuously without reboot (rebootCount: 26 unchanged), battery at "Powered" (USB), channel utilization low (1.68%).                                                                                                                         |
| **MEDRE adapter reliability**         | **NOT ASSESSED** — CLI-level validation only. MEDRE adapter session lifecycle, health transitions, and send_one pipeline remain untested against real hardware.                                                                                                       |

#### 2.0.7 NOT EXECUTED (this session)

| Item                                                                | Reason                                                                                                                            |
| ------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| MEDRE adapter lifecycle (start/stop/health) via `pytest` live tests | mtjk package not installed in project venv; meshtastic 2.7.8 available only via platformio penv. Live pytest suite requires mtjk. |
| `send_one` path via MEDRE adapter                                   | Requires MEDRE adapter running against real hardware. Not tested.                                                                 |
| MEDRE session reconnect with exponential backoff                    | Requires adapter session; only CLI-level reconnect tested.                                                                        |
| Soak test (sustained runtime)                                       | Not in scope for minimal validation.                                                                                              |
| Second-node inbound reception                                       | Requires second node to send during test window. Not attempted.                                                                   |
| Encrypted channel support                                           | Not tested.                                                                                                                       |
| Admin operations, config writes, firmware changes                   | Explicitly excluded as destructive.                                                                                               |
| BLE connectivity                                                    | Not tested.                                                                                                                       |
| Multi-hop delivery                                                  | Not tested.                                                                                                                       |

**Outbound gate (`outbound_mode = "listen_only"`) evidence note:** When the Meshtastic adapter is configured with `outbound_mode = "listen_only"`, outbound delivery is suppressed before RF transmission. Suppressed deliveries appear as non-retryable adapter failures in delivery receipts with detail `outbound suppressed: listen_only mode`. This is intentional operator-configured suppression, not a transport failure. Queue counters (`queue_total_sent`, `queue_pending`) do not reflect suppressed items — they are rejected before enqueue. Inbound evidence and diagnostics are unaffected. See `docs/runbooks/configuration.md` (Outbound Gate Semantics).

**Shutdown queue abandonment note:** Items remaining in the Meshtastic adapter-local outbound queue at shutdown are lost — not persisted, not requeued, not recovered on restart. The queue is in-memory and non-durable. This is a documented non-guarantee. Delivery receipts already written to SQLite survive, but in-flight queue items do not.

### 2.1 Live Smoke Test Evidence (Tier: H — recorded 2026-05-10)

| Field                              | Value                                                                                                                                                                                                                                                                                                      |
| ---------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Test file**                      | `tests/test_meshtastic_live.py`                                                                                                                                                                                                                                                                            |
| **Evidence tier**                  | H (historical)                                                                                                                                                                                                                                                                                             |
| **Last execution date**            | 2026-05-10                                                                                                                                                                                                                                                                                                 |
| **Executor**                       | Live agent (automated)                                                                                                                                                                                                                                                                                     |
| **Connection type**                | Serial (direct USB connection to `/dev/ttyACM0`)                                                                                                                                                                                                                                                           |
| **Node hardware**                  | LilyGO T-LORA V2.1, node `!25d6e474`                                                                                                                                                                                                                                                                       |
| **Firmware version**               | 2.7.19                                                                                                                                                                                                                                                                                                     |
| **Channel**                        | Test (PRIMARY, LONG_FAST)                                                                                                                                                                                                                                                                                  |
| **mtjk version**                   | 2.7.8.post2+ (imported as `meshtastic`)                                                                                                                                                                                                                                                                    |
| **MEDRE commit**                   | Pre-beta HEAD (2026-05-10)                                                                                                                                                                                                                                                                                 |
| **Python version**                 | 3.12                                                                                                                                                                                                                                                                                                       |
| **Environment**                    | Local development machine                                                                                                                                                                                                                                                                                  |
| **Test command**                   | `pytest tests/test_meshtastic_live.py -m live -v`                                                                                                                                                                                                                                                          |
| **Total tests run**                | 10                                                                                                                                                                                                                                                                                                         |
| **Passed / Failed / Skipped**      | 10 passed / 0 failed / 0 skipped                                                                                                                                                                                                                                                                           |
| **Wall time**                      | 34.47s                                                                                                                                                                                                                                                                                                     |
| **Raw mtjk sendText**              | ✅ `sendText()` returned `MeshPacket` with populated `id`. Outbound packet IDs were unique across sends.                                                                                                                                                                                                   |
| **Raw mtjk sendData**              | ✅ `sendData()` returned `MeshPacket` with populated `id`.                                                                                                                                                                                                                                                 |
| **Raw mtjk receive callback**      | ✅ Pubsub callback fired on packet reception. Received packets have expected shape (`decoded`, `id`, `portnum`). Inbound telemetry packet observed (not just text).                                                                                                                                        |
| **MEDRE adapter start**            | ✅ Adapter created client via `_create_client()`, connected and subscribed.                                                                                                                                                                                                                                |
| **MEDRE adapter health → healthy** | ✅ `health_check()` returned `"healthy"` after start.                                                                                                                                                                                                                                                      |
| **MEDRE adapter stop**             | ✅ `stop()` closed client, unsubscribed cleanly.                                                                                                                                                                                                                                                           |
| **Caveats observed**               | Initial harness had two bugs fixed in-tree before final run: (1) `isConnected` TypeError — attribute used instead of correct API; (2) `pypubsub` ListenerMismatchError — callback signature mismatch (`pub.sendMessage` vs `pypubsub.subscribe`). Both fixed. Final 10/10 pass reflects corrected harness. |
| **Reconnect observations**         | Not explicitly tested in this run. Session maintained stable connection throughout 34.47s execution.                                                                                                                                                                                                       |
| **Destructive operations**         | None performed. No admin packets, no firmware changes, no config writes.                                                                                                                                                                                                                                   |
| **Second-node inbound**            | **NOT EXECUTED** — requires a second Meshtastic node not present in this run.                                                                                                                                                                                                                              |

### 2.2 Soak Test Evidence (Tier: NOT EXECUTED)

| Field                           | Value                                    |
| ------------------------------- | ---------------------------------------- |
| **Test file**                   | `tests/test_soak.py::TestMeshtasticSoak` |
| **Last execution date**         | **NOT EXECUTED**                         |
| **Soak duration (seconds)**     | **NOT EXECUTED**                         |
| **Messages sent**               | **NOT EXECUTED**                         |
| **Messages succeeded**          | **NOT EXECUTED**                         |
| **Max reconnect attempts seen** | **NOT EXECUTED**                         |
| **Session health throughout**   | **NOT EXECUTED**                         |
| **Caveats observed**            | **NOT EXECUTED**                         |

### 2.3 Meshtastic Known Limitations (confirmed from source and live testing)

- No full MEDRE adapter `send_one` integration with real hardware.
- No inbound message reception from a second node.
- No multi-hop mesh delivery testing.
- No encrypted channel support.
- No telemetry, position, nodeinfo, or admin packet processing.
- BLE connectivity documented but not exercised.
- `mtjk` is a fork; distribution name is `mtjk`, import name is `meshtastic`.
- No factory reset, no ham mode, no channel deletion performed during testing.
- **Duplicate-send caveat:** CLI-level `sendText` on channel 0 confirmed working (2026-05-12). Only one send performed. Duplicate-send risk (session retries, reconnection re-sends) not assessed against real hardware. The MEDRE adapter's `send_one` retry logic (max 3 transient retries) is tested via monkeypatch only.
- **ACK reliability:** Classified **UNRELIABLE** (Track 2, 2026-05-12). meshtastic 2.7.8 CLI does not print ACK for broadcast sends. No delivery acknowledgment observed. Protocol does not guarantee ACK for broadcast messages.
- **Delivery guarantee:** Classified **BEST EFFORT** (Track 2, 2026-05-12). Fire-and-forget LoRa broadcast. Second-node presence in node DB confirms radio range overlap only, NOT message delivery.
- **Second-node observation:** A second node (`!ee4a65b1`, "Meshtastic 65b1") was observed on channel 0 during both initial and Track 2 validation (SNR -0.25 dB at Track 2, UNSET hardware). Its presence confirms radio range overlap but does NOT confirm message delivery. No second-node inbound or ACK was observed.

## 3. MeshCore Operational Evidence

> **Evidence tier:** NOT EXECUTED. No live evidence of any tier. S-tier unit tests pass.
> **Live procedures:** `docs/runbooks/live-operational-evidence.md` §2.14.
> **Hardware probe (2026-05-12):** CP2104 `/dev/ttyUSB0` (stable by-id, likely T-Beam) — no serial chatter observed. Serial path confirmed NOT VIABLE for MeshCore SDK (companion heartbeat protocol, not MeshCore serial). BLE preconditions met but connection NOT ATTEMPTED. MeshCore firmware source available at `/home/jeremiah/dev`. `esptool` available via pipx. Firmware flash and live validation pending follow-up work.
> **Maturity:** Alpha (Tier 2) / Experimental for hardware path per Contract 62 §3.3. Cannot promote beyond alpha until hardware-validated live evidence is recorded.

### 3.1 Live Smoke Test Evidence (Tier: NOT EXECUTED)

| Field                         | Value                             |
| ----------------------------- | --------------------------------- |
| **Test file**                 | `tests/test_meshcore_live.py`     |
| **Last execution date**       | **NOT EXECUTED**                  |
| **Executor**                  | **NOT EXECUTED**                  |
| **Connection type**           | **NOT EXECUTED** (TCP/serial/BLE) |
| **Node hardware**             | **NOT EXECUTED**                  |
| **SDK version**               | **NOT EXECUTED**                  |
| **MEDRE commit**              | **NOT EXECUTED**                  |
| **Python version**            | **NOT EXECUTED**                  |
| **Environment**               | **NOT EXECUTED**                  |
| **Total tests run**           | **NOT EXECUTED**                  |
| **Passed / Failed / Skipped** | **NOT EXECUTED**                  |
| **Adapter start**             | **NOT EXECUTED**                  |
| **Health check → healthy**    | **NOT EXECUTED**                  |
| **Send text → success**       | **NOT EXECUTED**                  |
| **Inbound callback received** | **NOT EXECUTED**                  |
| **Diagnostics snapshot**      | **NOT EXECUTED**                  |
| **Stop → clean teardown**     | **NOT EXECUTED**                  |
| **Caveats observed**          | **NOT EXECUTED**                  |
| **Reconnect observations**    | **NOT EXECUTED**                  |

### 3.1b Hardware Probe Evidence (2026-05-12)

> **Not live-transport evidence.** Documents physical device findings relevant to MeshCore follow-up validation.

| Field                        | Value                                                                                           |
| ---------------------------- | ----------------------------------------------------------------------------------------------- |
| **Probe date**               | 2026-05-12                                                                                      |
| **Device**                   | CP2104 USB-to-UART bridge at `/dev/ttyUSB0`                                                     |
| **Stable by-id path**        | `Silicon_Labs_CP2104_USB_to_UART_Bridge_Controller_*/if00-port0`                                |
| **Likely hardware**          | T-Beam (CP2104 is typical T-Beam USB-UART bridge)                                               |
| **Serial chatter**           | None observed at 9600 or 115200 baud. Device may be unflashed or running non-MeshCore firmware. |
| **esptool availability**     | Available via pipx. `esptool chip_id` not yet run.                                              |
| **MeshCore firmware source** | Available at `/home/jeremiah/dev` (local source repo)                                           |
| **MeshCore Python library**  | Available at `/home/jeremiah/dev` (local source repo)                                           |
| **Follow-up status**         | **Pending** — firmware flash attempt required before live test                                  |

### 3.2 MeshCore Known Limitations (confirmed from source)

- No inbound message reception from a second node.
- No multi-hop mesh delivery testing.
- No bridge compatibility with Meshtastic.
- No BLE connectivity with PIN pairing tested.
- No reconnection handling under real network conditions.
- Duplicate-send risk acknowledged (session retries up to 3 times).
- **Hardware gap:** CP2104 device at `/dev/ttyUSB0` identified but produces no serial chatter. Firmware flash required before any MeshCore interaction is possible. This is a specific, documented gap — not a vague blocker.

## 4. LXMF/Reticulum Operational Evidence

> **Evidence tier:** NOT EXECUTED. No live evidence of any tier. S-tier unit tests pass.
> **Live procedures:** `docs/runbooks/live-operational-evidence.md` §2.15.
> **Context (2026-05-12):** Local source repos available at `/home/jeremiah/dev` for LXMF and Reticulum. Reticulum live path setup (install from source, configure transport, generate identity) pending follow-up validation.
> **Hardware probe (2026-05-12):** RNode KISS probe to ttyUSB0 (CP2104) returned NO RESPONSE at 115200 and 57600 baud. Serial path BLOCKED for LXMF/Reticulum. Requires RNode firmware verification or alternative transport interface.
> **Maturity:** Experimental / SDK-validated per Contract 62 §3.4. Cannot promote until Reticulum live path validated and delivery state model confirmed against real network.

### 4.1 Live Smoke Test Evidence (Tier: NOT EXECUTED)

| Field                         | Value                               |
| ----------------------------- | ----------------------------------- |
| **Test file**                 | `tests/test_lxmf_live.py`           |
| **Last execution date**       | **NOT EXECUTED**                    |
| **Executor**                  | **NOT EXECUTED**                    |
| **Connection type**           | **NOT EXECUTED** (`reticulum`)      |
| **RNS version**               | **NOT EXECUTED**                    |
| **lxmf version**              | **NOT EXECUTED**                    |
| **Identity source**           | **NOT EXECUTED** (loaded/generated) |
| **MEDRE commit**              | **NOT EXECUTED**                    |
| **Python version**            | **NOT EXECUTED**                    |
| **Environment**               | **NOT EXECUTED**                    |
| **Total tests run**           | **NOT EXECUTED**                    |
| **Passed / Failed / Skipped** | **NOT EXECUTED**                    |
| **Fake mode lifecycle**       | **NOT EXECUTED**                    |
| **Real mode start/connect**   | **NOT EXECUTED**                    |
| **Real mode deliver**         | **NOT EXECUTED**                    |
| **Inbound callback received** | **NOT EXECUTED**                    |
| **Diagnostics snapshot**      | **NOT EXECUTED**                    |
| **Stop → clean teardown**     | **NOT EXECUTED**                    |
| **Caveats observed**          | **NOT EXECUTED**                    |
| **Reconnect observations**    | **NOT EXECUTED**                    |

### 4.1b Local Source Repos (2026-05-12)

> **Not live-transport evidence.** Documents available source code for Reticulum/LXMF follow-up setup.

| Resource                | Location                                 | Notes                              |
| ----------------------- | ---------------------------------------- | ---------------------------------- |
| LXMF source             | `/home/jeremiah/dev` (local source repo) | Available for `pip install -e`     |
| Reticulum source        | `/home/jeremiah/dev` (local source repo) | Available for `pip install -e`     |
| MeshCore firmware       | `/home/jeremiah/dev` (local source repo) | For MeshCore device, not LXMF      |
| MeshCore Python library | `/home/jeremiah/dev` (local source repo) | For MeshCore adapter, not LXMF     |
| pipx preference         | User prefers pipx for PyPI CLI tools     | esptool already available via pipx |

**Setup steps for LXMF live path:**

1. Install Reticulum from local source: `pip install -e /path/to/rns-source`
2. Install LXMF from local source: `pip install -e /path/to/lxmf-source`
3. Configure Reticulum transport (local TCP or serial interface)
4. Generate identity file: `LXMF_CONNECTION_TYPE=reticulum` + `LXMF_IDENTITY_PATH=/path/to/identity.key`
5. Run `pytest tests/test_lxmf_live.py -m live -v`
6. Record R-tier evidence here

### 4.2 LXMF Known Limitations (confirmed from source)

- No synchronous delivery confirmation. Outbound returns `OUTBOUND` state.
- No inbound from a separate, independent Reticulum instance.
- No propagation node store-and-forward testing.
- No multi-hop mesh delivery testing across heterogeneous transports.
- No resource transfer for large messages.
- Production deployment readiness is not claimed.
- **Reticulum live path gap:** Local source repos available but no Reticulum instance configured. This is a specific, documented gap — not a vague blocker. Requires follow-up: install from source, configure transport, run live test.
- **Delivery state model unvalidated:** The `OUTBOUND → SENDING → SENT → DELIVERED` progression (1,260 LOC session) is implemented but never observed against real Reticulum infrastructure. Experimental downgrade risk per Contract 62 §5.4 if live path reveals fundamental issues.

## 5. Deterministic Test Evidence (Tier: S — confirmed)

This section records evidence from deterministic/unit tests that do not require
live services. These are confirmed from CI runs.

### 5.1 Current Evidence (as of 2026-05-11)

| Field                    | Value                                       |
| ------------------------ | ------------------------------------------- |
| **Test command**         | `pytest` (default, no live)                 |
| **Last confirmed date**  | 2026-05-11                                  |
| **Passed**               | 3237                                        |
| **Skipped**              | 4                                           |
| **Deselected**           | 63 (live + soak tests excluded by default)  |
| **compileall**           | Clean (`python -m compileall -q src tests`) |
| **All adapters covered** | Yes (Matrix, Meshtastic, MeshCore, LXMF)    |

### 5.2 Historical Evidence (superseded)

> The following counts are from a prior run and are preserved for traceability.
> They are NOT the current evidence — use §5.1 above for current numbers.

| Field           | Value                                          |
| --------------- | ---------------------------------------------- |
| **Run date**    | 2026-05-10                                     |
| **Total tests** | 2076 (including 27 resource containment tests) |
| **Passed**      | 2076                                           |
| **Failed**      | 0                                              |
| **Deselected**  | 61 (live + soak tests excluded by default)     |
| **compileall**  | Clean                                          |

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

| Transport  | Evidence type       | Status       | Reason                                                                                                                                                                           | Required command                                                                       | Required env vars                                                              |
| ---------- | ------------------- | ------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| Matrix     | Soak test           | NOT EXECUTED | No sustained Matrix session executed against real homeserver                                                                                                                     | `SOAK_DURATION_SECONDS=30 pytest tests/test_soak.py::TestMatrixSoak -m live -v -s`     | `MATRIX_HOMESERVER`, `MATRIX_USER_ID`, `MATRIX_ACCESS_TOKEN`, `MATRIX_ROOM_ID` |
| Meshtastic | Soak test           | NOT EXECUTED | No sustained Meshtastic session executed against real hardware                                                                                                                   | `SOAK_DURATION_SECONDS=30 pytest tests/test_soak.py::TestMeshtasticSoak -m live -v -s` | `MESHTASTIC_CONNECTION_TYPE`, `MESHTASTIC_HOST` or `MESHTASTIC_SERIAL_PORT`    |
| Meshtastic | Second-node inbound | NOT EXECUTED | No second Meshtastic node available in test environment                                                                                                                          | (same as Meshtastic live smoke, with second node transmitting)                         | Same as Meshtastic live + second node on same channel                          |
| MeshCore   | Live smoke          | NOT EXECUTED | CP2104 `/dev/ttyUSB0` identified (stable by-id, likely T-Beam). No serial chatter observed. MeshCore firmware source available at `/home/jeremiah/dev`. Firmware flash required. | `pytest tests/test_meshcore_live.py -m live -v`                                        | `MESHCORE_CONNECTION_TYPE`, `MESHCORE_HOST` or `MESHCORE_SERIAL_PORT`          |
| MeshCore   | Soak test           | NOT EXECUTED | No soak test class exists for MeshCore; hardware not yet flashed                                                                                                                 | N/A (test class does not exist yet)                                                    | N/A                                                                            |
| LXMF       | Live smoke          | NOT EXECUTED | Local source repos for Reticulum and LXMF available at `/home/jeremiah/dev`. Reticulum not yet installed or configured. Follow-up setup required.                                | `pytest tests/test_lxmf_live.py -m live -v`                                            | `LXMF_CONNECTION_TYPE`, `LXMF_IDENTITY_PATH`                                   |
| LXMF       | Soak test           | NOT EXECUTED | No soak test class exists for LXMF; Reticulum not configured                                                                                                                     | N/A (test class does not exist yet)                                                    | N/A                                                                            |

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
8. Ensure every new evidence entry includes a `tier` field per
   `docs/contracts/61-operational-evidence-contract.md` §2.
9. Record longrun evidence in `docs/runbooks/longrun-validation.md` §5.

## 9. Cross-References

| Document                                                 | Relationship                                           |
| -------------------------------------------------------- | ------------------------------------------------------ |
| `docs/contracts/61-operational-evidence-contract.md`     | Evidence schema, classification tiers, required fields |
| `docs/runbooks/live-operational-evidence.md`             | Detailed Matrix and Meshtastic live procedures         |
| `docs/runbooks/longrun-validation.md`                    | Longrun evidence capture and recording                 |
| `docs/runbooks/soak-testing.md`                          | Soak harness infrastructure and procedures             |
| `docs/contracts/32-beta-readiness-checklist.md`          | Beta entry criteria referencing evidence status        |
| `docs/contracts/37-transport-maturity-classification.md` | Transport maturity tiers using evidence scores         |
| `docs/contracts/39-operational-risk-register.md`         | Risk register informed by evidence gaps                |
| `docs/contracts/48-runtime-observability-contract.md`    | Diagnostics field definitions                          |
| `docs/contracts/59-runtime-durability-contract.md`       | Durability claims requiring evidence                   |
