# Bounded outage retention for gate-refusal evidence

Admission-gate refusals are authoritative loss accounting, and during an
extended storage outage the retained refusal evidence was the one pressure
half with no bound: producer staging keys were bounded and dropped with an
explicit log marker, while gate-refusal keys grew one per source, outcome,
and 60-second window for as long as the outage lasted.

The retained refusal half is now bounded by the documented
`INBOUND_PRESSURE_MAX_PENDING_KEYS` constant (1024). Overflow discards the
oldest refusal keys with exact saturating discard accounting and a
coalesced warning — the first discard immediately, then at most one line
per 5-second window, each carrying cumulative counts — never per-row
flooding, never silent. The first fully successful pressure flush after
the distress persists one durable accounting-gap row in the new
`inbound_pressure_outage_gaps` table: the detected outage interval plus
content-free counters for refusals observed while the gap was open, facts
retained by the recovery batch, and facts discarded. Post-recovery state
therefore lets a reader distinguish zero refusals during the outage from
refusals with discarded retained facts; a zero-counter row is the durable
proof an outage happened without refusal loss. The table installs through
the allowlisted additive prerelease path, and every counter saturates at
the SQLite signed-64-bit ceiling.

The frozen producer-pressure seam is untouched: `ProducerPressureReport`,
the closed outcome vocabulary, and the `report_producer_pressure` context
sink are unchanged. Producer telemetry remains best-effort and is never a
substitute for authoritative gate evidence, and the gap record is
core-internal accounting that never claims admission, grants a receipt, or
authorizes retry or resend. `docs/spec/durable-ingress.md` records the
decision with its rejected alternatives (unbounded retention, a durable
spill journal beside SQLite, head-of-line blocking at the gate).
