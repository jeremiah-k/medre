# Preserve retry ownership during durable fanout recovery

Recovering partially admitted ingress work now preserves existing delivery
generations and their retry schedules. A target that already reached attempt 2
or succeeded through the retry worker is no longer sent again as attempt 1.
Missing fanout targets can still be admitted and delivered after restart.

Outbox creation returns existing equal or newer generations without claiming
pending work or clearing backoff. Recovery claims and attempt reservation remain
with the retry worker; explicit replay still allocates fresh generations.
Unrecoverable reads of existing outbox rows fail closed.
