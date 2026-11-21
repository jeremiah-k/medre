# Auditing the hard content privacy ceiling

Every event admitted under the `content_ceiling` policy carries one
immutable hard ceiling: an absolute bound on how long the relay's own
retained content may be used at all, including for delivery. When a ceiling
elapses, the guarded enforcement sweep purges every enumerated content
store for that event, verifies with a residual battery that nothing is
left, and only then records the content-free enforcement receipt. The
normative invariant and the store-by-store census live in
[the ceiling specification](../spec/content-privacy-ceiling.md).

Two read-only commands let an operator check that contract against the
real database. Both emit JSON only, read storage strictly read-only, and
never purge, rewrite, or record anything: remediation is always the
enforcement sweep's job, never the audit's.

## The two modes

```bash
medre storage ceiling audit --storage-path /path/to/state/medre.sqlite
medre storage ceiling verify --storage-path /path/to/state/medre.sqlite
```

**Audit** — what would be purged. Walks the residual battery for content
whose ceiling has elapsed and reports, per census store, the residual rows
the next sweep would remove (`elapsed_pending`). It also flags the
documented late-write case: enforced events that regained content-potential
values through writes authorized before their ceiling
(`residual_enforced`). Nothing is removed.

**Verify** — the post-sweep proof. Re-runs the same residual battery
against the current database for every enforcement receipt in a bounded
window and reports pass/fail per receipt with the receipt's recorded
counters (rows, logical bytes, blobs, and blob bytes removed). The verdict
is `passed` only when every examined receipt re-checks clean. The command
exits with code 1 when the verdict is `failed`, so the one-shot proof is
scriptable.

## Options

```bash
medre storage ceiling audit --storage-path PATH [--limit N] [--event EVENT_ID]
medre storage ceiling verify --storage-path PATH [--limit N] [--event EVENT_ID]
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

## Reading the output

Both reports are metadata-only: event identifiers and census store labels
are content-free identity. No message text, excerpt, blob bytes, or other
content value is ever read or printed.

- `elapsed_pending` — elapsed ceilings without a receipt: the sweep's next
  candidates, with the per-store residual counts that would be purged.
- `residual_enforced` — enforced events still carrying residual values:
  the honest boundary between "gone by enforcement" and writes already
  authorized before the instant. Read mediation keeps such content
  unreachable in the interim; the next sweep re-purges the residual values
  without rewriting the immutable receipt, and the verification passes
  again afterwards.
- `receipts` (verify) — one entry per enforcement receipt: `enforced_at`,
  the ceiling, the recorded counters, and `status` plus `residual_stores`
  when the battery observes residuals now.

A failing verification means content-potential values exist now for an
enforced event. It does not mean the receipt was wrong: the receipt
records what its transaction removed and verified. Run the sweep (or wait
for the default-off `retry.ceiling_sweep` cycle if enabled) and re-run the
verification; if a failure persists after a sweep, stop and investigate —
that is a loud inconsistency, not a normal state.

## Safety and boundaries

| Command                        | Mutates storage | Safe against a live database |
| ------------------------------ | --------------- | ---------------------------- |
| `medre storage ceiling audit`  | No (read-only)  | Yes                          |
| `medre storage ceiling verify` | No (read-only)  | Yes                          |

The commands open the database strictly read-only; no schema or
application data is written, migrated, or checkpointed, though SQLite may
create or update WAL bookkeeping sidecars for its readers. The audit
covers the logical stores in the specification's census only: adapter
checkpoints, plugin state, provider-native stores, SQLite free pages and
WAL frames, backups, exports, and provider copies remain the documented
unmeasured scopes. Receipt presence is not a physical-erasure certificate;
byte-level erasure still requires the explicit operator step documented in
[the ceiling specification](../spec/content-privacy-ceiling.md).
