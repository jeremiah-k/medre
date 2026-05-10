# Matrix Live Smoke Test Runbook

> Last updated: 2026-05-10
> Scope: `tests/test_matrix_live.py`

This runbook describes how to run the Matrix live smoke tests against a
real Matrix homeserver, what the tests cover, and what they do not cover.


## Purpose

The live smoke harness validates that the MEDRE Matrix adapter works
against a real Matrix homeserver — not just against the
`FakeMatrixAdapter` and mock-based unit tests.  It is **optional** and
**skipped by default**.  Default `pytest` runs remain fake-only.

What live smoke proves:

- The adapter can connect to a real homeserver using an access token.
- `health_check()` transitions correctly through the lifecycle.
- Outbound `room_send` produces a real `event_id` starting with `$`.
- The adapter starts, sends, and stops cleanly without leaking tasks.
- The adapter reconnects automatically on transient sync failures (bounded attempts, exponential backoff).
- Stop/start cycles preserve adapter state; a second `start()` after `stop()` re-establishes sync.
- Health reports `degraded` during reconnect cycles and `healthy` after recovery.

What live smoke does **not** prove:

- Inbound message reception (requires a second actor to send a message).
- Self-message suppression with real sync echoes (timing-sensitive).
- MEDRE-origin envelope suppression (secondary check; unit-tested).
- E2EE reactions, edits, deletes, attachments, media (text E2EE is covered by the E2EE harness, see section below).
- Admin API, webhooks, or any non-text Matrix features.
- Meshtastic, MeshCore, LXMF, or any non-Matrix adapter connectivity.
- Production credential management or token rotation.


## Required Environment Variables

| Variable                 | Example                       | Description                          |
|--------------------------|-------------------------------|--------------------------------------|
| `MATRIX_HOMESERVER`      | `http://localhost:8008`       | Full URL of the Matrix homeserver    |
| `MATRIX_USER_ID`         | `@bot:localhost`              | Fully-qualified Matrix user ID       |
| `MATRIX_ACCESS_TOKEN`    | `syt_xxxxxxxxxxxxx`           | Access token for the bot account     |
| `MATRIX_ROOM_ID`         | `!abc123:localhost`           | Room ID to send test messages to     |
| `MATRIX_DEVICE_ID`       | `DEVICEABC`                   | Stable device ID (required for E2EE harness) |
| `MATRIX_STORE_PATH`      | `/tmp/nio-store`              | Crypto store directory (required for E2EE harness) |

If any of the first four variables is unset, all live tests skip with a descriptive message. E2EE-specific tests additionally require `MATRIX_DEVICE_ID` and `MATRIX_STORE_PATH` and an encrypted room.


## Local Homeserver Setup

You do **not** need Docker.  Both Synapse and Conduit can run locally.

### Option 1: Synapse via pip (recommended)

```bash
# Install Synapse
pip install matrix-synapse

# Generate a minimal config
python -m synapse.app.homeserver \
  --server-name localhost \
  --config-path homeserver.yaml \
  --generate-config \
  --report-stats=no

# Start Synapse (default port 8008)
python -m synapse.app.homeserver --config-path homeserver.yaml

# Register a bot user
register_new_matrix_user \
  -c homeserver.yaml \
  -u bot -p secret \
  http://localhost:8008

# Obtain an access token
curl -s -X POST \
  -d '{"type":"m.login.password","user":"bot","password":"secret"}' \
  http://localhost:8008/_matrix/client/v3/login
# The response JSON contains "access_token": "syt_..."
```

### Option 2: Conduit (lightweight Rust homeserver)

```bash
# Download binary from https://conduit.rs
# Or build from https://gitlab.com/famedly/conduit
./conduit  # starts on port 6167 by default

# Register a user via any Matrix client (Element, etc.)
# Set MATRIX_HOMESERVER="http://localhost:6167"
```

### Option 3: Docker (optional)

```bash
docker run -d --name synapse -p 8008:8008 \
  -e SYNAPSE_SERVER_NAME=localhost \
  -e SYNAPSE_REPORT_STATS=no \
  matrixdotorg/synapse:latest

docker exec synapse register_new_matrix_user \
  -u bot -p secret -c /data/homeserver.yaml http://localhost:8008

curl -s -X POST \
  -d '{"type":"m.login.password","user":"bot","password":"secret"}' \
  http://localhost:8008/_matrix/client/v3/login
```

> **Note:** Docker is optional.  The primary instructions use pip (Synapse)
> or a native binary (Conduit).  No Docker dependency is required.


## Account and Token Setup

1. **Create a dedicated bot account** on your homeserver.  Do not use your
   personal Matrix account for testing.

2. **Obtain an access token** via:
   - The login API endpoint (`/_matrix/client/v3/login`), or
   - Element → Settings → Help & About → Access Token.

3. **Do not commit or log the token.**  The live test file uses
   placeholders only.  Set the token via an environment variable.

4. **Future note:** A mmrelay-like `auth` command for interactive login
   and credential management may be useful in a future tranche, but the
   current tranche uses environment-variable access tokens exclusively.


## Room Setup

1. **Create a test room** using your Matrix client (Element, etc.).
2. **Invite the bot user** to the room.
3. **Ensure the bot has joined** the room.
4. **Copy the room ID** (format: `!opaque:server`).  Set it as
   `MATRIX_ROOM_ID`.
5. The room should be **unencrypted** for the base live smoke tests.
6. For E2EE harness tests: create a separate encrypted room (enable encryption in room settings in Element). The bot must be joined to it. This room is used only by E2EE-specific tests.


## Running the Tests

```bash
# Install the Matrix adapter dependency (plaintext alpha)
pip install -e ".[matrix]"

# Set environment variables
export MATRIX_HOMESERVER="http://localhost:8008"
export MATRIX_USER_ID="@bot:localhost"
export MATRIX_ACCESS_TOKEN="syt_..."
export MATRIX_ROOM_ID="!room:localhost"

# Run live tests only
pytest tests/test_matrix_live.py -m live -v

# Run all tests EXCEPT live (default behavior)
pytest
```

### Running E2EE live tests

E2EE live tests are skipped by default alongside the base live tests. To run E2EE live tests:

```bash
# Install E2EE dependencies
pip install -e ".[matrix-e2e]"

# Set all environment variables including E2EE-specific ones
export MATRIX_HOMESERVER="http://localhost:8008"
export MATRIX_USER_ID="@bot:localhost"
export MATRIX_ACCESS_TOKEN="syt_..."
export MATRIX_ROOM_ID="!encrypted-room:localhost"   # must be an encrypted room
export MATRIX_DEVICE_ID="MEDRE_SMOKE_01"
export MATRIX_STORE_PATH="/tmp/nio-smoke-store"

# Run live tests including E2EE
pytest tests/test_matrix_live.py -m live -v

# Run everything including live
pytest -m ""
```

### Expected Output (successful run)

```
tests/test_matrix_live.py::TestMatrixLiveSmoke::test_adapter_starts_and_reports_healthy PASSED
tests/test_matrix_live.py::TestMatrixLiveSmoke::test_adapter_health_unknown_after_stop PASSED
tests/test_matrix_live.py::TestMatrixLiveSmoke::test_adapter_health_unknown_before_start PASSED
tests/test_matrix_live.py::TestMatrixLiveSmoke::test_send_text_message_captures_event_id PASSED
tests/test_matrix_live.py::TestMatrixLiveSmoke::test_full_lifecycle_start_send_stop PASSED
tests/test_matrix_live.py::TestMatrixLiveSmoke::test_self_message_suppression_note PASSED
tests/test_matrix_live.py::TestMatrixLiveSmoke::test_medre_origin_envelope_suppression_note PASSED
```

### Expected Output (missing env vars — skip behavior)

```
tests/test_matrix_live.py::TestMatrixLiveSmoke::test_adapter_starts_and_reports_healthy SKIPPED
tests/test_matrix_live.py::TestMatrixLiveSmoke::test_adapter_health_unknown_after_stop SKIPPED
...
7 skipped in X.XXs
```

With reason: *"Set MATRIX_HOMESERVER, MATRIX_USER_ID, MATRIX_ACCESS_TOKEN,
and MATRIX_ROOM_ID env vars to run live Matrix tests"*


## Common Failures

| Symptom                             | Cause                                    | Fix                                                   |
|-------------------------------------|------------------------------------------|-------------------------------------------------------|
| `MatrixConnectionError: mindroom-nio not installed` | Missing dependency | `pip install -e ".[matrix]"`                         |
| `MatrixConnectionError: failed to authenticate` | Bad token or user ID | Verify token via `curl` login; check user ID format (`@bot:server`) |
| `MatrixSendError: no room_id`       | Missing `MATRIX_ROOM_ID`                 | Set the room ID env var; ensure bot has joined the room |
| `assert info.health == "healthy"` fails | Homeserver unreachable or token expired | Check homeserver URL; regenerate token                |
| `native_message_id is None`         | Homeserver returned error response       | Check homeserver logs; verify room membership         |
| All tests SKIP                      | Env vars not set                         | Set all four `MATRIX_*` environment variables         |
| Health stays `degraded`             | Reconnect cycle in progress              | Wait for reconnect or check homeserver availability   |
| Health `failed` after restart test  | Token expired during test                | Regenerate token; re-export env var                   |


## Cleanup

After running tests:

1. **No persistent state is created by plaintext tests.** Test messages are sent to the room but no files, databases, or configuration are written by the test harness.

2. **E2EE tests create a crypto store.** The `MATRIX_STORE_PATH` directory will contain a SQLite database after E2EE tests run. This is the nio crypto store. Deleting it means the next run will create a new crypto identity.

3. **Test messages remain in the room.** You may optionally redact them via your Matrix client.

4. **Unset environment variables** if running in a shared environment:

   ```bash
   unset MATRIX_HOMESERVER MATRIX_USER_ID MATRIX_ACCESS_TOKEN MATRIX_ROOM_ID MATRIX_DEVICE_ID MATRIX_STORE_PATH
   ```

5. **Stop the homeserver** if started locally for testing.


## E2EE Statement

**E2EE text alpha is now available.** The Matrix adapter supports encrypted rooms for text messages when installed with `pip install -e ".[matrix-e2e]"` and configured with `store_path` + `device_id`. See the E2EE harness section below for live test instructions.

**Plaintext alpha remains the primary path.** Base live smoke tests target **unencrypted rooms only** and work with `pip install -e ".[matrix]"` (no crypto libs). Plaintext rooms work identically in both modes.

**E2EE text alpha scope:**
- Inbound: encrypted messages auto-decrypted to `RoomMessageText` during sync.
- Outbound: `room_send` auto-encrypts for encrypted rooms.
- Key lifecycle: automatic via `sync_forever`.
- Crypto store: persisted under `store_path`, loaded on `restore_login`.

**Unsupported in E2EE text alpha:**
- Reactions, edits, media, attachments.
- Cross-signing, key backup, key import/export.
- Interactive device verification (emoji/QR).
- Unverified device policy: `ignore_unverified_devices` is now an explicit `MatrixConfig` field (default `False`). Live E2EE tests set `ignore_unverified_devices=True` in config.

**Implemented in E2EE text alpha:**
- Undecryptable event handling: `MegolmEvent` callback counts events, logs warning (event_id/room_id only, no session_id), does not forward to canonical pipeline.
- `RoomEncryptionEvent` callback sets `encrypted_room_seen`, logs at INFO level, does not forward.
- Diagnostics: `undecryptable_event_count`, `last_crypto_error`, `encrypted_room_seen` — exclude session_id, keys, and tokens.

See the alpha operation runbook (`docs/runbooks/matrix-alpha-operation.md`, sections 8 and 13) and the E2EE readiness contract (`docs/contracts/25-matrix-e2ee-readiness.md`) for full posture details.


## E2EE Live Harness

### Prerequisites

- `pip install -e ".[matrix-e2e]"` (installs `mindroom-nio[e2e]` with crypto libs).
- An encrypted Matrix room (created via Element, encryption enabled in room settings).
- The bot user joined to the encrypted room.
- A persistent `store_path` directory (not `/tmp` if you want state across runs).
- A stable `device_id`.

### Environment Variables

| Variable | Required | Example | Notes |
|----------|----------|---------|-------|
| `MATRIX_HOMESERVER` | Yes | `http://localhost:8008` | Full URL |
| `MATRIX_USER_ID` | Yes | `@bot:localhost` | Bot's user ID |
| `MATRIX_ACCESS_TOKEN` | Yes | `syt_...` | Bot's access token |
| `MATRIX_ROOM_ID` | Yes | `!encrypted:localhost` | Must be an encrypted room |
| `MATRIX_DEVICE_ID` | Yes | `MEDRE_SMOKE_01` | Stable device ID |
| `MATRIX_STORE_PATH` | Yes | `/tmp/nio-smoke-store` | Writable directory for crypto store |

### Running

```bash
pip install -e ".[matrix-e2e]"

export MATRIX_HOMESERVER="http://localhost:8008"
export MATRIX_USER_ID="@bot:localhost"
export MATRIX_ACCESS_TOKEN="syt_..."
export MATRIX_ROOM_ID="!encrypted:localhost"
export MATRIX_DEVICE_ID="MEDRE_SMOKE_01"
export MATRIX_STORE_PATH="/path/to/nio-smoke-store"

pytest tests/test_matrix_live.py -m live -v
```

### What to expect on first run

1. The adapter creates a new crypto store in `MATRIX_STORE_PATH`.
2. First `sync_forever` iteration uploads device keys (identity + one-time keys) to the homeserver.
3. The adapter's device appears as an unverified device on the bot's account.
4. Outbound `room_send` to the encrypted room auto-encrypts. The first send may take longer as it shares the Megolm session with room members.
5. Inbound encrypted messages from other users may not decrypt until the sender's client encrypts for the adapter's device (typically the next message after the adapter uploads keys).

### What to expect on subsequent runs

1. `restore_login` loads the existing crypto store from `MATRIX_STORE_PATH`.
2. All previously received room keys are available.
3. Decryption of historical and new messages works immediately.
4. Device identity is preserved (same `device_id`).

### First run vs subsequent runs — quick reference

| Aspect | First run | Subsequent runs |
|--------|-----------|-----------------|
| Crypto store | Created fresh | Loaded from disk |
| Device keys | Uploaded to homeserver | Already registered |
| Room keys | Not yet distributed | Available from store |
| Inbound decryption | May fail until sender re-encrypts | Works immediately |
| Outbound encryption | Works (auto-shares session) | Works (session in store) |


## Stop/Start Cycle and Reconnect Tests

The live smoke harness includes tests that validate adapter behavior across lifecycle transitions and transient failure recovery.

### Stop/start cycle expectations

1. **Start → healthy.** After `start()`, `health_check()` returns `"healthy"` with `connected=True`, `logged_in=True`, `sync_task_running=True`.
2. **Stop → unknown.** After `stop()`, `health_check()` returns `"unknown"`. No sync task is running. The nio client is closed.
3. **Restart → healthy.** Calling `start()` again after `stop()` re-creates the nio client, restores login, and starts a fresh sync loop. `health_check()` returns `"healthy"`.
4. **No task leaks.** After `stop()`, no lingering asyncio tasks from the adapter. Verified by comparing `asyncio.all_tasks()` before and after.
5. **Crypto continuity on restart (E2EE).** When `store_path` and `device_id` are stable, restarting the adapter preserves the crypto identity. `crypto_store_loaded` is `True` on the second start.

### Reconnect behavior expectations

1. **Transient failure → auto-reconnect.** When a sync iteration fails with a transient error, the adapter does not enter `failed` immediately. It begins a reconnect cycle.
2. **Health during reconnect.** `health_check()` returns `"degraded"` with `reconnecting=True` and `reconnect_attempts > 0` during the reconnect cycle.
3. **Recovery.** When the underlying issue resolves (homeserver comes back, network recovers), the next reconnect attempt succeeds. `health_check()` returns `"healthy"` with `reconnecting=False`, `reconnect_attempts=0`.
4. **Budget exhaustion → failed.** If the reconnect budget is exhausted without a successful sync, the adapter transitions to `"failed"` state. Manual restart required.
5. **Permanent error → failed.** Permanent errors (expired token, deactivated account) are not retried. The adapter enters `"failed"` immediately.

### What reconnect tests do NOT cover

- Simulating specific network failure modes (requires network simulation tooling).
- Verifying no message loss during reconnect gaps (requires a second actor).
- Measuring backoff timing precision (requires time-sensitive assertions).
- Recovery from process-level crashes (requires external supervisor).


## Live Validation Evidence

### Test Results

- **File:** `tests/test_matrix_live.py` (also `tests/test_matrix_e2ee_live.py`, `tests/test_soak.py::TestMatrixSoak`)
- **Last run:** 2026-05-10
- **Executor:** Live agent (automated)
- **Command:** `pytest tests/test_matrix_live.py -m live -v`
- **MEDRE commit:** Pre-beta HEAD (2026-05-10)
- **Python version:** 3.12
- **mindroom-nio version:** Installed via `pip install -e ".[matrix]"`
- **Homeserver:** matrix.org (public homeserver)
- **Environment:** Local development machine
- **Result:** ✅ **13 passed**, 0 failed, 0 skipped
- **Non-live regression:** 202 passed, 0 failed
- **Start/connect:** ✅ `restore_login` succeeded, sync task running
- **Health check → healthy:** ✅ `info.health == "healthy"`, `info.platform == "matrix"`
- **Room join:** ✅ Room `!sRlwdLCwIGBpSzoRsV:matrix.org` joined
- **Room encryption status:** Unencrypted (plaintext alpha path)
- **Outbound send → event_id:** ✅ `room_send` returned event_id starting with `$`
- **Self-message suppression:** ✅ Own messages suppressed by sender match
- **Stop → clean teardown:** ✅ No leaked tasks after `stop()`
- **Reconnect observations:** ✅ Health reports `degraded` during reconnect, `healthy` after recovery. Budget exhaustion → `failed`.
- **Restart idempotency:** ✅ Stop → start cycle re-establishes sync; second `health_check()` returns `healthy`
- **Caveats observed:** Initial harness had a bug where `health_check()` was awaited as a coroutine instead of called as a regular method. Fixed in-tree before final run. No remaining issues.
- **Soak test result:** **NOT EXECUTED** (see `tests/test_soak.py::TestMatrixSoak`)

### E2EE Evidence

- **File:** `tests/test_matrix_e2ee_live.py`
- **Last run:** 2026-05-10
- **E2EE mode:** `e2ee_required` config used
- **mindroom-nio[e2e] version:** Installed via `pip install -e ".[matrix-e2e]"`
- **vodozemac version:** Pulled as dependency
- **Result:** ✅ **7 passed**, 0 failed, 0 skipped
- **Crypto store loaded:** ✅ `crypto_store_loaded == True`
- **Baseline (unencrypted room in E2EE mode):** Tests validated startup with crypto deps loaded. `crypto_store_loaded == True`. No undecryptable events.
- **Encrypted-room follow-up (room `!rnmyZMhUoraPwZUDPP:matrix.org`):**
  - **Pre-fix:** 2 tests failed with `OlmUnverifiedDeviceError`. nio's strict default (`ignore_unverified_devices=False`) blocked Megolm session key sharing because the bot's device was not verified by other room members.
  - **Root cause:** No cross-signing support in `mindroom-nio`. Device verification via cross-signing is not implemented.
  - **Fix applied:** Adapter configured `ignore_unverified_devices=True` in `room_send()`.
  - **Post-fix re-test:** Full suite 7/7 pass in 3.73s. Encrypted send succeeded, event_id returned.
- **Trust tradeoff note:** Setting `ignore_unverified_devices=True` bypasses nio's verified-device check. This is an intentional tradeoff for MEDRE alpha: the Olm/Megolm stack initializes correctly, keys are uploaded, and messages are encrypted in transit, but there is no cryptographic guarantee that the receiving device is the intended one. Production device verification is deferred to a future tranche. See `docs/contracts/25-matrix-e2ee-readiness.md` §5.2 for rationale.


## Explicit Scope Exclusions

The following are explicitly **out of scope** for the live smoke harness
and the Matrix adapter (both plaintext and E2EE text alpha):

- E2EE reactions, edits, deletes, attachments, media (text E2EE is in scope)
- Cross-signing, key backup, key import/export
- Interactive device verification (emoji/QR)
- Admin API
- Webhooks or HTTP server
- Meshtastic, MeshCore, LXMF, or any non-Matrix connectivity
- Auth command / interactive login / credential storage
- Room creation / management
- Federation testing
- Multi-user scenarios (requires a second actor)
