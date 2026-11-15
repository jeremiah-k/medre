# Producer pressure joins the durable pre-admission evidence

Radio SDKs stage inbound callbacks outside MEDRE's bounded admission gate —
one coroutine-scheduling submission or one asyncio task per packet from a
reader thread — and a producer-side refusal there previously left no trace
in the pressure vocabulary and had no content-free channel to report
through.

The adapter contract gains `ProducerPressureReport`, a frozen content-free
fact carrying the closed producer outcome vocabulary (`scheduled`,
`overflow`, `cancelled_before_admission`), a coalesced burst count, and an
optional machine-token `reason` such as `callback_capacity`. The type enforces token shape only; it cannot
recognize sensitive values that happen to fit the pattern. Adapter authors
must not pass message content, sender identity, or key material as `reason`. `AdapterContext` gains the optional
`report_producer_pressure` sink, callable from any thread including
non-asyncio SDK reader threads.

The runtime binds one sink per adapter where it constructs the context.
The sink coalesces reports into a bounded in-memory buffer — oldest keys
drop on overflow of the accounting itself — and folds them into the same
pending store and single-flight flush the admission gate refusal path
uses, so reporting never creates a task per report. The sink never raises
into the adapter and never claims admission; staged telemetry that never
reaches storage is lost by design and never becomes a receipt, retry, or
resend authority. A durable spill journal for producer reports would need
its own WAL, idempotency, and disk-failure proof and is deliberately not
taken.

The durable pressure table's outcome CHECK widens from the three
admission-gate outcomes to the six-outcome closed vocabulary through the
allowlisted additive prerelease migration (guarded table rebuild, rows
preserved, idempotent), keeping one table with adapter/source attribution
and minute windows intact. `medre inspect pressure` reports a derived
`outcome_totals` rollup, the evidence bundle documents the six-outcome row
contract, and `docs/schemas/inbound-pressure.schema.json` publishes the
inspect payload. Producer outcomes are pre-admission facts; core outcomes
are admission-gate facts; `admitted` remains the only durable ingress
fact.
