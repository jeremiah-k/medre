# Prepare initial delivery before reservation and bound idle dispatch scans

Initial relation and conversation projection now runs under a renewed but
unreserved outbox claim. Preparation failure does not consume a native attempt;
a still-owned claim returns to pending and a short adapter-local cooldown avoids
hot looping on persistent storage faults. Reserved native handoffs retain their
existing durable evidence and retry fences.

Outbox dispatch continues polling promptly for initial work, but full status
counts are refreshed only after a claim, completed dispatch, or configured retry
interval rather than on every empty half-second scan. An additional SQLite
index supports the one-per-adapter active-owner guard; the due-work index is
already present. These are scheduling/diagnostic changes, not new delivery
authority or a database migration.
