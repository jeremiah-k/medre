# Auditing the hard content privacy ceiling

Every event admitted under the `content_ceiling` policy carries one
immutable hard ceiling: an absolute bound on how long the relay's own
retained content may be used at all, including for delivery. When a ceiling
elapses, the guarded enforcement sweep purges every enumerated content
store for that event, verifies with a residual battery that nothing is
left, and only then records the content-free enforcement receipt. The
normative invariant and the store-by-store census live in
[the ceiling specification](../spec/content-privacy-ceiling.md).

The sweep is **disabled by default**. An elapsed ceiling denies content
reads and scrubs transport metadata from newly written native references at
insertion time, but does not by itself purge physical SQLite rows or create
a receipt.
Enable the sweep in the bridge YAML configuration (SQLite storage only):

```yaml
retry:
  ceiling_sweep:
    enabled: true
```

See [configuration](configuration.md#content-ceiling-sweep) for the
schedule and prerequisites. Other storage backends do not implement this
purge transition; enabling the flag there cannot enforce stored ceilings.
The purge and its receipt occur when the enabled sweep processes the event.

Two read-only commands let an operator check that contract against the
real database. Both emit JSON only, read storage strictly read-only, and
never purge, rewrite, or record anything: remediation is always the
enforcement sweep's job, never the audit's.

## The four ceiling states

Every ceiling-bearing event sits in one of four states, and each state has
a direct observation on a real database. The fourth is a boundary every
report states explicitly rather than a third row state.

| State                                 | Meaning                                                                                                                                      | Observation                                                                                                                                                                                                                                                                 |
| ------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Not expired                           | The event's immutable ceiling is still in the future; content is readable and in use.                                                        | `audit --event EVENT_ID` reports `"state": "not_elapsed"` with `"receipt": null` and the content rows counted under `residual_stores`.                                                                                                                                      |
| Expired / read-denied but not purged  | The ceiling has elapsed, authorized reads refuse the content, and no enabled sweep has processed the event yet; the rows are still stored.   | `audit --event` reports `"state": "elapsed_pending"`; the window audit lists the event under `elapsed_pending` with per-store residual counts; `verify --event` exits with code `1` and `"event_state": "no_receipt"`.                                                      |
| Purged and receipt verified           | Enforcement removed the enumerated content, recorded the immutable content-free receipt, and the residual battery re-checks clean.           | `audit --event` reports `"state": "enforced_clean"` with `"residual_stores_total": 0`; `verify` reports `"verdict": "passed"` with exit code `0` and `"status": "passed"` for each receipt.                                                                                 |
| Logical purge is not physical erasure | The receipt proves logical removal only; freed SQLite pages, WAL frames, backups, exports, and provider copies still hold recoverable bytes. | Every audit and verify report carries `"physical_erasure_claimed": false` and lists `sqlite_free_pages_and_wal` among the `unmeasured` scopes. Byte-level erasure needs the explicit quiesced procedure in [the ceiling specification](../spec/content-privacy-ceiling.md). |

An enforced event that regained content values through a write authorized
before its ceiling is an additional documented observation, reported as
`residual_enforced` (below); the next sweep re-purges those values without
rewriting the receipt.

## The two modes

```bash
medre storage ceiling audit --storage-path /path/to/state/medre.sqlite
medre storage ceiling verify --storage-path /path/to/state/medre.sqlite
```

**Audit** — what each elapsed event currently holds. Walks the residual
battery for content whose ceiling has elapsed and reports, per census store,
the residual rows observed for each event in this snapshot
(`elapsed_pending`). It also flags the
documented late-write case: enforced events that regained content-potential
values through writes authorized before their ceiling
(`residual_enforced`). Nothing is removed.

**Verify** — bounded-page post-sweep receipt verification. Re-runs the same residual battery
against the current database for every enforcement receipt in a bounded
window and reports pass/fail per receipt with the receipt's recorded
counters (rows, logical bytes, blobs, and blob bytes removed). The verdict
is `passed` only when every examined receipt re-checks clean. The command
exits with code 1 when the verdict is `failed`, so verification is
scriptable. To verify the full receipt history, follow
`receipts_next_after_event` using `--after-event` while
`receipts_has_more` is true, require every page to pass, and separately
audit events with elapsed ceilings but no receipt. A clean page never
proves the unchecked history clean.

## Options

```bash
medre storage ceiling audit --storage-path PATH [--limit N] [--as-of UTC_TIMESTAMP] [--event EVENT_ID | --after-event EVENT_ID]
medre storage ceiling verify --storage-path PATH [--limit N] [--event EVENT_ID | --after-event EVENT_ID]
```

- `--storage-path PATH` — the SQLite database to read (default: the
  standard database path).
- `--limit N` — bounded report window, 1–100, default 25. The reports
  carry explicit `*_has_more` flags when more entries exist than the
  window showed.
- `--event EVENT_ID` — report exactly one event. The audit reports its
  state (`unknown`, `not_elapsed`, `elapsed_pending`,
  `residual_enforced`, `enforced_clean`) with its receipt counters when
  present; the verification fails closed when the event carries no
  receipt, because absence of proof is not proof.

- `--as-of UTC_TIMESTAMP` — choose an offset-aware ISO 8601 audit cutoff
  (e.g. `2026-11-21T18:00:00Z`). Reuse the _same_ cutoff across all audit
  pages. Without it, each invocation uses its current UTC clock; a policy
  whose ceiling elapses after an earlier page has passed its event ID can
  otherwise be omitted by the remaining pages. Receipt verification is
  independent of an elapsed-time cutoff.
- `--after-event EVENT_ID` — continue in event ID order. Pass the report's
  `receipts_next_after_event` to verify the next receipt page. For audit,
  page the pending and residual lists independently using their
  `*_next_after_event` values. A null continuation means the list ended.
  Each invocation reads its own snapshot. Keep the store quiescent across
  a whole-store verification or audit, and reuse `--as-of` for audit pages.
  Pagination is not a single database snapshot across invocations.

## Reading the output

Both reports are metadata-only: event identifiers and census store labels
are content-free identity. SQLite evaluates residual predicates against
stored values and returns counts and identifiers. The reports contain no
message text, excerpts, blob bytes, or other content values.

- `elapsed_pending` — elapsed ceilings without a receipt: the sweep's next
  candidates, with the per-store residual counts observed for each event in
  this snapshot. A sweep can remove additional shared rows, such as a shared
  blob once its last remaining association is gone.
- `residual_enforced` — enforced events still carrying residual values:
  the honest boundary between "gone by enforcement" and writes already
  authorized before the instant. Read mediation keeps such content
  unreachable until a subsequent sweep reaches the receipt and purges the
  residual values without rewriting the immutable receipt, and the
  verification passes
  again afterwards.
- `receipts` (verify) — one entry per enforcement receipt: `enforced_at`,
  the ceiling, the recorded counters, and `status` plus `residual_stores`
  when the battery observes residuals now.

Each audit finding describes one event in the snapshot the invocation
observed. The per-store counts are not additive across events and do not
predict a sweep: two elapsed events can share one attachment blob, so neither
reports `attachment_blobs:last_reference` until the purge that removes its
association makes the other the sole owner. Only a finding for the event that
is already the blob's sole owner predicts that deletion.

A failing verification means content-potential values exist now for an
enforced event. It does not mean the receipt was wrong: the receipt
records what its transaction removed and verified. Enable
[`retry.ceiling_sweep`](configuration.md#content-ceiling-sweep),
run MEDRE in live mode, and re-run verification after the sweep reaches
the receipt's page. A passing window covers only its examined receipts.
Check `receipts_examined` for an empty window and audit separately for
elapsed ceilings awaiting enforcement. A failed single-event verdict
distinguishes `event_state=no_receipt` from `event_state=receipt_failed`.
With no receipt there is no purge proof: verify the event exists and its
ceiling has elapsed before asking enforcement to create a receipt. An
unknown event cannot be fixed by sweeping. `receipt_failed` means proof
exists but residual content remains: enable the sweep and re-verify after
it revisits the receipt. If residual stores remain after that page was
processed, stop and investigate — that is a loud inconsistency, not a
normal state.

## Rehearse on a disposable copy

Rehearse the whole audit → enforce → verify path on a disposable copy of
the real database before enabling the sweep on the live installation. Stop
MEDRE and every adapter and native profile owner first, then copy with the
stopped-state snapshot procedure: its SQLite backup protocol produces a
standalone database that retains committed WAL content, which a plain file
copy of a WAL database does not guarantee.

The commands below are one complete sequence for a single-host installation
whose state root is `/srv/medre-state`: paste the whole fenced block into a
single shell session, because every line uses the working-directory variable
the first line defines. That first line creates the working directory with
`mktemp -d` — a fresh directory, mode 0700, owned by your user — and every
path in the block lives inside it. Never substitute a fixed shared path such
as `/tmp/ceiling-rehearsal`: another local user could pre-create it and
replace the enforcement script before it runs. Adjust the root and the
inventory paths to the installation (see [stopped-state
snapshots](stopped-state-snapshots.md) for multi-file installations); the
rehearsal sends nothing and writes nothing outside that private directory
and the copy.

```bash
set -euo pipefail # fail the rehearsal if capture, audit, enforcement or verify fails
REH=$(mktemp -d) # private 0700 working directory for everything below
cat > "$REH/inventory.json" <<'JSON'
{
  "format": "medre-stopped-inventory",
  "version": 1,
  "providers": ["core"],
  "files": [
    {
      "provider": "core",
      "path": "state/medre.sqlite",
      "role": "core_sqlite",
      "format": "sqlite"
    }
  ]
}
JSON
python -m medre.maintenance.stopped_cli capture \
  --inventory "$REH/inventory.json" \
  --root /srv/medre-state \
  --snapshot "$REH/snap-001" --stopped
python -m medre.maintenance.stopped_cli verify \
  --snapshot "$REH/snap-001"
python -m medre.maintenance.stopped_cli restore \
  --snapshot "$REH/snap-001" \
  --destination "$REH/root" --stopped

# 1. Audit the copy (read-only). Elapsed, unpurged events appear under
#    elapsed_pending with per-store residual counts.
medre storage ceiling audit \
  --storage-path "$REH/root/state/medre.sqlite" --limit 25

# 2. Enforce on the copy. There is no storage subcommand for this on
#    purpose: enforcement is the guarded purge-and-verify transition the
#    opt-in sweep calls. This one-shot script is the documented
#    storage-level invocation for trusted operator tooling; it runs the
#    same BEGIN IMMEDIATE transition the sweep runs and prints
#    metadata-only summaries per enforced event.
cat > "$REH/enforce_once.py" <<'PY'
"""Run one bounded content-ceiling enforcement pass."""

import asyncio
import sys

from medre.core.storage.sqlite.storage import SQLiteStorage


async def main(path: str) -> None:
    store = SQLiteStorage(db_path=path)
    await store.initialize()
    try:
        enforced = await store.enforce_content_ceilings(limit=50)
        for row in enforced:
            print(row.event_id, "rows", row.rows_removed,
                  "logical_bytes", row.logical_bytes_removed)
    finally:
        await store.close()


asyncio.run(main(sys.argv[1]))
PY
python "$REH/enforce_once.py" \
  "$REH/root/state/medre.sqlite"

# 3. Enforce in bounded passes until one full audit finds nothing left.
#    One pass enforces at most 50 elapsed events and revisits at most 50
#    receipted events for late-write residuals, so a single pass leaves
#    work behind on a larger copy. The pass is done when one audit call
#    reports "elapsed_pending": [], "residual_enforced": [], and both
#    elapsed_pending_has_more and residual_enforced_has_more false.
while :; do
  medre storage ceiling audit \
    --storage-path "$REH/root/state/medre.sqlite" --limit 25 \
    > "$REH/audit.json" || {
    cat "$REH/audit.json"
    exit 1 # never continue after audit fails
  }
  if python - "$REH/audit.json" <<'PY'
import json
import sys

try:
    with open(sys.argv[1]) as handle:
        report = json.load(handle)
    clean = (
        report["elapsed_pending"] == []
        and report["residual_enforced"] == []
        and not report["elapsed_pending_has_more"]
        and not report["residual_enforced_has_more"]
    )
except (OSError, ValueError, TypeError, KeyError) as exc:
    print(f"invalid audit report: {exc}", file=sys.stderr)
    raise SystemExit(2) from exc
if not clean:
    print("pending:", len(report["elapsed_pending"]),
          "residual:", len(report["residual_enforced"]))
raise SystemExit(0 if clean else 1)
PY
  then
    break
  else
    status=$?
    [ "$status" -eq 1 ] || exit "$status"
  fi
  python "$REH/enforce_once.py" "$REH/root/state/medre.sqlite"
done

# 4. Verify the whole receipt history. Each verify call re-checks one
#    bounded page of receipts and exits 1 when its verdict is failed;
#    follow receipts_next_after_event with --after-event while
#    receipts_has_more is true and require "verdict": "passed" on every
#    page. Any failed page aborts; one clean page never proves the rest.
after=""
while :; do
  if [ -n "$after" ]; then
    medre storage ceiling verify \
      --storage-path "$REH/root/state/medre.sqlite" \
      --limit 25 --after-event "$after" > "$REH/verify.json"
  else
    medre storage ceiling verify \
      --storage-path "$REH/root/state/medre.sqlite" \
      --limit 25 > "$REH/verify.json"
  fi
  after=$(python - "$REH/verify.json" <<'PY'
import json
import sys

with open(sys.argv[1]) as handle:
    report = json.load(handle)
if report["verdict"] != "passed":
    sys.exit("verify reported a failed receipt page")
print(report["receipts_next_after_event"] or "")
PY
) || {
  cat "$REH/verify.json"
  exit 1 # failed receipt verification fails the rehearsal
}
  [ -z "$after" ] && break
done
```

The rehearsal proves the logical path only. It changes nothing on the live
installation and erases no bytes anywhere: the copy still holds freed pages
and WAL images, and so does the live store until the explicit erasure
procedure in [the ceiling specification](../spec/content-privacy-ceiling.md)
is performed on it. Events whose ceiling has not elapsed stay `not_elapsed`
and untouched. Each pass is bounded twice: it enforces at most 50 elapsed
events without a receipt and revisits at most 50 already-receipted events
for late-write residuals, each bound advancing a durable cursor that wraps
at the end of its queue. A copy with more pending or residual work than one
pass covers therefore needs repeated passes, and an empty `elapsed_pending`
list alone is not the finish line: a late write landing on a receipt the
residual cursor has not yet revisited keeps `residual_enforced` populated
while `elapsed_pending` is already empty and the first verify page passes.
The rehearsal is complete when one audit call reports
`"elapsed_pending": []` and `"residual_enforced": []` with both `*_has_more`
flags false — page the audit lists with `--after-event` and their
`*_next_after_event` values when a report says otherwise — and every
`verify` page reports `"verdict": "passed"` from the first receipt through
`receipts_has_more: false` (exit code 0 on each page). The production path
is the opt-in sweep below; the rehearsal predicts its behavior because both
call the same guarded transition, and the enabled sweep repeats its bounded
passes on its own schedule.

## Failure and corruption policy

A failed sweep tick changes nothing durable. Each event's enforcement runs
as one `BEGIN IMMEDIATE` transaction, and a verification failure rolls the
whole transaction back: no receipt is written, no partial purge commits,
and the durable cursor stays where it was. The worker logs the failure,
increments `ceiling_sweep_errors` in the runtime snapshot, and leaves the
content expired and read-denied; the audit keeps reporting
`elapsed_pending`, and the next due tick retries. When an external writer
holds the SQLite write reservation longer than the storage connection's
5,000 ms busy timeout, the colliding tick fails with one lock-busy error
and later ticks proceed; the measured pressure campaign under exactly that
pressure still receipted every target once the holds ended. The sweep
never blocks native retry processing by design: delivery claims continue
around the maintenance step.

A failed verification is not a broken receipt. `verdict: "failed"` with
`event_state: "receipt_failed"` means content-potential values exist now
for an enforced event — usually the documented late-write case. Keep the
sweep enabled and re-run verification after it revisits the receipt's
page; the receipt stays immutable and is never rewritten or deleted. If
residual stores remain after a full cursor cycle has passed the event,
stop and investigate before broadening enablement: that is a loud
inconsistency, not a normal state. `event_state: "no_receipt"` on an
`elapsed_pending` event is the expected pre-enforcement answer — enable
the sweep, or rehearse first on a copy. `no_receipt` for an event the
audit reports as `unknown` cannot be fixed by sweeping; check the event
identifier and the store path instead.

A corrupt store is a restore, never a repair. If `medre storage status`
or a reopen reports an integrity failure, stop every writer, keep a
read-only copy aside for investigation, and restore the last verified
stopped-state snapshot into a fresh root. Never delete receipt rows,
hand-edit content tables, or remove database or WAL files by hand to
"finish" a purge: receipts are the append-only proof that enforcement
completed, so deleting them fabricates "never enforced", and hand-edited
deletions skip the residual battery the guarded transition verifies. The
only sanctioned mutation of ceiling content is the guarded enforcement
transition.

## Opt-in rollout profile

The sweep stays disabled until an operator declares it. The profile below
is the bounded starting point; it is opt-in, and the loader default
remains `enabled: false`:

```yaml
retry:
  enabled: false # the ceiling sweep does not require the retry worker
  ceiling_sweep:
    enabled: true
    limit: 50
    interval_seconds: 60
```

Run `medre config check` before deployment. The measured consequences of
the 2026-11-23 pressure campaign
([measurement record](../dev/ceiling-sweep-pressure-measurement-2026-11-23.md))
apply at this profile:

- A sweep tick normally takes tens of milliseconds, and delivery claims
  keep flowing around it (largest measured gap between claims 0.54 s
  against a 0.5 s poll cadence). When an external writer holds the SQLite
  write reservation past the 5,000 ms busy timeout, the colliding tick
  fails once at that timeout — the worst measured tick was 5,005–5,006 ms
  — and the next tick proceeds. The busy timeout bounds lock waiting, not
  total tick wall time.
- Receipt latency scales with backlog depth. An arrival waits up to about
  one full cursor cycle plus its walk position — roughly
  ceil(elapsed backlog / limit) ticks. The measured worst stream receipt
  tail was 18.48–18.53 s at a 0.5 s interval behind roughly 1,000 elapsed
  policies; at the default 60 s interval, a backlog of a few thousand
  elapsed policies puts that same shape in the tens of minutes. An arrival
  behind an already-receipted history pays ceil(history / limit) ticks
  before its first visit (measured 20.07 s at 2,000 history policies,
  limit 50, 0.5 s interval).
- Two budgets remain unverified by measurement: the continuous WAL
  high-water mark (footprints were sampled at intervals, so the recorded
  maxima are lower bounds) and interruption inside an open purge
  transaction (kill and cancellation landed on tick boundaries only).
  Restart consistency after a boundary kill, bounded lock-busy
  absorption, and full receipt-battery agreement are measured passes.
- Rollback is the declaration itself: set `ceiling_sweep.enabled` back to
  `false` (or remove the section) and restart. Elapsed ceilings stay
  durable and read-denied, audit and verify remain available read-only,
  and nothing already receipted is undone.

Keep the fences around the sweep in mind while it runs. Deadline
maintenance (`retry.deadline_sweep`) is a separate policy with distinct
authority: it only retires provably unstarted outbox rows past the
effective delivery deadline; it never purges content, never writes
ceiling receipts, and never resolves a confirmation-pending row. Neither
sweep sends anything: native submission authority ends at the hard
ceiling, the persisted route bounds let recovery settle existing evidence
but never authorize another native send, and the maintenance worker never
claims delivery work. A receipt remains a logical, content-free fact —
never an erasure certificate; the `unmeasured` scopes in every report and
the explicit erasure procedure own that boundary.

## Safety and boundaries

| Command                        | Mutates storage | Safe against a live database |
| ------------------------------ | --------------- | ---------------------------- |
| `medre storage ceiling audit`  | No (read-only)  | Yes                          |
| `medre storage ceiling verify` | No (read-only)  | Yes                          |

The commands open the database strictly read-only; no schema or
application data is written, migrated, or checkpointed, though SQLite may
create or update WAL bookkeeping sidecars for its readers. The audit
covers the logical stores in the specification's census only: adapter
checkpoints, plugin state, transport-native stores, SQLite free pages and
WAL frames, backups, exports, and transport copies remain the documented
unmeasured scopes. Receipt presence is not a physical-erasure certificate;
byte-level erasure still requires the explicit operator step documented in
[the ceiling specification](../spec/content-privacy-ceiling.md).
