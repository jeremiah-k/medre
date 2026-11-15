# Strict prerelease SQLite schema identity

Initialization rejects missing and unexpected tables, columns, indexes, views,
and triggers before executing schema DDL or changing journal mode. Declared SQL
definitions enforce column types, nullability, defaults, keys, constraints, and
index expressions and predicates. A nonempty unstamped database is refused.
Fresh tables, indexes, and the version stamp commit atomically. Pressure-history
reads propagate missing-table errors rather than treating incompatible state as
empty history. No additive
repair or compatibility upgrade is provided; operators explicitly replace or
export development state. Schema version remains `1` until a supported release.
