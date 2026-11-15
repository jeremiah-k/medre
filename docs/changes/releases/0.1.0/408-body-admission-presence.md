# Canonical admission presence is not current content availability

The immutable `event_body_identity` fact now stores `present_at_admission` as a
boolean integer, and `get_body_presence_at_admission()` returns `bool | None`.
The former `retained`/`unavailable` labels wrongly implied that an immutable
record could describe eventual content expiry. This is a prerelease schema
break and includes no backwards-compatibility reader or migration.
