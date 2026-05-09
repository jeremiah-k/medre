# Matrix Live Smoke Test Runbook

> Last updated: 2026-05-09
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

What live smoke does **not** prove:

- Inbound message reception (requires a second actor to send a message).
- Self-message suppression with real sync echoes (timing-sensitive).
- MEDRE-origin envelope suppression (secondary check; unit-tested).
- E2EE, reactions, edits, deletes, attachments, media.
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

If any variable is unset, all live tests skip with a descriptive message.


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
5. The room should be **unencrypted** (E2EE is not supported in tranche 1).


## Running the Tests

```bash
# Install the Matrix adapter dependency
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


## Cleanup

After running tests:

1. **No persistent state is created.**  Test messages are sent to the room
   but no files, databases, or configuration are written by the test
   harness.

2. **Test messages remain in the room.**  You may optionally redact them
   via your Matrix client.

3. **Unset environment variables** if running in a shared environment:

   ```bash
   unset MATRIX_HOMESERVER MATRIX_USER_ID MATRIX_ACCESS_TOKEN MATRIX_ROOM_ID
   ```

4. **Stop the homeserver** if started locally for testing.


## E2EE Statement

**End-to-end encryption is not supported.**  The Matrix adapter in
tranche 1 does not implement E2EE.  Live smoke tests target
**unencrypted rooms only**.  Do not attempt to run these tests against
an encrypted room — the adapter will not be able to decrypt inbound
messages or encrypt outbound messages.  E2EE support is deferred to a
future tranche.  Selecting `mindroom-nio` (which has olm/megolm support
in its codebase) does not activate E2EE in this tranche.

**Plaintext alpha vs future E2EE production posture.** In plaintext
alpha mode, `store_path` and `device_id` are optional (no crypto state
to persist). Future E2EE production mode will require both: `store_path`
to persist Olm/Megolm session keys across restarts, and `device_id` for
stable device identification. Docker deployments should install
`mindroom-nio[e2e]` once E2EE mode is implemented; missing E2EE
dependencies in encrypted mode should fail clearly rather than silently
falling back to plaintext. Cross-signing/verification and room key
backup/import/export remain deferred. See the alpha operation runbook
(`docs/runbooks/matrix-alpha-operation.md`, section 8) for full
posture details.


## Explicit Scope Exclusions

The following are explicitly **out of scope** for the live smoke harness
and the Matrix tranche 1 adapter:

- End-to-end encryption (E2EE)
- Reactions (`m.annotation`)
- Edits (`m.replace`)
- Deletes / redactions (`m.redaction`)
- Attachments / media (`m.file`, `m.image`, `m.audio`, `m.video`)
- Admin API
- Webhooks or HTTP server
- Meshtastic, MeshCore, LXMF, or any non-Matrix connectivity
- Auth command / interactive login / credential storage
- Room creation / management
- Federation testing
- Multi-user scenarios (requires a second actor)
