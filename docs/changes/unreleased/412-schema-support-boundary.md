# Clarify the database compatibility boundary

Strict prerelease schema identity rejects unpublished development shapes before
DDL; it is not a promise to forbid additive migrations forever. After MEDRE
declares a supported schema baseline, forward-only migrations can be introduced
for compatible additions with explicit versioning and failure tests, without
creating readers or migration chains for abandoned pre-release data. Correct
storage documentation to recognize `plugin_state` as an already-reserved table,
and state which validation step runs before schema DDL (tables and columns)
versus after open (hidden columns, constraints, indexes, views, triggers).
