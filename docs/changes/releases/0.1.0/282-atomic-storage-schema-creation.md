# Atomic single-transaction storage schema creation

Schema and index DDL each execute as one explicit transaction during SQLite
storage initialization. A fresh database therefore pays one journal cycle for
its whole schema instead of one cycle per `CREATE` statement, which bounds the
initialization cost of every newly created database file. The transaction also
makes creation atomic: a failed or interrupted initialize can no longer leave
a half-built database behind.

Behavior is unchanged for successful initialization. The resulting schema is
identical, WAL mode is enabled exactly as before, and schema version, shape,
foreign-key, and constraint validation run in the same order after DDL.
