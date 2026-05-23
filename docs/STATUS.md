# MEDRE Transport Capability Status

> **Generated:** 2026-05-22
>
> **Context:** This is a living document. It tracks which MEDRE capabilities are implemented, tested, and validated across each transport adapter. It exists so operators and developers can see, at a glance, what works and what does not.
>
> **Policy:** No capability is marked `live-validated` unless there is recorded live evidence in the repository (test results, runbook logs, or CI artifacts). No `ready` labels. No aspirational statuses. If it has not been tested and confirmed, it says so.

This document is the single source of truth for per-transport capability tracking. The operator workflows runbook (`docs/runbooks/operator-workflows.md`) references this file for capability status.

## Capability Matrix

| Capability                          | Matrix         | Meshtastic              | MeshCore    | LXMF        |
| ----------------------------------- | -------------- | ----------------------- | ----------- | ----------- |
| Config load                         | live-validated | fake-tested             | fake-tested | fake-tested |
| Instance-scoped env overrides       | live-validated | fake-tested             | fake-tested | fake-tested |
| Env-first adapter creation          | fake-tested    | fake-tested             | fake-tested | fake-tested |
| Env-driven route creation           | fake-tested    | fake-tested             | fake-tested | fake-tested |
| Fake lifecycle                      | live-validated | fake-tested             | fake-tested | fake-tested |
| Real adapter import safe            | live-validated | opt-in live test exists | designed    | designed    |
| Live start/health                   | live-validated | opt-in live test exists | not started | not started |
| Outbound delivery                   | live-validated | opt-in live test exists | not started | not started |
| Inbound decode                      | live-validated | opt-in live test exists | not started | not started |
| Storage native refs                 | live-validated | fake-tested             | fake-tested | fake-tested |
| Evidence bundle                     | live-validated | fake-tested             | fake-tested | fake-tested |
| Delivery reliability                | fake-tested    | fake-tested             | designed    | designed    |
| Run-session path                    | live-validated | not started             | not started | not started |
| Operator runbook                    | live-validated | opt-in live test exists | designed    | designed    |
| Live validation recorded            | live-validated | not started             | not started | not started |
| Matrix live adapter (local Synapse) | live-validated |                         |             |             |

## Interpretation

These statuses mean specific things. Do not read between the lines.

| Status                    | What it means                                                                                                                                                                                                                                  |
| ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `not started`             | No implementation exists. No tests. No code. It is planned or designed but not built.                                                                                                                                                          |
| `designed`                | There is a spec, contract, or design document describing how it should work. No working code yet.                                                                                                                                              |
| `fake-tested`             | The capability works with fake/mock adapters. Unit tests pass. Storage round-trips. No real network traffic is involved. This proves the pipeline wiring is correct, not that the transport SDK works.                                         |
| `opt-in live test exists` | There is a test harness or runbook for live validation, gated by environment variables. It has not been run against a real transport in a recorded session, or the results have not been committed. The harness exists. The evidence does not. |
| `live-validated`          | The capability has been tested against a real transport (real homeserver, real radio, real network) and the results are recorded in the repository. Runbooks reference the specific test dates and outcomes.                                   |
| `blocked`                 | There is a known blocker preventing progress. The blocker is documented in the relevant runbook or contract.                                                                                                                                   |

## Per-Transport Notes

### Matrix

Matrix is the most mature transport. Live validation was recorded on 2026-05-10 (13 plaintext tests passed, 7 E2EE tests passed) and again on 2026-05-22 (15 live tests passed, 1 xfailed against local Docker Synapse). See `docs/runbooks/matrix-alpha-operation.md` section "Live Validation Evidence" and `docs/runbooks/matrix-local-bringup.md` section "Live Validation Evidence" for details.

The Matrix adapter supports plaintext and E2EE text alpha. E2EE supports encrypted rooms for text messages only. See the alpha operation runbook for the full unsupported features list.

Delivery reliability has env-only fake validation — the env-only deployment
tests (test_env_only_reliability.py) validate successful delivery, duplicate
suppression, and loop-prevention metadata through RuntimeBuilder + fake
adapters. The pipeline records adapter-level delivery receipts (with
`adapter_message_id` on success), retry receipts with backoff, and dead-lettered
receipts when retries exhaust. The RetryWorker processes due retry receipts
when the `[retry]` section is enabled. Replay and recover commands exist for
manual re-delivery. Transport-aware rate limiting and a dead-letter admin UI
are not yet implemented.

Opt-in Matrix live tests use pytest convenience variables such as MATRIX_HOMESERVER, MATRIX_USER_ID, MATRIX_ACCESS_TOKEN, and MATRIX_ROOM_ID. The local Synapse test harness additionally requires `MATRIX_LOCAL_SYNAPSE=1`. Runtime adapter config overrides use instance-scoped `MEDRE_ADAPTER__<TOKEN>__<FIELD>` and `MEDRE_ROUTE__<TOKEN>__<FIELD>` variables.

### Meshtastic

Meshtastic has a complete alpha operation runbook and a live smoke test harness. Real connectivity (TCP and serial) is implemented. The adapter uses pubsub callbacks for inbound and queued `send_one` for outbound.

As of this writing, no live validation against a physical radio has been recorded in the repository. The harness exists. An operator with a Meshtastic node needs to set the pytest convenience variables for radio connection settings and run the live smoke tests. Runtime adapter config overrides use instance-scoped `MEDRE_ADAPTER__<TOKEN>__<FIELD>` and `MEDRE_ROUTE__<TOKEN>__<FIELD>` variables. See `docs/runbooks/meshtastic-live-smoke.md`.

### MeshCore

MeshCore has an alpha operation runbook based on SDK source extraction (version 2.3.7, audited from PyPI). The adapter design follows the same pattern as Matrix and Meshtastic. Real connectivity (TCP, serial, BLE) is specified but implementation status is at the `designed` or `fake-tested` level for most capabilities.

See `docs/runbooks/meshcore-alpha-operation.md` and `docs/contracts/19-meshcore-connectivity-readiness.md` for SDK findings.

### LXMF

LXMF has an alpha operation runbook covering the Reticulum/LXMF stack. The adapter delegates to an owned `LxmfSession` which manages the `RNS.Reticulum`, `RNS.Identity`, and `LXMF.LXMRouter` lifecycle. Fake mode is the default.

See `docs/runbooks/lxmf-alpha-operation.md`. As of this writing, most capabilities beyond config load and fake lifecycle are at `fake-tested` status.

## Known Limitations

These apply to all transports unless specifically noted.

1. **No exactly-once delivery.** Messages can be lost, duplicated, or dropped at any stage. Adapter-level delivery receipts, retry receipts, and dead-lettered receipts exist and are persisted in storage, but there is no end-to-end exactly-once guarantee. The delivery pipeline is at-least-once with duplicate suppression on inbound native refs.

2. **No dead-letter admin UI or management command.** Dead-lettered receipts are recorded in storage when retries are exhausted, but there is no dedicated CLI command or UI for browsing, replaying, or managing dead-lettered events. Operators can inspect them via `medre inspect receipts --event <id>` or evidence bundles.

3. **Runtime capacity control exists; transport-aware rate limiting is incomplete.** The runtime enforces a configurable max-inflight-delivery limit via the capacity controller. Meshtastic has basic adapter-local outbound queue pacing. Meshtastic queue overflow is explicit: when the queue is full, new enqueues are rejected with a transient error (not silently evicted), allowing pipeline retry. Queue stats (depth, max size, enqueued, sent, failed, rejected) are visible in adapter diagnostics. Being queued / locally accepted does not mean RF-delivered. Matrix relies on homeserver-side rate limiting; MEDRE does not yet model Matrix rate-limit headers or adaptive transport backoff as runtime policy. Matrix M_LIMIT_EXCEEDED / HTTP 429 responses are classified as transient and retried with bounded backoff. The retry_after_ms header is not yet honored.

4. **Graceful shutdown is bounded, not fully durable.** On stop, the runtime stops accepting new work and stops the retry worker, then waits up to `limits.shutdown_drain_timeout_seconds` for in-flight delivery and replay capacity to drain. Work still inside adapter SDK sync loops, adapter-local queues, or inbound callbacks is not durably queued before pipeline acceptance; in-flight work may still be abandoned after the drain timeout.

5. **No inbound persistence.** Inbound events are published directly to the pipeline. If the pipeline is slow or fails, the event is gone. No retry, no redelivery at the inbound stage.

6. **No structured logging.** All log output is format-string based. No trace IDs, no correlation across events, no structured fields.

7. **No metrics export.** Diagnostics counters exist in memory but there is no Prometheus endpoint, no statsd, no external export. The only observability is logs, `health_check()`, and `diagnostics()`.

8. **Single-operator only.** Everything is tested and documented for a single person on a single machine. Multi-node, multi-operator, and deployment scenarios do not exist.

9. **Matrix-specific.** Multi-room concurrent inbound has not been tested against a real homeserver. E2EE text alpha does not support reactions, edits, media, cross-signing, or key backup.

10. **Meshtastic-specific.** Inbound processing is text messages only. Telemetry, position, and nodeinfo portnum types are not processed inbound.

11. **MeshCore-specific.** SDK findings are based on source extraction, not hardware testing. BLE connectivity is not implemented.

12. **LXMF-specific.** Multi-hop mesh delivery is not tested. E2EE beyond Reticulum's native link-layer encryption is not in scope.

## How to Update This Document

When a capability status changes:

1. Update the status in the table above.
2. Add or update the per-transport notes with the date and evidence (test results, runbook references, CI artifacts).
3. Do not mark anything `live-validated` without recorded evidence. If you ran the live tests but did not commit the results, the status stays at `opt-in live test exists`.
4. Update the generation date at the top of this file.
