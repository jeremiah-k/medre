# Matrix Live Validation

Live smoke test procedures for the Matrix adapter against a real homeserver.

## Quick Validation

```bash
pip install -e ".[matrix]"

export MATRIX_HOMESERVER=http://localhost:8008
export MATRIX_USER_ID=@bot:localhost
export MATRIX_ACCESS_TOKEN=<matrix-access-token>
export MATRIX_ROOM_ID="!abc123:localhost"

pytest tests/test_matrix_live.py -m live -v
```

Expected: 13 passed / 0 failed / 0 skipped (plaintext alpha path).

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

Expected: 3 passed. Confirms encrypted room creation, encrypted outbound send, and third-party inbound at Docker SDK-boundary via second nio client.

## Third-Party Inbound Test

Requires a second Matrix account sending during the test window:

```bash
export MATRIX_INBOUND_SENDER="@alice:localhost"

pytest tests/test_matrix_live.py::TestMatrixLiveSmoke::test_inbound_message_received -m live -v
```

While the test waits (30 s window), send a message from `@alice:localhost` into `MATRIX_ROOM_ID`. If no second account sends, the test xfails — acceptable. Deterministic unit tests in `tests/test_matrix_adapter.py` cover the same logic paths.

## Test File Reference

| Test file                                        | Marker   | What it validates                                  |
| ------------------------------------------------ | -------- | -------------------------------------------------- |
| `tests/test_matrix_live.py`                      | `live`   | Adapter lifecycle, send/receive, health, reconnect |
| `tests/integration/test_synapse_connectivity.py` | `docker` | SDK connectivity against Docker Synapse            |
| `tests/integration/test_synapse_bridge_smoke.py` | `docker` | Full pipeline with real Matrix SDK                 |
| `tests/integration/test_synapse_e2ee_smoke.py`   | `docker` | E2EE encrypted room lifecycle                      |
| `tests/test_matrix_e2ee_live.py`                 | `live`   | E2EE mode startup and encrypted-room operations    |

## Evidence Tiers Achieved

| Tier           | Sub-class                    | Date       | Result                           |
| -------------- | ---------------------------- | ---------- | -------------------------------- |
| H (historical) | External live (matrix.org)   | 2026-05-10 | 13/13 plaintext, 7/7 E2EE        |
| R              | Docker SDK-boundary          | 2026-05-22 | 15 passed, 1 xfailed             |
| R              | Docker SDK-boundary E2EE     | 2026-05-25 | 3/3 passed                       |
| —              | External live (sk.community) | 2026-05-12 | NOT EXECUTED (token rejected)    |
| —              | External live (matrix.org)   | 2026-05-12 | NOT EXECUTED (password rejected) |

## Known Gaps

- Third-party inbound confirmed at Docker SDK-boundary only; external-live not confirmed.
- No E2EE reactions, edits, deletes, or attachments.
- No cross-signing support in `mindroom-nio`.
- Soak tests: NOT EXECUTED.

## See Also

- [transport-setup/matrix.md](../transport-setup/matrix.md) — adapter setup, config, and troubleshooting
- [diagnostics-and-evidence.md](../diagnostics-and-evidence.md) — evidence provenance and bundle collection
