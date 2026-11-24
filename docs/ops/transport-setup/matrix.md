# Matrix Transport Setup

Install the provider in the same environment as MEDRE using the
[installation guide](../install.md). Activate a project environment before
invoking CLI tools; persistent uv tools already expose `medre` on PATH.
The provider supplies both its SDK-free fake and its native SDK dependencies.

Setting up and running the MEDRE Matrix adapter against a real homeserver.

For an ordinary bridge, use a dedicated account on a homeserver you already
have access to. You do not need homeserver administrator access. Server setup
below is optional for people hosting their own Matrix service. For custom
presentation and the separate application-service design, see
[message presentation](../message-presentation.md).

## Before you begin

### Prerequisites

| Requirement       | Details                                                                                                                                                                                                   |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Matrix homeserver | Synapse or Conduit, local or reachable over the network                                                                                                                                                   |
| Bot account       | A dedicated Matrix user, not your personal account                                                                                                                                                        |
| Python            | 3.12 or later                                                                                                                                                                                             |
| Package install   | Install matrix in the MEDRE environment once; see [installation](../install.md). The distribution declares its Olm/Megolm crypto backend as a dependency, so there is no separate crypto extra to select. |
| Access token      | Obtained via login API or Element UI                                                                                                                                                                      |
| A test room       | Encrypted, bot has joined it (the setup below defaults to encrypted rooms)                                                                                                                                |
| Network access    | Your machine can reach the homeserver's HTTP(S) port                                                                                                                                                      |

You do not need Docker, a domain name, or federation. MEDRE connects to an existing homeserver account; it does not host or administer the homeserver. Running a local Synapse or Conduit server for provider testing is covered in the [developer testing guide](../../dev/testing.md#local-synapse-homeserver-for-matrix-testing).

## Set up

### Token Generation and E2EE Identity Setup

#### MEDRE auth login (recommended for E2EE)

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

#### Explicit cross-signing recovery

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

#### Login API (curl)

The bot account must already exist on the homeserver; create it as you would
any other account. On a locally hosted test server, the Synapse registration
tool creates users; see the
[developer testing guide](../../dev/testing.md#local-synapse-homeserver-for-matrix-testing).

```bash
# Get a token
curl -s -X POST \
  -d '{"type":"m.login.password","user":"bot","password":"secret"}' \
  http://localhost:8008/_matrix/client/v3/login
```

The response JSON includes an `access_token` field. Copy that value.

#### Element UI

Open Element, log in as the bot user, go to Settings → Help and About → copy the access token.

#### Token handling

Do not commit the token. Do not log the token. Set it as an environment variable and leave it there. The `MatrixConfig.__repr__` method redacts the token in log output, but you are responsible for not leaking it yourself.

### Room Setup

For a scripted private encrypted test room, prefer the provisioning command
(requires completed credentials from `medre adapter matrix auth login`):

```bash
medre adapter matrix provision \
  --space-name "Lab Space" \
  --room-name "Lab Room" \
  --invite '@colleague:matrix.org' \
  --admin '@colleague:matrix.org'
```

This creates one private (invite-only, still federating) space and one
private encrypted room linked to it, writes `m.room.encryption`
(`m.megolm.v1.aes-sha2`) in the room's creation `initial_state`, invites the
requested users to both, and pre-assigns their admin power 100 so it is
effective on join. The command verifies the encryption algorithm, power
read-back, and parent/child linkage from server state, and prints room/space
IDs plus permalinks (IDs are not credentials).

Manual alternative (plaintext or hand-managed rooms):

1. Open a Matrix client (Element, or any other).
2. Create a new room. Give it any name.
3. Invite the bot user to the room.
4. Accept the invite from the bot account (log in as the bot in a second client session or via the join API).
5. Copy the room ID exactly. Legacy rooms use `!opaquestring:localhost`;
   room version 12 uses a domainless `!opaque_id`. Both forms work. Room aliases
   (the `#name:server` form) will not work in the allowlist.
6. Confirm the room is unencrypted for plaintext testing. If the room has a lock icon in Element, it is encrypted — see E2EE section below.

### Allowlist Configuration

The adapter accepts an optional `room_allowlist`: a set of room IDs. When set, the inbound callback ignores messages from any room not in the set. When unset (`None`), the adapter accepts messages from all rooms.

Always set the allowlist to exactly the room(s) you intend to monitor. Running without an allowlist means the adapter processes every message from every room the bot has joined.

Set `MEDRE_ADAPTER__BRIDGE__ROOM_ALLOWLIST` for the instance named `bridge`.
Environment overrides use comma-separated room IDs; YAML uses a list:

```bash
export MEDRE_ADAPTER__BRIDGE__ROOM_ALLOWLIST='!abc123:localhost,!def456:localhost'
```

### Running

#### Configure and start the runtime

Save a private `matrix.yaml` with an explicit instance and room allowlist:

```yaml
adapters:
  matrix:
    bridge:
      adapter_kind: real
      encryption_mode: e2ee_required
      room_allowlist: ["!abc123:localhost"]
routes: {}
```

New private rooms and direct messages in Element are encrypted by default, so
the example requires encryption: `plaintext` is the option for an unencrypted
room, and in an encrypted room it cannot decrypt any message, so nothing is
processed. `e2ee_optional` falls back to plaintext when the crypto library does
not load; `e2ee_required` fails startup instead of downgrading.

Supply account settings through MEDRE's environment overrides or the provider's
credential sidecar. The instance token matches `adapters.matrix.bridge`:

```bash
export MEDRE_ADAPTER__BRIDGE__HOMESERVER=http://localhost:8008
export MEDRE_ADAPTER__BRIDGE__USER_ID=@bot:localhost
export MEDRE_ADAPTER__BRIDGE__ACCESS_TOKEN="<matrix-access-token>"

medre config check --config matrix.yaml
medre run --config matrix.yaml
```

This example starts one Matrix connection. Add a second adapter and create the
routes with [guided configuration](../guided-configuration.md) to relay messages.
`MATRIX_*` variables belong to test fixtures and direct harnesses; `medre run`
uses `MEDRE_ADAPTER__<TOKEN>__<FIELD>` instead. Set the sync wait with
`MEDRE_ADAPTER__BRIDGE__INITIAL_SYNC_TIMEOUT_SECONDS` and the request long-poll
with `MEDRE_ADAPTER__BRIDGE__SYNC_TIMEOUT_MS` when overriding their defaults.

## Verify

### Expected Startup Output

With the default 45-second wait, acknowledged progress precedes completed
startup. A representative log sequence for the configured instance is:

```text
INFO medre.runtime.app Starting adapter matrix.bridge
INFO medre.adapters.bridge Waiting up to 45s for the first Matrix sync acknowledgement
INFO medre.adapters.bridge.session Matrix durable sync acknowledged its first batch (completes_sync=True)
INFO medre.adapters.matrix.adapter Matrix adapter bridge health=healthy reasons=none
INFO medre.adapters.bridge MatrixAdapter bridge started
```

If the wait expires, startup instead reports `degraded` with reason
`waiting_initial_sync_ack` and native recovery remains active. Other failure or
crypto restrictions can also prevent healthy startup. A later acknowledged
sync updates current health without rewriting the startup observation.

### Expected Shutdown Output

```text
INFO  medre  Shutdown requested — stopping
INFO  medre  MatrixAdapter stopped
INFO  medre  PipelineRunner stopped
INFO  medre  Matrix runtime shut down cleanly
```

### Health States

| State      | Meaning                                                                    |
| ---------- | -------------------------------------------------------------------------- |
| `unknown`  | Adapter has not started, or has been stopped                               |
| `healthy`  | Client is connected, logged in, and at least one sync has succeeded        |
| `degraded` | Awaiting first sync, actively reconnecting, or beyond the stale-sync bound |
| `failed`   | Sync task has crashed permanently, or client exists but is not logged in   |

Immediately after authentication, health remains `degraded` until the first successful sync establishes Matrix progress. During a later transient outage, `degraded` means the adapter is actively attempting to restore the sync connection with capped exponential backoff. Transient sync failures keep retrying until reconnection succeeds or the adapter is stopped. The state becomes `failed` only when MEDRE cannot safely continue (for example, a stale sync owner refuses cancellation or durable sync state cannot be reset safely), or when the client is no longer logged in.

### First sync during startup

Authentication and room membership precede sync readiness. With the default
budget, the provider logs `Waiting up to 45s for the first Matrix sync acknowledgement`
while it waits for accepted native progress. `initial_sync_timeout_seconds`
controls the displayed budget; zero disables waiting and emits no wait log.
If it expires, the sync owner stays active, health is `degraded`, and
`waiting_initial_sync_ack` is recorded as the reason until progress arrives. Real sync failures
and reconnects remain visible immediately.

The runtime's `adapter_start_timeout_seconds` bounds the entire adapter start,
including authentication, joins and this wait. Increase it for slow startup
if necessary. See [running MEDRE](../running-medre.md#current-health-after-startup)
for startup versus current health observations.

## Options

Keys not needed for the common case, with the adapter's defaults:

| Key                                        | Default      | What it changes                                                                                               | When to touch it                                              |
| ------------------------------------------ | ------------ | ------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------- |
| `room_allowlist`                           | unset        | Rooms whose messages are accepted inbound                                                                     | Always set it to exactly the rooms you monitor                |
| `encryption_mode`                          | `plaintext`  | `e2ee_required` / `e2ee_optional` operate in encrypted rooms (the crypto backend ships with the distribution) | When the target rooms are encrypted                           |
| `verification_policy`                      | `permissive` | Encrypted-send peer-device trust policy                                                                       | Only for strict refusal of unverified recipient devices       |
| `require_encrypted_rooms`                  | `false`      | Drops events from rooms not established as encrypted                                                          | When every routed room must be encrypted                      |
| `auto_join_rooms`                          | empty        | Room IDs joined at startup                                                                                    | When startup should join rooms without a client joining first |
| `initial_sync_timeout_seconds`             | `45.0`       | Startup wait for the first acknowledged sync; `0` disables                                                    | Slow homeservers or large accounts                            |
| `sync_timeout_ms`                          | `30000`      | Long-poll window per sync request                                                                             | Rarely; sync latency tuning                                   |
| `sync_stale_timeout_seconds`               | `300.0`      | Recycles the sync loop without durable progress; `0` disables                                                 | Only after confirming normal long-poll timing                 |
| `startup_backlog_suppress_seconds`         | `60.0`       | Suppresses events older than the start baseline; `0` disables                                                 | Homeservers with heavy federation lag                         |
| `megolm_key_request_rate_limit_per_minute` | `30`         | Missing-room-key request budget per minute                                                                    | Only when key recovery is rate-limited                        |
| `megolm_key_request_max_inflight`          | `4`          | Concurrent missing-room-key recovery tasks                                                                    | Only when key recovery needs more parallelism                 |
| `metadata_embedding_mode`                  | `safe`       | How MEDRE metadata is embedded in outbound messages                                                           | Presentation changes                                          |
| `origin_label`                             | empty        | Prefix identifying this origin on the other side                                                              | Labeling in mixed bridges                                     |
| `relay_prefix`                             | unset        | Custom rendering template for relayed text                                                                    | Custom presentation                                           |

`device_id` and `store_path` are internal: the adapter discovers the device ID
and derives the store path; see the
[Matrix internals page](../../dev/transport-internals/matrix.md).

### E2EE text validation

The Matrix adapter distribution declares `mindroom-nio[e2e]`, with Olm/Megolm crypto libraries, as a dependency. When `encryption_mode` is set to `e2ee_required` or `e2ee_optional`, the adapter operates in encrypted rooms.

The adapter discovers its device ID via `whoami()` and derives an internal store path automatically — no operator configuration of `device_id` or `store_path` is required. The store persists Olm/Megolm session keys and device keys across restarts.

In `plaintext` mode the adapter does not initialise the crypto subsystem. No device ID discovery or store path is needed.

#### E2EE Limitations

- Encrypted text and media share the peer-device trust gate; native relations and
  retained-media delivery follow their separate capability and attachment rules.
- Own-device cross-signing is supported. `verification_policy: permissive` is the
  default: native encrypted delivery runs without a MEDRE peer-trust scan or
  trust-gap warnings. Strict refusal diagnostics remain available.
- Opt-in `verification_policy: strict` refuses encrypted sends until every
  recipient device is affirmatively verified in MEDRE's own nio store. This
  includes sibling devices of the bot account. Missing membership, keys, or trust
  data produces a retryable refusal; no plaintext fallback occurs.
- Cross-signing MEDRE's own device or verifying peers in another client does
  **not** establish local nio trust. MEDRE provides no operator peer-verification
  workflow; retain permissive mode when that trust cannot be established.
- No room-key backup/import/export or interactive verification workflow is managed by
  MEDRE.
- Access token is a plain string in config/credential sidecar (no automatic token
  rotation).

## Troubleshooting

| Symptom                                      | Likely cause                                                                                                          | Fix                                                                                                                                                                                                                                                                                              |
| -------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `M_UNKNOWN_TOKEN` on startup                 | Expired or invalid access token                                                                                       | Generate a new token via login API or Element                                                                                                                                                                                                                                                    |
| `M_FORBIDDEN Invalid username/password`      | Wrong credentials                                                                                                     | Verify user ID and password encoding                                                                                                                                                                                                                                                             |
| Adapter enters `failed` state                | Unsafe stale-sync recycle, durable sync-state reset failure, lost login state, or another fail-closed lifecycle error | Check logs, fix the underlying cause, then restart                                                                                                                                                                                                                                               |
| Repeated `stale_sync_recoveries`             | Homeserver/network sync loop is not making durable progress                                                           | Check homeserver reachability and sync latency; tune `sync_stale_timeout_seconds` only after confirming normal long-poll timing (a positive value must exceed `sync_timeout_ms/1000 + 15`, or 15 seconds flat when the sync timeout is disabled, so healthy long-poll requests are not recycled) |
| Rising `megolm_recovery_rate_limited`        | Missing-room-key recovery is exceeding the configured network request budget                                          | Investigate crypto/key availability; avoid raising the limit until the undecryptable-event source is understood                                                                                                                                                                                  |
| No inbound events received                   | Room not in allowlist                                                                                                 | Add the room ID to the instance's `room_allowlist` or its `MEDRE_ADAPTER__<TOKEN>__ROOM_ALLOWLIST` override                                                                                                                                                                                      |
| Self-messages not suppressed                 | sender mismatch                                                                                                       | Verify the instance's `MEDRE_ADAPTER__<TOKEN>__USER_ID` matches the bot's MXID exactly                                                                                                                                                                                                           |
| `OlmUnverifiedDeviceError` in encrypted room | Peer-device permissive send policy not applied                                                                        | Update to current MEDRE version; E2EE sends intentionally permit unverified peer devices                                                                                                                                                                                                         |
| `cross_signing_reset_required=true`          | Local/server own-device identity state disagrees                                                                      | Back up state; restore the matching E2EE store or use the explicit password-authenticated reset workflow                                                                                                                                                                                         |
| `cross_signing_chain_status=missing`         | No own-device cross-signing identity is established                                                                   | Re-run `medre adapter matrix auth login --adapter-id <id>` with a fresh password                                                                                                                                                                                                                 |
| `ENCRYPTION_ENABLED=False` in diagnostics    | The nio crypto backend did not load in this environment                                                               | Reinstall the Matrix adapter with its declared dependencies; see [installation](../install.md)                                                                                                                                                                                                   |

Cross-signing diagnostic fields are defined in the
[Matrix internals page](../../dev/transport-internals/matrix.md).

### Known Limitations

1. **Continuous auto-reconnect for transient sync failures.** The adapter retries
   for the lifetime of the started adapter with exponential backoff capped at 60 s.
   Authentication/configuration problems still require operator action, and MEDRE
   fails closed if a stale sync owner cannot be terminated safely.
2. **Shutdown is bounded, not lossless before durable admission.** Once an event
   crosses MEDRE's durable ingress boundary it remains pending across restart, but
   transport work cancelled before canonicalization/admission still depends on
   Matrix sync/checkpoint recovery.
3. **Durable ingress is internal, not a Matrix-side queue.** Canonical events, native
   refs, and pending work are committed before normal `publish_inbound()` returns;
   persistent transport recovery still depends on the Matrix checkpoint contract.
4. **Server-directed outbound backpressure, not proactive shaping.** MEDRE
   intercepts room-send rate limits before mindroom-nio sleeps/retries them. A valid
   `retry_after_ms` becomes a durable retry hint; a positive value also extends the
   shared Matrix-adapter cooldown. Durable retries are scheduled no earlier than the
   larger of retry-policy backoff and the server hint; missing or invalid hints remain
   transient failures without a shared cooldown.
   MEDRE leaves client-global 429 handling unchanged for sync, join, and key-management
   requests, and does not guess a fixed send rate before the homeserver supplies
   rate-limit feedback.
5. **Single-room testing only.** Multi-room behavior has not been tested against a
   real homeserver.
6. **Reconnect does not recover from permanent auth failures.** Revoked/expired
   tokens require a new token and manual restart.
7. **No metrics endpoint.** `medre diagnostics --format prometheus` exports a
   scrape-compatible text snapshot, but MEDRE does not run a Prometheus HTTP
   endpoint.

### Sharing a room with MMRelay

[MMRelay](https://github.com/jeremiah-k/meshtastic-matrix-relay) can bridge
its Meshtastic mesh into the same room this adapter monitors: set the room in
`room_allowlist`, and give the MMRelay operator this bot's Matrix user ID to
list under `matrix.trusted_relays` on their side. Keep one relay per radio
mesh: two relays attached to the same mesh while sharing a room relay each
other's messages back until the mesh and the room flood. See
[sharing a room with MMRelay](../shared-rooms-with-mmrelay.md) for
verification in both directions, MMRelay-side warnings, privacy notes, and
account migration.

## Reference

- Device identity and crypto store behavior, validation harnesses, and Classic
  Sync durability:
  [Matrix transport internals](../../dev/transport-internals/matrix.md)
- [guided configuration](../guided-configuration.md) — the route builder used
  to connect two adapters

### See Also

- [live-validation/matrix.md](../live-validation/matrix.md) — live smoke test procedures
- [live-validation/matrix-meshtastic.md](../live-validation/matrix-meshtastic.md) — Matrix ↔ Meshtastic cross-transport bring-up
- [diagnostics-and-evidence.md](../diagnostics-and-evidence.md) — evidence provenance and bundle collection
- [recovery-and-replay.md](../recovery-and-replay.md) — crash recovery and replay
