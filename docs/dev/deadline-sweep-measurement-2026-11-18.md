# Deadline sweep impact measurement — November 18 held-lane profile

`python scripts/measure_held_lane_workload.py DESTINATION --sweep-comparison
--history-rows 120` runs the production durable-runtime assembly twice on
fresh disposable SQLite state with one fixed held-lane workload, differing
only in the opt-in `retry.deadline_sweep` policy. This is the first recorded
sweep-impact profile for the delivery-deadline cleanup transition; it is a
measurement, not a pass/fail verdict, and no storage schema, default policy
or scheduler behavior changed for it.

## Fixed workload

| Element                 | Value                                                                                                                                                                                  |
| ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Seeded terminal history | 120 real sent generations (settler lane, timings excluded)                                                                                                                             |
| Parked obligations      | 60 pending rows whose content ceiling elapses 2 s into the run, parked past every claim predicate via a future `next_attempt_at`                                                       |
| Healthy work            | 2 rounds x 3 events fanning out to the held lane plus three healthy lanes (18 healthy rows per run)                                                                                    |
| Held lane               | one handoff parked behind an explicit barrier for the whole run                                                                                                                        |
| Cooldown lane           | one refusal recovered through the dispatcher schedule                                                                                                                                  |
| Dispatcher cadence      | 0.5 s poll, batch 20, delivery limit 100, retries on                                                                                                                                   |
| Sweep policy            | interval 0.5 s, limit 50 (off in the first run, on in the second)                                                                                                                      |
| Sample counts           | healthy latency 18; fresh-admission writer 8; parked staging 60; claim non-empty 17; claim empty 15; final claim probe 25; count refreshes 33; work-status 10 during hold + 10 at rest |

## Environment

Linux 7.0.0-43-generic x86_64, glibc 2.39, Python 3.14.8, SQLite 3.53.1,
core source `00e4be224d14dce3127be519dd2a9843754d3a40` (clean tree, the
commit introducing the comparison mode), no Docker, no transport SDK, fake
adapters only. Two earlier executions of the identical profile on the same
tree retired 60/60 parked rows under the enabled policy with 10 and 11 sweep
invocations and 18/18 healthy rows, matching the recorded run below.

## Results

| Field (matching phases)              | Sweep disabled                      | Sweep enabled                                    |
| ------------------------------------ | ----------------------------------- | ------------------------------------------------ |
| Sweep invocations / retired          | 0 / 0                               | 11 / 60 (worker counters agree, 0 errors)        |
| Sweep elapsed per invocation (ms)    | not run                             | p50 2.959, max 21.661 (11 samples)               |
| Fresh-admission writer latency (ms)  | p50 44.856, max 75.485 (8 samples)  | p50 47.662, max 74.037 (8 samples)               |
| Parked-row staging latency (ms)      | p50 15.863, max 27.913 (60 samples) | p50 14.754, max 28.615 (60 samples)              |
| Healthy rows completed               | 18/18                               | 18/18                                            |
| Healthy completion latency (ms)      | p50 777.561, max 880.611            | p50 884.488, max 1082.511                        |
| Claim non-empty query (ms)           | p50 16.263, max 28.149 (17 samples) | p50 18.648, max 42.745 (17 samples)              |
| Claim empty query (ms)               | p50 0.908, max 13.537 (15 samples)  | p50 1.529, max 14.518 (15 samples)               |
| Final empty claim probe (ms)         | p50 0.619, max 2.570 (25 samples)   | p50 0.507, max 1.021 (25 samples)                |
| Count refreshes (ms)                 | p50 0.367, max 13.240 (33 samples)  | p50 0.838, max 14.880 (33 samples)               |
| Rejected capacity starts             | 0                                   | 0                                                |
| Final status counts                  | 1 in progress, 66 pending, 142 sent | 1 in progress, 6 pending, 60 abandoned, 142 sent |
| DB + WAL bytes before close          | 1,089,536 + 4,190,072 (+32,768 shm) | 1,085,440 + 4,185,952 (+32,768 shm)              |
| DB file bytes after close            | 1,114,112                           | 1,114,112                                        |
| Work-status summary during hold (ms) | p50 6.854, max 8.579 (10 samples)   | p50 5.969, max 9.194 (10 samples)                |
| Work-status summary at rest (ms)     | p50 7.259, max 9.286 (10 samples)   | p50 4.993, max 6.527 (10 samples)                |

## Assessment

Under this profile the sweep is cheap and bounded: eleven invocations
retiring 60 rows cost at most about 22 ms per invocation while parked in the
worker's poll cycle, and no recorded writer, claim, count, healthy-lane or
footprint field regressed beyond run-to-run noise (healthy completion
latency rides the 0.5 s poll cadence in both runs; its 1082 ms maximum in
the enabled run is within the spread of the repeat runs). No hotspot was
demonstrated, so no product correction is proposed from this measurement.

## Limits

- Fake handoffs prove local acceptance only; no transport SDK or recipient
  delivery participates, and the held lane models a stalled destination.
- Parked obligations are synthetic pending rows created through the real
  admission and adoption boundary with a mid-run ceiling; the sweep observes
  producer-installed ceiling rows as production would, but no transport
  attempted them.
- Timings are measurement data only: no machine-speed gate and no capacity
  promise. One reproducible profile is recorded here; sustained pressure,
  broad retention and larger histories remain backlog work.
