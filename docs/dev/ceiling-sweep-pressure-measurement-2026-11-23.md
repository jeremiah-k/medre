# Content-ceiling sweep pressure measurement — November 23 adversarial profile

`python scripts/measure_ceiling_sweep.py DEST --loads ...` runs the production
SQLite content-ceiling enforcement surface (the opt-in `retry.ceiling_sweep`
worker policy driving `enforce_content_ceilings`) under adversarial controlled
loads, each load twice — sweep disabled (baseline) and sweep enabled — on fresh
disposable SQLite state, and repeats each condition to report spread. This
record fixes the loads and the acceptance budgets before any full-scale number
was read. A budget miss is a result, not something to tune away; no load
is adjusted until it passes and no budget is relaxed after the fact.

The record is measurement only. The sweep remains default-off
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
connection mid-window and at rest.

The original disabled twin retained unreceipted offline history while the
enabled twin purged it before the probes started. Those historical comparisons
are confounded. The corrected harness receipts the same history in both
conditions before timing; the changed procedure requires a fresh run.

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
| continuous-stream  | 800 receipted history policies, 200 future policies, 12 s of ceiling-bearing admissions through the real boundary at up to 20/s (ceiling elapsing 2 s after each admission), claim trickle throughout                                                                                        |
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

The following numbers are the **original historical run**, before the
measurement-harness corrections in this PR. B1 and B2 are confirmed misses;
the other entries are not complete proof of their frozen budgets where the
method or evidence was insufficient. The corrected harness has **not** been
rerun at full scale, so do not present its changed results as if measured.
The frozen B1–B10 budgets above have not been relaxed or retuned.

## Results — budget outcomes

| Budget                                         | history-heavy (3+3 runs)                                              | continuous-stream (3+3 runs)                                     | fanout-attachments (3+3 runs)                                          | blocked-writer (2+2 runs)                                                                                                      | interrupt-restart (1 run)                       |
| ---------------------------------------------- | --------------------------------------------------------------------- | ---------------------------------------------------------------- | ---------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------- |
| B1 tick ≤ 5,000 ms                             | PASS (max 61.1 ms)                                                    | PASS (max 44.1 ms)                                               | PASS (max 68.5 ms)                                                     | **MISS** (max 5,006.7 and 5,009.0 ms)                                                                                          | n/a (no worker sweep; resume used direct calls) |
| B2 stream receipt tail                         | n/a                                                                   | **MISS** (max tail 18.483–18.522 s vs a 13.5–15.5 s bound)       | n/a                                                                    | n/a                                                                                                                            | n/a                                             |
| B3 wrap repair ≤ 42 ticks                      | UNVERIFIED CURSOR POSITION (18–19 ticks observed)                     | n/a                                                              | n/a                                                                    | n/a                                                                                                                            | n/a                                             |
| B4 writer impact ≤ 3x / p99 ≤ 1,000 ms         | UNVERIFIED TWIN COMPARISON (ratios 0.91–1.17, enabled p99 103–133 ms) | UNVERIFIED TWIN COMPARISON (ratios ≤ 1.11, enabled p99 16–25 ms) | UNVERIFIED TWIN COMPARISON (ratios 0.96–1.16, enabled p99 81–92 ms)    | exempt (external blocker owns the inflation; both twins inflate alike)                                                         | n/a                                             |
| B5 claim liveness ≤ 5,000 ms                   | UNVERIFIED ORIGINAL METRIC (gap ≤ 0.52 s)                             | UNVERIFIED ORIGINAL METRIC (gap ≤ 0.52 s)                        | UNVERIFIED ORIGINAL METRIC (gap ≤ 0.51 s)                              | exempt (gap 5.44 s enabled vs 1.05 s disabled worst, blocker-driven)                                                           | n/a                                             |
| B6 zero missed admissions                      | UNVERIFIED TIMING/CAUSE (0 errors)                                    | UNVERIFIED TIMING/CAUSE (0 errors)                               | UNVERIFIED TIMING/CAUSE (0 errors)                                     | UNVERIFIED TIMING/CAUSE (1 bounded busy error per enabled run, confined to the 6 s hold, all 300 targets receipted afterwards) | UNVERIFIED TIMING/CAUSE (no partial state)      |
| B7 reader p95 ≤ 200 ms                         | UNVERIFIED UNDER LOAD (2.1–12.2 ms)                                   | UNVERIFIED UNDER LOAD (1.8–5.6 ms)                               | UNVERIFIED UNDER LOAD (58.8–68.5 ms, data-volume-driven in both twins) | UNVERIFIED UNDER LOAD (1.8–4.1 ms)                                                                                             | n/a                                             |
| B8 WAL ≤ 256 MB, clean close, DB never shrinks | UNMEASURED PEAK (peak WAL 4.60 MB, no sidecars after close)           | UNMEASURED PEAK (peak WAL 4.21 MB)                               | UNMEASURED PEAK (peak WAL 4.22 MB)                                     | UNMEASURED PEAK (peak WAL 4.13 MB)                                                                                             | PASS                                            |
| B9 restart/cancel consistency                  | n/a                                                                   | n/a                                                              | n/a                                                                    | n/a                                                                                                                            | BOUNDARY RESTART ONLY (interior not exercised)  |
| B10 audit clean, counters agree                | PARTIAL (2,240 receipts, 0 findings)                                  | PARTIAL (1,002–1,006 receipts, 0 findings)                       | PARTIAL (300 receipts, 0 findings)                                     | PARTIAL (300 receipts, 0 findings)                                                                                             | PARTIAL (all 1,200 receipts verified, 0 failed) |

## Results — per-load detail

history-heavy (enabled runs; disabled twins had no sweep ticks and left all
2,240 elapsed rows unpurged with a maximum age of 35.7–35.9 s at close):

| Field                                | r0                    | r1                    | r2                    |
| ------------------------------------ | --------------------- | --------------------- | --------------------- |
| Sweep ticks / enforced               | 81 / 241              | 81 / 241              | 81 / 241              |
| Tick ms p50 / p95 / max              | 15.3 / 24.7 / 44.0    | 18.6 / 29.4 / 54.8    | 18.7 / 33.5 / 61.1    |
| First target receipt after start (s) | 20.068                | 20.073                | 20.078                |
| Residual wrap repair (s / ticks)     | 9.707 / 19            | 9.518 / 18            | 9.648 / 19            |
| Admission ms p50 / p95 / p99         | 53.2 / 96.6 / 105.1   | 57.1 / 107.4 / 133.4  | 57.9 / 84.2 / 103.0   |
| Claim non-empty ms p99 / max gap (s) | 17.5 / 0.515          | 25.1 / 0.511          | 22.3 / 0.524          |
| Reader mid-run ms p95                | 2.4                   | 6.7                   | 2.6                   |
| Sweep errors                         | 0                     | 0                     | 0                     |
| DB bytes before close (sampled WAL)  | 5,185,536 (4,597,952) | 5,185,536 (4,597,952) | 5,185,536 (4,597,952) |

The 20.07 s first-target receipt is exactly the arithmetic prediction for
walking 2,000 already-receipted elapsed policies at 50 per 0.5 s tick
(40 ticks); the sweep is bounded and wrap-guaranteed, but a new arrival
waiting behind an elapsed history pays ceil(history / limit) ticks first.

continuous-stream (enabled runs; disabled twins left all 203–206 streamed
targets unpurged with identical admission latencies):

| Field                                         | r0                 | r1                | r2                 |
| --------------------------------------------- | ------------------ | ----------------- | ------------------ |
| Streamed targets / receipted                  | 202 / 202          | 204 / 204         | 206 / 206          |
| Tick ms p50 / p95 / max                       | 10.9 / 25.1 / 44.1 | 9.2 / 25.6 / 29.9 | 13.2 / 26.5 / 27.4 |
| Stream admission ms p95 (enabled vs disabled) | 14.2 vs 15.6       | 13.9 vs 14.6      | 13.3 vs 12.8       |
| Admission during tick ms max (samples)        | 19.6 (11)          | 14.5 (9)          | 14.1 (5)           |
| Receipt tail after elapse s p50 / max         | 13.6 / 18.483      | 13.6 / 18.490     | 13.7 / 18.522      |
| Claim non-empty ms p99 / max gap (s)          | 32.3 / 0.518       | 26.5 / 0.514      | 17.5 / 0.517       |
| Sweep errors                                  | 0                  | 0                 | 0                  |

The B2 miss: the frozen bound was one full cursor cycle plus 10 intervals
after elapse (ceil(~1,006 elapsed policies / 50) = 21 ticks + 10 = 31 ticks
≈ 15.5 s); the measured maximum tail was 18.48–18.52 s in every repeat.
Targets admitted while their ceiling sorts beyond the cycle's saved upper
bound cannot be visited until the cycle wraps, so they pay the remaining
fraction of the current cycle plus their walk position in the next one; the
frozen formula under-modeled that second segment. All targets are receipted
with zero errors, so the sweep is fair in the spec's sense (bounded per tick,
wraps under sustained newer arrivals) while the tail bound as frozen is
missed by roughly one visit-page of ticks.

fanout-attachments (enabled runs; disabled twins kept all 150 blobs and
elapsed rows):

| Field                                  | r0                 | r1                 | r2                 |
| -------------------------------------- | ------------------ | ------------------ | ------------------ |
| Sweep ticks / enforced                 | 42 / 300           | 42 / 300           | 42 / 300           |
| Tick ms p50 / p95 / max                | 18.0 / 52.5 / 56.2 | 17.7 / 56.6 / 66.3 | 18.1 / 58.9 / 68.5 |
| Shared blobs surviving                 | 0 of 150           | 0 of 150           | 0 of 150           |
| Admission ms p95 (enabled vs disabled) | 74.1 vs 63.9       | 79.4 vs 82.9       | 69.2 vs 65.9       |
| Claim non-empty ms p99 / max gap (s)   | 18.2 / 0.508       | 18.6 / 0.509       | 16.9 / 0.513       |
| Reader mid-run ms p95                  | 59.1               | 60.2               | 66.8               |
| Sweep errors                           | 0                  | 0                  | 0                  |

The final check observed zero blobs after all target purges. It does **not**
prove each shared blob survived the first owner purge while another owner
retained a reference, nor prove exactly-once physical deletion. Those remain
separate, unmeasured assertions even when owners were purged in different ticks.

blocked-writer (2 repeats per condition; the 6.0 s hold exceeds the storage
connection's 5,000 ms busy timeout):

| Field                            | enabled r0               | enabled r1               | disabled r0/r1    |
| -------------------------------- | ------------------------ | ------------------------ | ----------------- |
| Ticks / enforced                 | 32 / 300                 | 32 / 300                 | 0 / 0             |
| Tick ms p50 / p95 / max          | 16.9 / 1,549.7 / 5,006.7 | 17.3 / 1,547.5 / 5,009.0 | n/a               |
| Sweep errors                     | 1                        | 1                        | 0                 |
| Admission ms p99/max (enabled)   | 1,012.6 / 1,012.6        | 1,020.3 / 1,020.3        | 1,084.8 / 1,275.1 |
| Claim non-empty ms max / gap (s) | 946.9 / 5.445            | 954.0 / 5.443            | 1,555.0 / 1.053   |
| Reader mid-run ms p95            | 1.9                      | 2.5                      | 4.1               |

The B1 miss: the measured longest tick was 5,006.7/5,009.0 ms.
The 5,000 ms SQLite busy timeout limits the lock wait, **not total tick
wall time**. The old harness did not record timestamped error types and
acquired hold windows. Attributing each error to `SQLITE_BUSY` during the
6-second hold remains unverified until rerun. The observed 7–9 ms excess over
the frozen 5,000 ms budget is recorded as a miss; SQLite's lock-wait timeout
is not a hard limit on the wall time of a complete sweep tick. Inflation on this load is blocker-driven in both twins:
admission maxima of ~1.0–1.3 s and claim maxima of ~0.6–1.6 s appear with the
sweep off as well, and the enabled-only 5.44 s claim gap is the claim call
that started just before the failing busy tick.

interrupt-restart (1 run): the parent killed the sweeping child right after
its first committed tick (50 receipts, kill phase between ticks). Reopening
after SIGKILL passed `integrity_check`, held exactly the committed state —
50 receipts with zero receipts coexisting with body rows, zero cleared
envelopes without receipts, and the durable enforcement cursor preserved
verbatim mid-backlog (`["…16:07:14.019274+00:00", "ir-target-000049",
…upper…]`) — and the resumed sweep receipted all remaining targets in 0.237 s
with the read-only verification battery passing all 1,200 receipts. The
separate mid-tick cancellation phase stopped the worker in 11 ms with the
same consistency guarantees and all 800 receipts verified; the sample caught
a tick boundary rather than a tick interior (ticks on this backlog take tens
of milliseconds). The teardown figure therefore describes stop-at-boundary.
Neither interior SIGKILL nor interior cancellation was measured.

## Findings for the enablement decision

These are findings for the owner of the default-enable decision, not defects
fixed by this record.

- Receipt tail under sustained newer arrivals grows to roughly one full cursor
  cycle plus the arrival's walk position, not one cycle plus a small constant
  (the B2 miss above). The tail scales linearly with the elapsed-policy volume
  divided by the sweep limit times the sweep interval; at the default 60 s
  interval the observed stream shape would put the worst tail at tens of
  minutes once the elapsed backlog reaches a few thousand policies.
- A new arrival waiting behind an already-elapsed receipted history pays
  ceil(history / limit) ticks before its first visit (20.07 s at 2,000 history,
  limit 50, 0.5 s interval); bounded and wrap-guaranteed, but proportional to
  backlog depth rather than constant.
- Under external write pressure exceeding the busy timeout, the colliding tick
  fails with one bounded `SQLITE_BUSY` error that the worker's poll-down
  absorbs while every other tick proceeds; nothing in the measured runs
  blocked native retry processing unboundedly — the worst enabled claim gap on
  loads without an external blocker was 0.52 s against a 0.5 s poll cadence,
  and reader queries never contended with the writer lock.
- With the sweep off during these bounded runs, expired target content remained while reads stay
  denied: every disabled twin closed with its full elapsed backlog unpurged
  (age growing for as long as the window ran), confirming the audit's
  default-off exposure statement with measured ages rather than a single
  synthetic sample.
- Logical purge does not reclaim space in these runs: the DB file never shrank
  and the WAL grew at sampled points (largest observed sample 4.60 MB) and being
  removed only by clean close. Nothing in this record measures page-level
  erasure; a receipt remains a logical, content-free fact, not a WAL-erasure
  certificate.

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

Each invocation prints JSON with per-run results for both conditions, except
`interrupt-restart`, which emits one `enabled_only` result and a cancellation
phase. The historical figures above predate the fixes. Do not combine them
with a later corrected run as one baseline. Destinations must be fresh or empty
of the run's `*.sqlite*` files; The history-heavy, continuous-stream and fanout-attachments
invocations are the long ones (tens of minutes of sweep-interval pacing); the
others finish in minutes.

## Review corrections and outstanding measurements

The script now records paired claim completions (including failures), uses
matching receipted-history starting states, starts readers while foreground
traffic is active, and runs the competing SQLite writer in a separate thread
with actual hold windows and sweep-error timestamps. It also joins spawned
load tasks before closing storage, checks for cleared content without receipts,
requires the full successful receipt-verification count, and reopens a fresh
SQLiteStorage after cancellation. These are **method corrections**, not new
performance results. Collect another full-scale run to qualify B2/B4–B8/B10.

B9's observed SIGKILL and cancellation were at tick boundaries; neither
established an interrupted open purge transaction. That interior case remains
an explicit unmeasured gate. Shared-blob last-reference retention, cursor
position behind the late residual write, and continuous WAL high-water marks
also remain unproved by the original measurements. The disabled-control
retention observation should be reported separately from like-for-like latency.

The history-heavy record has 2,000 receipts created during offline seeding,
240 newly receipted targets and one residual repair (241 enforcement actions).
The repair does not create a new receipt. B10 needs those counters separated;
2,240 total receipts must never be compared directly to 241 worker actions.
The stream admission window was 12 seconds; the history and fanout trickles
were 30 and 20 seconds, respectively. Longer wall time includes seeding,
completion waits and repeats, not continuous admission.
