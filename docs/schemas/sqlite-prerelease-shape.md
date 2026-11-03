# Prerelease SQLite schema identity

The executable source of the complete SQLite layout is
[`schema.py`](../../src/medre/core/storage/sqlite/schema.py). Initialization
compares its declared tables and columns with the persisted SQLite catalog
before schema writes, then compares the complete catalog — constraints,
indexes, views and triggers — after the database opens. Unexpected objects,
including triggers, are refused. SQLite-managed objects are excluded.

Schema version remains `1` while there is no supported database baseline.
Unsupported development layouts require explicit backup and replacement.
Once a baseline is supported, explicit forward-only additive migrations may be
introduced without supporting abandoned prerelease layouts.
See [storage policy](../spec/storage.md#10-pre-release-database-policy) for
operator recovery and transactional initialization requirements.

The deadline-cleanup ordering index is an approved additive predecessor:
initialization installs `idx_outbox_deadline_cleanup_order` atomically when
that index alone (or other explicitly allowlisted additive objects) is absent.
It indexes `(created_at, outbox_id)` for unclaimed, unreserved `pending` and
`retry_wait` rows. No table shape or schema-version change is required.
Internal metadata also stores `outbox_deadline_cleanup_cursor` as a JSON pair
`[created_at, outbox_id]`; `["", ""]` starts a new pass. Cursor progress commits
or rolls back with the cleanup transitions and survives restart.

The widened `operator_delivery_decisions` action vocabulary is likewise an
approved additive predecessor: initialization rebuilds the table from the
declared DDL when its stored definition still carries only
`action = 'cancel_unstarted'` instead of
`action IN ('cancel_unstarted', 'resolve_uncertain', 'resume_scheduled')`.
SQLite cannot `ALTER` a `CHECK`, so the rebuild renames the legacy rows
aside, recreates the table from the declared DDL, copies every row with an
explicit column list, drops the legacy table, and recreates its index — all
inside the same `BEGIN IMMEDIATE` transaction as the other additive steps,
with preserved row counts and a clean `PRAGMA foreign_key_check` asserted
before commit. No table references `operator_delivery_decisions`, so no
foreign-key rewriting is required. The predecessor definition is derived
from the authoritative DDL, never hand-written, and must match the stored
catalog byte for byte; any other layout difference still refuses to start.
