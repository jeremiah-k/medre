# Operational Readiness Gap Audit

> Contract version: 4
> Last updated: 2026-05-10
> Track: 7 (Operational Runtime Hardening)

This document audits the current operational readiness of the MEDRE runtime and its four adapters. It is an honest assessment for operators and maintainers. Nothing here should be read as a feature proposal, deployment guide, or production readiness claim. Contract 16 (`16-production-connectivity-readiness.md`) remains the authoritative readiness assessment per adapter.

All four adapters are in alpha. Fake mode is the default development path for all four. All four now have optional live smoke harnesses (excluded from default CI). No adapter has been tested against real hardware or services in default CI. The current cross-transport assessment is consolidated in contract 28 (`28-alpha-readiness-report.md`).


## 1. Per-Adapter Operational Status

### 1.1 Matrix

**What works now (fake/deterministic).** The decode/render/deliver pipeline runs end to end with fake data. `MatrixCodec` converts nio-shaped events into `CanonicalEvent` instances without importing nio. `MatrixRenderer` builds `m.room.message` content dicts with reply threading and a metadata envelope subtree. Self-message suppression is tested. Room allowlist filtering is tested. `FakeMatrixAdapter` enforces the rendering boundary.

**Real connectivity status.** Matrix has the most real client code. `start()` creates a real `nio.AsyncClient`, restores login from an access token, registers event callbacks, and launches `sync_forever` as a background task. `deliver()` calls `room_send` and returns a real `AdapterDeliveryResult` with the `event_id` from the response. The optional live smoke harness (`tests/test_matrix_live.py`) can verify this against a real homeserver when explicitly enabled. This code has not been exercised in default CI.

**Gaps.**

- No inbound message reception verified against a real homeserver. The sync loop starts, but no test confirms a real event flowing through `_on_room_message` to `publish_inbound`.
- E2EE text alpha is active: inbound decryption and outbound encryption work for text in encrypted rooms. Initial encrypted-room test hit `OlmUnverifiedDeviceError`; after adapter fix (`ignore_unverified_devices=True`), the full E2EE live suite passed 7/7 in 3.73s against room `!rnmyZMhUoraPwZUDPP:matrix.org` (see `docs/runbooks/operational-evidence.md` §1.3). No cross-signing support in nio. Production device verification deferred.
- Reactions, edits, deletes, and attachments are all deferred. Only text and replies work.
- The `access_token` is a plain string in config. No secure storage or rotation mechanism.
- `mindroom-nio` is a fork. Its maintenance status relative to upstream `matrix-nio` is unverified.
- Sync loop error handling is untested under real network conditions (timeouts, reconnects, rate limiting).

### 1.2 Meshtastic

**What works now (fake/deterministic).** The decode/classify/deliver pipeline works with fake packet dicts. `MeshtasticCodec` converts packet dicts into `CanonicalEvent` instances including `replyId` extraction. `MeshtasticPacketClassifier` classifies by portnum, detects ACKs, and extracts sender/channel/packet_id. The outbound queue (`MeshtasticOutboundQueue`) handles message pacing with configurable delay.

**Real connectivity status.** The adapter creates real client connections via `_create_client()` for TCP, serial, and BLE modes when `mtjk` is installed. The `start()` method is guarded by `HAS_MESHTASTIC` — non-fake modes raise `MeshtasticConnectionError` when `mtjk` is absent. Client creation has not been tested against real hardware (only monkeypatched fake interfaces in tests). No real `send_text` has been executed against hardware. Pubsub callback subscription (`_subscribe_callbacks`) is wired but untested with real traffic. An optional live smoke harness (`tests/test_meshtastic_live.py`) verifies raw `mtjk` interface connectivity (TCP/serial connect, sendText/sendData, pubsub callbacks) against a real node but does not exercise the MEDRE adapter's `_create_client` path.

**Gaps.**

- Client creation code exists (`_create_client`) but has not been tested against real hardware (only monkeypatched fakes).
- No real `send_text` execution against hardware. Outbound goes through the queue but no real send has completed.
- No real packet callbacks received. `_subscribe_callbacks` is wired but `_on_receive_callback` is tested only with manual dict injection.
- The 512-byte payload limit is not enforced in the renderer. Real messages that exceed it will silently fail or be truncated by the radio.
- The `_on_packet` callback is synchronous but publishes async. Error propagation from the async publish back to the callback context is unreviewed.
- `mtjk` is a fork. Version pinning and firmware compatibility are unverified.
- `startup_backlog_suppress_seconds` exists in config but has never been tested against real stale packets.
- Live smoke harness tests raw `mtjk` API, not the MEDRE adapter's `_create_client()` path. The adapter's client creation has not been tested against a real node.

### 1.3 MeshCore

**What works now (fake/deterministic).** The decode/classify/deliver pipeline works with fake event payloads. `MeshCoreCodec` and `MeshCorePacketClassifier` follow the Meshtastic structural pattern.

**Real connectivity status.** None. `start()` raises `MeshCoreConnectionError` for any non-fake connection type. There is no real client code. No real SDK has been integrated. No real MeshCore event payloads have been verified.

**Gaps.**

- No SDK selected or integrated. No known stable PyPI package.
- No real connection code at all.
- No outbound delivery. `deliver()` returns `None`.
- Packet format assumptions are based on source code review, not live observation. The real format may differ.
- This is the most speculative adapter. It is structurally ready but substantively empty.

### 1.4 LXMF

**What works now (fake/deterministic).** The decode/classify/deliver pipeline works with fake message payloads. `LxmfCodec` converts LXMF-shaped dicts into `CanonicalEvent` instances. `LxmfFieldsHelper` embeds and extracts MEDRE metadata under field key `0xFD`. `LxmfRenderer` builds payloads with `content`, `title`, `fields`, and `destination_hash`.

**Real connectivity status.** None. `start()` raises `LxmfConnectionError` for non-fake types. No `rns` or `lxmf` imports exist. No real identity loading, message send/receive, or delivery method selection.

**Gaps.**

- No Reticulum or LXMF library integration.
- No real identity loading. `identity_path` is a placeholder.
- No real message send/receive.
- Relation reconstruction from fields envelope is explicitly deferred.
- Delivery method selection (`direct`, `opportunistic`, `propagated`, `paper`) is a config hint only.
- Field key `0xFD` for the metadata envelope is an assumption that has not been validated against real LXMF field usage.
- Reticulum's networking stack may conflict with asyncio's event loop. The async/sync boundary needs design work.
- Identity management (creation, storage, rotation) is entirely unscoped.


## 2. Health Reporting Gaps

### 2.1 AdapterHealth vs AdapterInfo Contradiction

Contract 02 (`02-adapter-runtime-contract.md`) Section 6.2 defines `AdapterHealth` as a rich dataclass with fields: `adapter`, `state` (an `AdapterLifecycleState` enum), `connected` (bool), `latency_ms`, `queue_depth`, `last_event_at`, `error`, and `details` (dict).

The actual implementation does not use `AdapterHealth` anywhere. Instead:

- `src/medre/adapters/base.py` defines `AdapterInfo` with a `health: str` field defaulting to `"unknown"`. This is a plain string, not a structured dataclass.
- All four adapters return `AdapterInfo` (not `AdapterHealth`) from `health_check()`.
- The Matrix adapter sets `health` to one of `"healthy"`, `"unknown"`, or `"failed"` based on sync task and client login state.
- The Meshtastic, MeshCore, and LXMF adapters set `health` to `"healthy"` if `_started` is true, otherwise `"unknown"`.

**Gap:** The spec says `AdapterHealth` with structured fields (latency, queue depth, error details, connection state). The code returns a flat string inside `AdapterInfo`. There is no latency measurement, no queue depth reporting, no structured error details, no last-event timestamp, and no connected/disconnected boolean.

### 2.2 AdapterLifecycleState vs AdapterState

Contract 02 Section 7.1 defines five states: `INITIALIZING`, `RUNNING`, `DEGRADED`, `DRAINING`, `STOPPED`.

The actual `src/medre/core/lifecycle/states.py` defines seven states: `INITIALIZING`, `READY`, `DEGRADED`, `BACKPRESSURED`, `DISCONNECTED`, `STOPPING`, `FAILED`.

**Gap:** Two different state machines exist in the spec and code. The code adds `BACKPRESSURED` and `DISCONNECTED` that the spec does not mention. The spec has `RUNNING` and `DRAINING` that the code replaces with `READY` and `STOPPING`. The code has `FAILED` as a terminal state; the spec has `STOPPED`.

### 2.3 Non-Matrix Health Check Behavior

Matrix is the only adapter with meaningful health logic: it checks `sync_failure`, client existence, and `logged_in` status.

The other three adapters (Meshtastic, MeshCore, LXMF) all use the same trivial pattern:

```python
health = "healthy" if self._started else "unknown"
```

None of them check actual transport connectivity, because none of them have real connections. This means `health_check()` on the three transport adapters is purely a "did `start()` get called" flag, not a health probe.

**Gap:** When real connectivity is added, these health checks need to probe the actual transport. This is not currently documented as a contract anywhere. The behavior of `health_check()` when the adapter is in a degraded or disconnected state (transport unreachable, partial failure) is undefined.

### 2.4 LifecycleManager Health Aggregation

> **Note (Wave 1 cleanup):** `LifecycleManager` has been removed as dead code.
> :class:`~medre.runtime.app.MedreApp` is the sole runtime lifecycle authority.
> Adapter state tracking uses the state machine in
> :mod:`~medre.core.lifecycle.states` directly.

The runtime previously had a `LifecycleManager.health_check_all()` method that
queried each adapter's `health_check()` and updated the internal state registry.
It handled illegal transitions by forcing to the reported state with a warning.

**Gap:** The runtime has no periodic health polling. Health is classified at
startup via :func:`~medre.core.runtime.supervision.classify_runtime_health` but
there is no watchdog, no health check interval, and no automatic state
transition based on health degradation during steady-state operation.


## 3. Logging Gaps

### 3.1 What Exists

The observability subsystem provides:

- `setup_logging(level, json_format)` configures the `medre` root logger with stdout output. Supports human-readable and JSON-structured formats.
- `get_logger(name)` returns child loggers under the `medre.` namespace.
- `diagnostic_event(event_id, category, message, **context)` emits structured warnings for diagnostic events (adapter failures, replay skips, correlation misses).
- `Diagnostician` records and counts failures by category with `snapshot()` for reporting.

### 3.2 What Is Missing

- **No log correlation by trace_id.** `CanonicalEvent` has an optional `trace_id` field, but the logging subsystem does not attach it to log entries. There is no per-request or per-event logging context that threads through the pipeline. An operator tracking a single event's journey must grep for the event_id.
- **No log level configuration per adapter.** All adapters use the same root `medre` logger configuration. There is no way to set `matrix` to DEBUG while keeping `meshcore` at INFO.
- **No structured error codes.** Adapter errors are logged as exception strings. There are no standardized error codes or error categories that an operator can filter on.
- **No inbound/outbound event logging by default.** The `_PipelineLoggingMiddleware` logs every event at DEBUG level. It does not log adapter-specific ingress/egress details (native IDs, channel mappings, payload sizes) at any level.
- **No log rotation or retention configuration.** The logging subsystem writes to stdout. Log management (rotation, retention, shipping) is entirely the operator's responsibility.


## 4. Metrics and Tracing Gaps

### 4.1 What Exists

`EventMetrics` provides in-process counters: `events_ingressed`, `events_stored`, `events_routed`, `events_delivered`, `events_dropped`, `events_failed`. Counters are keyed by event kind string. `snapshot()` returns a plain-dict copy.

`Diagnostician` provides in-process failure counters: `planner_failures`, `renderer_failures`, `storage_failures`, `adapter_failures`, `replay_skips`, `replay_downgrades`, `correlation_misses`.

### 4.2 What Is Missing

- **No metrics export.** There is no Prometheus endpoint, StatsD sink, OpenTelemetry integration, or any mechanism to expose `EventMetrics` or `Diagnostician` counters to an external monitoring system. The counters exist only in memory and are lost on process restart.
- **No histograms or timing.** `EventMetrics` tracks counts only. There are no latency histograms for delivery time, routing time, or rendering time. The `DeliveryOutcome.duration_ms` field is computed but not aggregated anywhere.
- **No distributed tracing.** There is no OpenTelemetry span creation, no trace propagation, no span context attached to events flowing through the pipeline. The `trace_id` field on `CanonicalEvent` exists but nothing populates it from incoming events and nothing propagates it through outbound delivery.
- **No adapter-level metrics.** Individual adapters do not report their own metrics (messages sent/received, errors, queue depth, latency to transport). The `AdapterInfo` dataclass has no metrics fields.
- **No health check metrics.** Health check results are not tracked over time. There is no history of adapter state transitions.
- **Spec observability unimplemented.** Contract 02 Section 12.2 lists `core/observability/` as providing observability. What exists is a logging helper and in-memory counters. The spec implies a richer subsystem that does not exist.


## 5. Health Aggregation Gaps

### 5.1 No Aggregate Health Endpoint

There is no HTTP endpoint, CLI command, or API to query the overall health of the runtime. Health classification is available programmatically via :func:`~medre.core.runtime.supervision.classify_runtime_health` and the boot summary produced by :class:`~medre.runtime.app.MedreApp`.

An operator has no way to ask "is the system healthy?" without accessing the runtime's diagnostic snapshot programmatically.

### 5.2 No Health-Based Routing Decisions

The router does not consider adapter health when making routing decisions. If an adapter is in `FAILED` or `DISCONNECTED` state, the router will still match routes targeting that adapter and the pipeline will attempt delivery. The delivery will fail, produce a failed receipt, and potentially dead-letter the event.

There is no mechanism to skip routes whose target adapter is unhealthy, or to route to a fallback adapter when the primary is down.

### 5.3 No Cascade Detection

If one adapter fails, there is no cascade detection. The failure is recorded as a receipt and a diagnostic event, but no logic evaluates whether repeated failures on one adapter should trigger alerts, route changes, or adapter restarts.


## 6. Replay Operational Gaps

### 6.1 What Works

The replay engine supports five modes (`STRICT`, `RE_RENDER`, `RE_ROUTE`, `BEST_EFFORT`, `DRY_RUN`) with explicit stage guarantees. Replay never mutates historical events. Non-BEST_EFFORT modes produce zero storage side effects. `Diagnostician` integration records skips, downgrades, and failures. `target_adapters` filtering allows scoping delivery to specific adapters.

### 6.2 Operational Gaps

- **No replay progress tracking.** Replay runs to completion or failure. There are no intermediate checkpoints. A replay of 10,000 events that crashes at event 9,999 must be restarted from the beginning.
- **No replay resumption.** No mechanism to resume a failed replay from the last processed event.
- **No replay rate limiting.** A BEST_EFFORT replay will attempt delivery as fast as the pipeline allows. There is no throttle for adapter rate limits or backpressure.
- **No receipt deduplication.** Replaying events that already have successful delivery receipts produces duplicate receipts. This is documented in contract 07 but remains an operational hazard.
- **No dead-letter replay shortcut.** Replaying dead-lettered events requires a manual query of `delivery_receipts` to collect `event_id` values, then passing them as `correlation_ids`. There is no single command for "replay all dead-lettered events."
- **No replay scheduling.** Replay is a synchronous operation. There is no way to schedule a replay for a future time or on a recurring basis.


## 7. Policy and Plugin Scaffolding Gaps

### 7.1 Policy Pipeline

Contract 04 Section 10 defines a four-stage policy architecture: ingress, event, route, delivery. Each stage has a `Policy` protocol, `PolicyResult`, and concrete policies like `RouteRateLimitPolicy`, `QuietHoursPolicy`, `MaxLengthPolicy`, and `CapabilityFallbackPolicy`.

**Current state:** `src/medre/core/policies/__init__.py` is an empty file. No `Policy` protocol, no `PolicyResult`, no concrete policies, and no policy evaluation pipeline exist. The runtime proceeds directly from routing to delivery planning without policy evaluation.

**Gap:** The entire policy subsystem is spec-level only. Rate limiting, quiet hours, content filtering, permission checking, and deduplication are not implemented. The only "policy" behavior that exists is capability fallback in the `FallbackResolver`, which downgrades event features based on target adapter capabilities. This is not a policy stage; it is a planning concern.

### 7.2 Plugin API

Contract 05 defines a complete plugin API: `Plugin` protocol, `PluginContext`, `PluginStateStore`, `PluginCapability`, convenience methods (reply, send, react, emit), and security boundaries.

**Current state:** None of this is implemented. The `plugin_state` SQL table exists in the schema but is not wired to any runtime service. No plugin loader, host, or capability enforcement exists. The contract documents the intended interface for future implementation.

**Gap:** The plugin subsystem is entirely scaffolding. No third-party code can extend the runtime. The `plugin.custom` event kind exists in the taxonomy but no plugin produces it.


## 8. Dead-Letter and Retry Management Gaps

### 8.1 What Exists

- `DeliveryFailureKind` taxonomy with 6 categories (planner, renderer, adapter_transient, adapter_permanent, target_not_found, deadline_exceeded).
- `RetryExecutor` for backoff computation, exhaustion detection, and retry/dead-letter receipt construction.
- `DeliveryReceipt` with `attempt_number` and `parent_receipt_id` for receipt lineage.
- Dead-letter receipt appended after the primary failed receipt on retry exhaustion.
- `delivery_status` SQL view for projecting current delivery state.

### 8.2 What Is Missing

- **No background retry scheduler.** The pipeline records `next_retry_at` on failed receipts but never acts on it. No timer, no background task, no event-driven mechanism re-attempts delivery. Manual replay via BEST_EFFORT mode is the only retry path.
- **No dead-letter queue management.** Dead-lettered events are recorded as receipts. No admin interface, no reprocessing UI, no listing or querying of dead-lettered events exists outside of raw SQL.
- **No retry budget.** No per-adapter or per-plan retry rate limiting. An adapter with persistent failures could accumulate unlimited dead-letter receipts.
- **No receipt deduplication.** Replaying events with existing successful receipts duplicates them.
- **No adapter-level error customization.** Error classification uses Python exception types. Adapters cannot declare custom retryable/permanent error codes.
- **No reconnection logic.** No adapter handles reconnection or connection loss. The lifecycle is start/stop with no automatic recovery. If the Matrix homeserver drops the connection, the sync task fails silently and `health_check()` reports `"failed"`, but no reconnect attempt is made.


## 9. Runbook Gaps

### 9.1 What Exists

Two runbooks:
- `docs/runbooks/matrix-live-smoke.md` for the optional Matrix live smoke harness.
- `docs/runbooks/meshtastic-live-smoke.md` for the optional Meshtastic live smoke harness.

### 9.2 What Is Missing

There are no operational runbooks for:

- Starting and stopping the runtime
- Adding or removing an adapter at runtime
- Reloading route configuration
- Diagnosing delivery failures from receipts
- Replaying failed or dead-lettered events
- Interpreting `Diagnostician` output
- Interpreting `EventMetrics` snapshots
- Responding to adapter health degradation
- Managing the SQLite storage backend (backup, compaction, migration)
- Configuring logging levels and formats
- Debugging routing mismatches (event matches zero routes)


## 10. Contradictions and Inconsistencies Found

### 10.1 AdapterHealth Dataclass vs Matrix String Health

Contract 02 defines `AdapterHealth` as a rich dataclass with `state`, `connected`, `latency_ms`, `queue_depth`, `last_event_at`, `error`, and `details`. The actual code returns `AdapterInfo` with `health: str` (a plain string like `"healthy"` or `"unknown"`). The rich health model does not exist in code.

### 10.2 Privacy Mode: Rich Spec vs No Implementation

Contract 06 defines four privacy modes (`off`, `minimal`, `safe`, `full`) with a default of `safe`. These control what metadata gets embedded in Matrix events. No adapter config field, no code, and no runtime logic implements privacy mode selection. The `MatrixRenderer` embeds metadata unconditionally. The privacy mode is a spec-level design that has no code backing.

### 10.3 Spec Observability vs Actual Observability

Contract 02 Section 12.2 lists `core/observability/` as providing observability for the runtime. The actual module provides `setup_logging()`, `get_logger()`, `EventMetrics` (in-memory counters), and `Diagnostician` (in-memory failure counters). No metrics export, no distributed tracing, no adapter-level metrics, no health check tracking. The spec implies a richer subsystem than what exists.

### 10.4 Non-Matrix health_check Behavior Not Documented

The Meshtastic, MeshCore, and LXMF adapters return `"healthy"` if `self._started` is true, regardless of actual transport state. This is not documented anywhere as a contract. An operator relying on `health_check()` to detect transport failure on these adapters will always see `"healthy"` as long as `start()` was called, even if the underlying connection is dead.

### 10.5 Lifecycle State Machine Divergence

Contract 02 defines five states (`INITIALIZING`, `RUNNING`, `DEGRADED`, `DRAINING`, `STOPPED`). The code defines seven states (`INITIALIZING`, `READY`, `DEGRADED`, `BACKPRESSURED`, `DISCONNECTED`, `STOPPING`, `FAILED`). The names differ (`RUNNING` vs `READY`, `STOPPED` vs `FAILED`), and the code adds states not in the spec (`BACKPRESSURED`, `DISCONNECTED`). Neither state machine is authoritative; both are documented.

### 10.6 Codec Base Class Inconsistency

Matrix's codec inherits from `AdapterCodec` (the abstract base in `base.py`). The transport codecs (Meshtastic, MeshCore, LXMF) are standalone classes. This is noted in contract 15 as a minor accidental inconsistency that could cause issues with a future codec registry.


## 11. Explicit Out-of-Scope Constraints

The following are explicitly not part of this audit and must not be inferred from this document:

- **No production deployment instructions.** This document does not describe how to deploy, configure, or operate the MEDRE runtime in a production environment.
- **No admin API.** No admin API exists or is specified for production use. There is no HTTP server, no REST endpoint, no management interface.
- **No webhooks.** No webhook integration exists. No adapter receives inbound events via HTTP. No outbound webhook delivery is implemented.
- **No production readiness claims.** No adapter is production-ready. All four adapters use fake delivery. None has been tested against real hardware or real network services in default CI.
- **No feature proposals.** This document enumerates gaps. It does not propose features, priorities, or implementation plans for closing those gaps.
- **Tranche constraints preserved.** This audit covers tranche 1 behavior only. No feature expansion beyond the current tranche is implied.


## 12. Summary of Gaps by Category

| Category | Gap Severity | Summary |
|---|---|---|
| Adapter connectivity | Critical | All adapters use fake delivery. Matrix has real client code but no CI verification. Other three have no real connectivity. |
| Health reporting | High | `AdapterHealth` spec vs `AdapterInfo` string mismatch. Transport adapters report trivial health. No periodic health polling. |
| Metrics | High | In-memory counters only. No export, no histograms, no distributed tracing. Lost on restart. |
| Policy pipeline | High | Empty package. Four-stage policy architecture is spec-only. No rate limiting, filtering, or deduplication. |
| Retry management | Medium | Receipt recording works. No background scheduler, no dead-letter management, no retry budgets. |
| Logging | Medium | Structured logging exists. No trace correlation, no per-adapter levels, no error codes. |
| Replay | Medium | Five modes work. No progress tracking, no resumption, no rate limiting, no scheduling. |
| Plugin API | Low | Full spec exists. Zero implementation. Scaffolding only. |
| Runbooks | Low | Two runbooks for Matrix and Meshtastic live smoke. No operational runbooks. |
| Lifecycle state machine | Low | Spec and code diverge on state names and count. Both documented. Neither authoritative. |
| Privacy modes | Low | Spec defines four modes. No code implements them. |


## 13. Contract Cross-References

| Topic | Contract |
|---|---|
| Production connectivity per adapter | `16-production-connectivity-readiness.md` |
| Adapter baseline consistency audit | `15-adapter-baseline-consolidation.md` |
| Adapter runtime contract (lifecycle, health, capabilities) | `02-adapter-runtime-contract.md` |
| Routing and delivery planning | `04-routing-planning-contract.md` |
| Plugin API | `05-plugin-api-contract.md` |
| Replay and event log | `07-replay-event-log-contract.md` |
| Metadata embedding and privacy modes | `06-metadata-embedding-contract.md` |
| Phase 1 limitations | `phase-1-limitations.md` |

Contract 16 is the readiness authority. This document supplements it with operational runtime gaps. Where this document and Contract 16 conflict on adapter readiness facts, Contract 16 takes precedence.
