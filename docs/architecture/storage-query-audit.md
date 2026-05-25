# Storage Query Audit

**Date:** 2026-05-15
**Scope:** All SQLite query paths used by trace, recover, evidence, inspect, replay, native-ref resolution, and receipt lineage commands.
**Source files audited:**

- `src/medre/core/storage/sqlite.py` — all prepared statements and dynamic queries
- `src/medre/cli/trace_commands.py` — trace event, trace replay
- `src/medre/cli/inspect_commands.py` — inspect event, receipts, native-ref
- `src/medre/cli/evidence_commands.py` — evidence bundle (delegates to runtime)
- `src/medre/cli/recover_commands.py` — recover runbook
- `src/medre/runtime/evidence/` — evidence storage section
- `src/medre/runtime/run_session/evidence.py` — receipt polling, native ref collection
- `src/medre/core/storage/replay.py` — replay engine iteration

---

## Section 1: Overview

This audit identifies every SQL query shape exercised by MEDRE's CLI and runtime
commands, evaluates index coverage for each, and documents N+1 query patterns.
The goal is to ensure hot query paths are covered by indexes without over-indexing
columns that are never filtered on.

### Tables

| Table                 | Row count estimate                      | Primary key                                                         |
| --------------------- | --------------------------------------- | ------------------------------------------------------------------- |
| `canonical_events`    | Per-event (grows with volume)           | `event_id TEXT`                                                     |
| `event_relations`     | 0–N per event                           | `id INTEGER AUTOINCREMENT`                                          |
| `native_message_refs` | 0–N per event                           | `id TEXT` + `UNIQUE(adapter, native_channel_id, native_message_id)` |
| `delivery_receipts`   | 1–N per event per adapter (append-only) | `sequence INTEGER AUTOINCREMENT`                                    |
| `plugin_state`        | Static / low volume                     | `PRIMARY KEY(plugin_id, key)`                                       |

### Existing indexes (pre-audit)

| Index                       | Columns                                                                        | Purpose                                                                                                    |
| --------------------------- | ------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------- |
| `idx_events_timestamp`      | `(timestamp, event_id)`                                                        | Event timeline ORDER BY                                                                                    |
| `idx_relations_event_id`    | `(event_id, id)`                                                               | Relation lookup per event                                                                                  |
| `idx_nrefs_event_id`        | `(event_id)`                                                                   | Native ref lookup per event                                                                                |
| `idx_receipts_plan`         | `(delivery_plan_id, target_adapter, target_channel, attempt_number, sequence)` | Plan receipt queries + delivery_status view (groups by delivery_plan_id + target_adapter + target_channel) |
| `idx_receipts_event`        | `(event_id, sequence)`                                                         | Receipt lookup per event                                                                                   |
| `idx_receipts_source`       | `(source, replay_run_id)`                                                      | Source+run filtering                                                                                       |
| `idx_receipts_retry_due`    | `(status, failure_kind, next_retry_at)`                                        | RetryWorker due-receipt poll (Q13)                                                                         |
| `idx_receipts_parent_retry` | `(parent_receipt_id, source)`                                                  | NOT EXISTS subquery in list_due_retry_receipts (Q13)                                                       |

---

## Section 2: Query Table

Every distinct query shape found across the audited files.

| #   | Query                                     | Table(s)                                               | WHERE columns                                                                                                                                        | ORDER BY                           | Frequency                                                   | Indexed?                                              | Notes                                                                                                                                                                 |
| --- | ----------------------------------------- | ------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------- | ----------------------------------------------------------- | ----------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Q1  | `get(event_id)`                           | `canonical_events`                                     | `event_id = ?`                                                                                                                                       | —                                  | per-inspect, per-trace, per-replay, per-evidence            | ✅ PK                                                 | Hottest single-row lookup                                                                                                                                             |
| Q2  | `list_relations(event_id)`                | `event_relations`                                      | `event_id = ?`                                                                                                                                       | `id ASC`                           | per-trace, per-recover, per-evidence                        | ✅ idx_relations_event_id                             |                                                                                                                                                                       |
| Q3  | `resolve_native_ref`                      | `native_message_refs`                                  | `adapter = ?, native_channel_id IS ?, native_message_id = ?`                                                                                         | —                                  | per-dedup-check, per-receipt in evidence                    | ✅ UNIQUE autoindex                                   | Covers NULL channel via `IS ?`                                                                                                                                        |
| Q4  | `delivery_status(plan, adapter, channel)` | `delivery_status` VIEW → `delivery_receipts` self-join | `delivery_plan_id = ?, target_adapter = ?, target_channel IS ?`                                                                                      | —                                  | per-delivery-check                                          | ✅ idx_receipts_plan                                  | View subquery groups by delivery_plan_id + target_adapter + target_channel; route_id is part of evidence `delivery_state_by_target` grouping but not the storage view |
| Q5  | `list_receipts_for_plan`                  | `delivery_receipts`                                    | `delivery_plan_id = ?, target_adapter = ?`                                                                                                           | `attempt_number ASC, sequence ASC` | per-plan-inspect                                            | ✅ idx_receipts_plan                                  |                                                                                                                                                                       |
| Q6  | `list_receipts_by_replay_run`             | `delivery_receipts`                                    | `replay_run_id = ?`                                                                                                                                  | `sequence ASC`                     | per-trace-replay, per-inspect, per-evidence                 | ❌ **MISSING**                                        | `idx_receipts_source(source, replay_run_id)` cannot serve `replay_run_id` alone; full scan                                                                            |
| Q7  | `list_receipts_for_event`                 | `delivery_receipts`                                    | `event_id = ?`                                                                                                                                       | `sequence ASC`                     | per-trace, per-inspect, per-recover, per-evidence, per-poll | ✅ idx_receipts_event                                 | Hottest receipt query                                                                                                                                                 |
| Q8  | `list_native_refs_for_event`              | `native_message_refs`                                  | `event_id = ?`                                                                                                                                       | `created_at ASC`                   | per-trace, per-recover, per-evidence                        | ⚠️ Partial                                            | `idx_nrefs_event_id(event_id)` covers WHERE but ORDER BY `created_at` requires extra sort                                                                             |
| Q9  | `query(EventFilter)`                      | `canonical_events`                                     | Dynamic: `event_kind IN`, `source_adapter IN`, `timestamp >=`, `timestamp <=`                                                                        | `timestamp ASC`                    | per-replay, per-broad-query                                 | ✅ idx_events_timestamp                               | Compound filters; index covers timestamp range + ORDER BY                                                                                                             |
| Q10 | `query() relations batch`                 | `event_relations`                                      | `event_id IN (?)`                                                                                                                                    | —                                  | per-query (batch)                                           | ✅ idx_relations_event_id                             | Batch fetch after Q9                                                                                                                                                  |
| Q11 | `count_events`                            | `canonical_events`                                     | —                                                                                                                                                    | —                                  | per-evidence                                                | N/A                                                   | Full scan, acceptable                                                                                                                                                 |
| Q12 | `count_receipts`                          | `delivery_receipts`                                    | —                                                                                                                                                    | —                                  | per-evidence                                                | N/A                                                   | Full scan, acceptable                                                                                                                                                 |
| Q13 | `list_due_retry_receipts`                 | `delivery_receipts`                                    | `status = 'failed', failure_kind = 'adapter_transient', next_retry_at IS NOT NULL, next_retry_at <= ?, attempt_number < ?, NOT EXISTS (child retry)` | `next_retry_at ASC, sequence ASC`  | per-RetryWorker-cycle                                       | ✅ idx_receipts_retry_due + idx_receipts_parent_retry | `max_attempts` parameterised (default 3); NOT EXISTS excludes receipts already handled by a child retry receipt                                                       |

---

## Section 3: Findings

### F1: Missing index — `delivery_receipts.replay_run_id`

- **Query affected:** Q6 (`_SELECT_RECEIPTS_BY_REPLAY_RUN`)
- **WHERE:** `replay_run_id = ?` (no `source` filter)
- **Current index:** `idx_receipts_source(source, replay_run_id)` — leading column is `source`, so this index **cannot** serve queries that filter `replay_run_id` alone
- **Callers:** `trace_commands._trace_replay`, `inspect_commands._inspect_receipts`, `runtime/evidence._collect_storage_section`
- **Impact:** Full table scan of `delivery_receipts` on every replay-run query. `delivery_receipts` is the highest-volume append-only table.
- **Severity:** Medium-High

### F2: Suboptimal index — `native_message_refs(event_id)` vs `(event_id, created_at)`

- **Query affected:** Q8 (`_SELECT_NREFS_FOR_EVENT`)
- **WHERE:** `event_id = ?`
- **ORDER BY:** `created_at ASC`
- **Current index:** `idx_nrefs_event_id(event_id)` — covers the WHERE but SQLite must sort results by `created_at` after the index scan
- **Callers:** `trace_commands._trace_event`, `recover_commands._recover`, `runtime/evidence._collect_storage_section`, `run_session/evidence._collect_native_refs`
- **Impact:** Minor — result sets per event are typically small. But the fix is trivial: extend the index to include `created_at` as the second column, eliminating the sort entirely.
- **Severity:** Low

### F3: N+1 pattern — `trace_commands._trace_replay` (lines 112–117)

- **Pattern:** After fetching receipts by `replay_run_id`, iterates over unique `event_id` values and calls `storage.get(eid)` individually for each.
- **Impact:** N round-trips to SQLite (one per distinct event in the replay run). For large replay runs this can be significant.
- **Fix suggestion:** Use a single `query()` call with `EventFilter` or add a batch `get_many()` method that does `WHERE event_id IN (...)`. The `query()` method already does this batch-fetch for relations.
- **Severity:** Medium

### F4: N+1 pattern — `run_session/evidence._collect_native_refs` (lines 166–171)

- **Pattern:** After fetching `list_native_refs_for_event(event_id)`, iterates over each outbound ref and calls `storage.resolve_native_ref(adapter, channel, message)` individually.
- **Impact:** Each `resolve_native_ref` is a single-row lookup (covered by UNIQUE index), so performance is acceptable. But the N+1 pattern is wasteful when many refs exist.
- **Fix suggestion:** The resolve calls are verification-only (confirming each ref still resolves). Consider whether this verification is necessary or if the initial `list_native_refs_for_event` result is sufficient.
- **Severity:** Low

### F5: N+1 pattern — `replay.py ReplayEngine._iter_by_ids` (lines 1037–1048)

- **Pattern:** For `correlation_ids` replay, fetches each event individually via `storage.get(eid)` in a loop.
- **Impact:** N round-trips. Same root cause as F3 — no batch get method.
- **Fix suggestion:** Same as F3 — batch `get_many()` or `WHERE event_id IN (...)`.
- **Severity:** Medium

### F6: N+1 pattern — `replay.py ReplayEngine.count_matching` (lines 1004–1010)

- **Pattern:** Same as F5 but for counting. Each `storage.get(eid)` fetches the full event just to check filters.
- **Fix suggestion:** Batch fetch with post-filtering.
- **Severity:** Low (counting is less frequent than replay)

### F7: No redundant indexes found

- All existing indexes serve distinct query shapes. No duplicates or fully-overlapping indexes.

### F8: Missing index for RetryWorker query (Q13) — resolved

- **Query affected:** Q13 (`list_due_retry_receipts`)
- **WHERE:** `status = 'failed' AND failure_kind = 'adapter_transient' AND next_retry_at IS NOT NULL AND next_retry_at <= ? AND attempt_number < ?`
- **ORDER BY:** `next_retry_at ASC, sequence ASC`
- **Current index coverage:** `idx_receipts_retry_due` — partial index on `(next_retry_at) WHERE status = 'failed' AND failure_kind = 'adapter_transient' AND next_retry_at IS NOT NULL`. Covers the majority of the filter; `attempt_number < ?` is evaluated as a post-filter on the index result.
- **Callers:** RetryWorker cycle loop
- **Parameterised `max_attempts`:** The query accepts `max_attempts` (default 3) as a parameter. Receipts with `attempt_number >= max_attempts` are excluded. This means the retry ceiling is configurable per invocation rather than hardcoded.
- **Severity:** Resolved — partial index provides adequate coverage for the RetryWorker cycle poll.

### F9: Retry due partial index — idx_receipts_retry_due

- **Index definition:**
  ```sql
  CREATE INDEX IF NOT EXISTS idx_receipts_retry_due
      ON delivery_receipts(next_retry_at)
      WHERE status = 'failed'
        AND failure_kind = 'adapter_transient'
        AND next_retry_at IS NOT NULL;
  ```
- **Purpose:** Serves the RetryWorker's `list_due_retry_receipts` query (Q13). The partial index only indexes rows that match the retryable filter (`status='failed'`, `failure_kind='adapter_transient'`, `next_retry_at IS NOT NULL`), keeping the index small relative to the full `delivery_receipts` table.
- **Coverage:** The index column `next_retry_at` supports both `next_retry_at <= ?` and `ORDER BY next_retry_at ASC`. The `attempt_number < ?` condition (from parameterised `max_attempts`) is applied as a post-filter on the index result set.
- **Callers:** RetryWorker cycle loop, `count_pending_retry`.

### F10: Retry deduplication — NOT EXISTS clause (resolved)

- **Query affected:** Q13 (`list_due_retry_receipts`, `count_pending_retry`)
- **Problem:** Without deduplication, `list_due_retry_receipts` could return receipts that already had a child retry receipt (i.e. a delivery receipt with `source='retry'` and `parent_receipt_id` pointing back to the failed receipt). This caused repeated retry attempts for the same failure.
- **Fix:** Added a `NOT EXISTS` subquery that checks for child receipts with `source = 'retry'` linked via `parent_receipt_id`. Also added `idx_receipts_parent_retry(parent_receipt_id, source)` to cover the subquery.
- **Severity:** Resolved — prevents the retry blocker where already-handled due receipts were re-queued.

### F11: Dead-letter lineage specificity (resolved)

- **Problem:** `_check_dead_lettered` and `_find_dead_letter_receipt_id` in the RetryWorker matched any dead-lettered receipt for the same `event_id`/`target_adapter`, regardless of retry lineage. This could incorrectly identify a dead-letter from a different retry chain.
- **Fix:** Both methods now also filter by `parent_receipt_id`, matching the specific retry chain that produced the failure.
- **Severity:** Resolved — dead-letter detection now respects retry lineage boundaries.

### F12: No unindexed JOIN foreign keys

- `event_relations.event_id` → `canonical_events.event_id`: covered by `idx_relations_event_id`
- `native_message_refs.event_id` → `canonical_events.event_id`: covered by `idx_nrefs_event_id` (and proposed replacement)
- `delivery_receipts.event_id` → `canonical_events.event_id`: covered by `idx_receipts_event`

### F13: No ORDER BY on unindexed columns in hot paths

- `timestamp ASC` on `canonical_events`: covered by `idx_events_timestamp`
- `sequence ASC` on `delivery_receipts`: covered by PK or composite indexes
- `created_at ASC` on `native_message_refs`: addressed by F2

---

## Section 4: Recommendations

### R1: Add `idx_receipts_replay_run` (HIGH priority)

```sql
CREATE INDEX IF NOT EXISTS idx_receipts_replay_run
    ON delivery_receipts(replay_run_id);
```

- **Why:** Query Q6 filters by `replay_run_id` alone. The existing `idx_receipts_source(source, replay_run_id)` cannot serve this because `source` is not in the WHERE clause. Every `trace replay`, `inspect --replay-run`, and evidence bundle with a replay_run_id triggers a full table scan.
- **Queries benefited:** Q6
- **Estimated impact:** Full scan → index seek. Impact grows linearly with receipt table size.

> **Note:** The retry due index (`idx_receipts_retry_due`) recommended under the original F8 finding has been added to the `_INDEXES` block in `sqlite.py`. Finding F8 is resolved. See F9 for the index definition.

### R2: Replace `idx_nrefs_event_id` with `idx_nrefs_event_created` (LOW priority)

```sql
-- Remove old:
-- CREATE INDEX IF NOT EXISTS idx_nrefs_event_id ON native_message_refs(event_id);
-- Add new:
CREATE INDEX IF NOT EXISTS idx_nrefs_event_created
    ON native_message_refs(event_id, created_at);
```

- **Why:** Query Q8 filters by `event_id` and orders by `created_at ASC`. A composite index on `(event_id, created_at)` eliminates the in-memory sort. The old single-column index `(event_id)` is a strict prefix of the new one, so the new index is strictly more useful.
- **Queries benefited:** Q8
- **Estimated impact:** Minor — result sets per event are small. Eliminates a sort step.
- **Implementation:** Add the new index and remove the old one. SQLite `DROP INDEX IF EXISTS` + `CREATE INDEX IF NOT EXISTS` in the `_INDEXES` block.

### R3: Document N+1 patterns for future improvement (no code change)

The N+1 patterns (F3, F5, F6) are in CLI and replay code paths that are not in the hot runtime delivery loop. They affect operator-facing commands (trace, evidence, replay) rather than per-event delivery. A batch `get_many()` method on `SQLiteStorage` would fix all three patterns simultaneously, but this is an optimization, not a correctness issue.

## Section 5: Lineage Ordering Guarantees

This section documents the deterministic ordering properties that underpin
receipt lineage, event replay traceability, and evidence bundle assembly.

### Receipts: ORDER BY sequence ASC (deterministic, append-only)

`delivery_receipts.sequence` is an `INTEGER PRIMARY KEY AUTOINCREMENT`. Every
receipt is assigned a monotonically increasing sequence at INSERT time. Ordering
by `sequence ASC` is deterministic and stable across restarts: SQLite
auto-increment never reuses a value from a previous session. All receipt query
paths use this ordering.

### Events: ORDER BY timestamp ASC, event_id ASC tiebreaker

Event queries use `ORDER BY timestamp ASC, event_id ASC`. The `event_id`
tiebreaker ensures deterministic ordering when two events share the same logical
timestamp (set by the source adapter). Events are ordered by their logical
occurrence time, not by storage insertion time.

### Native refs: ORDER BY created_at ASC, id ASC tiebreaker

Native message refs for an event are ordered by `created_at ASC, id ASC`. The
`id` tiebreaker ensures deterministic ordering when multiple refs share the same
timestamp. Index `idx_nrefs_event_id(event_id)` covers the WHERE clause;
recommendation R2 proposes extending it to cover the ORDER BY as well.

### Replay receipts: grouped by replay_run_id

Every replay receipt has `source='replay'` and a non-null `replay_run_id`. All
receipts produced by a single `medre replay --mode BEST_EFFORT` invocation share
the same `replay_run_id`. Multiple BEST_EFFORT runs of the same events produce
different `replay_run_id` values. The `replay_run_id` is unique per run, never
null for replay receipts, and never shared across runs. Replay receipts are not
grouped in sequence space; they are interleaved with live receipts in append
order.

### Live/replay interleaving: determined by sequence (append order)

Replay receipts are stored in the same `delivery_receipts` table as live
receipts. Because `sequence` is assigned at INSERT time, replay receipts always
have a higher sequence than any receipt inserted before them. When live events
are injected between replay runs, the sequence ordering reflects true append
order:

```text
...original live (seq 1..160)...
...replay run A (seq 161..166)...
...new live events (seq 167..176)...
...replay run B (seq 177..182)...
```

This ordering is deterministic across restart because `sequence` values are
persistent in SQLite and never reused.

### Ordering stability across restart

`sequence` is an auto-increment integer stored in the SQLite database file. It
survives crashes and restarts. After restart, new receipts continue from the
next auto-increment value with no gap filling. Gaps in the sequence indicate
lost in-flight deliveries (no receipt was written). A `delivery_outbox` row may still exist for these events.

`replay_run_id` is an operator-assigned string (or auto-generated UUID). It is
stored on each replay receipt and persists across restarts. Repeated replays of
the same event produce distinct `replay_run_id` values, making each run
independently traceable.

### Indexes NOT recommended

| Column                                | Why not                                                                                             |
| ------------------------------------- | --------------------------------------------------------------------------------------------------- |
| `canonical_events.event_kind`         | Low-cardinality column; query Q9 always has ORDER BY timestamp which uses `idx_events_timestamp`    |
| `canonical_events.source_adapter`     | Low-cardinality; always combined with timestamp range in practice                                   |
| `canonical_events.parent_event_id`    | Never used in a WHERE clause                                                                        |
| `delivery_receipts.parent_receipt_id` | Now indexed via `idx_receipts_parent_retry(parent_receipt_id, source)` for NOT EXISTS deduplication |
| `delivery_receipts.status`            | Low-cardinality; never queried alone                                                                |
