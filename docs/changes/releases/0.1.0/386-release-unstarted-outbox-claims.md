# Release unused outbox claims when shutdown interrupts dispatch

If shutdown begins after the dispatcher claims a batch but before an item is
scheduled, return only unreserved items to their durable pending or retry-wait
state. This avoids a needless lease-expiry delay after restart. Claims carrying
an existing attempt reservation remain fenced for reconciliation; the change
does not treat uncertain native submissions as unsent.
