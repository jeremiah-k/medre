# MEDRE Transport Capability Status

> **Generated:** 2026-05-26 (Tranche 3 Matrix hardening update; relation alignment verified by source audit, no live tests executed this session)
>
> **Baseline:** HEAD 41a07c7, Python 3.12.3, medre 0.1.0
>
> **Context:** This is a living document. It tracks which MEDRE capabilities are implemented, tested, and validated across each transport adapter. It exists so operators and developers can see, at a glance, what works and what does not.
>
> **Policy:** No capability is marked `live-validated` unless there is recorded live evidence in the repository (test results, runbook logs, or CI artifacts). No `ready` labels. No aspirational statuses. If it has not been tested and confirmed, it says so.
>
> **Evidence boundaries (Tranche 6):** `live-validated` includes Docker SDK-boundary evidence (e.g. local Docker Synapse). Docker SDK-boundary validates SDK integration and adapter wiring but not external network behavior, federation, or real-world rate limits. See `docs/runbooks/operational-evidence.md` §Evidence sub-classification for the full taxonomy (Docker SDK-boundary / external live / hardware).

This document is the single source of truth for per-transport capability tracking. The operator workflows runbook (`docs/runbooks/operator-workflows.md`) references this file for capability status.

## Capability Matrix

| Capability                          | Matrix                               | Meshtastic              | MeshCore    | LXMF        |
| ----------------------------------- | ------------------------------------ | ----------------------- | ----------- | ----------- |
| Config load                         | live-validated                       | fake-tested             | fake-tested | fake-tested |
| Instance-scoped env overrides       | live-validated                       | fake-tested             | fake-tested | fake-tested |
| Env-first adapter creation          | fake-tested                          | fake-tested             | fake-tested | fake-tested |
| Env-driven route creation           | fake-tested                          | fake-tested             | fake-tested | fake-tested |
| Route policy enforcement            | fake-tested                          | fake-tested             | fake-tested | fake-tested |
| Fake lifecycle                      | live-validated                       | fake-tested             | fake-tested | fake-tested |
| Real adapter import safe            | live-validated                       | opt-in live test exists | designed    | designed    |
| Live start/health                   | live-validated                       | opt-in live test exists | not started | not started |
| Outbound delivery                   | live-validated                       | opt-in live test exists | not started | not started |
| Inbound decode                      | live-validated                       | opt-in live test exists | not started | not started |
| Storage native refs                 | live-validated                       | fake-tested             | fake-tested | fake-tested |
| Evidence bundle                     | live-validated                       | fake-tested             | fake-tested | fake-tested |
| Delivery reliability                | fake-tested                          | fake-tested             | designed    | designed    |
| Delivery evidence (unified inspect) | fake-tested                          | fake-tested             | not started | not started |
| Run-session path                    | live-validated                       | not started             | not started | not started |
| Operator runbook                    | live-validated                       | opt-in live test exists | designed    | designed    |
| Live validation recorded            | live-validated                       | not started             | not started | not started |
| Local delivery outbox               | fake-tested                          | fake-tested             | fake-tested | fake-tested |
| Matrix live adapter (local Synapse) | live-validated (Docker SDK-boundary) |                         |             |             |

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

Matrix is the most mature transport. Live validation was recorded on 2026-05-10 (13 plaintext tests passed, 7 E2EE tests passed) and again on 2026-05-22 (15 live tests passed, 1 xfailed against local Docker Synapse — Docker SDK-boundary evidence). Docker Synapse E2EE harness executed 2026-05-25: 3/3 passed (`MEDRE_SYNAPSE_PORT=8009 pytest tests/integration/test_synapse_e2ee_smoke.py -m docker -v`, Python 3.12.3, nio E2EE ENCRYPTION_ENABLED=True, Synapse v1.153.0, Docker loopback). Third-party inbound confirmed at Docker SDK-boundary via second nio client (external-live not confirmed). See `docs/runbooks/matrix-alpha-operation.md` section "Live Validation Evidence", `docs/runbooks/matrix-local-bringup.md` section "Live Validation Evidence", and `docs/runbooks/operational-evidence.md` §1.1c for details. Docker SDK-boundary evidence validates SDK integration and adapter wiring; it does not validate external network behavior, federation, or real-world rate limits. External live re-run (2026-05-12) failed on credential issues, not code issues.

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

The unified delivery evidence surface (`medre inspect`, `medre evidence --event-id`) exposes a delivery explanation shape with event_id, route/target info, final status, failure kind, retryable flag, attempt/retry policy fields, next_retry_at, native adapter message IDs, and per-adapter metadata (including Matrix txn_id for homeserver deduplication and undecryptable_event_count for E2EE diagnosis). Evidence is best-effort and local-process scoped — not exactly-once, not distributed. See `docs/dev/runtime-delivery-contract.md` → Unified Delivery Evidence for the full specification.

Route policy enforcement is `fake-tested` across all transports. `allowed_event_types` is enforced as structural route-source matching during route expansion. `allowed_source_adapters`, `allowed_dest_adapters`, `sender_allowlist`, `room_allowlist`, and `channel_allowlist` are evaluated after route matching and before delivery side effects. A denial produces `failure_kind="policy_suppressed"` (permanent, not retryable). Policy fields are config-file-only (not settable via environment variables). The `room_allowlist` route-policy field is distinct from the Matrix adapter-level `room_allowlist` config — the adapter-level field controls which rooms the Matrix sync loop processes, while the route-policy field controls which source rooms a route accepts. Meshtastic `channel_mapping` is display labels only — it does not participate in route-policy `channel_allowlist` evaluation.

Opt-in Matrix live tests use pytest convenience variables such as MATRIX_HOMESERVER, MATRIX_USER_ID, MATRIX_ACCESS_TOKEN, and MATRIX_ROOM_ID. The local Synapse test harness additionally requires `MATRIX_LOCAL_SYNAPSE=1`. Runtime adapter config overrides use instance-scoped `MEDRE_ADAPTER__<TOKEN>__<FIELD>` and `MEDRE_ROUTE__<TOKEN>__<FIELD>` variables.

**Tranche 3 hardening (2026-05-26):** Source-audit verification confirmed that Matrix relations (replies via `m.in_reply_to`, reactions via `m.annotation`) are correctly aligned with Matrix-native content format in relations.py, codec.py, and renderer.py. Transaction-ID deduplication for outbound delivery is implemented (deterministic `txn_id` per delivery). Rate-limit responses (M_LIMIT_EXCEEDED / HTTP 429) are classified as transient and surfaced immediately as `AdapterSendError(transient=True)`; they are not retried within the adapter's bounded retry loop. Undecryptable MegolmEvent counting is implemented. No new live validation was executed. No status changes in the capability matrix. mindroom-nio's `room_send` does not encrypt `m.reaction` events in encrypted rooms (known nio limitation — the MMRelay emote fallback path uses `m.room.message` which IS encrypted).

### Meshtastic

Meshtastic has a complete alpha operation runbook and a live smoke test harness. Real connectivity (TCP and serial) is implemented. The adapter uses pubsub callbacks for inbound and queued `send_one` for outbound.

As of this writing, no live validation against a physical radio has been recorded in the repository. The harness exists. An operator with a Meshtastic node needs to set the pytest convenience variables for radio connection settings and run the live smoke tests. Runtime adapter config overrides use instance-scoped `MEDRE_ADAPTER__<TOKEN>__<FIELD>` and `MEDRE_ROUTE__<TOKEN>__<FIELD>` variables. See `docs/runbooks/meshtastic-live-smoke.md`.

Meshtastic adapter diagnostics expose aggregate inbound classification counters (`classifier_packets_seen`, `classifier_packets_relayed`, `classifier_packets_ignored`, `classifier_packets_dropped`, `classifier_packets_deferred`, plus reason-level sub-counters). These counters explain aggregate inbound skips but do not mean live validation and do not persist every ignored/dropped/deferred packet. Queue stats (`queue_total_enqueued`, `queue_total_sent`, `queue_total_failed`, `queue_total_rejected`, `queue_total_requeued`, `queue_total_exhausted`, `queue_total_permanent_failed`, `queue_send_max_attempts`) are visible in diagnostics. Being queued/enqueued means adapter-local queue acceptance only. Being sent means the SDK/client send returned success. Neither means RF confirmation, remote receipt, or ACK. Transient SDK send failures are retried from the adapter-local queue up to `queue_send_max_attempts`; permanent failures are not retried. Retry is best-effort, adapter-local, in-memory, non-durable across process restart, and not exactly-once. Startup backlog suppression via `startup_backlog_suppress_seconds` is wired to ingress pre-decode stale packet suppression using `rxTime`. It is best-effort, session-scoped/in-memory, not cryptographic replay prevention, not durable across restarts, not exactly-once. Suppressed packets do not create canonical events or delivery/evidence receipts. See `docs/runbooks/meshtastic-alpha-operation.md` section 13 item 6.

Tranche 2 (branch `t2-meshtastic-reference-alignment`) added diagnostic-only classifier field extraction for `encrypted`, `hopStart`, `hopLimit`, `rxTime`, `rxSnr`, `rxRssi`, and `priority` from real packet dicts. These fields improve data extraction fidelity and adapter diagnostics but do not change any classification action, canonical event structure, or operational behavior. No classification policy changed — only data extraction fidelity improved.

### MeshCore

MeshCore has an alpha operation runbook based on SDK source extraction (version 2.3.7, audited from PyPI). The adapter design follows the same pattern as Matrix and Meshtastic. `MeshCoreSession` contains full real-mode session code: TCP/serial/BLE factory wiring, event subscription, bounded exponential backoff reconnect, transient/permanent error classification, inbound callback normalization (sync and async), and partial-startup cleanup (`_cleanup_failed_start()` clears `_meshcore`, `_message_callback`, `_subscriptions`, and `connected` flag on subscription failure after successful client creation). This code is source-audited and mock-tested only; no hardware validation has occurred. Implementation status is at the `fake-tested` level for most capabilities, with session lifecycle code source-audited and mock-tested.

MeshCore sends directly through the session without an intermediary outbound queue. A successful send means local node acceptance, not mesh delivery, ACK receipt, RF confirmation, or remote-node reception. The `message_delay_seconds` config field is accepted but not currently enforced; it is reserved for future pacing. Target-aware UTF-8 byte-budget rendering and a classifier action taxonomy (`relay`/`ignore`/`drop`/`deferred`) with aggregate in-memory diagnostics counters are source-audited and mock-tested. These counters explain aggregate inbound skips; they do not constitute live validation, per-packet persistence, or exactly-once accounting.

Startup backlog suppression is explicitly deferred for MeshCore. MeshCore has no message history, no store-and-forward, and no initial sync. When the adapter connects, events arrive live; there is no backlog to suppress. The `sender_timestamp` field is sender-side and unverified, so timestamp-based suppression would risk dropping live packets. If MeshCore gains store-and-forward semantics, this decision should be revisited.

See `docs/runbooks/meshcore-alpha-operation.md` and `docs/contracts/19-meshcore-connectivity-readiness.md` for SDK findings.

### LXMF

LXMF has an alpha operation runbook covering the Reticulum/LXMF stack. The adapter delegates to an owned `LxmfSession` which manages the `RNS.Reticulum`, `RNS.Identity`, and `LXMF.LXMRouter` lifecycle. Fake mode is the default. The session includes bounded exponential backoff reconnect, delivery state tracking (`OUTBOUND → SENDING → SENT → DELIVERED`), threading bridge (`call_soon_threadsafe` for Reticulum→asyncio), failed-start cleanup (`_teardown_sdk()` clears all SDK objects), delivery-state loop bridge (no direct-apply fallback — updates dropped when loop unavailable), async callback coroutine close (no `asyncio.run` fallback from Reticulum threads), shutdown race guard (`RuntimeError` catch on `call_soon_threadsafe`), and `CancelledError` handling in task done callbacks. Source-audited and mock-tested only; no live Reticulum validation has occurred.

See `docs/runbooks/lxmf-alpha-operation.md`. As of this writing, most capabilities beyond config load and fake lifecycle are at `fake-tested` status.

## Known Limitations

These apply to all transports unless specifically noted.

1. **No exactly-once delivery.** Messages can be lost, duplicated, or dropped at any stage. Adapter-level delivery receipts, retry receipts, and dead-lettered receipts exist and are persisted in storage, but there is no end-to-end exactly-once guarantee. The delivery pipeline is at-least-once with duplicate suppression on inbound native refs.

2. **No dead-letter admin UI or management command.** Dead-lettered receipts are recorded in storage when retries are exhausted, but there is no dedicated CLI command or UI for browsing, replaying, or managing dead-lettered events. Operators can inspect them via `medre inspect receipts --event <id>` or evidence bundles.

3. **Local delivery outbox is durable but does not provide exactly-once or RF confirmation.** The outbox (`delivery_outbox` table) persists pending, retry_wait, in_progress, queued, sent, dead_lettered, cancelled, and abandoned items across process restart. However:
   - **(a) Crash timing risk:** A process may crash after local adapter send succeeds but before the sent receipt is committed — recovery may resend.
   - **(b) Meshtastic queue ambiguity:** Meshtastic adapter-local queue contents are in-memory and non-durable — items queued but not sent at crash time are lost, though a `queued` outbox row may survive if committed before the crash (such rows are ambiguous after restart and are not automatically retried).
   - **(c) No end-to-end tracking:** The outbox does not track RF confirmation, ACK, remote receipt, or end-to-end delivery.

   Operators can inspect aggregate outbox counts through runtime diagnostics snapshots (`medre diagnostics --refresh-health`) and can query detailed rows in the `delivery_outbox` table through SQLite/storage-level inspection. A first-class outbox inspect/recover CLI command is future work. The RetryWorker processes due outbox items when `[retry] enabled = true`.

4. **Runtime capacity control exists; transport-aware rate limiting is incomplete.** The runtime enforces a configurable max-inflight-delivery limit via the capacity controller. Meshtastic has bounded adapter-local outbound queue retry: transient SDK send failures are retried up to `queue_send_max_attempts` from the in-memory queue; permanent failures and exhausted retries are dropped. Retry is best-effort, adapter-local, non-durable across process restart, and not exactly-once. Meshtastic queue overflow is explicit: when the queue is full, new enqueues are rejected with a transient error (not silently evicted), allowing pipeline retry. Queue stats (depth, max size, enqueued, sent, failed, rejected, requeued, exhausted, max attempts) are visible in adapter diagnostics. Being queued / locally accepted does not mean RF-delivered. Matrix relies on homeserver-side rate limiting; MEDRE does not yet model Matrix rate-limit headers or adaptive transport backoff as runtime policy. Matrix M_LIMIT_EXCEEDED / HTTP 429 responses are classified as transient and surfaced immediately as `AdapterSendError(transient=True)`; they are not retried within the adapter's bounded retry loop. The retry_after_ms header is not yet honored.

5. **Graceful shutdown is bounded, not fully durable.** On stop, the runtime stops accepting new work and stops the retry worker, then waits up to `limits.shutdown_drain_timeout_seconds` for in-flight delivery and replay capacity to drain. Work still inside adapter SDK sync loops, adapter-local queues, or inbound callbacks is not durably queued before pipeline acceptance; in-flight work may still be abandoned after the drain timeout.

6. **No inbound persistence.** Inbound events are published directly to the pipeline. If the pipeline is slow or fails, the event is gone. No retry, no redelivery at the inbound stage.

7. **No structured logging.** All log output is format-string based. No trace IDs, no correlation across events, no structured fields.

8. **No metrics export.** Diagnostics counters exist in memory but there is no Prometheus endpoint, no statsd, no external export. The only observability is logs, `health_check()`, and `diagnostics()`.

9. **Single-operator only.** Everything is tested and documented for a single person on a single machine. Multi-node, multi-operator, and deployment scenarios do not exist.

10. **Matrix-specific.** Multi-room concurrent inbound has not been tested against a real homeserver. E2EE text alpha does not support reactions, edits, media, cross-signing, or key backup.

11. **Meshtastic-specific.** Inbound processing is text messages only. Telemetry, position, and nodeinfo portnum types are not processed inbound.

12. **MeshCore-specific.** SDK findings are based on source extraction, not hardware testing. BLE is implemented at the session layer (`MeshCore.create_ble()` path wired); hardware validation against a real BLE node is pending. Mock-based BLE validation tests pass without hardware.

13. **LXMF-specific.** Multi-hop mesh delivery is not tested. E2EE beyond Reticulum's native link-layer encryption is not in scope.

## How to Update This Document

When a capability status changes:

1. Update the status in the table above.
2. Add or update the per-transport notes with the date and evidence (test results, runbook references, CI artifacts).
3. Do not mark anything `live-validated` without recorded evidence. If you ran the live tests but did not commit the results, the status stays at `opt-in live test exists`.
4. Update the generation date at the top of this file.
