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

The numbers below are the **corrected full-scale run** of 2026-11-23 on core
commit `97e485419e03c1f1b70ef606d5abdeae3a4606c6` (clean tree), produced by
the corrected harness of this record on one development machine (15 GB RAM,
4 cores). Timings are machine-relative and the disabled twin of each load is
the control. Repeats per condition: history-heavy 3, continuous-stream 3,
fanout-attachments 3, blocked-writer 2, interrupt-restart 1. The frozen
B1–B10 budgets above have not been relaxed or retuned; a miss is a result.

Machine context, recorded because timings are machine-relative: the 1-minute
load average sat between 3.7 and 5.8 across the campaign (a neighbor process
shared the four cores; swap stayed at its 1 GB baseline and 2–4 GB of memory
stayed available). Per load, 1-minute load average before → after:
blocked-writer 3.8 → 2.2, interrupt-restart 3.7 → 4.4, history-heavy
4.4 → 5.7, continuous-stream 5.7 → 5.8, fanout-attachments 5.8 → 4.6. The
interval-paced results that matter most (B2 tails, tick maxima) matched the
superseded run to within tens of milliseconds, so the shared machine did not
move them. Invocation wall times on this machine: blocked-writer ~2 minutes,
interrupt-restart seconds, history-heavy ~4 minutes, continuous-stream
~2 minutes, fanout-attachments ~10 minutes.

## Results — budget outcomes

| Budget                                         | history-heavy (3+3 runs)                                         | continuous-stream (3+3 runs)                                  | fanout-attachments (3+3 runs)                         | blocked-writer (2+2 runs)                                                                                  | interrupt-restart (1 run)                                        |
| ---------------------------------------------- | ---------------------------------------------------------------- | ------------------------------------------------------------- | ----------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------- |
| B1 tick ≤ 5,000 ms                             | PASS (max 28.8–43.6 ms)                                          | PASS (max 25.0–29.6 ms)                                       | PASS (max 79.9–151.4 ms)                              | **MISS** (max 5,005.1–5,006.3 ms)                                                                          | n/a (no worker sweep; resume used direct calls)                  |
| B2 stream receipt tail                         | n/a                                                              | **MISS** (max tail 18.481–18.527 s vs a 15.5 s per-run bound) | n/a                                                   | n/a                                                                                                        | n/a                                                              |
| B3 wrap repair ≤ 42 ticks                      | PASS (20 ticks; 9.60–9.87 s)                                     | n/a                                                           | n/a                                                   | n/a                                                                                                        | n/a                                                              |
| B4 writer impact ≤ 3x / p99 ≤ 1,000 ms         | PASS (ratios 0.77–1.10, enabled p99 70.7–112.6 ms)               | PASS (ratios 0.87–1.14, enabled p99 15.1–21.3 ms)             | PASS (ratios 0.84–1.02, enabled p99 118.5–194.0 ms)   | exempt (external blocker owns the inflation; both twins inflate)                                           | n/a                                                              |
| B5 claim liveness ≤ 5,000 ms                   | PASS (gap ≤ 0.54 s)                                              | PASS (gap ≤ 0.52 s)                                           | PASS (gap ≤ 0.54 s)                                   | exempt (gap 6.40 s enabled vs 5.41–5.51 s disabled worst, blocker-driven)                                  | n/a                                                              |
| B6 zero missed admissions                      | PASS (0 errors)                                                  | PASS (0 errors)                                               | PASS (0 errors)                                       | PASS (1 bounded lock-busy error per enabled run inside the 6 s hold, all 300 targets receipted afterwards) | PASS (no partial state)                                          |
| B7 reader p95 ≤ 200 ms                         | PASS (enabled 1.9–5.2 ms)                                        | PASS (enabled 2.3–3.5 ms)                                     | PASS (enabled 85.7–126.6 ms vs disabled 57.6–72.3 ms) | exempt (1.5–2.0 ms)                                                                                        | n/a                                                              |
| B8 WAL ≤ 256 MB, clean close, DB never shrinks | **UNVERIFIED peak** (sampled WAL 4.60 MB; no sidecars after close) | **UNVERIFIED peak** (sampled WAL 4.21 MB) | **UNVERIFIED peak** (sampled WAL 4.22 MB) | **UNVERIFIED peak** (sampled WAL 4.17 MB) | **UNVERIFIED peak** (footprint only) |
| B9 restart/cancel consistency                  | n/a                                                              | n/a                                                           | n/a                                                   | n/a                                                                                                        | **UNVERIFIED interior** (boundary kill/cancel passed)             |
| B10 audit clean, counters agree                | PASS (2,240 receipts = 2,000 offline + 240 targets; 241 actions) | PASS (1,002–1,004 receipts = 800 offline + streamed)          | PASS (300 receipts = 300 enforced)                    | PASS (300 receipts = 300 enforced)                                                                         | PASS (1,200 + 800 receipts verified, 0 failed)                   |

## Results — per-load detail

history-heavy (corrected run, enabled rows; both twins start from the same
2,000-receipt elapsed history. Disabled twins had no sweep ticks and left all
240 elapsed targets unpurged with a maximum age of 35.35–35.61 s at close):

| Field                                | r0                    | r1                    | r2                    |
| ------------------------------------ | --------------------- | --------------------- | --------------------- |
| Sweep ticks / enforced               | 81 / 241              | 81 / 241              | 81 / 241              |
| Tick ms p50 / p95 / max              | 13.4 / 24.9 / 43.6    | 16.8 / 22.4 / 32.2    | 17.0 / 24.0 / 28.8    |
| First target receipt after start (s) | 20.079                | 20.066                | 20.077                |
| Residual wrap repair (s / ticks)     | 9.602 / 20            | 9.869 / 20            | 9.773 / 20            |
| Admission ms p50 / p95 / p99         | 48.2 / 79.0 / 112.6   | 44.6 / 64.9 / 70.7    | 47.8 / 73.1 / 86.7    |
| Claim non-empty ms p99 / max gap (s) | 22.0 / 0.538          | 17.5 / 0.516          | 19.9 / 0.514          |
| Reader mid-run ms p95                | 5.2                   | 1.9                   | 3.2                   |
| Sweep errors                         | 0                     | 0                     | 0                     |
| DB bytes before close (sampled WAL)  | 5,185,536 (4,597,952) | 5,185,536 (4,597,952) | 5,185,536 (4,597,952) |

The 20.07 s first-target receipt is exactly the arithmetic prediction for
walking 2,000 already-receipted elapsed policies at 50 per 0.5 s tick
(40 ticks); the sweep is bounded and wrap-guaranteed, but a new arrival
waiting behind an elapsed history pays ceil(history / limit) ticks first.
The late residual write landed on `hh-history-000000`, an already-receipted
event behind the residual cursor; the wrap repaired it in 20 ticks
(bound: ceil(2,000 / 50) + 2 = 42), verified by the read-only per-event
audit battery returning `enforced_clean` without rewriting the receipt.

continuous-stream (corrected run, enabled rows; both twins start from the
same 800-receipt elapsed history. Disabled twins left all streamed targets
unpurged):

| Field                                         | r0                 | r1                 | r2                 |
| --------------------------------------------- | ------------------ | ------------------ | ------------------ |
| Streamed targets / receipted (incl. history)  | 204 / 1,004        | 204 / 1,004        | 202 / 1,002        |
| Tick ms p50 / p95 / max                       | 15.7 / 24.4 / 26.3 | 13.4 / 24.4 / 25.0 | 14.0 / 25.0 / 29.6 |
| Stream admission ms p95 (enabled vs disabled) | 14.2 vs 16.3       | 13.5 vs 12.9       | 16.9 vs 14.8       |
| Admission during tick ms max (samples)        | 14.2 (7)           | 13.8 (8)           | 19.3 (8)           |
| Receipt tail after elapse s p50 / max         | 13.6 / 18.481      | 13.6 / 18.527      | 13.6 / 18.506      |
| Claim non-empty ms p99 / max gap (s)          | 24.8 / 0.511       | 17.0 / 0.514       | 25.4 / 0.524       |
| Sweep errors                                  | 0                  | 0                  | 0                  |

The B2 miss: the frozen bound is one full cursor cycle plus 10 intervals
after elapse. With 800 receipted history policies plus ~200 streamed elapsed
policies visible, one cycle is ceil(~1,004 / 50) = 21 ticks, so the bound is
31 ticks = 15.5 s; the measured maximum tail was 18.48–18.53 s in every
repeat. Targets admitted while their ceiling sorts beyond the cycle's saved
upper bound cannot be visited until the cycle wraps, so they pay the
remaining fraction of the current cycle plus their walk position in the next
one; the frozen formula under-modeled that second segment. The median target
is receipted within the bound (p50 13.6 s). All targets are receipted with
zero errors, so the sweep is fair in the spec's sense (bounded per tick,
wraps under sustained newer arrivals) while the tail bound as frozen is
missed by roughly one visit-page of ticks.

fanout-attachments (corrected run, enabled rows; disabled twins kept all
150 blobs and all 300 elapsed targets unreceipted):

| Field                                  | r0                  | r1                 | r2                 |
| -------------------------------------- | ------------------- | ------------------ | ------------------ |
| Sweep ticks / enforced                 | 41 / 300            | 41 / 300           | 41 / 300           |
| Tick ms p50 / p95 / max                | 17.6 / 54.2 / 151.4 | 17.9 / 70.6 / 81.6 | 17.6 / 51.9 / 79.9 |
| Shared blobs surviving                 | 0 of 150            | 0 of 150           | 0 of 150           |
| Admission ms p95 (enabled vs disabled) | 70.8 vs 84.5        | 116.6 vs 114.8     | 81.1 vs 93.9       |
| Claim non-empty ms p99 / max gap (s)   | 16.4 / 0.514        | 28.4 / 0.541       | 15.0 / 0.517       |
| Reader mid-run ms p95                  | 126.6               | 88.3               | 85.7               |
| Sweep errors                           | 0                   | 0                  | 0                  |

The final check observed zero blobs after all target purges. It does **not**
prove each shared blob survived the first owner purge while another owner
retained a reference, nor prove exactly-once physical deletion. Those remain
separate, unmeasured assertions even when owners were purged in different
ticks. Reader queries on this load cost more with the sweep on (enabled
85.7–126.6 ms vs disabled 57.6–72.3 ms p95) while remaining inside the
frozen 200 ms bound; the volume effect is present in both twins and the
absolute margin stays wide.

blocked-writer (corrected run, 2 repeats per condition; the 6.0 s hold
exceeds the storage connection's 5,000 ms busy timeout. The blocker acquired
all four holds per run, rejecting no begins):

| Field                            | enabled r0               | enabled r1               | disabled r0       | disabled r1       |
| -------------------------------- | ------------------------ | ------------------------ | ----------------- | ----------------- |
| Sweep calls / enforced           | 33 calls / 300           | 33 calls / 300           | 0 / 0             | 0 / 0             |
| Tick ms p50 / p95 / max          | 16.9 / 1,546.0 / 5,006.3 | 17.2 / 1,547.3 / 5,005.1 | n/a               | n/a               |
| Sweep errors                     | 1 (inside the 6 s hold)  | 1 (inside the 6 s hold)  | 0                 | 0                 |
| Admission ms p99 / max           | 1,016.4 / 1,016.4        | 1,025.2 / 1,025.2        | 1,278.7 / 1,278.7 | 1,266.4 / 1,266.4 |
| Claim non-empty ms max / gap (s) | 952.5 / 6.403            | 956.7 / 6.397            | 556.9 / 5.507     | 18.1 / 5.412      |
| Reader mid-run ms p95            | 1.7                      | 1.7                      | 2.0               | 1.5               |

The B1 miss: the measured longest tick was 5,005.1–5,006.3 ms, and this run
verifies the mechanism the original run could only conjecture. Each enabled
run recorded exactly one sweep error, a `StorageError` wrapping
"database is locked", whose timestamp lands inside the acquired 6.0 s hold
window (5.09 s after that hold's begin), so the failed tick's wall time is
the 5,000 ms busy timeout plus overhead. The 5,000 ms SQLite busy timeout
limits the lock wait, **not total tick wall time**; SQLite's lock-wait
timeout is not a hard limit on the wall time of a complete sweep tick. The
excess over the frozen 5,000 ms budget is 5–6 ms and is recorded as a miss.
Inflation on this load is blocker-driven in both twins: admission maxima of
~1.0–1.3 s and claim gaps of 5.4–6.4 s appear with the sweep off as well,
and one disabled-twin trickle staging failed against a held window (29
staged, 28 sent). Every enabled run still receipted all 300 targets after
the holds ended.

interrupt-restart (1 run): the parent killed the sweeping child right after
its first committed tick (50 receipts, kill phase between ticks). Reopening
after SIGKILL passed `integrity_check`, held exactly the committed state —
50 receipts with zero receipts coexisting with body rows, zero cleared
envelopes without receipts, and the durable enforcement cursor preserved
verbatim mid-backlog (`["…19:24:23.708353+00:00", "ir-target-000049",
…upper…]`) — and the resumed sweep receipted all remaining targets in
0.227 s with the read-only verification battery passing all 1,200 receipts.
The separate cancellation phase stopped the worker in 11 ms with the same
consistency guarantees and all 800 receipts verified; the sample again
caught a tick boundary rather than a tick interior (ticks on this backlog
take tens of milliseconds), and the probe recorded no `CancelledError`
inside a tick. The teardown figure therefore describes stop-at-boundary.
Neither interior SIGKILL nor interior cancellation was measured.

## Original run, superseded instrumentation

The first full-scale run of this record produced its figures with the
harness before the method corrections listed at the end of this document:
completion-to-completion claim gaps dropped failed calls, the enabled twin
purged receipted offline history before the probes started while the
disabled twin retained it, the competing writer ran on the event loop
without recorded hold windows, readers started at rest, and restart
verification accepted cleared content without receipts and incomplete
receipt batteries. Its B1 and B2 misses were the reason the instrumentation
was corrected, and both misses were reproduced by the corrected run above:
B1 blocked-writer max ticks 5,006.7/5,009.0 ms (corrected: 5,005.1–5,006.3),
B2 stream receipt tails 18.483–18.522 s (corrected: 18.481–18.527). Its
other entries carried UNVERIFIED markers because the recorded evidence could
not prove the frozen budgets; the corrected run replaces them with the
verdicts in the table above. The two runs are never averaged or merged;
figures from the original run are kept only in this section.

## Findings for the enablement decision

These are findings for the owner of the default-enable decision, not defects
fixed by this record.

- Receipt tail under sustained newer arrivals grows to roughly one full cursor
  cycle plus the arrival's walk position, not one cycle plus a small constant
  (the B2 miss above; reproduced by the corrected run at 18.48–18.53 s against
  a 15.5 s bound, with the median target inside the bound at 13.6 s). The tail
  scales linearly with the elapsed-policy volume divided by the sweep limit
  times the sweep interval; at the default 60 s interval the observed stream
  shape would put the worst tail at tens of minutes once the elapsed backlog
  reaches a few thousand policies.
- A new arrival waiting behind an already-elapsed receipted history pays
  ceil(history / limit) ticks before its first visit (20.066–20.079 s at
  2,000 history, limit 50, 0.5 s interval); bounded and wrap-guaranteed, but
  proportional to backlog depth rather than constant.
- Under external write pressure exceeding the busy timeout, the colliding
  tick fails with one bounded lock-busy error 5.09 s into the 6 s hold, which
  the worker's poll-down absorbs while every other tick proceeds; the error
  timestamps and the blocker's acquired hold windows confirm the confinement.
  Nothing in the measured runs blocked native retry processing unboundedly —
  the worst enabled claim gap on loads without an external blocker was
  0.54 s against a 0.5 s poll cadence, and reader queries served during
  active hold windows at 1.5–2.0 ms p95, so readers do not contend with the
  writer lock under WAL mode.
- With the sweep off during these bounded runs, expired target content
  remained while reads stay denied: every disabled twin closed with its full
  elapsed backlog unpurged (history-heavy maximum age 35.35–35.61 s at
  close), confirming the audit's default-off exposure statement with measured
  ages rather than a single synthetic sample. This retention observation is
  a control measurement, kept separate from the like-for-like latency
  comparisons above.
- Logical purge does not reclaim space in these runs: the DB file never
  shrank and the WAL grew at sampled points (largest observed sample 4.60 MB)
  and being removed only by clean close. Nothing in this record measures
  page-level erasure; a receipt remains a logical, content-free fact, not a
  WAL-erasure certificate.

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
phase. Destinations must be fresh or empty of the run's `*.sqlite*` files.
On the machine of the corrected run, the fanout-attachments invocation takes
about ten minutes (bulk seeding dominates), history-heavy and
continuous-stream a few minutes each, and blocked-writer and
interrupt-restart finish in minutes. Keep runs from different source
revisions in separate destinations and never merge them as one baseline.

## Method corrections and outstanding measurements

The corrected harness records paired claim completions (including failures),
receipts the same history in both conditions before timing, starts readers
while foreground traffic is active, and runs the competing SQLite writer in
a separate thread with actual hold windows and sweep-error timestamps. It
also joins spawned load tasks before closing storage, checks for cleared
content without receipts, requires the full successful receipt-verification
count, and reopens a fresh SQLiteStorage after cancellation. The corrected
full-scale run measured the B2 miss and established B4–B7 and B10 on that evidence: each
blocked-writer sweep error is attributed to an acquired hold window by
timestamp, the twin comparisons are like-for-like from equal receipted
starting states, the claim gaps are completion-to-completion including
failed calls, and the residual wrap repaired the late write behind the
residual cursor in 20 ticks.

The result is **six budgets supported as passes (B3–B7, B10), two measured
misses (B1, B2), and two partly observed but unverified budgets (B8, B9)**.
This is deliberately stricter than calling endpoint snapshots a passed peak-WAL
limit or calling boundary recovery an in-transaction interruption. It does
not modify the ten frozen budgets or reinterpret the recorded samples.

Still unmeasured after that run: the interior interrupt cases — B9's
observed SIGKILL and cancellation again landed on tick boundaries, so
neither established an interrupted open purge transaction, and that interior
case remains an explicit unmeasured gate; shared-blob retention between the
first and last owner purges, where the loads record only the final
zero-survivor count; and continuous WAL high-water marks, where footprints
are sampled at seed, before close and after close, so the sampled maxima are
lower bounds. The disabled-control retention observation is reported
separately from like-for-like latency in the findings above.

The history-heavy record has 2,000 receipts created during offline seeding,
240 newly receipted targets and one residual repair (241 enforcement
actions). The repair does not create a new receipt. B10 evaluates those
counters separated; 2,240 total receipts must never be compared directly to
241 worker actions. The stream admission window was 12 seconds; the history
and fanout trickles were 30 and 20 seconds, respectively. Longer wall time
includes seeding, completion waits and repeats, not continuous admission.
