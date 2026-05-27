# Matrix Transport Setup

Setting up and running the MEDRE Matrix adapter against a real homeserver. Alpha status — not production.

## Prerequisites

| Requirement       | Details                                                                                                                       |
| ----------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| Matrix homeserver | Synapse or Conduit, local or reachable over the network                                                                       |
| Bot account       | A dedicated Matrix user, not your personal account                                                                            |
| Python            | 3.11 or later                                                                                                                 |
| Package install   | `pip install -e ".[matrix]"` (plaintext). `pip install -e ".[matrix-e2e]"` (adds Olm/Megolm crypto libs for encrypted rooms). |
| Access token      | Obtained via login API or Element UI                                                                                          |
| A test room       | Unencrypted, bot has joined it                                                                                                |
| Network access    | Your machine can reach the homeserver's HTTP(S) port                                                                          |

You do not need Docker, a domain name, or federation. A local homeserver on localhost is sufficient.

## Homeserver Setup

### Synapse via pip (recommended)

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

### Conduit (lightweight alternative)

Download a binary from conduit.rs or build from source. Conduit starts on port 6167 by default.

### Docker (optional)

```bash
docker run -d --name synapse -p 8008:8008 \
  -e SYNAPSE_SERVER_NAME=localhost \
  -e SYNAPSE_REPORT_STATS=no \
  matrixdotorg/synapse:latest
```

## Token Generation

### Login API (curl)

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

### Element UI

Open Element, log in as the bot user, go to Settings → Help and About → copy the access token.

### Token handling

Do not commit the token. Do not log the token. Set it as an environment variable and leave it there. The `MatrixConfig.__repr__` method redacts the token in log output, but you are responsible for not leaking it yourself.

## Room Setup

1. Open a Matrix client (Element, or any other).
2. Create a new room. Give it any name.
3. Invite the bot user to the room.
4. Accept the invite from the bot account (log in as the bot in a second client session or via the join API).
5. Copy the room ID. It looks like `!opaquestring:localhost`. Room aliases (the `#name:server` form) will not work in the allowlist.
6. Confirm the room is unencrypted for plaintext alpha. If the room has a lock icon in Element, it is encrypted — see E2EE section below.

## Allowlist Configuration

The adapter accepts an optional `room_allowlist`: a set of room IDs. When set, the inbound callback ignores messages from any room not in the set. When unset (`None`), the adapter accepts messages from all rooms.

In alpha mode, always set the allowlist to exactly the room(s) you intend to monitor. Running without an allowlist means the adapter processes every message from every room the bot has joined.

Environment variable: `MATRIX_ROOM_ALLOWLIST` — a comma-separated list of room IDs.

```bash
export MATRIX_ROOM_ALLOWLIST="!abc123:localhost,!def456:localhost"
```

## Running

### Environment Variables

| Variable                 | Required | Default        | Example                         | Notes                          |
| ------------------------ | -------- | -------------- | ------------------------------- | ------------------------------ |
| `MATRIX_HOMESERVER`      | Yes      |                | `http://localhost:8008`         | Full URL, no trailing slash    |
| `MATRIX_USER_ID`         | Yes      |                | `@bot:localhost`                | Starts with `@`                |
| `MATRIX_ACCESS_TOKEN`    | Yes      |                | `syt_xxxxxxxxxxxxx`             | Keep it secret                 |
| `MATRIX_ROOM_ALLOWLIST`  | No       | (all rooms)    | `!abc:localhost,!def:localhost` | Comma-separated room IDs       |
| `MATRIX_ADAPTER_ID`      | No       | `matrix-alpha` | `my-adapter`                    | Adapter identifier for logging |
| `MATRIX_SYNC_TIMEOUT_MS` | No       | `30000`        | `60000`                         | Sync long-poll timeout in ms   |

### Via medre run

```bash
export MEDRE_ADAPTER__BRIDGE__HOMESERVER=http://localhost:8008
export MEDRE_ADAPTER__BRIDGE__USER_ID=@bot:localhost
export MEDRE_ADAPTER__BRIDGE__ACCESS_TOKEN=syt_xxxxxxxxxxxxx
export MEDRE_ADAPTER__BRIDGE__ROOM_ALLOWLIST='["!abc123:localhost"]'

medre run
```

The `MEDRE_ADAPTER__<TOKEN>__<FIELD>` variables follow the runtime
configuration convention. `<TOKEN>` matches the adapter instance name under
`[adapters.matrix.<token>]` in TOML. The `MATRIX_*` variables are used only by
pytest fixtures and direct adapter instantiation (see the table above).

The runner:

1. Configures logging (INFO level to stderr).
2. Reads and validates all environment variables into a `MatrixConfig`.
3. Creates subsystems: `EventBus`, `RenderingPipeline`, `SQLiteStorage`, `Diagnostician`, `Router`.
4. Registers the `MatrixRenderer` on the rendering pipeline.
5. Creates the `MatrixAdapter` with the validated config.
6. Wires a `PipelineRunner` with all subsystems.
7. Registers signal handlers for SIGINT and SIGTERM.
8. Starts the `PipelineRunner`, then starts the `MatrixAdapter`.
9. Logs initial diagnostics.
10. Waits for a shutdown signal.
11. On shutdown: stops the adapter, stops the pipeline runner, closes the database.

### Expected Startup Output

```text
INFO  medre  Matrix Operation Alpha: config loaded for @bot:localhost
INFO  medre  PipelineRunner started
INFO  medre  MatrixAdapter matrix-alpha started
INFO  medre  Initial diagnostics: {'status': 'healthy', 'details': {'connected': True, 'logged_in': True, 'sync_task_running': True}}
INFO  medre  Matrix Operation Alpha running — awaiting shutdown signal
```

### Expected Shutdown Output

```text
INFO  medre  Shutdown requested — stopping
INFO  medre  MatrixAdapter stopped
INFO  medre  PipelineRunner stopped
INFO  medre  Matrix Operation Alpha shut down cleanly
```

## Health States

| State      | Meaning                                                                  |
| ---------- | ------------------------------------------------------------------------ |
| `unknown`  | Adapter has not started, or has been stopped                             |
| `healthy`  | Client is connected, logged in, and sync is running                      |
| `degraded` | Sync is running but actively reconnecting after a transient failure      |
| `failed`   | Sync task has crashed permanently, or client exists but is not logged in |

When the adapter is in `degraded` state, it is actively attempting to restore the sync connection with exponential backoff. Once reconnection succeeds, the state returns to `healthy`. If the reconnect budget is exhausted, the state transitions to `failed` and requires manual restart.

## E2EE Text Alpha

The `.[matrix-e2e]` extra installs `mindroom-nio[e2e]` with Olm/Megolm crypto libraries. When `encryption_mode` is set to `e2ee_required` or `e2ee_optional`, the adapter operates in encrypted rooms.

The adapter discovers its device ID via `whoami()` and derives an internal store path automatically — no operator configuration of `device_id` or `store_path` is required. The store persists Olm/Megolm session keys and device keys across restarts.

In `plaintext` mode the adapter does not initialise the crypto subsystem. No device ID discovery or store path is needed.

### E2EE Limitations

- Text messages only in encrypted rooms. No reactions, edits, media, or attachments.
- No cross-signing support in `mindroom-nio`. The adapter sets `ignore_unverified_devices=True` for non-plaintext modes — required by upstream nio.
- No room key backup, import/export, or interactive device verification.
- Access token is a plain string in config (no secure storage or rotation).
- `mindroom-nio` is a fork; maintenance status relative to upstream is unverified.

## Device Identity and Crypto Store

The adapter manages device identity and crypto store paths internally. Operators do not configure `device_id` or `store_path`.

When the adapter starts with a non-plaintext `encryption_mode`, it calls `whoami()` to discover the device ID. The crypto store path is derived automatically from the resolved state directory: `{state_dir}/adapters/{adapter_id}/matrix/store`.

## Validation Procedures

### Outbound Validation

1. Start the adapter.
2. Trigger a `deliver()` call with a rendered text message targeting your test room.
3. Open the room in Element. Confirm the message appears.
4. Check the return value from `deliver()`. It should contain an `event_id` starting with `$`.

### Inbound Validation

1. Start the adapter with the allowlist set to your test room.
2. From a second Matrix account (not the bot), send a plain text message in the test room.
3. Confirm that `publish_inbound()` is called with a canonical event.
4. Confirm the event's `source_transport_id` matches the sender's MXID, not the bot's.

### Third-party Inbound Validation

```bash
export MATRIX_HOMESERVER="http://localhost:8008"
export MATRIX_USER_ID="@bot:localhost"
export MATRIX_ACCESS_TOKEN="syt_..."
export MATRIX_ROOM_ID="!test:localhost"
export MATRIX_INBOUND_SENDER="@alice:localhost"  # second account

pytest tests/test_matrix_live.py::TestMatrixLiveSmoke::test_inbound_message_received -m live -v
```

While the test waits (30 s window), send a message from the second account. If no second account is available, the test will xfail — deterministic unit tests in `tests/test_matrix_adapter.py` cover the same logic paths.

### Self-message Suppression Validation

1. Start the adapter.
2. Send a message through the adapter using `deliver()`.
3. Wait for the sync loop to echo the message back.
4. Confirm that `publish_inbound()` is not called for the echoed message.

### Diagnostics Counters

| Counter                       | Description                                                  |
| ----------------------------- | ------------------------------------------------------------ |
| `inbound_published`           | Events successfully published via `publish_inbound()`        |
| `inbound_suppressed_self`     | Events dropped because sender == bot user_id                 |
| `inbound_suppressed_envelope` | Events dropped because MEDRE envelope source_adapter matched |
| `inbound_filtered_allowlist`  | Events dropped because room was not in the allowlist         |

## Known Limitations

1. **Bounded auto-reconnect.** The adapter reconnects on transient failures with exponential backoff up to a maximum. Budget exhaustion requires manual restart.
2. **No graceful shutdown signaling.** `stop()` cancels the sync task. Anything in flight is lost.
3. **No inbound queue or persistence.** Inbound events are published directly. No retry, no dead letter queue.
4. **No rate limiting.** The adapter sends as fast as you call `deliver()`. Homeservers rate-limit by default.
5. **Single-room testing only.** Multi-room behavior has not been tested against a real homeserver.
6. **Reconnect does not recover from permanent auth failures.** Revoked/expired tokens require a new token and manual restart.
7. **No metrics.** No Prometheus endpoint, no external metrics export. Only log output, `health_check()`, and `diagnostics()` counters.

## Troubleshooting

| Symptom                                      | Likely cause                                       | Fix                                                              |
| -------------------------------------------- | -------------------------------------------------- | ---------------------------------------------------------------- |
| `M_UNKNOWN_TOKEN` on startup                 | Expired or invalid access token                    | Generate a new token via login API or Element                    |
| `M_FORBIDDEN Invalid username/password`      | Wrong credentials                                  | Verify user ID and password encoding                             |
| Adapter enters `failed` state                | Permanent sync error or exhausted reconnect budget | Check logs, fix underlying cause, restart                        |
| No inbound events received                   | Room not in allowlist                              | Add room ID to `MATRIX_ROOM_ALLOWLIST`                           |
| Self-messages not suppressed                 | sender mismatch                                    | Verify `MATRIX_USER_ID` matches bot's MXID exactly               |
| `OlmUnverifiedDeviceError` in encrypted room | `ignore_unverified_devices` not applied            | Update to current MEDRE version which handles this automatically |
| `ENCRYPTION_ENABLED=False` in diagnostics    | `.[matrix-e2e]` not installed                      | `pip install -e ".[matrix-e2e]"`                                 |

## See Also

- [live-validation/matrix.md](../live-validation/matrix.md) — live smoke test procedures
- [live-validation/matrix-meshtastic.md](../live-validation/matrix-meshtastic.md) — Matrix ↔ Meshtastic cross-transport bring-up
- [diagnostics-and-evidence.md](../diagnostics-and-evidence.md) — evidence provenance and bundle collection
- [recovery-and-replay.md](../recovery-and-replay.md) — crash recovery and replay
