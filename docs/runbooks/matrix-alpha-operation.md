# Matrix Alpha Operation Runbook

> Last updated: 2026-05-10
> Scope: Real Matrix Operation Alpha (Track 7)
> Status: Alpha. Not production. Not hardened. Not complete. Plaintext is the primary alpha path. E2EE text alpha is available as an add-on for encrypted rooms (see section 13).

This runbook describes how to run MEDRE against a real Matrix homeserver in alpha mode. Alpha mode means the MatrixAdapter connects to a real homeserver using real credentials, syncs real rooms, sends real messages, and receives real events. It does not mean the system is ready for anything beyond a single operator on a local or test homeserver.

Everything in this document is conservative. If something has not been tested against a real homeserver and confirmed working, this document says so. If something is known to be broken or missing, this document says that too.

**Plaintext alpha** is the primary path. **E2EE text alpha** is an add-on that enables encrypted room operation for text messages only. See section 13 for E2EE setup and section 14 for troubleshooting encrypted rooms.

**mmrelay (meshtastic-matrix-relay)** should be used as a practical behavioral reference for Matrix client workflows and E2EE handling patterns. It is a working Meshtastic-to-Matrix bridge that demonstrates real-world nio usage. However, it should NOT be copied architecturally or line-for-line — MEDRE's architecture (canonical events, adapter isolation, pipeline stages) remains authoritative. See `docs/spec/modular-event-engine-spec.md` §26 for the full set of architectural lessons from mmrelay.


## 1. Purpose

Alpha operation validates that the MEDRE Matrix adapter works end to end against a real Matrix homeserver with real network calls. This is the first time the adapter leaves mock and fake territory.

Scope boundaries:

- One transport: Matrix. No other transports are in scope for this runbook.
- One operator: a single person running against a local or test homeserver.
- Plain text messages and replies in unencrypted rooms. E2EE text alpha adds encrypted room support for text only (see section 13).
- No production deployment, no scaling, no monitoring, no alerting.
- No claims about reliability, durability, or correctness beyond what manual testing confirms.

This runbook complements `docs/runbooks/matrix-live-smoke.md`. The smoke test validates adapter methods in isolation. Alpha operation validates the full wiring: config, adapter, codec, inbound, outbound, and health, running together.


## 2. Prerequisites

| Requirement | Details |
|------------|---------|
| Matrix homeserver | Synapse or Conduit, local or reachable over the network |
| Bot account | A dedicated Matrix user, not your personal account |
| Python | 3.11 or later |
| Package install | `pip install -e ".[matrix]"` (plaintext alpha, recommended). E2EE text alpha: `pip install -e ".[matrix-e2e]"` (adds Olm/Megolm crypto libs for encrypted rooms). |
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

> **Note on E2EE.** Alpha authenticates with access tokens over plain HTTP(S). The `.[matrix]` extra installs the base `mindroom-nio` package (no crypto) — this is the recommended plaintext alpha. The `.[matrix-e2e]` extra installs `mindroom-nio[e2e]` with Olm/Megolm crypto libraries — use this for the E2EE text alpha. E2EE text alpha is now active: when installed with `.[matrix-e2e]` and `encryption_mode` is set to `e2ee_required` or `e2ee_optional`, the adapter operates in encrypted rooms (see section 13). The adapter discovers its device ID via `whoami()` and derives an internal store path automatically — no operator configuration of `device_id` or `store_path` is required. Plaintext rooms work identically in both modes.


## 5. Room Setup

1. Open a Matrix client (Element, or any other).
2. Create a new room. Give it any name.
3. Invite the bot user to the room.
4. Accept the invite from the bot account (log in as the bot in a second client session or via the join API).
5. Copy the room ID. It looks like `!opaquestring:localhost`. Room aliases (the `#name:server` form) will not work in the allowlist.
6. Confirm the room is unencrypted for plaintext alpha. If the room has a lock icon in Element, it is encrypted — see section 13 for E2EE text alpha setup. Plaintext alpha cannot read encrypted room content. E2EE text alpha supports encrypted rooms for text messages only.


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

The environment variable convention for this is `MATRIX_ROOM_ALLOWLIST`, a comma-separated list of room IDs. The runner (`medre run`) parses this into the set automatically. If you are wiring the adapter manually, parse it yourself:

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
| `MATRIX_SYNC_TIMEOUT_MS` | No | `30000` | `60000` | Sync long-poll timeout in milliseconds. |
| `MEDRE_DB_PATH` | No | `:memory:` | `/tmp/medre.db` | SQLite database path. Defaults to in-memory (lost on shutdown). |

### 7.2 Running with the runner

`medre run` is the primary alpha operation entry point. It wires the full pipeline, handles configuration from environment variables, manages signal-based shutdown, and provides structured logging.

```bash
# Set the required environment variables
export MATRIX_HOMESERVER=http://localhost:8008
export MATRIX_USER_ID=@bot:localhost
export MATRIX_ACCESS_TOKEN=syt_xxxxxxxxxxxxx
export MATRIX_ROOM_ALLOWLIST=!abc123:localhost

# Run
medre run
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
INFO  medre  Matrix Operation Alpha: config loaded for @bot:localhost
INFO  medre  PipelineRunner started
INFO  medre  MatrixAdapter matrix-alpha started
INFO  medre  Initial diagnostics: {'status': 'healthy', 'details': {'connected': True, 'logged_in': True, 'sync_task_running': True, 'last_sync_error': None, 'reconnecting': False, 'reconnect_attempts': 0, 'last_successful_sync': '2026-05-09T12:00:00Z', 'rooms_tracked': 1, 'delivery_attempts': 0, 'delivery_successes': 0, 'delivery_failures': 0, 'crypto_store_loaded': None}}
INFO  medre  Matrix Operation Alpha running — awaiting shutdown signal
```

If you see the "running" line, the runner has:

1. Validated all required environment variables.
2. Created and initialized the SQLite storage.
3. Started the PipelineRunner.
4. Started the MatrixAdapter (nio client created, login restored, sync loop running).
5. Logged initial diagnostics confirming connection, login, sync task state, and operational counters (reconnect attempts, delivery stats, room tracking).

**Shutdown.** Press Ctrl+C (or send SIGTERM) to trigger a graceful shutdown:

```
INFO  medre  Shutdown requested — stopping
INFO  medre  MatrixAdapter stopped
INFO  medre  PipelineRunner stopped
INFO  medre  Matrix Operation Alpha shut down cleanly
```

The runner catches SIGINT and SIGTERM, signals the adapter to stop, then stops the pipeline runner, then closes the database. Any in-flight sync operations are cancelled. See Known Limitation #2 for what this means about in-flight messages.

If you do not see the startup lines above, check the troubleshooting section (section 14) for common configuration and connectivity errors.


## 8. Device Identity and Crypto Store (Internal)

The Matrix adapter manages device identity and crypto store paths internally. Operators do not configure `device_id` or `store_path`.

**Device identity.** When the adapter starts with a non-plaintext `encryption_mode`, it calls the Matrix `whoami()` endpoint using the access token to discover the device ID associated with that token. No operator configuration is needed. The device ID is stable as long as the access token was created for the same device.

**Crypto store path.** The adapter derives an internal store path (per-adapter isolation) automatically. On the runtime path the `RuntimeBuilder` derives `{state_dir}/adapters/{adapter_id}/matrix/store` from the resolved state directory. Standalone sessions (library usage outside the runtime) should receive an explicit `store_path` from the caller to ensure persistence across restarts; if no path is provided the adapter will raise an error in E2EE modes. The store persists Olm/Megolm session keys and device keys across restarts.

**Plaintext mode.** In `plaintext` mode the adapter does not initialise the crypto subsystem. No device ID discovery or store path is needed.

**Advanced/internal overrides.** `device_id` and `store_path` exist as fields on `MatrixConfig` for test harnesses and advanced internal use. They are not operator-facing configuration. The `MEDRE_MATRIX_DEVICE_ID` and `MEDRE_MATRIX_STORE_PATH` environment variables exist in the env mapping but are not documented as operator configuration — they are reserved for internal/testing use only.

### 8.1 Docker deployments and E2EE dependencies

Plaintext alpha Docker deployments install `mindroom-nio` (no E2EE extras):

```
pip install -e ".[matrix]"
```

E2EE text alpha Docker deployments install the E2EE-enabled dependency:

```
pip install -e ".[matrix-e2e]"
```

The `.[matrix-e2e]` extra installs `mindroom-nio[e2e]`, which adds Olm/Megolm native crypto libraries (`vodozemac`), SQLite store dependencies (`peewee`), and related utilities. Without it, operating in encrypted rooms will fail — nio's `ENCRYPTION_ENABLED` will be `False` and the crypto subsystem will not initialize.

### 8.3 Deferred E2EE capabilities

The following E2EE-related capabilities are **deferred** and not part of the E2EE text alpha:

- Cross-signing setup and verification flows
- Room key backup
- Room key import/export
- Interactive device verification (emoji/QR)
- Unverified device policy: MEDRE internally sets `ignore_unverified_devices=True` for non-plaintext `encryption_mode` values. This is required by the upstream nio client — nio lacks cross-signing support (MSC1756), providing no API for programmatic device verification, so this flag is mandatory for any automated E2EE operation. There is no operator toggle for this. See `docs/contracts/25-matrix-e2ee-readiness.md` §5.2 for rationale.

These will be addressed in a future E2EE implementation tranche. See `docs/contracts/25-matrix-e2ee-readiness.md` for the detailed plan.

### 8.4 Live/manual E2EE test harness

The E2EE text alpha includes a live harness for testing encrypted room operation against a real homeserver. See `docs/runbooks/matrix-live-smoke.md` for full instructions. The harness:

- Extends the existing live smoke test pattern.
- Requires `MATRIX_E2E_ROOM_ID` in addition to the base live test variables.
- Validates that the adapter can decrypt inbound and encrypt outbound in an encrypted room.
- Remains skipped by default, gated by environment variables and the `live` pytest marker.


## 9. Expected Logs and Diagnostics

### 9.1 Healthy startup

The runner logs a startup sequence. The key line from the adapter is:

```
MatrixAdapter matrix-alpha started
```

Before that, you will see the runner's own startup lines (config loaded, PipelineRunner started). After the adapter starts, the runner logs initial diagnostics and then the "running" line. After that, the sync loop runs silently. There is no periodic "still alive" log. Silence is normal. The sync loop is long-polling the homeserver, waiting for new events.

### 9.2 Inbound message received

When someone sends a message in an allowlisted room, the `_on_room_message` callback fires. On success, you will see nothing in the logs (the event is published to the context silently). On failure, you will see an exception traceback:

```
MatrixAdapter matrix-alpha: error processing inbound event
Traceback (most recent call last):
  ...
```

In E2EE text alpha, encrypted messages that are successfully decrypted also fire `_on_room_message` as `RoomMessageText` — identical to plaintext. No log difference. Messages that fail to decrypt produce `MegolmEvent` which is handled by the dedicated `_on_megolm_event` callback: the event is counted, a warning is logged (event_id and room_id only), and the event is not forwarded to the canonical pipeline. The adapter tracks `undecryptable_event_count` and `last_crypto_error` in diagnostics (see section 14.16 for troubleshooting).

### 9.3 Self-message suppression

When the bot itself sends a message (including echoes of its own outbound messages via the sync loop), the adapter suppresses them. You will see:

```
MatrixAdapter matrix-alpha: suppressing self-message from @bot:localhost
```

This is logged at DEBUG level. If your logger is configured for INFO or higher, you will not see it.

### 9.4 Sync failure and automatic recovery

If a sync iteration fails due to a transient error (network blip, homeserver restart, temporary server error), the adapter automatically attempts reconnection with bounded exponential backoff. The error is logged:

```
MatrixAdapter matrix-alpha: sync error, attempting reconnect (attempt N): <exception details>
```

Transient errors include network timeouts, connection refused, and server-side 5xx responses. Permanent errors (expired/revoked token, deactivated account) are not retried — the adapter enters `failed` state and requires manual intervention (new token + restart).

See section 12 (Operational Resilience) for full reconnect/backoff behavior.

### 9.5 Health states

| State | Meaning |
|-------|---------|
| `unknown` | Adapter has not started, or has been stopped |
| `healthy` | Client is connected, logged in, and sync is running |
| `degraded` | Sync is running but actively reconnecting after a transient failure |
| `failed` | Sync task has crashed permanently, or client exists but is not logged in |

When the adapter is in `degraded` state, it is actively attempting to restore the sync connection. Once reconnection succeeds, the state returns to `healthy`. If the reconnect budget is exhausted, the state transitions to `failed` and requires manual restart.


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

### 10.6 Third-party inbound validation (Track 2)

This procedure validates the complete third-party inbound path: a message
from a different Matrix user arrives via the nio sync loop, passes sender
filtering and allowlist checks, gets decoded into a `CanonicalEvent`, and
triggers `publish_inbound()`.  This is the most operationally significant
beta unknown — it cannot be tested with a single account.

**Prerequisites:**

| Requirement | Details |
|------------|---------|
| Bot account | The MEDRE Matrix adapter account (with access token) |
| Second account | A different Matrix user on the same homeserver |
| Shared room | Both accounts must be in the same room |

**Procedure (manual):**

1. Set environment variables:

```bash
export MATRIX_HOMESERVER="http://localhost:8008"
export MATRIX_USER_ID="@bot:localhost"
export MATRIX_ACCESS_TOKEN="syt_..."
export MATRIX_ROOM_ID="!test:localhost"
export MATRIX_INBOUND_SENDER="@alice:localhost"  # second account
```

2. Run the third-party inbound live test:

```bash
pytest tests/test_matrix_live.py::TestMatrixLiveSmoke::test_inbound_message_received -m live -v
```

3. While the test is waiting (30 s window), send a message from the
   second account (`@alice:localhost`) into `MATRIX_ROOM_ID`.

4. The test should pass (not xfail), confirming:
   - `publish_inbound()` was called
   - `source_transport_id` matches `@alice:localhost`
   - `source_channel_id` matches `MATRIX_ROOM_ID`
   - `source_native_ref.native_message_id` is a Matrix event ID
   - `event_kind` is `"message.created"`
   - Payload contains `body` and `msgtype`

5. If no second account is available, the test will xfail with a
   descriptive message.  This is acceptable — the deterministic unit
   tests in `tests/test_matrix_adapter.py` (classes
   `TestThirdPartyInboundCanonicalEventShape` and
   `TestInboundDiagnosticsCounters`) cover the same logic paths
   without requiring live connectivity.

**Diagnostics counters:**

The adapter exposes inbound counters via `diagnostics()`:

| Counter | Description |
|---------|-------------|
| `inbound_published` | Events successfully published via `publish_inbound()` |
| `inbound_suppressed_self` | Events dropped because sender == bot user_id |
| `inbound_suppressed_envelope` | Events dropped because MEDRE envelope source_adapter matched |
| `inbound_filtered_allowlist` | Events dropped because room was not in the allowlist |

To inspect counters programmatically:

```python
diag = adapter.diagnostics()
print(f"published={diag['inbound_published']} "
      f"self_suppressed={diag['inbound_suppressed_self']} "
      f"envelope_suppressed={diag['inbound_suppressed_envelope']} "
      f"allowlist_filtered={diag['inbound_filtered_allowlist']}")
```

After a successful third-party inbound validation:
- `inbound_published` should be >= 1
- `inbound_suppressed_self` should be >= 0 (may include echo from outbound tests)
- `inbound_filtered_allowlist` should be 0 (if using correct allowlist)

**Automated test coverage (no live server required):**

| Test class | File | Coverage |
|-----------|------|----------|
| `TestThirdPartyInboundCanonicalEventShape` | `tests/test_matrix_adapter.py` | 8 tests: source_adapter, sender as transport_id, room as channel_id, payload shape, source_native_ref, event_kind, UUID event_id, notice msgtype |
| `TestInboundDiagnosticsCounters` | `tests/test_matrix_adapter.py` | 8 tests: published/self/envelope/allowlist counter increments, accumulation, reset on start, diagnostics exposure, no-ctx early return |
| `TestSelfMessageSuppression` | `tests/test_matrix_adapter.py` | 3 tests: self suppressed, other accepted, missing sender |
| `TestMEDREOriginLoopSuppression` | `tests/test_matrix_adapter.py` | 4 tests: same adapter suppressed, different adapter accepted, missing envelope, corrupt envelope |
| `TestRoomAllowlist` | `tests/test_matrix_adapter.py` | 4 tests: no allowlist, matching, non-matching, multiple rooms |


## 11. Known Limitations

This is an honest list. Everything here is real.

1. **Bounded auto-reconnect, not infinite retry.** The adapter automatically reconnects on transient sync failures with exponential backoff, up to a maximum number of attempts. If the reconnect budget is exhausted, the adapter enters `failed` state and requires a manual restart. See section 12 for the full reconnect specification.

2. **No graceful shutdown signaling.** The adapter does not drain in-flight messages on stop. `stop()` cancels the sync task and closes the client. Anything in flight is lost. Delivery retries are per-attempt; an in-flight delivery that is cancelled during shutdown is not retried.

3. **No inbound queue or persistence.** Inbound events are published directly via `context.publish_inbound()`. If that callback is slow or fails, the event is gone. There is no retry, no dead letter queue, no redelivery.

4. **No rate limiting.** The adapter sends as fast as you call `deliver()`. Matrix homeservers rate-limit by default. If you send too fast, the homeserver will reject messages and you will get `AdapterSendError` (transient).

5. **No connection health monitoring.** The sync loop either runs or it does not. There is no heartbeat, no periodic ping, no health metric beyond the basic `health_check()` states.

6. **Single-room testing only.** Alpha mode has only been validated with one room at a time. Multi-room behavior (multiple allowlisted rooms with concurrent inbound) has not been tested against a real homeserver.

7. **Reconnect does not recover from permanent auth failures.** If the access token is revoked or expires, the reconnect loop will not succeed (permanent error). The adapter enters `failed` state after exhausting the reconnect budget. You need a new token and a manual restart.

8. **No structured logging.** The adapter uses `ctx.logger.info/debug/error` with format strings. There are no structured log fields, no trace IDs, no correlation across events.

9. **No metrics.** There is no Prometheus endpoint, no counters, no histograms. Inbound diagnostics counters (`inbound_published`, `inbound_suppressed_self`, `inbound_suppressed_envelope`, `inbound_filtered_allowlist`) are available via `diagnostics()`, but there is no external metrics export. The only observability is the log output, the `health_check()` return value, and the `diagnostics()` counters.

10. **Runner is in alpha.** The runner (`medre run`) works for testing but has limited error recovery. If a subsystem fails during startup, the runner exits with a traceback rather than attempting partial recovery. There is no watchdog to restart the runner if it crashes.

11. **Plaintext is primary; E2EE is add-on.** Plaintext alpha is the recommended path. E2EE text alpha adds encrypted room support for text only (see section 13). Reactions, edits, media, cross-signing, key backup, and unverified device policy remain unsupported. Undecryptable event logging is now implemented (counted and logged safely, not forwarded).


## 12. Operational Risks

These are things that can go wrong. Read them before running the adapter.

### 12.1 Token leakage

The access token is a long-lived credential. Anyone who has it can impersonate the bot. Risks:

- Setting the token in a shell command that gets logged (e.g., `export MATRIX_ACCESS_TOKEN=syt_...` in a shared terminal).
- Committing `.env` files or scripts containing the token.
- Logging the token accidentally. The adapter redacts it in `__repr__`, but your own code might not.

Mitigation: use a dedicated bot account with minimal room membership. Rotate the token if you suspect it has been exposed. Unset the environment variable when you are done testing.

### 12.2 Sync interruption

The `sync_forever` loop can fail for many reasons: network blips, homeserver restarts, token expiry, resource exhaustion. When a transient failure occurs, the adapter automatically reconnects with exponential backoff (see section 12A). Permanent failures (token expiry, account deactivation) cause the adapter to enter `failed` state and require manual restart.

### 12.3 Reconnect limitations

Automatic reconnect handles transient sync failures only. The reconnect loop has a bounded attempt maximum and exponential backoff. Known limitations:

- **Token expiry is not recovered.** A revoked or expired token produces a permanent error. Reconnect attempts will exhaust the budget without success. A new token and manual restart are required.
- **Homeserver disappearance.** If the homeserver goes offline for longer than the reconnect budget allows, the adapter enters `failed`. A manual restart is needed when the homeserver returns.
- **No inbound replay.** Events that arrived during the disconnected period are not replayed. When sync resumes, it picks up from the last successful sync token. Events that occurred while disconnected are missed if the sync token was not persisted.
- **Delivery retry is transient-only.** Outbound delivery retries (up to 3 attempts) only cover transient send errors. Permanent send failures (unknown room, forbidden) are not retried.

### 12.4 Homeserver resource consumption

The long-polling sync loop holds an HTTP connection open to the homeserver. On Synapse, this is a `/_matrix/client/v3/sync` request with a 30-second timeout. On Conduit, behavior is similar. This is normal Matrix client behavior, but if you run many adapter instances against a small homeserver, you may see performance degradation.

### 12.5 Message ordering

The adapter does not guarantee ordering of outbound messages. If you call `deliver()` twice in rapid succession, the homeserver may process them in either order depending on its internal queueing. The adapter does not sequence or gate outbound sends.

### 12.6 Event duplication

If the adapter restarts after sending a message but before recording the event, the same message may be sent again on restart. The adapter has no deduplication logic for outbound messages. Inbound events may also be delivered twice if the sync token is not persisted between restarts.

Delivery retries (up to 3 attempts on transient send errors) can produce duplicates: the message may have been accepted by the homeserver on the first attempt, but the response was lost. The adapter retries the send, resulting in a duplicate event in the room. There is no idempotency key or deduplication mechanism. Operators monitoring rooms should be aware of this possibility, especially during network instability.


## 12A. Operational Resilience

This section documents the adapter's automatic recovery and retry behavior. These features handle transient failures only — permanent errors (expired tokens, deactivated accounts, unknown rooms) always require manual intervention.

### 12A.1 Automatic sync recovery with reconnect/backoff

When a sync iteration fails due to a transient error, the adapter does not require manual restart. It automatically attempts to re-establish the sync connection with bounded exponential backoff.

**Transient errors** (auto-retried):
- Network timeouts and connection refused
- Server-side 5xx responses
- Temporary DNS resolution failures
- TCP connection resets

**Permanent errors** (not retried):
- Expired or revoked access tokens (`M_UNKNOWN_TOKEN`)
- Deactivated accounts (`M_USER_DEACTIVATED`)
- Forbidden errors (`M_FORBIDDEN`)

### 12A.2 Reconnect attempt maximum and backoff strategy

| Parameter | Value | Notes |
|-----------|-------|-------|
| Maximum reconnect attempts | Bounded (implementation-defined) | After exhausting attempts, adapter enters `failed` state |
| Backoff strategy | Exponential with jitter | Prevents thundering herd on shared homeserver |
| Initial delay | Implementation-defined | Short delay before first reconnect attempt |
| Maximum delay | Capped | Backoff stops growing beyond a ceiling |

The reconnect attempt counter resets on a successful sync. Diagnostics expose the current `reconnect_attempts` count. The `reconnecting` boolean indicates whether the adapter is actively in a reconnect cycle.

During the reconnect cycle, the adapter reports `health="degraded"`. On successful sync restoration, health returns to `"healthy"`.

### 12A.3 Restart behavior and crypto continuity

When the adapter is stopped and restarted with the same access token and store path:

- **Plaintext rooms**: No special consideration. Sync resumes from the homeserver's last position.
- **E2EE rooms**: `restore_login` loads the crypto store from the internal store path. The Olm machine, device keys, and room keys are restored. Device identity is discovered via `whoami()` and is stable as long as the access token is associated with the same device. The `crypto_store_loaded` diagnostic field reports `True` when the crypto store was successfully loaded.

Crypto continuity is verified during startup: if the store_path directory is empty or missing and E2EE is active, the adapter creates a fresh store (first-run behavior). If the store exists and is valid, existing keys are loaded. The diagnostic `crypto_store_loaded` reflects the result.

### 12A.4 Delivery retry semantics

Outbound `deliver()` calls have built-in retry for transient send failures:

| Parameter | Value |
|-----------|-------|
| Maximum retries | 3 |
| Retry trigger | Transient send errors only |
| Permanent errors | No retry — error returned immediately |
| Duplicate risk | Yes — see section 12.6 |

**Transient send errors** include network timeouts, connection refused, and 5xx homeserver responses. **Permanent send errors** include `M_FORBIDDEN`, `M_UNKNOWN_ROOM`, and other 4xx client errors.

On each retry, the adapter waits with exponential backoff before reattempting. If all 3 attempts fail, the delivery fails and the error is returned to the caller.

**Duplicate risk**: Because the homeserver may have accepted the message on an earlier attempt but the response was lost, a successful retry can produce a duplicate event in the room. There is no transaction ID deduplication in the alpha. Operators should be aware of this during network instability.

Delivery diagnostics are available: `delivery_attempts`, `delivery_successes`, `delivery_failures` track cumulative counters since adapter start.

### 12A.5 Room-state tracking

The adapter tracks room state (joined rooms, membership) as part of sync processing. The `rooms_tracked` diagnostic field reports the number of rooms currently being tracked. This is informational and does not affect the allowlist filter — the allowlist continues to gate which rooms produce inbound events.

### 12A.6 Long-running deployment notes

For operators running the adapter as a long-lived process (e.g., a persistent bot, Docker container, systemd service):

**Docker restart policy:**

```bash
docker run -d --name medre-matrix \
  --restart unless-stopped \
  -e MEDRE_HOME=/opt/medre \
  -e MEDRE_MATRIX_HOMESERVER=http://homeserver:8008 \
  -e MEDRE_MATRIX_USER_ID=@bot:server \
  -e MEDRE_MATRIX_ACCESS_TOKEN=syt_... \
  -e MEDRE_MATRIX_ROOM_ALLOWLIST=!room:server \
  -v medre-data:/opt/medre \
  medre-matrix:latest
```

**Key considerations:**

1. **Crypto store persistence.** The adapter derives its store path under the state directory. Mount the `MEDRE_HOME` directory (or the XDG state directory) as a Docker volume or bind mount. If the container is recreated without persisting the state directory, the crypto identity is lost and must be re-established.

2. **Docker restart policy.** Use `--restart unless-stopped` or `--restart on-failure`. The adapter's built-in reconnect handles transient sync failures within a single process lifetime. The Docker restart policy handles process-level crashes (OOM, segfault, runner panic).

3. **Signal handling.** The runner catches SIGTERM (Docker sends this on `docker stop`) and performs graceful shutdown. SIGKILL (Docker sends this after the grace period) interrupts in-flight operations.

4. **`MEDRE_DB_PATH` persistence.** If using file-backed SQLite storage (not in-memory), mount the database path as a volume. Default is `:memory:` (lost on restart).

5. **Token rotation.** The adapter does not support runtime token rotation. To rotate the token, restart the container with the new `MATRIX_ACCESS_TOKEN` value.

6. **Resource limits.** The sync loop maintains a single long-polling HTTP connection. Memory usage is dominated by the nio crypto store (SQLite + in-room key cache). For alpha with a handful of rooms, 256 MB is sufficient.


## Live Validation Evidence

### Test Results

- **File:** `tests/test_matrix_live.py` (also `tests/test_matrix_e2ee_live.py`)
- **Last run:** 2026-05-12 — **NOT EXECUTED**
- **Command:** `pytest tests/test_matrix_live.py -m live -v`
- **Reason:** No Matrix environment variables present in the execution session. All required variables unset: `MATRIX_HOMESERVER`, `MATRIX_USER_ID`, `MATRIX_ACCESS_TOKEN`, `MATRIX_ROOM_ID`. E2EE-specific variables also unset: `MATRIX_DEVICE_ID`, `MATRIX_STORE_PATH`.
- **Policy:** Live tests are never run without pre-existing credentials. The agent does not request, generate, or print credentials.
- **Result:** All live tests would skip with reason: *"Set MATRIX_HOMESERVER, MATRIX_USER_ID, MATRIX_ACCESS_TOKEN, and MATRIX_ROOM_ID env vars to run live Matrix tests"*
- **Operator action required:** Set the four required environment variables (plus two E2EE variables for encrypted-room testing) and run the commands above. See the smoke test runbook (`docs/runbooks/matrix-live-smoke.md`) for detailed setup and environment variable instructions.

**Previous successful live validation:** 2026-05-10 — 13 passed, 0 failed (plaintext) and 7 passed, 0 failed (E2EE). See `docs/runbooks/matrix-live-smoke.md` for full evidence.


## 13. Explicit Unsupported Features

The following features are not supported in alpha mode. Do not attempt to use them. They are listed here so you do not have to wonder.

| Feature | Status | Notes |
|---------|--------|-------|
| End-to-end encryption (E2EE) | E2EE text alpha available (section 13) | Encrypted rooms are supported for text messages when installed with `.[matrix-e2e]` and `encryption_mode` is set to `e2ee_required` or `e2ee_optional`. Device ID is discovered via `whoami()` and store path is derived internally. Reactions, edits, media, cross-signing, key backup, and unverified device policy remain unsupported. Undecryptable event logging is now implemented (counted, logged, not forwarded). Plaintext rooms work identically in both modes. |
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

### 14.5 `AdapterPermanentError: no room_id in result`

The `deliver()` call did not include a `target_channel` and the payload did not contain a `room_id` key. The adapter needs to know which room to send to. This error originates as a session-level `MatrixSendError` internally and is normalized to `AdapterPermanentError` at the adapter boundary.

### 14.6 `AdapterSendError: homeserver returned empty/missing event_id`

The homeserver accepted the request but did not return an event ID. This is unusual. Check the homeserver logs. It could indicate a Synapse bug, a database issue, or a malformed request.

### 14.7 Adapter starts but no inbound messages are received

Check these things, in order:

1. Is the room ID in the allowlist? If `room_allowlist` is set and the room is not in it, messages are silently dropped.
2. Is someone other than the bot sending messages? The adapter suppresses self-messages.
3. Is the room encrypted? If running plaintext alpha (`.[matrix]`), encrypted room messages cannot be decoded. Switch to E2EE text alpha (`.[matrix-e2e]`) with `encryption_mode` set to `e2ee_required` or `e2ee_optional` (see section 13). The adapter derives device ID and store path automatically.
4. Is the bot actually joined to the room? Check via Element or the Matrix membership API.
5. Is the sync task still running? Call `health_check()`. If it returns `"failed"`, the sync loop crashed.

### 14.8 Adapter crashes on startup with `ImportError: nio`

The `compat.py` guard should catch this and raise `MatrixConnectionError` instead. If you see a raw `ImportError`, something is wrong with the guard. Check that `mindroom-nio` is installed and importable:

```python
python -c "import nio; print(nio.__version__)"
```

### 14.9 High CPU usage or spinning

The `sync_forever` loop should be idle most of the time (long-polling with a 30-second timeout). If you see high CPU, it might be rapidly reconnecting due to persistent transient failures. Check `health_check()` for `degraded` state and the `reconnect_attempts` counter in diagnostics. If `reconnect_attempts` is climbing, the adapter is in a reconnect loop — see section 14.19.

### 14.10 Messages appear in Element but `deliver()` raises `AdapterPermanentError`

The homeserver rejected the message content. Check that the payload is a valid `m.room.message` content dict with a `msgtype` and `body`. The `MatrixRenderer` should produce this format, but if you are constructing payloads manually, double-check the structure. This error originates as a session-level `MatrixSendError` internally and is normalized to `AdapterPermanentError` at the adapter boundary.

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
medre run 2>&1
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

### 14.16 E2EE: adapter installed with `.[matrix-e2e]` but encrypted messages not decrypted

Check these in order:

1. **Is `encryption_mode` set?** The adapter defaults to `plaintext`. Set `MATRIX_ENCRYPTION_MODE=e2ee_required` (or `e2ee_optional`) to enable E2EE.
2. **Is `mindroom-nio[e2e]` actually installed?** Verify with:
   ```bash
   python -c "import nio; print(nio.crypto.ENCRYPTION_ENABLED)"
   ```
   This should print `True`. If `False`, the `[e2e]` extra is not installed. Run `pip install -e ".[matrix-e2e]"`.
3. **Is the device verified by the sender?** MEDRE internally passes `ignore_unverified_devices=True` to nio's `room_send` when `encryption_mode` is not `"plaintext"`. This is **not a MEDRE design choice** — nio does not support cross-signing (MSC1756) and provides no API for programmatic device verification, making this flag mandatory for every nio-based automated E2EE client. This is applied automatically and is not an operator toggle.
4. **Was the room key shared?** If the adapter joined the encrypted room before the crypto store was initialized, room keys may not have been distributed to this device. Sending a message from another client into the room after the adapter is running with E2EE should trigger key distribution.

### 14.17 E2EE: `ImportError` or `ModuleNotFoundError` for `vodozemac`

The `.[matrix-e2e]` extra was not installed or `vodozemac` failed to build. `vodozemac` is a Rust library and requires a Rust toolchain to build from source. On most systems, pre-built wheels are available via PyPI.

```bash
pip install -e ".[matrix-e2e]"
```

If the wheel is not available for your platform, install Rust first:

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
pip install -e ".[matrix-e2e]"
```

### 14.18 Adapter repeatedly reconnects (health oscillates between `degraded` and `healthy`)

The homeserver is intermittently unavailable or the network is unstable. Check:

1. **Homeserver health.** Is the homeserver restarting or under load? Check its logs.
2. **Network connectivity.** Is the link between the adapter and homeserver stable? Run a sustained `curl` test:
   ```bash
   while true; do curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8008/_matrix/client/versions; sleep 1; done
   ```
3. **Reconnect budget.** Check `reconnect_attempts` in diagnostics. If it keeps resetting to 0, recovery is succeeding but the underlying problem persists.

### 14.19 Adapter stuck in `degraded` state with increasing `reconnect_attempts`

The reconnect budget has not been exhausted yet, but every reconnect attempt is failing. Common causes:

1. **Homeserver is down.** Check that the homeserver process is running.
2. **Network partition.** The adapter cannot reach the homeserver's HTTP port.
3. **DNS resolution failure.** If using a hostname (not `localhost`), DNS may be failing.

If the reconnect budget is exhausted, the adapter transitions to `failed`. A manual restart is needed after the underlying issue is resolved.

### 14.20 Adapter in `failed` state after reconnect budget exhausted

The adapter tried to recover from a transient error but used all its reconnect attempts without success. To recover:

1. Diagnose and fix the underlying issue (homeserver down, network failure, etc.).
2. Restart the adapter: `stop()` then `start()`, or restart the runner process.
3. If using Docker, the `--restart on-failure` policy handles this automatically.

### 14.21 Delivery retries producing duplicate messages in the room

This is a known trade-off of the delivery retry mechanism (see section 12A.4). The first send attempt may have succeeded at the homeserver, but the response was lost. The retry produces a duplicate.

Mitigation: monitor rooms for duplicates during network instability. There is no deduplication mechanism in the alpha. This is a known limitation, not a bug.

### 14.22 `crypto_store_loaded` is `False` after restart with `.[matrix-e2e]`

The crypto store was not loaded on startup. Check:

1. **Is the state directory writable?** The adapter derives its store path under the state directory. Ensure the directory exists and is writable.
2. **Is the store directory empty?** On first run, the store is created but `crypto_store_loaded` reflects whether an existing store was loaded. A fresh store creation is not an error.
3. **File permissions.** The adapter needs read/write access to the store path directory.


## 13. E2EE Text Alpha

This section describes how to operate MEDRE in encrypted rooms. **Plaintext alpha remains the primary and recommended path.** E2EE text alpha is an add-on for operators who need encrypted rooms.

### 13.1 Install E2EE dependencies

```bash
pip install -e ".[matrix-e2e]"
```

This installs `mindroom-nio[e2e]` which adds `vodozemac` (Olm/Megolm), `peewee` (SQLite store), `atomicwrites`, and `cachetools`.

Verify installation:

```bash
python -c "import nio; print('ENCRYPTION_ENABLED:', nio.crypto.ENCRYPTION_ENABLED)"
```

Should print `ENCRYPTION_ENABLED: True`.

### 13.2 Encrypted room setup

Encrypted rooms must be created via a Matrix client (Element, etc.) or the Matrix room creation API. The adapter does not create rooms or toggle encryption.

1. Create a room in Element and enable encryption (toggle in room settings).
2. Invite the bot user to the room.
3. Accept the invite from the bot account.
4. Copy the room ID (format: `!opaque:server`).
5. Add the room ID to `MATRIX_ROOM_ALLOWLIST`.

### 13.3 Configuration

In addition to the standard alpha environment variables, E2EE text alpha requires setting the encryption mode:

```bash
export MATRIX_ENCRYPTION_MODE="e2ee_required"    # or "e2ee_optional"
export MATRIX_ROOM_ALLOWLIST="!encrypted:localhost"  # include the encrypted room
```

The adapter discovers its device ID via `whoami()` and derives an internal store path automatically. No operator configuration of `device_id` or `store_path` is needed.

### 13.4 Device identity (automatic)

- The adapter discovers its device ID via the Matrix `whoami()` endpoint on startup.
- The device ID is stable as long as the access token is associated with the same device.
- No operator configuration is required. The `device_id` field on `MatrixConfig` is reserved for test harnesses and internal use.
- If the access token is regenerated (e.g. re-login), a new device ID may be associated and the adapter will discover it automatically.

### 13.5 First-run expectations

On first run with E2EE enabled:

1. The adapter discovers its device ID via `whoami()` using the access token.
2. The adapter creates the nio client with `encryption_enabled=True` (nio's internal `ClientConfig` flag, automatic when `ENCRYPTION_ENABLED` is `True` at the nio library level). MEDRE triggers this by setting `encryption_mode` to a non-plaintext value.
3. `restore_login` creates a new crypto store in the internal store path (no existing store found).
4. The first `sync_forever` iteration uploads device keys (identity keys + one-time keys) to the homeserver. This registers the device.
4. Subsequent sync iterations handle key query, key claim, and group session sharing automatically.
5. The adapter's device appears as a new device on the bot's Matrix account. Other users in encrypted rooms will see an unverified device.

For the sender's encrypted messages to be decryptable by the adapter, the sender's client must encrypt for the adapter's device. This typically happens automatically on the next message sent after the adapter joins.

### 13.6 Restart expectations

On subsequent runs with the same access token and state directory:

1. The adapter discovers its device ID via `whoami()`.
2. `restore_login` loads the existing crypto store from the internal store path.
3. `logged_in=True` on success.
3. The Olm machine and all previously received room keys are restored.
4. `sync_forever` resumes from the last sync position.
5. Device verification state is preserved.

If `store_path` is changed or the store is deleted, the adapter creates a new crypto identity. Previous room keys are lost and previously decryptable messages become undecryptable.

### 13.7 What works in E2EE text alpha

| Capability | Status |
|---|---|
| Inbound encrypted text decryption | Working — `MegolmEvent` auto-decrypted to `RoomMessageText` during sync |
| Outbound encrypted text | Working — `room_send` auto-encrypts for encrypted rooms |
| Crypto store persistence | Working — store loads on `restore_login`, saves incrementally during sync |
| Automatic key management | Working — upload/query/claim/share handled by `sync_forever` |
| Plaintext rooms | Working — identical behavior to plaintext alpha |

### 13.8 What does NOT work in E2EE text alpha

| Feature | Status | Notes |
|---------|--------|-------|
| Reactions (`m.annotation`) | Not supported | No callback registered |
| Edits (`m.replace`) | Not supported | Edited messages appear as new messages |
| Media / attachments | Not supported | Text only |
| Cross-signing | Not supported | nio does not implement cross-signing (MSC1756); device verification via cross-signing not available |
| Key backup / export / import | Not supported | Not wired |
| Interactive device verification (emoji/QR) | Not supported | `Sas` class exists but not wired |
| Unverified device policy | Required by upstream nio | MEDRE internally passes `ignore_unverified_devices=True` to nio's `room_send` when `encryption_mode` is not `"plaintext"`. This is required by nio due to its lack of cross-signing support (MSC1756). No operator toggle. See contract 25 §5.2. |
| Redactions / deletes | Not supported | Not handled |

**Note:** Undecryptable event logging was previously unsupported but is now implemented. `MegolmEvent` callbacks count events, log warnings (event_id/room_id only, no session_id), and do not forward to the canonical pipeline. `RoomEncryptionEvent` callbacks set `encrypted_room_seen` and are not forwarded.


## Appendix A: Manual Wiring (Advanced/Developer Reference)

The runner (`medre run`) is the primary way to operate MEDRE in alpha mode. This appendix documents the manual wiring pattern for developers who need to construct the adapter and context by hand, for example when testing a specific subsystem in isolation or building a custom pipeline.

```python
import asyncio
import os

from medre.adapters.matrix import MatrixAdapter, MatrixConfig
from medre.core.contracts.adapter import AdapterContext

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
