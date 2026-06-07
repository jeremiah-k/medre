# Persistence Authority Audit

> **Classification:** Developer reference (derived from [storage.md](../spec/storage.md) Section 16)
> **Audience:** Runtime developers, code reviewers, operators wanting implementation detail.
> **Authority:** [storage.md](../spec/storage.md) is the normative specification. This document synthesizes Wave 1 audit findings into a compact per-domain inventory. If this document conflicts with storage.md, storage.md is correct.

## Domain Inventory

Each row covers one persisted SQLite domain. Classifications follow the legend below the table.

| Domain                                | Purpose                                                                         | Row identity                                                                                         | Owner                                                              | Write authority                                                                          | Mutation authority                                                                                                                                                                          | Delete authority                     | Retention                                        | Replay semantics                                                                                                                 | Recovery semantics                                                                                                                                           | Operator visibility                                                                                     | Classification               |
| ------------------------------------- | ------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------ | ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------ | ------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------- | ---------------------------- |
| `canonical_events`                    | Normalized event log; authoritative record of what happened                     | `event_id` (PK)                                                                                      | Pipeline ingress (after normalization and conversation assignment) | Pipeline via `append()`                                                                  | None (append-only)                                                                                                                                                                          | None                                 | Forever                                          | Read-only input. Replay never mutates or creates rows.                                                                           | Read-only input for orphan queries.                                                                                                                          | `medre inspect`, trace queries, evidence reports                                                        | Canonical fact               |
| `event_relations`                     | Inline and post-hoc relations attached to canonical events                      | `id` (auto-increment PK)                                                                             | Pipeline via `append()` (inline) and `store_relation()` (post-hoc) | Pipeline                                                                                 | None (append-only)                                                                                                                                                                          | None                                 | Forever                                          | Read-only. Replay reads relations but never inserts new ones.                                                                    | Not directly involved.                                                                                                                                       | Visible via event detail queries.                                                                       | Canonical fact               |
| `native_message_refs`                 | Native-to-canonical message correlation; idempotent first-writer-wins           | `id` (text PK); uniqueness on `(adapter, native_channel_id, native_message_id)`                      | Adapters (ingress) and delivery (outbound)                         | Core pipeline via `store_native_ref()` (adapters report facts, core records persistence) | None (idempotent insert only; no update)                                                                                                                                                    | None                                 | Forever                                          | BEST_EFFORT replay may create new outbound refs via adapter delivery. Never overwrites existing refs.                            | Not directly involved. Native refs are facts.                                                                                                                | `medre inspect`, evidence bundles via receipt linkage.                                                  | Native transport fact        |
| `delivery_receipts`                   | Append-only delivery evidence log; every attempt produces a new row             | `sequence` (auto-increment PK); `receipt_id` (unique)                                                | Pipeline delivery stage, RetryWorker, replay engine                | Pipeline / RetryWorker / replay via `append_receipt()`                                   | None (no UPDATE or DELETE on existing rows)                                                                                                                                                 | None                                 | Forever                                          | Creates new rows with `source='replay'`, `replay_run_id`, incremented `attempt_number`. Never mutates or deletes prior receipts. | Read-only. Recovery never fabricates a `sent` receipt or inserts fake success. Status labels are diagnostic.                                                 | `medre evidence`, `medre inspect`, trace queries. Current status via `delivery_status` view projection. | Append-only evidence         |
| `delivery_status` (view)              | Current delivery status projection: latest receipt per plan/adapter/channel     | Derived from `delivery_receipts` (no storage)                                                        | N/A (view)                                                         | N/A (no direct writes)                                                                   | N/A                                                                                                                                                                                         | N/A                                  | N/A                                              | N/A                                                                                                                              | N/A                                                                                                                                                          | Queried by operator commands and diagnostics.                                                           | Derived/report cache         |
| `delivery_outbox`                     | Operational delivery work state; mutable until terminal, then immutable history | `outbox_id` (PK); uniqueness on `(delivery_plan_id, target_adapter, target_channel, attempt_number)` | Pipeline planner (create), delivery workers (claim/transition)     | Pipeline planner via `create_outbox_item()`                                              | Delivery workers: non-terminal status transitions only (`pending`, `in_progress`, `queued`, `retry_wait`). Terminal rows (`sent`, `dead_lettered`, `cancelled`, `abandoned`) are immutable. | None                                 | Forever (terminal rows become immutable history) | Replay delegates real persistence to pipeline lifecycle. Replay does not directly manipulate outbox rows.                        | Recovery is read-only. `in_progress` rows with expired leases are re-claimable on restart. Orphan outbox rows are diagnostic signals, not lifecycle success. | `medre inspect`, outbox count queries, diagnostic snapshots.                                            | Operational work state       |
| `plugin_state`                        | Scoped key-value storage for plugins (schema-reserved; no current API exposed)  | `(plugin_id, key)` composite PK                                                                      | Schema-reserved (no current API)                                   | Not exposed                                                                              | Not exposed                                                                                                                                                                                 | None currently                       | Reserved / future plugin-defined                 | Not involved.                                                                                                                    | Not involved.                                                                                                                                                | Plugin-specific queries only.                                                                           | Schema-reserved              |
| `_medre_schema_meta`                  | Schema version and internal metadata                                            | `key` (PK)                                                                                           | `initialize()` on fresh database                                   | `initialize()` (version row)                                                             | `initialize()` (version row only)                                                                                                                                                           | None                                 | Forever                                          | Not involved.                                                                                                                    | Not involved.                                                                                                                                                | `medre inspect` schema version check.                                                                   | Schema metadata              |
| `actors` (spec-planned)               | Actor identity registry                                                         | `actor_id` (PK)                                                                                      | Identity resolution service                                        | Identity resolution service                                                              | Identity resolution service                                                                                                                                                                 | Identity resolution service          | Until explicitly deleted                         | Not defined yet.                                                                                                                 | Not defined yet.                                                                                                                                             | Not implemented.                                                                                        | Spec-planned not implemented |
| `native_identities` (spec-planned)    | Per-adapter native identity records                                             | `id` (auto-increment PK); uniqueness on `(adapter, native_id)`                                       | Identity resolution service                                        | Identity resolution service                                                              | Identity resolution service                                                                                                                                                                 | Identity resolution service          | Until explicitly deleted                         | Not defined yet.                                                                                                                 | Not defined yet.                                                                                                                                             | Not implemented.                                                                                        | Spec-planned not implemented |
| `actor_identity_links` (spec-planned) | Links between actors and native identities                                      | `id` (auto-increment PK); uniqueness on `(actor_id, native_identity_id)`                             | Identity resolution service                                        | Identity resolution service                                                              | Identity resolution service                                                                                                                                                                 | Identity resolution service          | Until explicitly deleted                         | Not defined yet.                                                                                                                 | Not defined yet.                                                                                                                                             | Not implemented.                                                                                        | Spec-planned not implemented |
| `actor_permissions` (spec-planned)    | Per-actor permission grants                                                     | `id` (auto-increment PK); uniqueness on `(actor_id, permission)`                                     | Authorization service                                              | Authorization service                                                                    | Authorization service                                                                                                                                                                       | Authorization service                | Until explicitly revoked                         | Not defined yet.                                                                                                                 | Not defined yet.                                                                                                                                             | Not implemented.                                                                                        | Spec-planned not implemented |
| `native_archive` (spec-planned)       | Opt-in raw native data archiving (compressed)                                   | `archive_id` (PK)                                                                                    | Archive-enabled adapters (opt-in)                                  | Archive-enabled adapters                                                                 | Not defined yet                                                                                                                                                                             | Configurable pruning (time or count) | Not defined yet.                                 | Not defined yet.                                                                                                                 | Not implemented.                                                                                                                                             | Spec-planned not implemented                                                                            | Spec-planned not implemented |

### Classification Legend

| Classification               | Meaning                                                                                                                                                   |
| ---------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Canonical fact               | Append-only, never mutated or deleted. The event log is the authoritative record.                                                                         |
| Append-only evidence         | Every attempt produces a new row. No row is ever updated or deleted. Current status is a projection.                                                      |
| Native transport fact        | Idempotent first-writer-wins correlation records. No update or delete path.                                                                               |
| Operational work state       | Mutable while work is in progress; immutable once terminal. No deletion path.                                                                             |
| Derived/report cache         | View or computed projection. Not stored; not writable.                                                                                                    |
| Schema metadata              | Internal version/metadata row. Written only during `initialize()`.                                                                                        |
| Schema-reserved              | Table exists in DDL but no storage API is exposed. Schema is reserved for future subsystem implementation. Not currently readable or writable at runtime. |
| Spec-planned not implemented | Table shape documented in [storage.md](../spec/storage.md) Sections 4.8/4.9 but not in current DDL. Do not assume runtime existence.                      |

## Deletion and Retention Rules

**There is no runtime `DELETE FROM` on any table.** This is a core invariant verified by the Wave 1 storage audit.

- `DROP VIEW IF EXISTS` and `DROP INDEX IF EXISTS` in `initialize()` are schema housekeeping, not data deletion. They drop and recreate the `delivery_status` view and indexes to match the current column shape.
- Terminal outbox rows (`sent`, `dead_lettered`, `cancelled`, `abandoned`) are immutable. They are never deleted, only superseded by new rows with different `attempt_number` values.
- All canonical facts and evidence rows are retained forever.
- Plugin state (`plugin_state`) is schema-reserved: the table exists in DDL but no storage API is currently exposed. Retention and access will be defined by the future plugin subsystem.
- Spec-planned identity/archive tables will define their own retention when implemented.

## Replay Persistence Semantics

Replay module storage access is read-only for all historical data. Real persistence happens through the pipeline lifecycle when BEST_EFFORT mode triggers actual deliveries.

**What replay reads:**

- `canonical_events` (event query)
- `event_relations` (relation reconstruction)
- `delivery_receipts` (receipt history, optional)

**What replay creates (BEST_EFFORT only, via pipeline):**

- New `delivery_receipts` rows with `source='replay'`, populated `replay_run_id`, and incremented `attempt_number`
- New `native_message_refs` rows via adapter delivery (outbound direction)

**What replay never does:**

- Never mutates or deletes existing receipt rows
- Never mutates or deletes existing outbox rows
- Never overwrites original native refs (first-writer-wins idempotency)
- Never modifies `canonical_events`
- Never directly writes to `delivery_outbox` (delegates to pipeline)

Replay is an ephemeral runtime operation, not a durable job system. Replay runs are not persisted. If the runtime crashes during replay, the run is lost and must be re-initiated manually. Replay deduplication is not provided; re-running replay may produce duplicate deliveries.

## Recovery Persistence Semantics

Recovery core and `medre recover` CLI are pure read-only diagnostics.

**What recovery reads:**

- `canonical_events` (event existence)
- `delivery_receipts` (receipt chain)
- `delivery_outbox` (outbox state)

**What recovery never does:**

- Never calls `append_receipt`, `DELETE`, `UPDATE`, or `INSERT` on any table
- Never fabricates a `sent` receipt or transitions an outbox row to terminal without a real delivery outcome
- Recovery status labels (`delivered`, `pending`, `orphan`, etc.) are diagnostic classifications, not persisted state

Recovery identifies orphans (events with no receipts) and stale work (outbox rows in non-terminal states). Fixing these requires actual delivery attempts, not recovery bookkeeping.

## Native-Ref Lifecycle

Native refs record the mapping between native transport message identifiers and canonical event IDs.

**Creation:**

- Inbound: adapter surfaces the native-to-canonical correlation fact to the runtime; core pipeline code calls `store_native_ref()` to persist it. Direction is `inbound`.
- Outbound: core delivery path calls `store_native_ref()` after the adapter confirms send. Direction is `outbound`.

**Idempotency:**

- `INSERT OR IGNORE` for non-NULL `native_channel_id` (SQLite UNIQUE constraint enforces idempotency).
- Explicit resolve-before-insert for NULL `native_channel_id` (SQLite treats each NULL as distinct in UNIQUE constraints). LXMF is the primary NULL-channel transport.

**First-writer-wins:** If the same `(adapter, native_channel_id, native_message_id)` tuple is stored twice (e.g., by replay after live delivery), the second insert is silently ignored. The original ref is preserved.

**No mutation, no deletion.** Native refs are transport facts, not operational state.

## Receipt Lifecycle

Receipts are the append-only evidence log for every delivery attempt.

**Creation:** `append_receipt()` inserts a new row. Every call creates a row; no update path exists.

**Receipt chain:** The `parent_receipt_id` field links retries to the original attempt. `attempt_number` increments with each retry. The full chain for a delivery is queryable via `list_receipts_for_plan()`.

**Status values:** `queued`, `sent`, `failed`, `dead_lettered`, `suppressed`.

**Sources:** `live` (normal pipeline), `retry` (RetryWorker), `replay` (replay engine). Each source is recorded in the `source` column.

**Current status projection:** The `delivery_status` view returns the latest receipt per `(delivery_plan_id, target_adapter, target_channel)` using `MAX(sequence)`. This is a read-only projection, not a mutable field.

**Capacity rejection:** Creates a new receipt with `failure_kind='capacity_rejection'` and `parent_receipt_id` set to the original receipt. The original receipt row is not mutated.

**Rendering evidence:** `rendering_evidence` stores serialized rendering evidence JSON for the delivery. NULL when no evidence is available (suppressed, failed, or skipped receipts).

## Outbox Lifecycle

The outbox persists operational delivery work state. Where receipts record what did happen, the outbox records what still needs to happen.

**Creation:** `create_outbox_item()` with idempotent create-or-reclaim semantics:

- Reclaimable row (`pending`, `retry_wait`): existing row is reclaimed with updated status and worker fields.
- Active row (`in_progress`, `queued`): returned unchanged. Active work is never stolen.
- Terminal row (`sent`, `dead_lettered`, `cancelled`, `abandoned`): returned unchanged. Terminal rows are immutable.

**Status transitions (non-terminal):**

- `pending` -> `in_progress` (claim)
- `in_progress` -> `queued` (hand to adapter-local queue)
- `queued` -> `in_progress` (stale reclaim after `STALE_QUEUED_GRACE_SECONDS`)
- `retry_wait` -> `pending` (release claim)

**Terminal transitions (no further changes allowed):**

- -> `sent` (delivery confirmed)
- -> `dead_lettered` (retries exhausted or terminal failure)
- -> `cancelled` (operator or shutdown)
- -> `abandoned` (drain timeout or ambiguous loss)

Terminal rows are never deleted. They become immutable operational history.

## Evidence Persistence Boundary

Evidence and read models are derived and read-only.

**Persisted evidence is receipt rows and `delivery_receipts.rendering_evidence`.** Nothing else in the database stores evidence.

**Computed on demand (not persisted):**

- Evidence bundles and convergence reports
- Orphan detection queries
- Recovery ledgers and status labels
- `medre evidence`, `medre trace`, diagnostic snapshots

These are projections against SQLite data. If a report contradicts the receipt chain, the receipts are the authority. No code path writes evidence storage outside the receipt row's `rendering_evidence` column.

Bundle schema version is unchanged. Evidence generation code does not create, mutate, or delete storage rows.

## Operator Visibility

Operators interact with persisted data through read-only diagnostic commands and derived views. No operator command writes to the database.

| Command          | What it reads                                      | Classification            |
| ---------------- | -------------------------------------------------- | ------------------------- |
| `medre inspect`  | Schema version, table stats, event/receipt samples | Diagnostic projection     |
| `medre evidence` | Receipt chains, rendering evidence, convergence    | Derived report            |
| `medre trace`    | Event lineage, receipt history, native refs        | Derived report            |
| `medre recover`  | Orphan events, stale outbox rows                   | Diagnostic classification |
| `medre smoke`    | Runtime health, adapter status                     | Ephemeral diagnostic      |

Operator documentation clarifies: data is immutable, replay/retry/recovery are the mechanisms for correcting delivery issues, reports are derived views, and there is no deletion workflow.

## Schema Version

`_EXPECTED_SCHEMA_VERSION` remains `1`. MEDRE is prerelease; the schema version is frozen until a release-tracked milestone. Column-shape validation in `initialize()` catches prerelease drift (missing columns) without a version bump.

This document does not imply, require, or suggest a schema bump, migration, DDL change, or compatibility shim.

## Cross-References

- **Normative specification:** [storage.md](../spec/storage.md) (Sections 4-16)
- **Ownership summary:** [storage.md Section 16](../spec/storage.md#16-storage-ownership-semantics)
- **Outbox status transitions:** [state-machines.md](../spec/state-machines.md)
- **Replay modes and constraints:** [storage.md Section 13](../spec/storage.md#13-replayrecovery-interface)
- **Delivery lifecycle vocabulary:** [delivery-lifecycle.md](../spec/delivery-lifecycle.md)
