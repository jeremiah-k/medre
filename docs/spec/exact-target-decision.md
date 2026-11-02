# Exact-target operator decision authority

## Scope and authority

`SQLiteStorage.record_exact_target_decision(ExactTargetDecision(...))` in
`src/medre/core/storage/sqlite/_operator_decision.py` records one audited,
idempotent operator decision over one observed exact delivery target. It is
the decision sibling of the
[storage cancellation authority](operator-exact-resolution.md) and the write
counterpart of the typed
[exact-target observation contract](exact-target-recovery.md): it consumes
observation evidence (identity, generation, status, update version) but an
observation never authorizes by itself. It exposes no CLI command and changes
no engine, replay, or adapter behavior.

The decision never stages a send, never creates a delivery generation, and
never mutates the observed outbox row: no status change, no `updated_at`
movement, no content-dependency settlement, no snapshot retirement. Any
resumption goes through the normal outbox claim/attempt fences the retry
worker already owns; a claim that follows a committed decision is the decided
outcome, not a conflict.

## The decision and its action vocabulary

`ExactTargetDecision` carries the decision ID, actor label, `action`, the
complete `(event_id, delivery_plan_id, target_adapter, target_channel)`
identity, the outbox ID, and the observed `expected_generation`,
`expected_status` and `expected_updated_at`. The action vocabulary is frozen
across this page, the Python model, the storage CHECK constraint, the JSON
Schema, and the tests:

* `resume_scheduled` — the operator decides the observed unstarted
  scheduled/retry work resumes through the normal claim/attempt fences. The
  audit row is the audited go-ahead.
* `resolve_uncertain` — the operator decides their open review of this exact
  target is resolved without intervention. The audit row closes the operator
  question; the normal fences remain the only resumption path.

Both actions are audit-only acknowledgements with identical eligibility,
CAS and idempotency semantics; they differ only in the recorded intent.
Actor labels are audit provenance, never authentication, as for cancellation.

## Decidable state: the three v1 cases

The write transaction revalidates everything under the same `BEGIN IMMEDIATE`
discipline as cancellation, then applies three cases:

1. **Unstarted scheduled work is decidable.** The row is `pending` or
   `retry_wait`, carries no `active_attempt` reservation and no claim
   ownership (`worker_id`, `locked_at` and `lease_until` are all null), no
   competing generation exists, and the observed tuple (finalized generation,
   status, `updated_at`) still matches. The decision commits exactly one
   audit row in `operator_delivery_decisions` and changes nothing else.
2. **In-progress, queued and accepted-or-uncertain native work is refused.**
   `in_progress`, `queued`, `abandoned`, any unrecognized status, or a
   reservation or claim ownership present, refuses the decision with:
   `In-progress, queued or uncertain native work is not decidable by this
   authority`. No audit row and no state change are produced. Non-acceptance
   is never inferred from a timeout, a missing native reference, a restart,
   or content expiry.
3. **Terminal failure is not reopenable.** `failed` or `dead_lettered` rows
   refuse with: `Reopening terminal failed or dead-lettered work requires a
   separate send-capable authority`. The repeat-send-capable decision is a
   separately named authority that is excluded from v1; this refusal states
   only that a different authority would be required and promises none.

Settled outcomes refuse fail-closed as well: `sent` or `cancelled` rows
refuse with `Completed or cancelled delivery work is not decidable by this
authority`. A row that no longer exists under the exact identity refuses
with `Exact destination no longer exists`. State-family refusals are
evaluated before the CAS comparison so the refusal names the state rather
than a stale expectation.

## Refusal vocabulary

Refusals raise the typed `OperatorDecisionRefused` with these frozen
messages, no audit row, and no state change:

| Condition                                              | Refusal message                                                             |
| ------------------------------------------------------ | --------------------------------------------------------------------------- |
| Decision ID reused with different parameters            | `Decision ID already binds another request`                                  |
| Row absent or identity no longer matches                | `Exact destination no longer exists`                                         |
| Observed tuple superseded                               | `Stale observed state; nothing decided`                                      |
| Competing generation exists                             | `A competing delivery generation exists`                                     |
| Row moved between verification and the guarded insert   | `Concurrent attempt changed; nothing decided`                                |
| Case 2 states (owned, queued, uncertain)                | `In-progress, queued or uncertain native work is not decidable by this authority` |
| Case 3 states (failed, dead-lettered)                   | `Reopening terminal failed or dead-lettered work requires a separate send-capable authority` |
| Settled outcomes (sent, cancelled)                      | `Completed or cancelled delivery work is not decidable by this authority`    |

## Idempotency

A decision ID is idempotent only when its actor, entire identity, outbox ID,
observed generation, observed update version, observed status and action all
agree with the recorded row — the same ten-field comparison cancellation
uses. An identical replay returns idempotent success without repeating the
mutation; reusing an ID with different parameters fails closed.

## Transaction boundary and concurrency

The acknowledgement runs under the module lock and one `BEGIN IMMEDIATE`
transaction, serializing with attempt reservation, retries, finalization and
newer-generation creation across separate SQLite handles. After the
state-family and CAS checks, a competing-generation sweep compares the
reserved `active_attempt` when present, exactly as cancellation does. The
audit insert is guarded: a conditional insert rechecks the full observed
state and must affect exactly one row, so a row that moved between
verification and commit produces `Concurrent attempt changed; nothing
decided`. An audit write failure rolls the transaction back and surfaces as
a storage error.

## Observation evidence is not authorization

The observation fingerprint, category and `cancel_eligible` flag are
evidence only. `execution_authorized` remains `False` on the observation
side and no decision flips it: revalidation happens inside the write
transaction against persisted state, so a decision constructed from a stale
observation is refused even when the observation itself was genuine.

## Audit shape and prerelease compatibility

The decision reuses the `operator_delivery_decisions` audit table and
columns. The action CHECK widens to the frozen three-value vocabulary; the
unique decision ID, observed-status and generation constraints are
unchanged, and audit rows only ever record case 1, so `observed_status`
remains constrained to `pending`/`retry_wait`. The widening is part of the
**required** prerelease SQLite shape under the
[prerelease storage compatibility rules](operator-exact-resolution.md#prerelease-storage-compatibility):
MEDRE refuses older single-action shapes at initialization, qualification
uses fresh state, and no migration path is provided.

## Non-goals

No operator command surface in this slice, no engine or replay change, no
provider adapter, no repeat-send authority for terminal or uncertain native
work, and no widening from one exact target to sibling destinations.
