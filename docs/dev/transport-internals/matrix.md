# Matrix transport internals

Device identity and crypto-store behavior, validation harnesses, and Classic
Sync durability behind the [Matrix transport setup guide](../../ops/transport-setup/matrix.md).
The normative contract lives in the
[Matrix transport profile](../../spec/transport-profiles/matrix.md).

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
authenticated `medre adapter matrix auth login` workflow in the
[Matrix transport setup guide](../../ops/transport-setup/matrix.md).

### Cross-signing diagnostics

| Key                                        | Meaning                                                                                                                 |
| ------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------- |
| `cross_signing_provider_supported`         | The installed Matrix SDK exposes the required cross-signing lifecycle API.                                              |
| `cross_signing_local_identity_present`     | The runtime E2EE store contains persisted local cross-signing identity material.                                        |
| `cross_signing_server_identity_present`    | The homeserver exposes a master cross-signing identity for the bot account.                                             |
| `cross_signing_current_device_self_signed` | The server-visible current device carries the expected self-signing signature.                                          |
| `cross_signing_chain_status`               | `unchecked`, `unsupported`, `missing`, `repairable`, `valid`, `mismatch`, `unverifiable`, `reset_required`, or `error`. |
| `cross_signing_repair_required`            | Safe bootstrap/repair work remains; ordinary runtime will not rotate identity material.                                 |
| `cross_signing_reset_required`             | Local/server identity disagreement or lost local identity requires operator recovery.                                   |
| `cross_signing_last_failure_category`      | Secret-free category for the latest reconciliation failure.                                                             |

These fields never include cross-signing keys, signatures, access tokens, room
keys, sidecar contents, or crypto objects.

## Validation Procedures

### Outbound Validation

1. Start the adapter.
2. Trigger a `deliver()` call with a rendered text message targeting your test room.
3. Open the room in Element. Confirm the message appears.
4. Check the return value from `deliver()`. Its `native_message_id` should be the Matrix event ID, starting with `$`.

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

pytest ../medre-adapter-matrix/tests/test_matrix_live.py::TestMatrixLiveSmoke::test_inbound_message_received -m live -v
```

While the test waits (30 s window), send a message from the second account. If no second account is available, the test will xfail — deterministic unit tests in `../medre-adapter-matrix/tests/test_matrix_adapter.py` cover the same logic paths.

`MATRIX_*` variables belong to this test harness and direct harnesses; the
`medre run` runtime uses `MEDRE_ADAPTER__<TOKEN>__<FIELD>` overrides instead.

### Self-message Suppression Validation

1. Start the adapter.
2. Send a message through the adapter using `deliver()`.
3. Wait for the sync loop to echo the message back.
4. Confirm that `publish_inbound()` is not called for the echoed message.

### Diagnostics Counters

Counter definitions are authoritative in the
[Matrix transport profile](../../spec/transport-profiles/matrix.md) Diagnostics
Keys table. The inbound counters (`inbound_published`,
`inbound_duplicate_admissions`, `inbound_suppressed_self`,
`inbound_suppressed_envelope`, `inbound_filtered_allowlist`,
`inbound_filtered_encryption_policy`, `inbound_suppressed_startup`,
`inbound_suppressed_stale`) all reset
when the adapter starts.

`inbound_filtered_encryption_policy` grows when `require_encrypted_rooms`
drops an event because the room is not established as encrypted. Dropped events
never reach decode or durable admission, but the sync checkpoint still advances,
so a counter that keeps growing while `inbound_published` stays flat means the
adapter treats the room as unencrypted (fail-closed).

## Classic Sync durability and recovery

Runtime-managed Matrix adapters use MEDRE-owned Classic Sync checkpoints. The
Matrix SDK may reconstruct a limited timeline before MEDRE sees its following live
event, but the checkpoint does not advance until every relevant timeline event has
passed durable admission. If admission fails, the SDK retains the event for replay.

`LIVE` and `RECOVERED` events are admitted and routed. `HISTORY` is recorded as a
durable suppression and is not routed. This replaces the older first-sync timestamp
heuristic for runtime-managed Matrix adapters. Separately from provenance, each
adapter lifetime suppresses events whose origin server timestamp predates its start
baseline by more than `startup_backlog_suppress_seconds` (default 60 seconds;
`0` disables) before decode, whichever path delivered them, so a restart does not
replay offline room history over destination transports.

With runtime storage available, the Matrix adapter intentionally does not enable
nio-owned sync-token persistence or persisted recovery state. MEDRE stores the
committed cursor in its primary SQLite database so plaintext and E2EE modes use the
same checkpoint-ownership model. Recovery-abandonment metadata is stored with that
checkpoint and exposed in adapter diagnostics. Without runtime storage, MEDRE omits
the durable callbacks and mindroom-nio retains ordinary sync-token persistence; this
fallback is non-durable and does not claim MEDRE-owned recovery.
