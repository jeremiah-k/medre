# Operator Surface Audit

> Compact map of every operator-facing command/report, its source data, output
> shape, field classification, and terminology notes. Intended to unblock
> surgical implementation agents and reduce overlap.

## 1. Command Surface

### 1.1 Read-Only Inspection (no runtime start, no storage mutation)

| Command                                            | Source                           | Output shape                        | Key sections                                                                                             |
| -------------------------------------------------- | -------------------------------- | ----------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `medre inspect event <id>`                         | SQLite (RO) via `--storage-path` | CanonicalEvent JSON                 | event_id, event_kind, source_adapter, payload, relations, timestamp, source_native_ref                   |
| `medre inspect event <id> --timeline`              | SQLite (RO)                      | `{event: ..., timeline: [...]}`     | Timeline entries: event, native_ref, receipt, relation                                                   |
| `medre inspect event <id> --evidence`              | SQLite (RO) + config             | `{event: ..., evidence: <bundle>}`  | Full evidence bundle (see §2.1)                                                                          |
| `medre inspect event <id> --recovery`              | SQLite (RO)                      | `{event: ..., recovery: <runbook>}` | Recovery runbook (see §2.4)                                                                              |
| `medre inspect receipts --event <id>`              | SQLite (RO)                      | Receipt array                       | receipt_id, status, target_adapter, route_id, attempt_number, failure_kind, error, source, replay_run_id |
| `medre inspect receipts --replay-run <id>`         | SQLite (RO)                      | Receipt array                       | Same as above, filtered by replay_run_id                                                                 |
| `medre inspect native-ref --adapter A --message M` | SQLite (RO)                      | Ref resolution JSON                 | adapter, native_channel_id, native_message_id, event_id, event (if found)                                |
| `medre inspect replay <run_id>`                    | SQLite (RO)                      | Replay timeline JSON                | Same shape as `medre trace replay`                                                                       |
| `medre trace event <id>`                           | SQLite (RO)                      | Timeline JSON or human-readable     | Chronological entries: event, relation, native_ref, receipt                                              |
| `medre trace replay <run_id>`                      | SQLite (RO)                      | Replay timeline JSON                | status, receipt_count, event_ids, timeline entries                                                       |

Source modules:

- `src/medre/cli/inspect_commands.py`
- `src/medre/cli/trace_commands.py`
- `src/medre/runtime/timeline.py` (assembly)
- `src/medre/runtime/trace.py` (serialisation)

### 1.2 Diagnostics (build-time or live-start)

| Command                                              | Source                                                        | Output shape                                |
| ---------------------------------------------------- | ------------------------------------------------------------- | ------------------------------------------- |
| `medre diagnostics --config <path>`                  | Config + RuntimeBuilder (build only, no start)                | Runtime snapshot JSON (17-section shape)    |
| `medre diagnostics --refresh-health --config <path>` | Config + RuntimeBuilder + app.start() + refresh_live_health() | Runtime snapshot JSON with live health data |

Source modules:

- `src/medre/cli/diagnostics_commands.py`
- `src/medre/runtime/snapshot.py`

### 1.3 Evidence Bundle

| Command                                                       | Source           | Output shape                                        |
| ------------------------------------------------------------- | ---------------- | --------------------------------------------------- |
| `medre evidence --storage-path <db> --json`                   | SQLite (RO) only | Evidence bundle (see §2.1)                          |
| `medre evidence --storage-path <db> --event <id> --json`      | SQLite (RO)      | Bundle with event-scoped storage + incident_summary |
| `medre evidence --storage-path <db> --replay-run <id> --json` | SQLite (RO)      | Bundle with replay-run receipts                     |

Source modules:

- `src/medre/cli/evidence_commands.py`
- `src/medre/runtime/evidence/_bundle.py` (orchestration)
- `src/medre/runtime/evidence/_diagnostics_sections.py`
- `src/medre/runtime/evidence/_storage_sections.py`
- `src/medre/runtime/evidence/_recovery_sections.py`
- `src/medre/runtime/evidence/_config_sections.py`

### 1.4 Smoke / Drill / Run-Session

| Command                             | Source                                   | Output shape                  |
| ----------------------------------- | ---------------------------------------- | ----------------------------- |
| `medre smoke --json`                | Fake adapter pipeline, in-memory storage | Smoke report (see §2.2)       |
| `medre smoke --drill <name> --json` | Drill runner                             | Drill report JSON             |
| `medre smoke --run-session --json`  | Full session lifecycle                   | Run-session report (see §2.2) |

Source modules:

- `src/medre/cli/smoke_commands.py`
- `src/medre/runtime/smoke.py`
- `src/medre/runtime/drill.py`
- `src/medre/runtime/run_session/orchestration.py`

### 1.5 Recovery

| Command                                                    | Source                               | Output shape                         |
| ---------------------------------------------------------- | ------------------------------------ | ------------------------------------ |
| `medre recover --storage-path <db> --event <id>`           | SQLite (RO) + failure classification | Recovery runbook (see §2.4)          |
| `medre recover --storage-path <db> --event <id> --dry-run` | SQLite (RO)                          | Runbook with dry_run preview section |
| `medre recover --storage-path <db> --failed-only`          | SQLite (RO)                          | Broad scan runbook                   |

Source modules:

- `src/medre/cli/recover_commands.py`
- `src/medre/core/recovery/classification.py`
- `src/medre/core/observability/classification.py`

### 1.6 Replay (may send messages)

| Command                                       | Source                                    | Output shape              |
| --------------------------------------------- | ----------------------------------------- | ------------------------- |
| `medre replay --mode <mode> --config <path>`  | Config + build (no start) + replay engine | Replay summary (see §2.3) |
| `medre replay --mode dry_run --config <path>` | Config + build                            | Summary (no deliveries)   |

Modes: `strict`, `re_render`, `re_route`, `best_effort`, `dry_run`

Source modules:

- `src/medre/cli/replay_commands.py`
- `src/medre/core/engine/replay/summary.py`
- `src/medre/core/engine/replay/types.py`

### 1.7 Config / Route / Utility

| Command                                 | Source                | Output                            |
| --------------------------------------- | --------------------- | --------------------------------- |
| `medre config check --config <path>`    | Config loader         | Human-readable validation summary |
| `medre config sample`                   | Template              | TOML sample config text           |
| `medre paths`                           | Path resolver         | Resolved MEDRE paths with status  |
| `medre adapters`                        | Config + SDK probe    | Adapter inventory                 |
| `medre routes validate --config <path>` | Config + route engine | Per-route validation summary      |
| `medre routes topology --config <path>` | Config                | Topology preview                  |
| `medre routes list --config <path>`     | Config                | Route detail listing              |
| `medre run --config <path>`             | Full runtime          | Runtime process (not a report)    |

Source modules:

- `src/medre/cli/config_commands.py`
- `src/medre/cli/route_commands.py`
- `src/medre/cli/run_commands.py`

---

## 2. Report Schemas

### 2.1 Evidence Bundle

Top-level keys in the JSON bundle returned by `medre evidence --json`:

| Field                          | Classification      | Notes                                            |
| ------------------------------ | ------------------- | ------------------------------------------------ |
| `schema_version`               | Schema/example-only | Always `1` pre-release                           |
| `status`                       | Derived diagnostic  | One of `passed`, `partial`, `error`              |
| `command`                      | Schema/example-only | Always `"evidence"`                              |
| `collected_at`                 | Schema/example-only | ISO-8601 collection start                        |
| `generated_at`                 | Schema/example-only | ISO-8601 generation complete                     |
| `medre_version`                | Schema/example-only | Package version string                           |
| `config_source`                | Adapter fact        | How config was discovered                        |
| `evidence_tier`                | Derived diagnostic  | Provenance tier (see §3.2)                       |
| `runtime_started`              | Adapter fact        | Whether runtime was started for live health      |
| `errors`                       | Derived diagnostic  | Section-level error messages                     |
| `limitations`                  | Schema/example-only | Known limitation strings                         |
| `adapter_status`               | Adapter fact        | Per-adapter runtime status list (see §3.1)       |
| `shutdown_evidence`            | Derived diagnostic  | Shutdown classification from snapshot (see §3.3) |
| `recovery_summary`             | Derived diagnostic  | Recovery ownership counts (see §3.4)             |
| `recovery_ledger`              | Derived diagnostic  | Per-item recovery actions (see §3.4)             |
| `convergence_summary`          | Derived diagnostic  | Per-target convergence (see §3.5)                |
| `orphan_report`                | Derived diagnostic  | Orphan/invalid-lineage findings                  |
| `lifecycle_convergence_report` | Derived diagnostic  | Lifecycle delivery findings                      |
| `sections`                     | Container           | Nested section objects (see below)               |

**Sections** (each has `status`, `data`, `error`):

| Section                | Source                   | Content                                                                                                                                                                                                                                  |
| ---------------------- | ------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `config_summary`       | Config object            | adapters, routes, limits, paths, storage, env_overrides_applied                                                                                                                                                                          |
| `route_validation`     | Config + route engine    | route_count, route_enabled, route_errors, valid                                                                                                                                                                                          |
| `diagnostics_snapshot` | Runtime snapshot (build) | Full 17-section runtime snapshot + adapter_status + shutdown_evidence                                                                                                                                                                    |
| `live_health`          | Runtime snapshot (live)  | Same as diagnostics_snapshot but with real health data; skipped unless `--include-refresh-health`                                                                                                                                        |
| `storage`              | SQLite (RO)              | db_path, event_count, receipt_count, event, native_refs_for_event, timeline, incident_summary, delivery_outcome_ledger, retry_outbox_summary, convergence_summary, orphan_report, lifecycle_convergence_report, delivery_state_by_target |
| `recovery`             | SQLite (RO) outbox       | recovery_summary, recovery_ledger, snapshot_context                                                                                                                                                                                      |

### 2.2 Smoke Report

| Field               | Classification                | Notes                                                 |
| ------------------- | ----------------------------- | ----------------------------------------------------- |
| `status`            | Authoritative lifecycle state | `"passed"` or `"failed"`                              |
| `evidence_level`    | Schema/example-only           | Always `"fake_bridge"` — does not overclaim           |
| `scenario_category` | Schema/example-only           | `"smoke"`                                             |
| `simulated`         | Schema/example-only           | `true`                                                |
| `command`           | Schema/example-only           | `"smoke"`                                             |
| `timestamp`         | Schema/example-only           | ISO-8601                                              |
| `event_id`          | Authoritative lifecycle state | Generated canonical event ID                          |
| `source_adapter`    | Adapter fact                  | Fake source adapter ID                                |
| `target_adapters`   | Adapter fact                  | List of target adapter IDs                            |
| `route_ids`         | Adapter fact                  | List of route IDs used                                |
| `delivery_receipts` | Authoritative lifecycle state | Per-target receipt records                            |
| `native_refs`       | Adapter fact                  | Native message references                             |
| `accounting`        | Derived diagnostic            | inbound_accepted, outbound_delivered, outbound_failed |
| `storage_path`      | Adapter fact                  | SQLite path (or null for in-memory)                   |
| `fail_reasons`      | Derived diagnostic            | List of failure reasons (only on fail)                |
| `limitations`       | Schema/example-only           | Known limitation strings                              |

Run-session report adds: `route_id`, `final_snapshot_checks` (schema_version, runtime_state), `final_snapshot_path`, `commands` (primary + specialized CLI commands for further inspection).

### 2.3 Replay Summary

| Field             | Classification                | Notes                                  |
| ----------------- | ----------------------------- | -------------------------------------- |
| `mode`            | Schema/example-only           | Replay mode string                     |
| `events_scanned`  | Derived diagnostic            | Total events examined                  |
| `events_replayed` | Derived diagnostic            | Events actually processed              |
| `by_status`       | Authoritative lifecycle state | Counts: passed, skipped, failed, error |
| `by_route`        | Derived diagnostic            | Per-route succeeded/failed counts      |
| `elapsed_ms`      | Derived diagnostic            | Wall-clock time                        |
| `errors`          | Derived diagnostic            | Error messages (capped at 10)          |

### 2.4 Recovery Runbook

| Field                    | Classification                | Notes                                               |
| ------------------------ | ----------------------------- | --------------------------------------------------- |
| `scope`                  | Schema/example-only           | `"event"` or `"scan"`                               |
| `event_id`               | Authoritative lifecycle state | Event being analyzed                                |
| `event_kind`             | Authoritative lifecycle state | Kind of the canonical event                         |
| `source_adapter`         | Adapter fact                  | Origin adapter                                      |
| `total_receipts`         | Derived diagnostic            | Receipt count                                       |
| `failed_targets`         | Authoritative lifecycle state | List of failed target details                       |
| `failure_classification` | Derived diagnostic            | Grouped: retryable, permanent, operational, unknown |
| `recommended_commands`   | Operator recommendation       | CLI commands to run next                            |
| `commands`               | Operator recommendation       | Structured: primary + specialized                   |
| `timeline`               | Derived diagnostic            | Chronological timeline entries                      |
| `replay_context`         | Derived diagnostic            | Prior replay run IDs (if any)                       |
| `warnings`               | Operator recommendation       | Duplicate-send risk, radio transport caveats        |
| `dry_run`                | Operator recommendation       | Preview section (only with `--dry-run`)             |

Per-failed-target fields: `target_adapter`, `status`, `attempt_number`, `receipt_id`, `failure_kind`, `category`, `target_channel`, `route_id`, `error`, `source`, `replay_run_id`.

---

## 3. Field Classification Reference

### 3.1 Adapter Status Evidence

Source: `src/medre/core/evidence/adapter_status.py`

| Field               | Classification                                    |
| ------------------- | ------------------------------------------------- |
| `adapter_id`        | Schema/example-only                               |
| `transport`         | Adapter fact                                      |
| `enabled`           | Adapter fact                                      |
| `configured`        | Adapter fact                                      |
| `adapter_kind`      | Adapter fact                                      |
| `operator_status`   | Derived diagnostic (mapped from lifecycle state)  |
| `current_state`     | Authoritative lifecycle state                     |
| `health`            | Adapter fact                                      |
| `connected`         | Derived diagnostic (derived from operator_status) |
| `failure_category`  | Adapter fact (caller-supplied)                    |
| `failure_reason`    | Adapter fact (caller-supplied)                    |
| `valid_transitions` | Derived diagnostic (from state machine)           |

**Operator status vocabulary** (canonical set from `OPERATOR_STATUSES`):

| Operator status  | Maps from lifecycle state                 |
| ---------------- | ----------------------------------------- |
| `disabled`       | `enabled=False` in config                 |
| `not_configured` | Enabled but no transport config           |
| `configured`     | Pre-startup (no lifecycle state observed) |
| `starting`       | `INITIALIZING`                            |
| `connected`      | `READY`                                   |
| `degraded`       | `DEGRADED` or `BACKPRESSURED`             |
| `unavailable`    | `DISCONNECTED`                            |
| `stopping`       | `STOPPING`                                |
| `failed`         | `FAILED`                                  |
| `stopped`        | `STOPPED`                                 |

### 3.2 Evidence Tier

Source: `src/medre/core/evidence/tiers.py`

| Tier           | When used                                                    |
| -------------- | ------------------------------------------------------------ |
| `synthetic`    | Fake adapters, replay, test fixtures (default; conservative) |
| `conformance`  | Conformance test suites with controlled inputs               |
| `docker`       | Docker bridge-artifact runs                                  |
| `live_service` | Never auto-inferred; explicit opt-in required                |
| `hardware`     | Never auto-inferred; explicit opt-in required                |

Inference: fake adapter_kind → synthetic; replay source → synthetic; docker artifact → docker; else → synthetic.

### 3.3 Shutdown Evidence

Source: `src/medre/core/evidence/shutdown.py`

| Field                      | Classification                                |
| -------------------------- | --------------------------------------------- |
| `runtime_state`            | Authoritative lifecycle state                 |
| `shutdown_status`          | Derived diagnostic                            |
| `shutdown_reason`          | Derived diagnostic                            |
| `pending_outbox_counts`    | Authoritative lifecycle state                 |
| `pending_retry_work_total` | Derived diagnostic                            |
| `retry_worker_*`           | Adapter fact (from runtime counters)          |
| `in_flight_count`          | Derived diagnostic (from capacity controller) |
| `tasks_cancelled`          | Derived diagnostic                            |
| `drain_timeout_detected`   | Derived diagnostic                            |
| `resume_expected`          | Operator recommendation                       |
| `outbox_shutdown_policy`   | Operator recommendation                       |

**Shutdown status values**: `running`, `graceful_stop`, `cancellation`, `adapter_failure`, `drain_timeout`, `shutdown_pending`, `stopped`, `failed`.

### 3.4 Recovery Ownership

Source: `src/medre/core/recovery/models.py`, `src/medre/core/recovery/builder.py`

**Recovery ownership statuses**: `recoverable`, `claimed_for_recovery`, `reclaimed`, `abandoned`, `unrecoverable`, `skipped`.

**Classification labels** (from `src/medre/core/recovery/classification.py`): `immediately_claimable`, `retry_eligible`, `stale`, `orphaned`, `terminal`, `inconsistent`.

**RecoverySummary fields**: `recoverable_items`, `claimed_items`, `reclaimed_items`, `skipped_items`, `abandoned_items`, `unrecoverable_items`, `total_items`, `consistency_valid`, `by_source`, `recovery_run_id`.

### 3.5 Convergence Diagnostics

Source: `src/medre/core/diagnostics/convergence/`

**Severity levels**: `safe`, `degraded`, `inconsistent`.

**ConvergenceSummary**: `severity_counts`, `targets` (per-target DeliveryTargetConvergence), `total_targets`, `worst_severity`, `warnings`, `orphan_count`, `evidence_bundle_ref`.

**Per-target fields**: `delivery_plan_id`, `target_adapter`, `target_channel`, `outbox_status`, `latest_receipt_status`, `latest_receipt_id`, `latest_attempt_number`, `severity`, `warnings`, `outbox_id`.

**LifecycleConvergenceReport**: `findings` (list of OrphanFinding), `total_findings`, `severity_counts`, `worst_severity`.

**OrphanFinding fields**: `kind`, `severity`, `record_id`, `record_type`, `details`, `extra`.

### 3.6 Delivery Outcome Ledger

Source: `src/medre/core/evidence/delivery_ledger.py`

Per-entry fields: `delivery_plan_id`, `event_id`, `route_id`, `target_adapter`, `target_channel`, `delivery_strategy`, `capability_field`, `capability_level`, `suppression_reason`, `final_status`, `attempt_number`, `retry_state`, `failure_kind`, `failure_taxon`, `failure_taxon_category`, `source`, `replay_run_id`, `receipt_ids`, `outbox_id`, `adapter_message_id`, `next_retry_at`, `error`.

Aggregate: `by_status`, `by_failure_taxon`.

**Retry-state labels** (derived, NOT authoritative lifecycle): `terminal`, `retryable`, `active`, `unknown`.

### 3.7 Failure Taxonomy

Source: `src/medre/core/evidence/failure_taxonomy.py`

**FailureTaxon values** (canonical categories):

| Group                   | Taxa                                                                                                                                                                                              |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Pipeline infrastructure | `planner_failure`, `renderer_failure`, `adapter_missing`                                                                                                                                          |
| Adapter                 | `adapter_transient`, `adapter_permanent`                                                                                                                                                          |
| Suppression             | `capability_suppressed`, `loop_suppressed`, `policy_suppressed`, `outbox_not_owned`                                                                                                               |
| Operational             | `capacity_rejection`, `shutdown_rejection`, `deadline_exceeded`                                                                                                                                   |
| Derived                 | `not_configured`, `unavailable`, `auth_failed`, `connection_failed`, `route_disabled`, `route_listen_only`, `delivery_failed`, `retry_exhausted`, `cancelled`, `shutdown_pending`, `not_executed` |

**Taxon categories** (coarse buckets): `retryable`, `permanent`, `operational`, `derived_terminal`, `unknown`.

### 3.8 Rendering Evidence

Source: `src/medre/core/rendering/evidence.py`

Attached to receipts as `rendering_evidence` JSON. Schema version: `"1"`. Contains: `schema_version`, `renderer_name`, `capability_level`, `delivery_strategy`, `fallback_applied`, `truncation_applied`, `original_text_chars`, `original_text_bytes`, `rendered_text_chars`, `rendered_text_bytes`, `relation_targets` (per-relation evidence). No payload duplication.

---

## 4. Relation / Conversation Fields

All relation fields are stored on `CanonicalEvent.relations` and surfaced in:

- `medre inspect event` output (relations list)
- `medre trace event` timeline entries with `entry_type = "relation"`
- Evidence bundle event summary: `relation_count`, `relation_types`
- Recovery runbook timeline: relation entries

Relation fields: `relation_type` (reply, reaction, edit, etc.), `target_event_id`, `native_ref`.

Conversation fields on receipts: `target_channel` (room ID / channel ID), `source_channel_id` on events.

---

## 5. Recovery / Replay Advice Surfaces

### Where recovery advice appears

| Surface                                            | Fields                                                                                      |
| -------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| `medre recover --event` runbook                    | `recommended_commands`, `commands`, `failure_classification`, `warnings`, `dry_run` preview |
| `medre inspect event --recovery`                   | Same runbook embedded in compound output                                                    |
| Evidence bundle storage section `incident_summary` | `recommended_commands`, `commands`, `classification`                                        |
| Evidence bundle top-level                          | `recovery_summary`, `recovery_ledger`                                                       |
| Convergence summary                                | Per-target `severity` + `warnings`                                                          |

### Where replay advice appears

| Surface                                 | Fields                                        |
| --------------------------------------- | --------------------------------------------- |
| `medre replay` stderr                   | BEST_EFFORT warning about duplicate-send risk |
| Recovery runbook `warnings`             | Radio transport duplicate-send risk           |
| Recovery runbook `commands.specialized` | `medre recover --event <id>`                  |
| Smoke report `limitations`              | First limitation shown as reminder            |

---

## 6. Stale / Ambiguous Terminology

| Term / Field                                              | Location                         | Issue                                                                                                   | Resolution                                                                                          |
| --------------------------------------------------------- | -------------------------------- | ------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| `evidence_level` (smoke) vs `evidence_tier` (evidence)    | Smoke report; evidence bundle    | Two names for the same concept across commands                                                          | Tracked for unification when smoke report schema can evolve. Smoke report is frozen pre-release.    |
| `shutdown_evidence`                                       | Evidence bundle top-level        | Name implies actual shutdown event, but it is derived from snapshot data at collection time             | Operator docs (diagnostics-and-evidence.md) now explicitly state this is a projection. No rename.   |
| `recovery_summary` / `recovery_ledger` in evidence bundle | Evidence bundle top-level        | Source is `snapshot_diagnostics`, not actual recovery; may mislead operators into thinking recovery ran | diagnostics-and-evidence.md now has "Snapshot vs. Real Recovery" section clarifying the distinction |
| `operator_status = "connected"`                           | Adapter status evidence          | Maps from `READY` lifecycle state, which means "adapter is ready" not necessarily "network connected"   | Documented in operator-workflows.md "Adapter Status Lifecycle" table as lifecycle-derived label     |
| `retry_outbox_summary.retry_worker`                       | Retry outbox summary             | Field name implies live worker state; value is derived from outbox snapshots                            | Documented in diagnostics-and-evidence.md "Recovery Sources" table as snapshot-derived              |
| `delivery_state_by_target`                                | Storage section incident_summary | Key is JSON-encoded composite; hard to read in raw JSON                                                 | Documented in operator-surface-audit.md §3.6 as composite key with field list                       |
| `convergence_truncated_warning`                           | Storage section                  | Only appears for global (no-event) queries when hitting 10K limit                                       | Documented in diagnostics-and-evidence.md convergence section with cap semantics                    |
| `fail_reasons`                                            | Smoke report                     | Only present on `status = "failed"`; inconsistent with other reports using `errors`                     | Tracked for alignment across report shapes. Smoke report is frozen pre-release.                     |

---

## 7. Implementation Follow-ups

These are likely surgical changes that implementation agents can tackle independently:

1. **Terminology unification**: Align `evidence_level` → `evidence_tier` in smoke report output. Low risk, JSON shape change. Tracked for post-release when smoke schema can evolve.
2. **Recovery source clarity**: `snapshot_context.source = "storage_snapshot"` is now documented in `diagnostics-and-evidence.md` under "Snapshot vs. Real Recovery".
3. **Adapter status mapping docs**: The `READY` → `"connected"` mapping is documented in `operator-workflows.md` "Adapter Status Lifecycle" table.
4. **Incident summary key format**: The JSON-encoded composite key in `delivery_state_by_target` is documented in §3.6 with the field list.
5. **Global convergence truncation**: The 10K limit and partial-view implications are documented in `diagnostics-and-evidence.md` convergence section.
6. **Rendering evidence in timeline**: Timeline entries do not currently surface rendering evidence; the `receipt` entry type could include a summary.

---

## 8. Schemas

Machine-readable schemas for the operator surface shapes:

| Schema file                                 | Shape                                |
| ------------------------------------------- | ------------------------------------ |
| `docs/schemas/evidence-bundle.schema.json`  | Evidence bundle top-level + sections |
| `docs/schemas/runtime-snapshot.schema.json` | 17-section runtime snapshot          |
| `docs/schemas/diagnostics.schema.json`      | Diagnostics collector output         |
| `docs/schemas/delivery-receipt.schema.json` | Individual receipt records           |
| `docs/schemas/canonical-event.schema.json`  | Canonical event records              |
| `docs/schemas/delivery-result.schema.json`  | Per-adapter delivery outcome         |

Examples: `docs/schemas/examples/`

---

## 9. Spec Authority

| Spec document                               | Governs                                                                |
| ------------------------------------------- | ---------------------------------------------------------------------- |
| `docs/spec/diagnostics-evidence.md`         | Evidence shape, snapshot contract, health vocabulary, tier definitions |
| `docs/spec/delivery-lifecycle.md`           | Receipt/outbox state machines, vocabulary tables                       |
| `docs/spec/state-machines.md`               | Adapter lifecycle states, transitions                                  |
| `docs/spec/appendices/failure-taxonomy.md`  | Per-transport failure classification, retry semantics                  |
| `docs/spec/appendices/release-readiness.md` | Transport maturity matrix                                              |
| `docs/spec/appendices/evidence-levels.md`   | Evidence tier definitions                                              |

Ops references (non-normative): `docs/ops/diagnostics-and-evidence.md`, `docs/ops/recovery-and-replay.md`, `docs/ops/operator-workflows.md`.
