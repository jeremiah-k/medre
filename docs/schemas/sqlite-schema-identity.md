# SQLite schema identity

The executable source of the complete SQLite layout is
[`schema.py`](../../src/medre/core/storage/sqlite/schema.py). Initialization
compares its declared tables and columns with the persisted SQLite catalog
before schema writes, then compares the complete catalog — constraints,
indexes, views and triggers — after the database opens. Unexpected objects,
including triggers, are refused. SQLite-managed objects are excluded.

Schema version remains `1`; the tagged 0.1.0 catalog identifies the released
baseline. Writable initialization accepts the exact released catalog and
refuses every other layout without mutation. A future release MAY declare an
explicit forward-only predecessor of a released catalog and migrate it inside
the startup transaction under the
[database compatibility policy](../spec/storage.md#10-database-compatibility-policy);
abandoned unpublished development layouts need not be supported. Operator
recovery follows [storage policy §10.5](../spec/storage.md#105-unsupported-database-recovery-and-reset).

Internal metadata stores `outbox_deadline_cleanup_cursor` as a JSON pair
`[created_at, outbox_id]`; `["", ""]` starts a new pass. Cursor progress commits
or rolls back with the cleanup transitions and survives restart.
