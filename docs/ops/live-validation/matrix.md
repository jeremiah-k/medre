# Matrix Live Validation

Live smoke test procedures for the Matrix adapter against a real homeserver.

## Quick Validation

```bash
pip install -e ".[matrix]"

export MATRIX_HOMESERVER=http://localhost:8008
export MATRIX_USER_ID=@bot:localhost
export MATRIX_ACCESS_TOKEN="<matrix-access-token>"
export MATRIX_ROOM_ID="!abc123:localhost"

pytest tests/test_matrix_live.py -m live -v
```

Expected: 13 passed / 0 failed / 0 skipped (plaintext path).

## Docker SDK-Boundary Tests

No external homeserver needed. Uses a local Docker Synapse container.

```bash
pip install -e ".[matrix,dev]"

# All Docker integration tests
PYTHONPATH=src pytest tests/integration/ -m docker -v

# Matrix (Synapse) only
PYTHONPATH=src pytest tests/integration/test_synapse_connectivity.py -m docker -v

# Synapse bridge smoke (full pipeline: real Matrix SDK -> PipelineRunner -> FakeMatrixAdapter)
PYTHONPATH=src pytest tests/integration/test_synapse_bridge_smoke.py -m docker -v
```

Gate: `MATRIX_LOCAL_SYNAPSE=1`. Docker tests are excluded from default runs.

Expected: 15 passed, 1 xfailed (third-party inbound requires second user during 30s window).

## E2EE Live Validation

```bash
pip install -e ".[matrix-e2e]"

# Docker E2EE harness
MEDRE_SYNAPSE_PORT=8009 pytest tests/integration/test_synapse_e2ee_smoke.py -m docker -v
```

Expected: 4 passed. Confirms cross-signing bootstrap persistence with
passwordless runtime re-verification, encrypted room creation, encrypted
outbound send, and third-party inbound at Docker SDK-boundary via second nio
client.

## Third-Party Inbound Test

Requires a second Matrix account sending during the test window:

```bash
export MATRIX_INBOUND_SENDER="@alice:localhost"

pytest tests/test_matrix_live.py::TestMatrixLiveSmoke::test_inbound_message_received -m live -v
```

While the test waits (30 s window), send a message from `@alice:localhost` into `MATRIX_ROOM_ID`. If no second account sends, the test xfails — acceptable. Deterministic unit tests in `tests/test_matrix_adapter.py` cover the same logic paths.

## Test File Reference

| Test file                                         | Marker       | What it validates                                  |
| ------------------------------------------------- | ------------ | -------------------------------------------------- |
| `tests/test_matrix_live.py`                       | `live`       | Adapter lifecycle, send/receive, health, reconnect |
| `tests/integration/test_synapse_connectivity.py`  | `docker`     | SDK connectivity against Docker Synapse            |
| `tests/integration/test_synapse_bridge_smoke.py`  | `docker`     | Full pipeline with real Matrix SDK                 |
| `tests/integration/test_synapse_e2ee_smoke.py`    | `docker`     | E2EE encrypted room lifecycle                      |
| `tests/test_matrix_e2ee_live.py`                  | `live`       | E2EE mode startup and encrypted-room operations    |
| `tests/test_matrix_sync_checkpoint_ownership.py`  | unit         | MEDRE-owned Classic cursor commit/ack ordering     |
| `tests/test_matrix_durable_admission_boundary.py` | unit         | nio admission rejection and durable handoff        |
| `tests/test_matrix_sync_recovery_sdk_contract.py` | `matrix_sdk` | Pinned mindroom-nio recovery API contract          |

## Evidence Tiers Achieved

| Tier           | Sub-class                    | Date       | Result                           |
| -------------- | ---------------------------- | ---------- | -------------------------------- |
| H (historical) | External live (matrix.org)   | 2026-05-10 | 13/13 plaintext, 7/7 E2EE        |
| docker         | Docker SDK-boundary          | 2026-05-22 | 15 passed, 1 xfailed             |
| docker         | Docker SDK-boundary E2EE     | 2026-05-25 | 3/3 passed                       |
| docker         | Docker SDK-boundary E2EE     | 2026-08-18 | 4/4 passed (incl. cross-signing) |
| —              | External live (sk.community) | 2026-05-12 | NOT EXECUTED (token rejected)    |
| —              | External live (matrix.org)   | 2026-05-12 | NOT EXECUTED (password rejected) |

## Cross-Signing Validation

Before an encrypted live run, prepare the runtime E2EE store with the same
adapter ID used in configuration:

```bash
medre adapter matrix auth login \
  --homeserver https://matrix.example.com \
  --user @bot:example.com \
  --adapter-id bridge
```

After startup, inspect adapter diagnostics. A fully established own-device
identity should report:

- `cross_signing_provider_supported=true`
- `cross_signing_local_identity_present=true`
- `cross_signing_server_identity_present=true`
- `cross_signing_current_device_self_signed=true`
- `cross_signing_chain_status=valid`
- `cross_signing_reset_required=false`

Also verify from a separate trusted Matrix client that the MEDRE device is
shown as cross-signed/verified by the bot account. This validates the observable
postcondition; do not treat a successful upload call alone as sufficient.

If `cross_signing_reset_required=true`, do not delete the E2EE store casually.
Back it up and restore matching state if possible. Use
`--reset-cross-signing` only as explicit password-authenticated recovery.

## Known Gaps

- Third-party inbound confirmed at Docker SDK-boundary only; external-live not
  confirmed.
- No E2EE reactions, edits, deletes, or attachments.
- Peer-device trust remains permissive (`ignore_unverified_devices=True`) and
  is not yet operator-configurable.
- No room-key backup/import/export workflow is managed by MEDRE.
- Soak tests: NOT EXECUTED.

## Durable Classic Sync checkpoint validation

The Phase 2 durable-ingress contract has three validation layers:

```bash
pytest -q tests/test_durable_ingress_storage.py \
  tests/test_durable_ingress_worker.py \
  tests/test_durable_ingress_crash_recovery.py
pytest -q tests/test_matrix_sync_checkpoint_ownership.py \
  tests/test_matrix_durable_admission_boundary.py
pytest -q tests/test_matrix_sync_recovery_sdk_contract.py -m matrix_sdk
```

With the Matrix extra installed, the SDK-contract test verifies the pinned
application-owned Classic Sync surfaces. The storage tests characterize the crash
windows independently of a homeserver: admission is atomic, pending work and the
committed cursor survive restart, stale leases are reclaimable, and native replay
resolves to the original canonical identity.

For Docker/live follow-up, verify a runtime-managed Matrix adapter reports
`checkpoint_owned_by_medre=true`. During forced downtime, continuity-recovered events
must increment `recovered_event_count`; initial cold history may increment
`history_event_count` but must remain durably suppressed. Any unrecoverable room gap
must increment `recovery_abandoned_room_count` and leave secret-free abandonment
evidence in `recovery_last_abandonment`.

## Live Validation

### Matrix <-> radio bridge (six directed paths, opt-in)

`tests/test_live_matrix_radio_bridge.py` runs ONE runtime with four real
adapters (matrix `e2ee_required` + meshtastic serial + meshcore BLE + lxmf
Reticulum) and three explicit bidirectional routes — matrix<->each radio,
no radio<->radio legs. A second bot-account device with its own crypto store
observes the encrypted room: far-side Megolm **decryption** of every leg is
asserted (same account, so it is disclosed as NOT an independent-sender
ingress test). Own-account echo posted by that device must stay suppressed
(negative control). One controlled restart must preserve the device identity
and crypto session.

Opt-in env: `MEDRE_MX_BRIDGE=1`, `MATRIX_HOMESERVER`, `MATRIX_USER_ID`,
`MATRIX_ACCESS_TOKEN`, `MATRIX_ROOM_ID`, `MATRIX_STORE_PATH`,
`MATRIX_OBSERVER_TOKEN`, `MATRIX_OBSERVER_DEVICE_ID`,
`MATRIX_OBSERVER_STORE_PATH`, plus the standard radio peer endpoints
(`MESHTASTIC_MEDRE_SERIAL_PORT`, `MESHTASTIC_PEER_SERIAL_PORT`,
`MESHCORE_MEDRE_BLE_ADDRESS`, `MESHCORE_PEER_BLE_ADDRESS`,
`LXMF_MEDRE_RNS_CONFIG`, `LXMF_MEDRE_IDENTITY`, `LXMF_MEDRE_STORAGE`,
`LXMF_PEER_RNS_CONFIG`, `LXMF_PEER_IDENTITY`). All RF stays on private
lab channels with bounded per-leg TX budgets.

```bash
pytest tests/test_live_matrix_radio_bridge.py -m live -v
```

A genuine Matrix->radio hop with an independent sender requires the invited
human user to post in the room (the bot's own echoes are suppressed by
design); the interactive session is the intended path for that evidence.

## See Also

- [transport-setup/matrix.md](../transport-setup/matrix.md) — adapter setup, config, and troubleshooting
- [diagnostics-and-evidence.md](../diagnostics-and-evidence.md) — evidence provenance and bundle collection
