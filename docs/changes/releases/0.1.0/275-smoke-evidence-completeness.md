# Require complete, durable fake smoke evidence

Require a persisted `sent` receipt and sufficient delivery accounting for every
configured route/target pair exercised by a smoke event. Treat failed shutdown,
missing final snapshots, and non-stopped runtime states as failed smoke runs.
Add multi-target regression tests for partial receipts, accounting discrepancies,
shutdown failure, and snapshot failure. Cover installation-preflight error
branches without leaking configuration or filesystem exception details.
