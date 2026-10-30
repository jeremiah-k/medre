# Prerelease SQLite schema identity

The executable source of the complete SQLite layout is
[`schema.py`](../../src/medre/core/storage/sqlite/schema.py). Initialization
compares its declared tables, columns, constraints, indexes and views with the
persisted SQLite catalog before schema writes. Unexpected objects, including
triggers, are refused. SQLite-managed objects are excluded.

Schema version remains `1` until a supported release boundary. A different
prerelease shape requires explicit backup and replacement, rather than migration.
See [storage policy](../spec/storage.md#10-pre-release-database-policy) for
operator recovery and transactional initialization requirements.
