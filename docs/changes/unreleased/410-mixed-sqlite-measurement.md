# Reproducible mixed SQLite workload measurement

`scripts/measure_mixed_sqlite.py` exercises overlapping synthetic admission,
outbox target staging, concurrent claims, durable ingress accounting and restart.
All tasks settle before storage closes, including on producer or claim failures.
Successful claim samples exclude empty queries; empty-query counts and latency
are reported separately. Idle workers wait for committed staging or producer
completion instead of immediately polling SQLite again.
Reports p50/p95/p99 latencies, claimant distribution, elapsed time, and SQLite
footprint without invented targets or a performance pass/fail verdict. Explicit
limitations exclude transport, crypto, delivery receipts and realistic native
links; these still require separate workload qualification.
