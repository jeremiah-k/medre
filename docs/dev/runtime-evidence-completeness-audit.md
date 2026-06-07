# Runtime Evidence Completeness Audit

> **Classification:** Developer reference (derived from [diagnostics-evidence.md](../spec/diagnostics-evidence.md))
> **Audience:** Runtime developers, code reviewers.
> **Authority:** [diagnostics-evidence.md](../spec/diagnostics-evidence.md) is the normative specification. This document records implementation gaps and completeness findings. If this document conflicts with the spec, the spec is correct.

## Runtime Evidence Surface Inventory

Nine operator-visible surfaces for runtime evidence. Each row records what data feeds the surface, what operator question it answers, whether it writes to storage, its persistence class, restart survival, known blind spots, and the command or report that exposes it.

| Surface                                      | Source data                                                                                                                                                                                                                                       | Operator question answered                                                            | Storage writes                                                                                             | Persistence                                               | Survives restart                                                   | Known blind spots                                                                                                                                                                                 | Visible command/report                                                                                          |
| -------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- | --------------------------------------------------------- | ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| **Runtime events / EventBuffer**             | `RuntimeEventType` enum (16 types) emitted by runtime internals; held in bounded in-memory buffer (cap 256)                                                                                                                                       | What happened inside the runtime between start and stop?                              | None                                                                                                       | Ephemeral (in-memory)                                     | No                                                                 | Lost on crash; not queryable from storage; no native-ref correlation                                                                                                                              | Snapshot `runtime_events` section; smoke/run-session report `runtime_events_count`                              |
| **Runtime diagnostics snapshot**             | `RuntimeSnapshot` frozen dataclass (9 fields) built by `capture_runtime_snapshot()` / `build_runtime_snapshot()`                                                                                                                                  | What is the aggregate runtime state right now?                                        | None (pure function, no I/O)                                                                               | Ephemeral                                                 | No                                                                 | Observational only (§ 12 caveat); adapter status reflects capture-time state; no continuous monitoring                                                                                            | `medre evidence` `diagnostics_snapshot` / `live_health` sections; snapshot JSON                                 |
| **Run-session report**                       | `run_bridge_session()` result; includes adapter_lifecycle, shutdown_status, retry_worker_summary, cross-linked CLI commands                                                                                                                       | Did a full bridge session succeed end-to-end? What happened at each stage?            | Receipts/outbox written via normal pipeline during session                                                 | Derived + storage side-effects                            | Receipts survive; report dict does not                             | Fake adapters only; single-event default; no sustained throughput proof                                                                                                                           | `medre smoke --run-session`                                                                                     |
| **Smoke output**                             | `smoke_runtime()` result; includes adapter_lifecycle, shutdown_status, runtime_events_count                                                                                                                                                       | Does the runtime build, start, route, deliver, and stop correctly with fake adapters? | Receipts/outbox written via normal pipeline during smoke                                                   | Derived + storage side-effects                            | Receipts survive; report dict does not                             | Fake adapters only; no real transport connectivity; single-event default                                                                                                                          | `medre smoke`                                                                                                   |
| **Evidence bundle runtime top-level fields** | `collect_evidence_bundle()` hoists adapter_status, shutdown_evidence, convergence_summary, orphan_report, recovery_summary, recovery_ledger, lifecycle_convergence_report to top level                                                            | What is the overall runtime and delivery state for this event (or globally)?          | None (read-only collection)                                                                                | Derived from storage                                      | Yes (storage-backed fields survive)                                | No `runtime` top-level section (runtime events are in-memory only, not hoisted); schema_version is 1                                                                                              | `medre evidence --event <ID> --storage-path PATH --json`                                                        |
| **Shutdown evidence**                        | `ShutdownEvidence` frozen dataclass built by `build_shutdown_evidence()`; fields include shutdown_status, resume_expected, outbox_shutdown_policy, pending_outbox_counts, drain_timeout_detected, tasks_cancelled, evidence_flush_status          | Did shutdown complete cleanly? Is there resumable work?                               | None from the model itself (pure function); shutdown_rejection receipts are durable                        | Derived (in-memory) for the model; receipts are persisted | ShutdownEvidence dict is lost; shutdown_rejection receipts survive | In-memory model not persisted; operator must capture snapshot at shutdown time; shutdown_rejection receipts are the durable record                                                                | Snapshot `shutdown_evidence` section; evidence bundle `shutdown_evidence` top-level                             |
| **Retry worker evidence/events**             | Retry events emitted to EventBuffer (retry_started, retry_attempted, retry_succeeded, retry_failed, retry_dead_lettered, retry_stopped, retry_abandoned, retry_start_refused); RetryWorker discovers due receipts via `list_due_retry_receipts()` | What retry attempts occurred? Were retries exhausted or abandoned?                    | Retry receipts appended to `delivery_receipts` (durable)                                                   | Derived (events ephemeral) + persisted (receipts durable) | Receipts survive; EventBuffer events do not                        | EventBuffer lost on crash; retry chain durable via receipt parent_receipt_id links; retry_abandoned vs retry_start_refused distinction is runtime-local (not persisted as distinct failure kinds) | `medre inspect receipts --event <ID> --storage-path PATH`; evidence bundle `retry_outbox_summary`               |
| **Adapter lifecycle evidence/events**        | `system.lifecycle` canonical events emitted on state transitions; RuntimeEventType adapter events (adapter_started, adapter_start_failed, adapter_stopped) in EventBuffer                                                                         | Is each adapter running? Did it start/stop cleanly?                                   | `system.lifecycle` events stored in `canonical_events` table; adapter health persisted in snapshot capture | Persisted (canonical events) + ephemeral (EventBuffer)    | Canonical events survive; EventBuffer events do not                | `system.lifecycle` events require event storage to be initialized; EventBuffer adapter events are in-memory only                                                                                  | `medre inspect event <ID> --storage-path PATH`; snapshot `lifecycle.adapters` section                           |
| **Shutdown rejection receipts**              | Pipeline appends delivery receipts with `failure_kind="shutdown_rejection"` during drain phase when in-flight work is cancelled                                                                                                                   | Which deliveries were suppressed during shutdown?                                     | Yes (durable receipt rows in `delivery_receipts`)                                                          | Persisted                                                 | Yes                                                                | Only covers deliveries that reached the pipeline; does not cover work that never entered the delivery spine                                                                                       | `medre inspect receipts --storage-path PATH`; evidence bundle receipt list; drill `shutdown_rejection` scenario |
| **CLI inspect/evidence/trace views**         | All of the above surfaces, composed through read-only CLI commands                                                                                                                                                                                | How do I investigate a specific event, receipt, or delivery chain?                    | None (read-only)                                                                                           | Derived from storage                                      | N/A (stateless commands)                                           | Inspect commands require `--storage-path`; cannot inspect in-memory state from a prior crashed process                                                                                            | `medre inspect event/receipts/native-ref/replay`, `medre trace event/replay`, `medre evidence`                  |

## Event Taxonomy

The `RuntimeEventType` enum defines 16 runtime event types, grouped by lifecycle phase. Each type carries a JSON-safe string value and a bounded detail dict.

**Adapter lifecycle:** `adapter_started`, `adapter_start_failed`, `adapter_stopped`
**Startup classification:** `startup_classified`, `route_skipped`, `route_unavailable`
**Runtime state:** `state_transition`
**Retry progression:** `retry_started`, `retry_attempted`, `retry_succeeded`, `retry_failed`, `retry_dead_lettered`, `retry_stopped`
**Retry cancellation:** `retry_abandoned` (running retry loop abandoned mid-flight), `retry_start_refused` (retry requested but refused before loop began because abandonment was already in effect)
**Diagnostics:** `health_refreshed` (read-only observational signal)

The `retry_abandoned` vs `retry_start_refused` distinction is clear in the enum docstrings: abandoned fires when the loop was already executing; start_refused fires when no loop was running. Both are runtime-local and stored only in the in-memory EventBuffer.

## Evidence Bundle Schema Alignment

The evidence bundle top-level shape (`collect_evidence_bundle()` output) has `schema_version: 1`. The bundle hoists runtime-derived fields to the top level:

- `adapter_status`: per-adapter status evidence derived from runtime snapshot. `None` in storage-only mode.
- `shutdown_evidence`: shutdown state evidence derived from runtime snapshot. `None` in storage-only mode.
- Storage-derived fields: `convergence_summary`, `orphan_report`, `lifecycle_convergence_report`, `recovery_summary`, `recovery_ledger`.

There is no `runtime` top-level section. Runtime events live in the in-memory EventBuffer and are not hoisted into the bundle because they are ephemeral. The `runtime_events` section appears only in the diagnostics snapshot, not in the evidence bundle's top-level shape.

Schema/example alignment was verified against the machine-readable JSON Schema and the spec text. No edits were needed.

## Smoke Report Enrichment

The smoke report (`smoke_runtime()` output) includes three runtime evidence fields:

- `adapter_lifecycle`: dict of adapter IDs to lifecycle state, sourced from `snapshot.lifecycle.adapters`.
- `shutdown_status`: the `shutdown_status` value from `shutdown_evidence`, or `None` if unavailable.
- `runtime_events_count`: count of events in the EventBuffer at report time.

On failure paths, these fields default to empty dict / `None` / 0 respectively.

## Run-Session Report Enrichment

The run-session report (`run_bridge_session()` output) includes three runtime evidence fields:

- `adapter_lifecycle`: dict of adapter IDs to lifecycle state.
- `shutdown_status`: `"stopped"` when the runtime reached stopped state, otherwise `None`.
- `retry_worker_summary`: dict with retry worker counts when the retry worker ran, otherwise `None`.

On failure paths, these fields default to empty dict / `None` / `None`.

Cross-linked CLI commands (`inspect_event`, `trace_event`, `evidence_bundle`, `recover_event`) are included when `storage_path` is available. When storage is ephemeral, commands are `None` with an explanatory note.

## Shutdown Evidence

The `ShutdownEvidence` model is a pure function output with no I/O or side effects. Key boundary:

- **In-memory evidence**: `ShutdownEvidence` dataclass fields (shutdown_status, resume_expected, outbox_shutdown_policy, pending_outbox_counts, drain_timeout_detected, tasks_cancelled, evidence_flush_status). These are derived at snapshot time and lost on process exit.
- **Durable receipts**: Delivery receipts with `failure_kind="shutdown_rejection"` persisted to `delivery_receipts` table. These survive crashes and restarts.

The adapter status observational caveat applies: `adapter_status` in the evidence bundle reflects the runtime snapshot at collection time, not continuous monitoring. An adapter may change state between collection and inspection.

## Shutdown Rejection Story

When the runtime shuts down, in-flight deliveries that cannot complete within the drain timeout receive `failure_kind="shutdown_rejection"`. The pipeline appends durable receipt rows for these deliveries. This provides the operator with a persistent record of which deliveries were suppressed during shutdown.

The `ShutdownEvidence` model detects drain timeout from runtime events or shutdown reason and sets `drain_timeout_detected=True`. The `resume_expected` field indicates whether non-terminal outbox work survives for restart recovery.

The drill command includes a `shutdown_rejection` scenario that exercises this path end-to-end with fake adapters.

## Retry Worker Visibility

Retry events flow through the EventBuffer (8 of the 16 event types are retry-related). The retry chain is fully durable via receipt `parent_receipt_id` links and `attempt_number` tracking.

The `retry_outbox_summary` field on the evidence bundle provides aggregate retry/outbox accountability with per-item details.

Known gaps:

- Retry abandoned/start_refused distinction is runtime-local. The failure kind on the receipt does not distinguish between abandoned and start_refused; both result in the same receipt state.
- EventBuffer retry events are lost on crash. The durable receipt chain is the authoritative record.

## Adapter Lifecycle Visibility

Adapter lifecycle state is visible through two independent channels:

1. **Canonical events**: `system.lifecycle` events stored in `canonical_events` table. These survive restarts and are queryable through standard event inspection tools.
2. **Runtime events**: `adapter_started`, `adapter_start_failed`, `adapter_stopped` in the EventBuffer. These are in-memory only.

The runtime snapshot's `lifecycle.adapters` section maps adapter IDs to their current lifecycle state. Both the smoke and run-session reports expose this as `adapter_lifecycle`.

## Native-Ref Persistence Gap

Native refs are persisted through two paths:

- Receipt append (synchronous with delivery outcome).
- `OutboundNativeRefRecord` callback write (asynchronous, queue-based transports).

No additional durable surface exists for native refs:

- No write-ahead log for native refs independent of the receipt append path.
- No outbox-level native-ref field.
- No crash-safe native-ref buffer.

If the process crashes between receiving a native ref from the transport and persisting it to SQLite, the native ref is permanently lost. This is a known survivability characteristic documented in the spec (diagnostics-evidence.md observational-only caveat, item on runtime event buffers being in-memory only).

The spec preserves durable semantics for native refs (what is persisted, what the guarantees are). This audit records the implementation gap without introducing process labels or temporary language into the spec.

## CLI Operator Views

Read-only CLI commands expose runtime evidence from storage:

| Command                                                   | Surface exposed                         |
| --------------------------------------------------------- | --------------------------------------- |
| `medre inspect event <ID> --storage-path PATH`            | Event summary, delivery state by target |
| `medre inspect event <ID> --timeline --storage-path PATH` | Chronological timeline with receipts    |
| `medre inspect event <ID> --evidence --storage-path PATH` | Per-event evidence bundle               |
| `medre inspect event <ID> --recovery --storage-path PATH` | Recovery runbook                        |
| `medre inspect receipts --event <ID> --storage-path PATH` | Delivery receipt queries                |
| `medre inspect native-ref --storage-path PATH`            | Native message reference lookup         |
| `medre inspect replay --storage-path PATH`                | Replay run inspection                   |
| `medre trace event <ID> --storage-path PATH`              | Standalone timeline                     |
| `medre evidence --storage-path PATH --json`               | Full evidence bundle collection         |

All inspect/evidence/trace commands require `--storage-path`. They cannot inspect in-memory state from a prior process.

## Docs Placement Rule

- **Spec docs** (`docs/spec/`) define durable semantics: data models, contracts, guarantees, normative requirements. No implementation gaps, no process labels, no temporary language.
- **Dev audit docs** (`docs/dev/`, this file) record implementation completeness, gaps, and findings. These are derived references that track what the runtime actually does relative to the spec.

If a gap recorded here is resolved, the entry is updated or removed. The spec is not modified to track implementation status.

## Tests

Schema and documentation consistency tests validate alignment between the spec text, JSON Schema definitions, and code behavior:

- 33 schema tests passed (JSON Schema validation against code output).
- 41 doc tests passed (spec text consistency checks).
- 39 smoke runtime evidence tests passed (smoke report shape, failure defaults, enrichment fields).
- 14 run-session report tests passed (report shape, runtime evidence fields, cross-linked commands).
- LSP native-ref annotation verified in run-session report test.

## Boundary Docstrings

Boundary docstrings added to the following areas to clarify evidence scope:

- **Shutdown evidence**: clarifies in-memory model vs durable receipt boundary. `ShutdownEvidence` is a pure function snapshot; shutdown_rejection receipts are the durable record.
- **Adapter status**: observational caveat. `adapter_status` reflects the runtime snapshot at collection time, not continuous monitoring.
- **Retry abandoned**: runtime-local distinction. The `retry_abandoned` vs `retry_start_refused` event type distinction is captured in the EventBuffer only and does not appear as a distinct failure kind on persisted receipts.
