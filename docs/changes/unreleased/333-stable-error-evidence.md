# Stable Sanitized Error Evidence

Keep truncation markers inside the shared 512-character error-detail limit.
Sanitized failures remain unchanged when validated again, so long redacted plugin
failure records round-trip through durable storage and do not block recovery.

Reapply redaction after truncation, including when the truncation marker completes
an empty secret assignment or redaction expands a short match. Retain a bounded
fixed point through repeated decoding and SQLite recovery.
