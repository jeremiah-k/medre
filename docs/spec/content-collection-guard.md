# Guarded logical collection of isolated core content

`collect_event_content(event_id, dry_run=True)` MUST default to a read-only
preview. The exact event ID MUST be bounded. An apply request MUST acquire the
SQLite write reservation before checking any owner, grant, record or copy
blocker. If safe, it MAY delete **only** separate canonical text and same-event
relation excerpt rows, in the same transaction that writes its immutable,
non-content collection receipt. Inconsistent admission facts MUST fail closed;
repeated collection MUST be idempotent, including across concurrent writers
and restarts. It MUST NOT delete immutable delivery, native reference, radio,
attachment, or provider state. A successful logical delete is not physical
erasure; WAL, backup, logs and remote copies are outside this API.

Pre-release initialize MAY automatically apply only exactly recognized additive
catalog states: the nullable `expired_at` grant field and the independent
collection-receipt table. All other schema changes MUST fail before DDL.
