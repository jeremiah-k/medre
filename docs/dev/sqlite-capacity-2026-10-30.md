# Mixed SQLite capacity baseline (not a production qualification)

Run in an empty disposable directory:

```bash
python scripts/measure_mixed_sqlite.py /tmp/medre-sqlite-mixed \
  --events 500 --fanout 4 --producers 4 --workers 3
```

The harness asserts exactly one claim per target and persistent event count,
reports p50/p95/p99 for admission and p95/p99 for successful claim batches,
separate empty-query counts and latencies, claimant distribution,
restart cost, storage-close duration, elapsed duration and SQLite+WAL/SHM bytes
while storage is open. Admission uses the durable ingress transaction; route
workers are outside this synthetic workload. Task failures cancel and settle
all producers and claimers before storage closes. Empty claim queries wait for
a staging-generation change or producer completion before querying again;
completion is observed before each query so a late target cannot be missed.
It exercises overlapping ingestion, fanout staging and claims, not native
transport, E2EE callbacks, receipt finalization, retry, or link outages.

**Next qualification:** add deterministic delayed native senders and recovery
of expired claims, run the workload under fixed disk quotas and WAL readers,
and capture queue-depth/lease fairness, latency and disk growth across repeated
restarts. Only then choose operational limits or readiness thresholds.
