# Matrix Alpha Operation Runbook

> Last updated: 2026-05-09
> Scope: Real Matrix Operation Alpha (Track 7)
> Status: Alpha. Not production. Not hardened. Not complete. Plaintext only — E2EE posture documented for future reference.

This runbook describes how to run MEDRE against a real Matrix homeserver in alpha mode. Alpha mode means the MatrixAdapter connects to a real homeserver using real credentials, syncs real rooms, sends real messages, and receives real events. It does not mean the system is ready for anything beyond a single operator on a local or test homeserver.

Everything in this document is conservative. If something has not been tested against a real homeserver and confirmed working, this document says so. If something is known to be broken or missing, this document says that too.


## 1. Purpose

Alpha operation validates that the MEDRE Matrix adapter works end to end against a real Matrix homeserver with real network calls. This is the first time the adapter leaves mock and fake territory.

Scope boundaries:

- One transport: Matrix. No other transports are in scope for this runbook.
- One operator: a single person running against a local or test homeserver.
- Plain text messages and replies only. Nothing else.
- No production deployment, no scaling, no monitoring, no alerting.
- No claims about reliability, durability, or correctness beyond what manual testing confirms.

This runbook complements `docs/runbooks/matrix-live-smoke.md`. The smoke test validates adapter methods in isolation. Alpha operation validates the full wiring: config, adapter, codec, inbound, outbound, and health, running together.


## 2. Prerequisites

| Requirement | Details |
|------------|---------|
| Matrix homeserver | Synapse or Conduit, local or reachable over the network |
| Bot account | A dedicated Matrix user, not your personal account |
| Python | 3.11 or later |
| Package install | `pip install -e ".[matrix]"` (installs `mindroom-nio`) |
| Access token | Obtained via login API or Element UI |
| A test room | Unencrypted, bot has joined it |
| Network access | Your machine can reach the homeserver's HTTP(S) port |

You do not need Docker. You do not need a domain name. You do not need federation. A local homeserver on localhost is sufficient.


## 3. Homeserver Setup

You need a Matrix homeserver that the MEDRE process can reach. For alpha, local is fine.

### 3.1 Synapse via pip (recommended)

```bash
pip install matrix-synapse

python -m synapse.app.homeserver \
  --server-name localhost \
  --config-path homeserver.yaml \
  --generate-config \
  --report-stats=no

python -m synapse.app.homeserver --config-path homeserver.yaml
```

Synapse starts on port 8008 by default.

### 3.2 Conduit (lightweight alternative)

Download a binary from conduit.rs or build from source. Conduit starts on port 6167 by default. Registration happens through any Matrix client pointed at it.

### 3.3 Docker (optional)

```bash
docker run -d --name synapse -p 8008:8008 \
  -e SYNAPSE_SERVER_NAME=localhost \
  -e SYNAPSE_REPORT_STATS=no \
  matrixdotorg/synapse:latest
```

Docker is not required. It is listed here because some people prefer it. The primary path is pip (Synapse) or native binary (Conduit).


## 4. Token Generation

The adapter authenticates with a long-lived access token. There are two ways to get one.

### 4.1 Login API (curl)

```bash
# Register a bot user first (Synapse only)
register_new_matrix_user \
  -c homeserver.yaml \
  -u bot -p secret \
  http://localhost:8008

# Get a token
curl -s -X POST \
  -d '{"type":"m.login.password","user":"bot","password":"secret"}' \
  http://localhost:8008/_matrix/client/v3/login
```

The response JSON includes an `access_token` field. Copy that value.

### 4.2 Element UI

Open Element, log in as the bot user, go to Settings, Help and About, and copy the access token.

### 4.3 Token handling

Do not commit the token. Do not log the token. Do not paste it into chat. Set it as an environment variable and leave it there. The `MatrixConfig.__repr__` method redacts the token in log output, but you are responsible for not leaking it yourself.

> **Note on E2EE.** Alpha authenticates with access tokens over plain HTTP(S). Future versions supporting E2EE will use `mindroom-nio[e2e]` as the dependency, which adds Olm/Megolm crypto libraries. The token handling mechanism will remain the same, but the runner and adapter will need to manage device keys and cross-signing in addition to the access token. E2EE mode will require stable `store_path` and `device_id` configuration (see section 8).


## 5. Room Setup

1. Open a Matrix client (Element, or any other).
2. Create a new room. Give it any name.
3. Invite the bot user to the room.
4. Accept the invite from the bot account (log in as the bot in a second client session or via the join API).
5. Copy the room ID. It looks like `!opaquestring:localhost`. Room aliases (the `#name:server` form) will not work in the allowlist.
6. Confirm the room is unencrypted. E2EE is not yet supported in alpha. If the room has a lock icon in Element, it is encrypted and the adapter will not be able to read message content. E2EE is planned as the future default. See section 13 for details.


## 6. Allowlist Configuration

The adapter accepts an optional `room_allowlist`: a set of room IDs. When set, the inbound callback ignores messages from any room not in the set. When unset (None), the adapter accepts messages from all rooms.

In alpha mode, you should always set the allowlist to exactly the room(s) you intend to monitor. Running without an allowlist means the adapter will process every message from every room the bot has joined, which is almost certainly not what you want during testing.

The allowlist is configured through `MatrixConfig.room_allowlist`. It is a set of room ID strings:

```python
room_allowlist={"!abc123:localhost", "!def456:localhost"}
```

Or None to accept all rooms:

```python
room_allowlist=None
```

The environment variable convention for this is `MATRIX_ROOM_ALLOWLIST`, a comma-separated list of room IDs. The runner (`python -m medre.runner`) parses this into the set automatically. If you are wiring the adapter manually, parse it yourself:

```python
import os

raw = os.environ.get("MATRIX_ROOM_ALLOWLIST", "")
allowlist = set(raw.split(",")) if raw.strip() else None
```


## 7. Running MEDRE in Alpha Mode

### 7.1 Environment variables

The runner reads all configuration from environment variables. The three required variables must be set before starting.

| Variable | Required | Default | Example | Notes |
|----------|----------|---------|---------|-------|
| `MATRIX_HOMESERVER` | Yes | | `http://localhost:8008` | Full URL, no trailing slash |
| `MATRIX_USER_ID` | Yes | | `@bot:localhost` | Must start with `@` |
| `MATRIX_ACCESS_TOKEN` | Yes | | `syt_xxxxxxxxxxxxx` | Keep it secret |
| `MATRIX_ROOM_ALLOWLIST` | No | (all rooms) | `!abc:localhost,!def:localhost` | Comma-separated room IDs. Unset or empty means all rooms are accepted. |
| `MATRIX_ADAPTER_ID` | No | `matrix-alpha` | `my-adapter` | Adapter identifier used in logging and health checks. |
| `MATRIX_DEVICE_ID` | No | | `DEVICEABC` | Device ID for the nio client session. |
| `MATRIX_STORE_PATH` | No | | `/tmp/nio-store` | Filesystem path for the nio crypto/state store directory. |
| `MATRIX_SYNC_TIMEOUT_MS` | No | `30000` | `60000` | Sync long-poll timeout in milliseconds. |
| `MEDRE_DB_PATH` | No | `:memory:` | `/tmp/medre.db` | SQLite database path. Defaults to in-memory (lost on shutdown). |

### 7.2 Running with the runner

`python -m medre.runner` is the primary alpha operation entry point. It wires the full pipeline, handles configuration from environment variables, manages signal-based shutdown, and provides structured logging.

```bash
# Set the required environment variables
export MATRIX_HOMESERVER=http://localhost:8008
export MATRIX_USER_ID=@bot:localhost
export MATRIX_ACCESS_TOKEN=syt_xxxxxxxxxxxxx
export MATRIX_ROOM_ALLOWLIST=!abc123:localhost

# Run
python -m medre.runner
```

The runner does the following in order:

1. Configures logging (INFO level to stderr).
2. Reads and validates all environment variables into a `MatrixConfig`.
3. Creates subsystems: `EventBus`, `RenderingPipeline`, `SQLiteStorage`, `Diagnostician`, `Router`.
4. Registers the `MatrixRenderer` on the rendering pipeline.
5. Creates the `MatrixAdapter` with the validated config.
6. Wires a `PipelineRunner` with all subsystems.
7. Builds an `AdapterContext` connecting the adapter to the pipeline.
8. Registers signal handlers for SIGINT and SIGTERM.
9. Starts the `PipelineRunner`, then starts the `MatrixAdapter`.
10. Logs initial diagnostics (connection state, login state, sync task status).
11. Waits for a shutdown signal.
12. On shutdown: stops the adapter, stops the pipeline runner, closes the database.

The old manual wiring approach (constructing `MatrixConfig` and `AdapterContext` by hand) is documented in Appendix A for developers who need fine-grained control.

### 7.3 Expected startup and shutdown behavior

**Startup.** On a successful start, you should see log lines like this (timestamps omitted):

```
INFO  medre.runner  Matrix Operation Alpha: config loaded for @bot:localhost
INFO  medre.runner  PipelineRunner started
INFO  medre.runner  MatrixAdapter matrix-alpha started
INFO  medre.runner  Initial diagnostics: {'status': 'healthy', 'details': {'connected': True, 'logged_in': True, 'sync_task_running': True, 'last_sync_error': None}}
INFO  medre.runner  Matrix Operation Alpha running — awaiting shutdown signal
```

If you see the "running" line, the runner has:

1. Validated all required environment variables.
2. Created and initialized the SQLite storage.
3. Started the PipelineRunner.
4. Started the MatrixAdapter (nio client created, login restored, sync loop running).
5. Logged initial diagnostics confirming connection, login, and sync task state.

**Shutdown.** Press Ctrl+C (or send SIGTERM) to trigger a graceful shutdown:

```
INFO  medre.runner  Shutdown requested — stopping
INFO  medre.runner  MatrixAdapter stopped
INFO  medre.runner  PipelineRunner stopped
INFO  medre.runner  Matrix Operation Alpha shut down cleanly
```

The runner catches SIGINT and SIGTERM, signals the adapter to stop, then stops the pipeline runner, then closes the database. Any in-flight sync operations are cancelled. See Known Limitation #2 for what this means about in-flight messages.

If you do not see the startup lines above, check the troubleshooting section (section 14) for common configuration and connectivity errors.


## 8. Crypto Store and Device Identity Posture

The Matrix adapter accepts two optional configuration fields that become critical when E2EE is introduced in a future release. In plaintext alpha mode they are safe to omit.

### 8.1 Alpha (plaintext): store_path and device_id are optional

| Field | Env var | Alpha behavior |
|-------|---------|----------------|
| `store_path` | `MATRIX_STORE_PATH` | **Optional.** If unset, nio operates without a persistent crypto store. Plaintext sync and send work normally. No session keys or device data are persisted. |
| `device_id` | `MATRIX_DEVICE_ID` | **Optional.** If unset, nio may receive a default device ID from the homeserver during login restore. Plaintext operation is unaffected. |

Alpha can be run entirely without these fields. The runner (`python -m medre.runner`) does not require them. This is intentional: plaintext alpha has no crypto state to persist.

### 8.2 Future E2EE production: store_path and device_id will be required

When E2EE support is implemented:

- **`store_path` will be required.** The nio crypto store must persist Olm/Megolm session keys, device keys, and cross-signing data across restarts. Without a stable `store_path`, the adapter would lose its crypto identity on every restart, making encrypted rooms unusable.
- **`device_id` will be required.** A stable device ID ensures the homeserver and other clients can identify this adapter's device for key verification and cross-signing.
- **Missing these fields in E2EE mode should fail clearly.** The adapter should refuse to start with a descriptive error rather than silently losing crypto state.

These requirements do not apply in alpha. They are documented here so operators planning a future E2EE deployment know what to expect.

### 8.3 Docker deployments and E2EE dependencies

Alpha Docker deployments install `mindroom-nio` (no E2EE extras):

```
pip install -e ".[matrix]"
```

When E2EE support is implemented, Docker images and deployment scripts should install the E2EE-enabled dependency instead:

```
pip install -e ".[matrix]"  # still works for plaintext
pip install mindroom-nio[e2e]  # required for E2EE rooms
```

The E2EE extra adds Olm/Megolm native crypto libraries. Without it, attempting to operate in encrypted mode should produce a clear error (e.g., `ImportError` or a dedicated check), not a silent fallback to plaintext.

### 8.4 Deferred E2EE capabilities

The following E2EE-related capabilities are **deferred** and not part of any current or near-term release:

- Cross-signing setup and verification flows
- Room key backup
- Room key import/export
- Interactive device verification (emoji/QR)

These will be addressed in a dedicated E2EE readiness contract when E2EE implementation begins. See the future `docs/contracts/25-matrix-e2ee-readiness.md` for the detailed plan.

### 8.5 Future live/manual E2EE test harness

A future live E2EE harness will be needed to validate encrypted room operation against a real homeserver. This harness does not exist yet and no live E2EE tests are required now. The plan is referenced here for tracking:

- The harness will extend the existing live smoke test pattern (`tests/test_matrix_live.py`, `docs/runbooks/matrix-live-smoke.md`).
- It will require a second Matrix account to send encrypted messages into a test room.
- It will validate that the adapter can decrypt inbound messages and encrypt outbound messages.
- It will remain skipped-by-default, gated by environment variables and the `live` pytest marker.

No implementation action is needed for this harness at this time.


## 9. Expected Logs and Diagnostics

### 8.1 Healthy startup

The runner logs a startup sequence. The key line from the adapter is:

```
MatrixAdapter matrix-alpha started
```

Before that, you will see the runner's own startup lines (config loaded, PipelineRunner started). After the adapter starts, the runner logs initial diagnostics and then the "running" line. After that, the sync loop runs silently. There is no periodic "still alive" log. Silence is normal. The sync loop is long-polling the homeserver, waiting for new events.

### 8.2 Inbound message received

When someone sends a message in an allowlisted room, the `_on_room_message` callback fires. On success, you will see nothing in the logs (the event is published to the context silently). On failure, you will see an exception traceback:

```
MatrixAdapter matrix-alpha: error processing inbound event
Traceback (most recent call last):
  ...
```

### 8.3 Self-message suppression

When the bot itself sends a message (including echoes of its own outbound messages via the sync loop), the adapter suppresses them. You will see:

```
MatrixAdapter matrix-alpha: suppressing self-message from @bot:localhost
```

This is logged at DEBUG level. If your logger is configured for INFO or higher, you will not see it.

### 8.4 Sync failure

If the sync loop crashes (network error, auth expiry, homeserver restart), the exception is captured internally. The next call to `health_check()` will return `health="failed"`. The error is logged:

```
MatrixAdapter matrix-alpha: sync task failed: <exception details>
```

### 8.5 Health states

| State | Meaning |
|-------|---------|
| `unknown` | Adapter has not started, or has been stopped |
| `healthy` | Client is connected and logged in |
| `failed` | Sync task has crashed, or client exists but is not logged in |

The adapter does not auto-reconnect. If the sync task fails, the adapter stays in `failed` until someone calls `stop()` and `start()` again.


## 10. Replay Validation Procedure

This section describes how to manually verify that the adapter behaves correctly in both directions.

### 10.1 Outbound validation

1. Start the adapter.
2. Trigger a `deliver()` call with a rendered text message targeting your test room.
3. Open the room in Element (or another client). Confirm the message appears.
4. Check the return value from `deliver()`. It should contain an `event_id` starting with `$` and the correct `room_id`.

### 10.2 Inbound validation

1. Start the adapter with the allowlist set to your test room.
2. From a second Matrix account (not the bot), send a plain text message in the test room.
3. Confirm that `publish_inbound()` is called with a canonical event.
4. Confirm the event's `source_transport_id` matches the sender's MXID, not the bot's.
5. Confirm the event's `channel_id` matches the room ID.

### 10.3 Self-message suppression validation

1. Start the adapter.
2. Send a message through the adapter using `deliver()`.
3. Wait for the sync loop to echo the message back.
4. Confirm that `publish_inbound()` is not called for the echoed message.

### 10.4 Allowlist validation

1. Start the adapter with `room_allowlist` set to room A.
2. Have someone send a message in room A. Confirm it is received.
3. Have someone send a message in room B (which the bot has also joined). Confirm it is not received.

### 10.5 Stop validation

1. Start the adapter. Confirm `health_check()` returns `"healthy"`.
2. Call `stop()`. Confirm `health_check()` returns `"unknown"`.
3. Confirm no lingering asyncio tasks. Use `asyncio.all_tasks()` before and after to check.


## 11. Known Limitations

This is an honest list. Everything here is real.

1. **No auto-reconnect.** If the sync task fails, the adapter stays dead until manually restarted. There is no exponential backoff, no retry loop, no watchdog. You have to call `stop()` then `start()` again yourself.

2. **No graceful shutdown signaling.** The adapter does not drain in-flight messages on stop. `stop()` cancels the sync task and closes the client. Anything in flight is lost.

3. **No inbound queue or persistence.** Inbound events are published directly via `context.publish_inbound()`. If that callback is slow or fails, the event is gone. There is no retry, no dead letter queue, no redelivery.

4. **No rate limiting.** The adapter sends as fast as you call `deliver()`. Matrix homeservers rate-limit by default. If you send too fast, the homeserver will reject messages and you will get `MatrixSendError`.

5. **No connection health monitoring.** The sync loop either runs or it does not. There is no heartbeat, no periodic ping, no health metric beyond the basic `health_check()` states.

6. **Single-room testing only.** Alpha mode has only been validated with one room at a time. Multi-room behavior (multiple allowlisted rooms with concurrent inbound) has not been tested against a real homeserver.

7. **No reconnection on token expiry.** If the access token is revoked or expires, the sync task fails and the adapter enters `failed` state. You need a new token and a manual restart.

8. **No structured logging.** The adapter uses `ctx.logger.info/debug/error` with format strings. There are no structured log fields, no trace IDs, no correlation across events.

9. **No metrics.** There is no Prometheus endpoint, no counters, no histograms. The only observability is the log output and the `health_check()` return value.

10. **Runner is in alpha.** The runner (`python -m medre.runner`) works for testing but has limited error recovery. If a subsystem fails during startup, the runner exits with a traceback rather than attempting partial recovery. There is no watchdog to restart the runner if it crashes.

11. **Plaintext only.** E2EE is not yet supported but is planned as the default for real deployments. Alpha operates on unencrypted rooms only. See section 13 for details.


## 12. Operational Risks

These are things that can go wrong. Read them before running the adapter.

### 12.1 Token leakage

The access token is a long-lived credential. Anyone who has it can impersonate the bot. Risks:

- Setting the token in a shell command that gets logged (e.g., `export MATRIX_ACCESS_TOKEN=syt_...` in a shared terminal).
- Committing `.env` files or scripts containing the token.
- Logging the token accidentally. The adapter redacts it in `__repr__`, but your own code might not.

Mitigation: use a dedicated bot account with minimal room membership. Rotate the token if you suspect it has been exposed. Unset the environment variable when you are done testing.

### 12.2 Sync interruption

The `sync_forever` loop can fail for many reasons: network blips, homeserver restarts, token expiry, resource exhaustion. When it fails, the adapter captures the exception but does not recover automatically. You have to notice the failure (via `health_check()` or the error log) and restart manually.

### 12.3 Missing reconnect logic

There is no reconnect logic. This is stated plainly because it is the most common question. If the connection drops, the adapter does not try again. This is a known limitation, not a bug. Reconnection with backoff and state recovery is future work.

### 12.4 Homeserver resource consumption

The long-polling sync loop holds an HTTP connection open to the homeserver. On Synapse, this is a `/_matrix/client/v3/sync` request with a 30-second timeout. On Conduit, behavior is similar. This is normal Matrix client behavior, but if you run many adapter instances against a small homeserver, you may see performance degradation.

### 12.5 Message ordering

The adapter does not guarantee ordering of outbound messages. If you call `deliver()` twice in rapid succession, the homeserver may process them in either order depending on its internal queueing. The adapter does not sequence or gate outbound sends.

### 12.6 Event duplication

If the adapter restarts after sending a message but before recording the event, the same message may be sent again on restart. The adapter has no deduplication logic for outbound messages. Inbound events may also be delivered twice if the sync token is not persisted between restarts.


## 13. Explicit Unsupported Features

The following features are not supported in alpha mode. Do not attempt to use them. They are listed here so you do not have to wonder.

| Feature | Status | Notes |
|---------|--------|-------|
| End-to-end encryption (E2EE) | Not yet supported in alpha | E2EE is planned as the future default for real deployments. The adapter does not handle encrypted events in alpha. Messages in encrypted rooms will be ignored or produce errors. The planned dependency for E2EE support is `mindroom-nio[e2e]`, which adds Olm/Megolm crypto libraries. Future E2EE production mode will require stable `store_path` and `device_id` (see section 8). Docker deployments should install `mindroom-nio[e2e]` once E2EE mode is implemented. Missing E2EE deps in encrypted mode should fail clearly, not silently fall back to plaintext. See future `docs/contracts/25-matrix-e2ee-readiness.md` for the full E2EE readiness plan. |
| Reactions | Not supported | The adapter registers callbacks for `RoomMessageText`, `RoomMessageNotice`, and `RoomMessageEmote` only. Reaction events are not processed. |
| Edits | Not supported | Edited messages appear as new messages. The adapter does not track `m.replace` relations. |
| Deletes / redactions | Not supported | Redacted messages, if received, are not handled. |
| Media / attachments / files | Not supported | The adapter handles text content only. No image, file, or audio events. |
| Threads | Not supported | Threaded messages are received but thread context is not preserved in canonical events. |
| Spaces | Not supported | Spaces are not relevant to the adapter's operation. |
| Webhooks | Not supported | Matrix does not use webhooks. MEDRE does not implement any webhook receiver for Matrix. |
| Admin APIs | Not supported | The adapter uses the client-server API only. No admin endpoints are called. |
| Non-Matrix transports | Not in scope | This runbook covers Matrix only. |
| Presence | Not supported | The adapter does not send or receive presence events. |
| Typing notifications | Not supported | The adapter does not send or receive typing notifications. |
| Read receipts | Not supported | The adapter does not send or track read receipts. |
| Room creation / management | Not supported | The adapter does not create rooms, set topics, or manage membership. |
| User profile operations | Not supported | The adapter does not set avatars, display names, or profile data. |


## 14. Troubleshooting

### 14.1 `MatrixConnectionError: mindroom-nio not installed`

You forgot to install the Matrix dependency.

```bash
pip install -e ".[matrix]"
```

### 14.2 `MatrixConnectionError: failed to authenticate as @bot:localhost`

The access token is wrong, expired, or the user ID does not match.

1. Verify the user ID format: it must be `@localpart:server` with the leading `@`.
2. Regenerate the token using the login API (section 4.1).
3. Confirm the homeserver URL is correct and reachable:
   ```bash
   curl -s http://localhost:8008/_matrix/client/versions
   ```
   This should return a JSON object with a `versions` array.

### 14.3 `MatrixConfigError: homeserver must start with 'http://' or 'https://'`

The `MATRIX_HOMESERVER` environment variable is missing the scheme. It must be a full URL.

Wrong: `localhost:8008`
Right: `http://localhost:8008`

### 14.4 `MatrixConfigError: user_id must start with '@'`

The `MATRIX_USER_ID` value is missing the leading `@`. Matrix user IDs always start with `@`.

Wrong: `bot:localhost`
Right: `@bot:localhost`

### 14.5 `MatrixSendError: no room_id in result`

The `deliver()` call did not include a `target_channel` and the payload did not contain a `room_id` key. The adapter needs to know which room to send to.

### 14.6 `MatrixSendError: homeserver returned empty/missing event_id`

The homeserver accepted the request but did not return an event ID. This is unusual. Check the homeserver logs. It could indicate a Synapse bug, a database issue, or a malformed request.

### 14.7 Adapter starts but no inbound messages are received

Check these things, in order:

1. Is the room ID in the allowlist? If `room_allowlist` is set and the room is not in it, messages are silently dropped.
2. Is someone other than the bot sending messages? The adapter suppresses self-messages.
3. Is the room encrypted? Encrypted rooms produce events the adapter cannot decode.
4. Is the bot actually joined to the room? Check via Element or the Matrix membership API.
5. Is the sync task still running? Call `health_check()`. If it returns `"failed"`, the sync loop crashed.

### 14.8 Adapter crashes on startup with `ImportError: nio`

The `compat.py` guard should catch this and raise `MatrixConnectionError` instead. If you see a raw `ImportError`, something is wrong with the guard. Check that `mindroom-nio` is installed and importable:

```python
python -c "import nio; print(nio.__version__)"
```

### 14.9 High CPU usage or spinning

The `sync_forever` loop should be idle most of the time (long-polling with a 30-second timeout). If you see high CPU, it might be rapidly reconnecting. Check `health_check()` and the logs for repeated sync failures.

### 14.10 Messages appear in Element but `deliver()` raises `MatrixSendError`

The homeserver rejected the message content. Check that the payload is a valid `m.room.message` content dict with a `msgtype` and `body`. The `MatrixRenderer` should produce this format, but if you are constructing payloads manually, double-check the structure.

### 14.11 `asyncio.CancelledError` warnings on shutdown

These are expected. The `stop()` method cancels the sync task, which raises `CancelledError` inside `sync_forever`. The adapter catches and suppresses it, but Python's runtime may still emit warnings. They are harmless.

### 14.12 `EnvironmentError: Required environment variable MATRIX_HOMESERVER is not set`

The runner requires `MATRIX_HOMESERVER`, `MATRIX_USER_ID`, and `MATRIX_ACCESS_TOKEN` to be set. Double-check that all three are exported in your shell:

```bash
echo $MATRIX_HOMESERVER
echo $MATRIX_USER_ID
echo $MATRIX_ACCESS_TOKEN
```

If any is empty, set it and try again.

### 14.13 Runner exits immediately with no output

The most common cause is a missing or empty required environment variable. The runner raises `EnvironmentError` before logging is configured in some code paths, so the error may go to stderr without the standard log format. Run the runner explicitly and check stderr:

```bash
python -m medre.runner 2>&1
```

### 14.14 `ValueError` or `TypeError` from `MatrixConfig.validate()`

The runner builds a `MatrixConfig` from environment variables and calls `validate()` before starting anything. Common causes:

- `MATRIX_HOMESERVER` does not start with `http://` or `https://`.
- `MATRIX_USER_ID` does not start with `@`.
- `MATRIX_SYNC_TIMEOUT_MS` is not a valid integer.
- `MATRIX_ACCESS_TOKEN` is empty.

Check the error message, fix the variable, and restart.

### 14.15 Runner starts but no inbound events arrive

After the "running" line appears, check the diagnostics output. If `connected` is `False` or `logged_in` is `False`, the adapter failed to authenticate. Verify the access token is valid for the given user ID on the given homeserver. Then check the troubleshooting items in 14.7 (allowlist, self-message suppression, encryption, room membership, sync task state).


## Appendix A: Manual Wiring (Advanced/Developer Reference)

The runner (`python -m medre.runner`) is the primary way to operate MEDRE in alpha mode. This appendix documents the manual wiring pattern for developers who need to construct the adapter and context by hand, for example when testing a specific subsystem in isolation or building a custom pipeline.

```python
import asyncio
import os

from medre.adapters.matrix import MatrixAdapter, MatrixConfig
from medre.adapters.base import AdapterContext

async def main():
    raw_allowlist = os.environ.get("MATRIX_ROOM_ALLOWLIST", "")
    allowlist = set(raw_allowlist.split(",")) if raw_allowlist.strip() else None

    config = MatrixConfig(
        adapter_id="matrix-alpha",
        homeserver=os.environ["MATRIX_HOMESERVER"],
        user_id=os.environ["MATRIX_USER_ID"],
        access_token=os.environ["MATRIX_ACCESS_TOKEN"],
        room_allowlist=allowlist,
    )

    adapter = MatrixAdapter(config)

    # Create a minimal context. The real runtime provides this.
    # For alpha testing, a stub context with a logger and publish_inbound
    # callback is sufficient.
    ctx = AdapterContext(
        logger=...,           # a logging.Logger or compatible object
        publish_inbound=...,  # an async callable accepting canonical events
        storage=...,          # a storage backend or None
    )

    await adapter.start(ctx)
    print("Adapter started. Press Ctrl+C to stop.")

    try:
        await asyncio.Event().wait()  # block forever
    except KeyboardInterrupt:
        pass
    finally:
        await adapter.stop()

asyncio.run(main())
```

This is not the recommended way to run MEDRE. Use the runner unless you have a specific reason to wire manually.
