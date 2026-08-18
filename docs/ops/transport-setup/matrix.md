# Matrix Transport Setup

Setting up and running the MEDRE Matrix adapter against a real homeserver. Pre-release — no stable public API.

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

## Token Generation and E2EE Identity Setup

### MEDRE auth login (recommended for E2EE)

For an encrypted Matrix adapter, use MEDRE's auth command with the adapter ID
that appears under `adapters.matrix` in the runtime configuration. The password
is used transiently for Matrix login and any homeserver UIA challenge needed to
bootstrap cross-signing; MEDRE persists the access token/device ID and E2EE
crypto state, but never persists the password.

```bash
medre adapter matrix auth login \
  --homeserver https://matrix.example.com \
  --user @bot:example.com \
  --adapter-id bridge
```

For automation, pipe the password instead of placing it on the command line:

```bash
printf '%s\n' "$MATRIX_PASSWORD" | \
  medre adapter matrix auth login \
    --homeserver https://matrix.example.com \
    --user @bot:example.com \
    --adapter-id bridge \
    --password-stdin
```

The `--adapter-id` is important: it selects the same runtime E2EE store used by
the configured adapter at `{state_dir}/adapters/{adapter_id}/matrix/store`.
Without `--adapter-id`, the command keeps its older SDK-free behavior and only
obtains/verifies/persists Matrix credentials; it does not prepare cross-signing
state.

Cross-signing bootstrap verifies the complete server-visible chain before the
credential sidecar is written: MEDRE master key → self-signing key → current
MEDRE device. Existing matching identity material is reused. Ordinary startup
will never replace master/self-signing identity material.

### Explicit cross-signing recovery

If diagnostics report `cross_signing_reset_required=true`, first back up the
Matrix state directory. Then run the same authenticated login with the explicit
reset switch:

```bash
medre adapter matrix auth login \
  --homeserver https://matrix.example.com \
  --user @bot:example.com \
  --adapter-id bridge \
  --reset-cross-signing
```

This operation is intentionally destructive: it may replace the account's
master/self-signing identity and therefore change how other Matrix clients view
the bot device. It requires a fresh password. Do not use it merely because the
local cross-signing sidecar is missing; restore the backed-up E2EE store first
when possible. MEDRE refuses automatic rotation when the homeserver already has
an identity that does not match local state.

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
6. Confirm the room is unencrypted for plaintext testing. If the room has a lock icon in Element, it is encrypted — see E2EE section below.

## Allowlist Configuration

The adapter accepts an optional `room_allowlist`: a set of room IDs. When set, the inbound callback ignores messages from any room not in the set. When unset (`None`), the adapter accepts messages from all rooms.

Always set the allowlist to exactly the room(s) you intend to monitor. Running without an allowlist means the adapter processes every message from every room the bot has joined.

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
| `MATRIX_ACCESS_TOKEN`    | Yes      |                | `<matrix-access-token>`         | Keep it secret                 |
| `MATRIX_ROOM_ALLOWLIST`  | No       | (all rooms)    | `!abc:localhost,!def:localhost` | Comma-separated room IDs       |
| `MATRIX_ADAPTER_ID`      | No       | `matrix-alpha` | `my-adapter`                    | Adapter identifier for logging |
| `MATRIX_SYNC_TIMEOUT_MS` | No       | `30000`        | `60000`                         | Sync long-poll timeout in ms   |

### Via medre run

```bash
export MEDRE_ADAPTER__BRIDGE__HOMESERVER=http://localhost:8008
export MEDRE_ADAPTER__BRIDGE__USER_ID=@bot:localhost
export MEDRE_ADAPTER__BRIDGE__ACCESS_TOKEN="<matrix-access-token>"
export MEDRE_ADAPTER__BRIDGE__ROOM_ALLOWLIST='["!abc123:localhost"]'

medre run
```

The `MEDRE_ADAPTER__<TOKEN>__<FIELD>` variables follow the runtime
configuration convention. `<TOKEN>` matches the adapter instance name under
`adapters.matrix.<token>` in YAML. The `MATRIX_*` variables are used only by
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
INFO  medre  Matrix runtime: config loaded for @bot:localhost
INFO  medre  PipelineRunner started
INFO  medre  MatrixAdapter matrix-alpha started
INFO  medre  Initial diagnostics: {'status': 'healthy', 'details': {'connected': True, 'logged_in': True, 'sync_task_running': True}}
INFO  medre  Matrix runtime running — awaiting shutdown signal
```

### Expected Shutdown Output

```text
INFO  medre  Shutdown requested — stopping
INFO  medre  MatrixAdapter stopped
INFO  medre  PipelineRunner stopped
INFO  medre  Matrix runtime shut down cleanly
```

## Health States

| State      | Meaning                                                                  |
| ---------- | ------------------------------------------------------------------------ |
| `unknown`  | Adapter has not started, or has been stopped                             |
| `healthy`  | Client is connected, logged in, and sync is running                      |
| `degraded` | Sync is running but actively reconnecting after a transient failure      |
| `failed`   | Sync task has crashed permanently, or client exists but is not logged in |

When the adapter is in `degraded` state, it is actively attempting to restore the sync connection with exponential backoff. Once reconnection succeeds, the state returns to `healthy`. If the reconnect budget is exhausted, the state transitions to `failed` and requires manual restart.

## E2EE text validation

The `.[matrix-e2e]` extra installs `mindroom-nio[e2e]` with Olm/Megolm crypto libraries. When `encryption_mode` is set to `e2ee_required` or `e2ee_optional`, the adapter operates in encrypted rooms.

The adapter discovers its device ID via `whoami()` and derives an internal store path automatically — no operator configuration of `device_id` or `store_path` is required. The store persists Olm/Megolm session keys and device keys across restarts.

In `plaintext` mode the adapter does not initialise the crypto subsystem. No device ID discovery or store path is needed.

### E2EE Limitations

- Text messages only in encrypted rooms. No reactions, edits, media, or attachments.
- Own-device cross-signing is supported with `mindroom-nio 0.40.0`, but MEDRE does not
  yet expose a peer-device verification policy. Encrypted sends intentionally use
  `ignore_unverified_devices=True` for compatibility.
- Cross-signing MEDRE's own device does **not** imply that MEDRE trusts every peer
  device in a room. Peer-device trust remains a separate future policy surface.
- No room-key backup/import/export or interactive verification workflow is managed by
  MEDRE.
- Access token is a plain string in config/credential sidecar (no automatic token
  rotation).

## Device Identity, Cross-Signing, and Crypto Store

The adapter manages device identity and crypto store paths internally. Operators do not
configure `device_id` or `store_path`.

When the adapter starts with a non-plaintext `encryption_mode`, it calls `whoami()` to
discover the device ID. The crypto store path is derived automatically from the resolved
state directory: `{state_dir}/adapters/{adapter_id}/matrix/store`. The store contains
sensitive Olm/Megolm and cross-signing material and must be backed up and protected like
a private key store.

At runtime MEDRE performs a non-destructive own-device cross-signing reconciliation. It
may verify the existing chain or repair a missing current-device self-signature when the
persisted master/self-signing identity matches the homeserver. Runtime startup has no
password and will not bootstrap or rotate master/self-signing keys. Missing/mismatched
identity material is reported through diagnostics and must be handled through the
authenticated auth workflow above.

### Cross-signing diagnostics

| Key | Meaning |
| --- | --- |
| `cross_signing_provider_supported` | The installed Matrix SDK exposes the required cross-signing lifecycle API. |
| `cross_signing_local_identity_present` | The runtime E2EE store contains persisted local cross-signing identity material. |
| `cross_signing_server_identity_present` | The homeserver exposes a master cross-signing identity for the bot account. |
| `cross_signing_current_device_self_signed` | The server-visible current device carries the expected self-signing signature. |
| `cross_signing_chain_status` | `unchecked`, `unsupported`, `missing`, `repairable`, `valid`, `mismatch`, `unverifiable`, `reset_required`, or `error`. |
| `cross_signing_repair_required` | Safe bootstrap/repair work remains; ordinary runtime will not rotate identity material. |
| `cross_signing_reset_required` | Local/server identity disagreement or lost local identity requires operator recovery. |
| `cross_signing_last_failure_category` | Secret-free category for the latest reconciliation failure. |

These fields never include cross-signing keys, signatures, access tokens, room
keys, sidecar contents, or crypto objects.

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
export MATRIX_ACCESS_TOKEN="<matrix-access-token>"
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
| `OlmUnverifiedDeviceError` in encrypted room | Peer-device permissive send policy not applied     | Update to current MEDRE version; E2EE sends intentionally permit unverified peer devices |
| `cross_signing_reset_required=true`          | Local/server own-device identity state disagrees   | Back up state; restore the matching E2EE store or use the explicit password-authenticated reset workflow |
| `cross_signing_chain_status=missing`          | No own-device cross-signing identity is established | Re-run `medre adapter matrix auth login --adapter-id <id>` with a fresh password |
| `ENCRYPTION_ENABLED=False` in diagnostics    | `.[matrix-e2e]` not installed                      | `pip install -e ".[matrix-e2e]"`                                 |

## See Also

- [live-validation/matrix.md](../live-validation/matrix.md) — live smoke test procedures
- [live-validation/matrix-meshtastic.md](../live-validation/matrix-meshtastic.md) — Matrix ↔ Meshtastic cross-transport bring-up
- [diagnostics-and-evidence.md](../diagnostics-and-evidence.md) — evidence provenance and bundle collection
- [recovery-and-replay.md](../recovery-and-replay.md) — crash recovery and replay
