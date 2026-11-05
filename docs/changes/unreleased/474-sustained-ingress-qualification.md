# Sustained multi-source contention qualification

The bounded admission gate, the durable pre-admission pressure evidence,
and the producer pressure sink each had isolated coverage; their behavior
under simultaneous multi-source load, a blocked SQLite writer, and a
sustained storage failure was not qualified, and the retention bound for
gate-refusal evidence during an extended database outage was undecided.

A deterministic contention suite (`tests/test_sustained_ingress_contention.py`)
qualifies those seams together in the default test suite, with real on-disk
SQLite state and bounded grant waves instead of fixed sleeps.
Simultaneous sources — one per ingress seam — drive overlapping bursts
against a periodically held downstream consumer: both sources make
admission progress in every grant wave, the wait queue stays within the
global bound and each source's equal fair share, and admitted plus
rejected plus timed_out plus deferred reconcile exactly with submissions
per source across the durable rows and the capacity counters. A second
SQLite connection holding `BEGIN IMMEDIATE` blocks the pressure flush
while the gate keeps issuing typed outcomes: the unwritten batch stays
within the bounded-retention contract, producer-half keys are the only
evictable ones, and no exception reaches the admission path. Storage
failure across a sustained reporting window loses only bounded, logged
producer telemetry while every gate-refusal key survives, and recovery
persists retained keys plus fresh windows exactly. Owner-loop sentinels
run within a generous bound under full gate saturation, proving
starvation freedom rather than a latency SLA.

Every persisted row in the scenario is asserted content-free — adapter
identity, closed-vocabulary outcome, counters only — and when
`MEDRE_SUSTAINED_INGRESS_REPORT_DIR` is set the suite emits one
content-free JSON measurement per scenario; nothing is written by
default.

`docs/spec/durable-ingress.md` records the qualification boundaries
(fake/core tier; no native, RF, hardware, or latency claims) and an open
decision: how to bound admission-gate refusal evidence retained during an
extended database outage without silently discarding authoritative
refusal accounting — stated with its constraints, not solved.
