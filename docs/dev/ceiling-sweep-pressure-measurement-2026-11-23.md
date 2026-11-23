# Content-ceiling sweep pressure measurement — November 23 adversarial profile

`python scripts/measure_ceiling_sweep.py DEST --loads ...` runs the production
SQLite content-ceiling enforcement surface (the opt-in `retry.ceiling_sweep`
worker policy driving `enforce_content_ceilings`) under adversarial controlled
loads, each load twice — sweep disabled (baseline) and sweep enabled — on fresh
disposable SQLite state, and repeats each condition to report spread. This
record fixes the loads and the acceptance budgets before any full-scale number
was read: this commit was created before the measurement runs it describes, and
the results section is filled by a later commit on the same branch against
those frozen budgets. A budget miss is a result, not something to tune away;
no load is adjusted until it passes and no budget is relaxed after the fact.

This slice is measurement only. The sweep remains default-off
(`retry.ceiling_sweep.enabled` defaults to false in the loader) and enabling it
by default is a separate owner decision informed by, not made by, this record.
Logical purge is not physical erasure: a receipt in
`event_content_ceiling_log` is not a WAL-erasure certificate, freed pages and
WAL frames survive logical enforcement, and byte-level erasure still requires
the quiesced `VACUUM` plus `wal_checkpoint(TRUNCATE)` procedure in the
[hard content privacy ceiling](../spec/content-privacy-ceiling.md).

## Method

The measurement drives the real assembly: `SQLiteStorage` in WAL mode, the
production `RetryWorker` poll cycle with the ceiling sweep policy enabled or
disabled, one fast fake claim lane so native dispatch work continues through
every window, and ceiling-bearing events created through the real
`admit_ingress(content_deadline=...)` producer boundary wherever admission
latency is itself measured. Bulk history rows (canon identity, separated body,
policy row) are direct writes shaped exactly like the producer boundary
writes; they exist to scale the cursors, not to measure admission. The sweep
surface is wrapped with a timing probe before the worker is constructed, claim
calls are wrapped the same way, and read-only operator-view queries
(`query_work_status_summary`) are sampled from an independent readonly
connection mid-window and at rest. The disabled twin runs the identical
workload with the sweep policy off, so like-for-like deltas isolate the sweep.

Captured per run: sweep tick wall times and per-tick enforcement counts,
dispatcher claim durations and the largest stall between consecutive claims,
trickle admission latencies and queue-to-sent latency at the trickle cadence,
stream admission latencies split into during-tick and off-tick samples, reader
latencies mid-window and at rest, elapsed-but-unpurged row age at close,
read-only audit counts (elapsed pending, residual enforced, receipts), worker
sweep counters, and SQLite footprints (DB and WAL bytes) at seed, before close
and after close. Where a load has a crash or cancellation phase the reopen
integrity check, the partial-purge scan, the resume time and the read-only
receipt verification battery over every receipt are recorded as well.

Repeats per load on one development machine (15 GB RAM, measurements run one
at a time): history-heavy 3, continuous-stream 3, fanout-attachments 3,
blocked-writer 2, interrupt-restart 1. Spread is reported as the minimum and
maximum of the repeated runs' headline values; a single sample is never
presented as a spread.

## Loads

| Load               | Fixed scale                                                                                                                                                                                                                                                                                  |
| ------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| history-heavy      | 3,000 future policies (ineligible), 2,000 elapsed policies receipted offline before the window, 240 elapsed unpurged targets, 30 s claim trickle, one late residual envelope write on a receipted event behind the residual cursor                                                           |
| continuous-stream  | 800 receipted history policies, 200 future policies, ~3.5 min ceiling-bearing admissions through the real boundary at ~20/s for 12 s (ceiling elapsing 2 s after each admission), claim trickle throughout                                                                                   |
| fanout-attachments | 300 elapsed targets each with 6 parked outbox generations carrying dispatch snapshots, inline metadata, radio progress, an observation with diagnostics, envelope values, an own excerpt and a shared blob reference (150 blobs, 2 events each), plus 2 quoting neighbors per target         |
| blocked-writer     | 300 elapsed targets with an external raw connection holding `BEGIN IMMEDIATE` for repeating windows (1.5 s, 6.0 s, 1.5 s, 1.5 s holds with 3.5 s gaps; the 6 s hold exceeds the storage connection's 5 s busy timeout)                                                                       |
| interrupt-restart  | 1,200 elapsed targets in a child process sweeping at a 0.2 s interval, SIGKILLed once the first tick commits, parent reopens, asserts consistency, resumes to completion and verifies every receipt; a second phase cancels the worker mid-tick on 800 elapsed targets and measures teardown |

## Acceptance budgets (frozen before measurement)

Budgets derive from the spec's structural obligations (bounded invocation,
fair progress under sustained streams, atomicity of the enforcement
transaction, wrap-based revisit) and from generous absolute caps; they are
machine-relative and the baseline twin of each load is the control. B2's
cycle bound is evaluated per run from the recorded counts.

| #   | Budget                                                                                                                                                                                                                                                                                                         |
| --- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| B1  | Every enabled sweep tick completes within 5,000 ms at limit 50 on every load (the poll cycle must stay short; tick wall time is also the direct bound on how long the inline maintenance step can delay claims)                                                                                                |
| B2  | In continuous-stream, every streamed target holds its enforcement receipt within one full policy-cursor cycle plus 10 sweep intervals after its ceiling elapsed, with zero sweep errors; one cycle = ceil(policies visible to the cycle / 50) ticks                                                            |
| B3  | In history-heavy, the late residual write is repaired within one full residual-cursor cycle: at most ceil(receipts at seeding / 50) + 2 ticks; the wall time is recorded without a wall budget                                                                                                                 |
| B4  | On loads without an external blocker, enabled admission p95 stays within 3x the disabled twin's admission p95, and enabled admission p99 stays under 1,000 ms absolute                                                                                                                                         |
| B5  | On loads without an external blocker, the largest stall between consecutive dispatcher claim calls stays under 5,000 ms and claim duration p99 stays under 5,000 ms                                                                                                                                            |
| B6  | On loads without an external blocker there are zero admission failures and zero sweep errors; on blocked-writer, sweep errors are confined to the held windows (bounded `SQLITE_BUSY` absorbed by the worker's poll-down) and every target is still receipted afterwards                                       |
| B7  | On loads without an external blocker, the independent reader's work-status query p95 stays under 200 ms                                                                                                                                                                                                        |
| B8  | Peak WAL bytes stay under 256 MB on every load; a clean close leaves no `-wal`/`-shm` sidecars; the DB file never shrinks (logical purge is expected to leave freelist pages behind)                                                                                                                           |
| B9  | After SIGKILL mid-sweep and after mid-tick cancellation, reopen passes `integrity_check`, no receipt coexists with its event's body rows, no event carries a cleared envelope without its receipt, the resumed sweep receipts every seeded target, and every receipt passes the read-only verification battery |
| B10 | On every enabled run, the worker's enforced count and the storage receipts agree, and the final read-only audit reports no elapsed-pending and no residual-enforced findings after the completion wait                                                                                                         |

## Results

Pending: this section is filled by the follow-up commit on the same branch
after the frozen budgets above are committed, and reports measured versus
budget per load with spread, listing every miss explicitly.

## Limitations

- Fake handoffs prove local acceptance only; no transport SDK, native send or
  recipient delivery participates in any load.
- Blocked-writer models a competing process holding the SQLite write
  reservation, not a slow disk or a network filesystem; SIGKILL models process
  death, not power loss or disk corruption.
- All timings are measurement data on one development machine with neighbors;
  they carry no machine-speed gate and no capacity promise, and the disabled
  twin of each load is the only control.
- Logical purge is not physical erasure; the WAL and DB byte footprints in
  these runs only grow, and nothing here measures the operator erasure
  procedure.

## Reproduction

```sh
mkdir -p /tmp/ceiling-sweep-measure
PYTHONPATH=src python scripts/measure_ceiling_sweep.py /tmp/ceiling-sweep-measure --loads history-heavy
PYTHONPATH=src python scripts/measure_ceiling_sweep.py /tmp/ceiling-sweep-measure --loads continuous-stream
PYTHONPATH=src python scripts/measure_ceiling_sweep.py /tmp/ceiling-sweep-measure --loads fanout-attachments
PYTHONPATH=src python scripts/measure_ceiling_sweep.py /tmp/ceiling-sweep-measure --loads blocked-writer
PYTHONPATH=src python scripts/measure_ceiling_sweep.py /tmp/ceiling-sweep-measure --loads interrupt-restart
```

Each invocation prints one JSON document with per-run results for both
conditions of its load; destinations must be fresh or empty of the run's
`*.sqlite*` files. The history-heavy, continuous-stream and fanout-attachments
invocations are the long ones (tens of minutes of sweep-interval pacing); the
others finish in minutes.
