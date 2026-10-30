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
