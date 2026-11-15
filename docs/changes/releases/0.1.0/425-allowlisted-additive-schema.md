# Permit strictly verified additive database updates

Existing version-1 databases may gain the nullable `expired_at` field and
independent collection receipt table in one SQLite write transaction. The
predecessor's exact catalog must match the authored variants, including
constraints and indexes; unrelated schema changes still fail before writes.
No legacy data transformation, version bump, or compatibility framework.
