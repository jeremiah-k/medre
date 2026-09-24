# Delivery Lifecycle Authority Model

> **Status:** Active
> **Classification:** Normative
> **Authority:** Authoritative specification for the MEDRE delivery lifecycle authority hierarchy, vocabulary provenance, boundary definitions, replay/recovery constraints, and conformance rules.
> **Last reviewed:** 2026-08-21

This document defines the authority hierarchy governing the MEDRE delivery
lifecycle. It specifies which sources are authoritative, which are derived, and
what each layer MAY and MAY NOT do. It does not reproduce state-machine tables,
receipt schemas, or transition graphs — those are defined in their own normative
documents and are normatively referenced here.

The key words **MUST**, **MUST NOT**, **REQUIRED**, **SHALL**, **SHALL NOT**,
**SHOULD**, **SHOULD NOT**, **RECOMMENDED**, **MAY**, and **OPTIONAL** in this
document are to be interpreted as described in RFC 2119.

---

## 1. Authority Hierarchy

The MEDRE delivery lifecycle has a strict authority hierarchy. Each layer has
defined authority over lifecycle state, and lower layers MUST NOT override
decisions made by higher layers.

### 1.1 Authority Stack (highest to lowest)

| Role                 | Layer                                         | Responsibility                                                                                                                                         |
| -------------------- | --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Internal code source | `delivery_state.py`                           | Defines closed status vocabularies, transition tables, classification sets. Executable authority for runtime status vocabularies and transition logic. |
| Normative spec       | [state-machines.md](state-machines.md)        | Human-readable normative specification of receipt and outbox state machines. Authoritative for understanding machine behavior and invariants.          |
| —                    | `delivery_receipts` table (SQLite)            | Append-only evidence trail. Immutable after creation. Authoritative record of what happened.                                                           |
| —                    | `delivery_outbox` table (SQLite)              | Mutable operational state. Tracks current work. Secondary to receipts for audit.                                                                       |
| —                    | Adapters                                      | Fact emitters. Report delivery outcomes to the pipeline. They do not own lifecycle state.                                                              |
| —                    | Projections, convergence diagnostics, reports | Derived views. Read-only computations over receipts and outbox. They do not define state.                                                              |

### 1.2 Authority Rules

1. `delivery_state.py` is the internal executable source for closed status vocabularies,
   terminal/claimable/accepted classification sets, and observed transition
   tables. No other module defines status strings independently. The runtime reads these
   constants at startup and enforces them at runtime.
2. [state-machines.md](state-machines.md) is the normative human-readable
   specification of the receipt and outbox state machines. When `delivery_state.py` and
   [state-machines.md](state-machines.md) conflict, that is a defect requiring
   reconciliation of both sources in the same change. Neither silently overrides the other.
3. The `delivery_receipts` table is the authoritative evidence trail. No
   component MAY rewrite, update, or delete a receipt row after creation.
4. The `delivery_outbox` table is mutable operational state for non-terminal rows. Terminal outbox rows (`sent`, `dead_lettered`, `cancelled`, `abandoned`) MUST NOT be transitioned or reclaimed. If future work needs another delivery after a terminal state, it must create new evidence / a new attempt / a new outbox row — it MUST NOT mutate the terminal row.
5. Adapters emit facts (`sent`, `enqueued`, errors). They do not own lifecycle
   state and MUST NOT be treated as lifecycle authorities.
6. Projections, views, convergence diagnostics, and report dicts are derived.
   They MUST NOT be treated as lifecycle authorities and MUST NOT be used to
   drive state transitions.
7. `DeliveryLifecycleService` is the runtime authority for delivery/outbox
   transition decisions. `RetryWorker` MAY poll, claim, acquire capacity, and
   emit operational events, but it MUST delegate abandonment, retry backoff,
   retry exhaustion/dead-letter, and retry-success outbox transitions to the
   lifecycle service rather than mutating those states directly.

---

## 2. Authoritative vs Derived Vocabularies

### 2.1 Authoritative Vocabularies

These vocabularies are defined in `delivery_state.py` (§4 of
[state-machines.md](state-machines.md)) and are the normative source:

| Vocabulary                    | Constant                        | Values                                                                                              |
| ----------------------------- | ------------------------------- | --------------------------------------------------------------------------------------------------- |
| Receipt statuses              | `RECEIPT_STATUSES`              | `queued`, `sent`, `failed`, `dead_lettered`, `cancelled`, `abandoned`, `suppressed`                 |
| Outbox statuses               | `OUTBOX_STATUSES`               | `pending`, `in_progress`, `queued`, `sent`, `retry_wait`, `dead_lettered`, `cancelled`, `abandoned` |
| Outcome statuses              | `OUTCOME_STATUSES`              | `success`, `queued`, `transient_failure`, `permanent_failure`, `skipped`                            |
| Adapter delivery statuses     | `ADAPTER_DELIVERY_STATUSES`     | `sent`, `enqueued`                                                                                  |
| Terminal receipt statuses     | `TERMINAL_RECEIPT_STATUSES`     | `sent`, `dead_lettered`, `cancelled`, `abandoned`, `suppressed`                                     |
| Non-terminal receipt statuses | `NON_TERMINAL_RECEIPT_STATUSES` | `queued`, `failed`                                                                                  |
| Terminal outbox statuses      | `TERMINAL_OUTBOX_STATUSES`      | `sent`, `dead_lettered`, `cancelled`, `abandoned`                                                   |
| Non-terminal outbox statuses  | `NON_TERMINAL_OUTBOX_STATUSES`  | `pending`, `in_progress`, `queued`, `retry_wait`                                                    |
| Claimable outbox statuses     | `CLAIMABLE_OUTBOX_STATUSES`     | `pending`, `retry_wait`                                                                             |
| Accepted outcome statuses     | `ACCEPTED_OUTCOME_STATUSES`     | `success`, `queued`                                                                                 |

This specification introduces no new lifecycle states. All valid status strings
are drawn from the vocabularies above.

### 2.2 Derived Vocabularies

The following are derived from authoritative vocabularies at query or report
time. They do not introduce new states:

| Derived artifact                                        | Source                                                        | Defined in                                               |
| ------------------------------------------------------- | ------------------------------------------------------------- | -------------------------------------------------------- |
| `delivery_status` SQL view                              | Latest authoritative receipt per event-scoped delivery target | [routing-delivery.md](routing-delivery.md) §9            |
| Convergence severity (`safe`/`degraded`/`inconsistent`) | Cross-reference of outbox + receipt statuses                  | [diagnostics-evidence.md](diagnostics-evidence.md) §21   |
| Recovery ownership statuses                             | Classification of outbox items at startup                     | [diagnostics-evidence.md](diagnostics-evidence.md) §22   |
| Health vocabulary (`healthy`/`degraded`/etc.)           | Adapter diagnostics projection                                | [diagnostics-evidence.md](diagnostics-evidence.md) §5    |
| Report dict enrichment fields                           | Parsed from receipt `error` and `rendering_evidence`          | [diagnostics-evidence.md](diagnostics-evidence.md) §17.2 |
| Delivery outcome ledger                                 | Grouped projection over receipts and outbox                   | [diagnostics-evidence.md](diagnostics-evidence.md) §19   |
| Lifecycle convergence findings                          | Detection-only analysis of receipt/outbox contradictions      | [diagnostics-evidence.md](diagnostics-evidence.md) §23   |

### 2.3 Closure Constraint

No MEDRE component responsible for delivery lifecycle state transitions MAY define, produce, or consume a **delivery lifecycle status string** that does not appear in one of the authoritative vocabularies in §2.1. The authoritative delivery lifecycle statuses are:

- receipt statuses
- outbox statuses
- outcome statuses
- adapter `delivery_status` values

Derived/report/operator vocabularies (such as convergence severity, health status, operator status, retry_state display labels, report enrichment fields, and recovery ownership classifications) are allowed when they are documented as non-authoritative and MUST NOT be used to drive lifecycle state transitions.

If a new delivery lifecycle status is needed, it MUST be added to `delivery_state.py` first, then reflected in [state-machines.md](state-machines.md), and then surfaced in consuming code.

---

## 3. Adapter Boundary

### 3.1 Adapters Emit Facts

Adapters are fact emitters, not lifecycle authorities. When an adapter calls
back with a delivery result, the pipeline records what the adapter reported.
The adapter does not directly mutate outbox state or append receipts — the
pipeline does, based on adapter-reported facts.

### 3.2 No Lifecycle Authority

Adapters MUST NOT be treated as authoritative sources for lifecycle state.
The pipeline owns the lifecycle transitions. Adapters report transport-layer
outcomes; the pipeline classifies them, persists evidence, and transitions
operational state.

### 3.3 Honest Recording

Receipts record the adapter's reported outcome honestly. The pipeline MUST NOT
upgrade a receipt status retroactively. If the adapter reports `sent`, the
receipt says `sent`. If the adapter reports failure, the receipt says `failed`.
See [routing-delivery.md](routing-delivery.md) §13.3.

### 3.4 Async Queued Delivery Correlation

Queue-based adapters (e.g. Meshtastic) return `delivery_status="enqueued"`
from `deliver()`, meaning the payload was accepted into an adapter-local queue
but has **not** been sent to the radio. The pipeline records a `queued` receipt
and a `queued` outbox item. When the adapter-local queue later completes the
send, it reports the outcome to the pipeline via callbacks.

The pipeline uses an **internal correlation key** (`outbox_id`) to match
delayed callbacks to the exact outbox item and receipt they correspond to.
This key is:

- Generated by the pipeline before adapter delivery.
- Propagated through `TargetDeliveryService` into the `RenderingResult`.
- Stored in the queue item metadata by the adapter (never in the wire payload).
- Returned in delayed callbacks (`OutboundNativeRefRecord`,
  `QueueTerminalRecord`).

The correlation strategy for `finalize_queued_delivery` is exact only:

1. **Exact `outbox_id` + `attempt_number` correlation** (required). Looks up the outbox item
   directly and validates its status is still `queued` or `in_progress`.
   Rejects callbacks for outbox items in any other status (stale-callback
   protection). The callback **MUST** carry both `outbox_id` and
   `attempt_number`; callbacks missing either key are hard-rejected with a
   warning log and produce no supplemental receipt.
   No plan-id-only or no-key fallback path exists.

`outbox_id` and `attempt_number` are internal implementation details. They
MUST NOT appear in rendered payloads sent to external platforms (Matrix,
Meshtastic radio, MeshCore, LXMF). They are not public API.

After correlation succeeds, storage receives one validated
`QueuedDeliveryFinalization` command. The sent receipt is the source of the
event-scoped delivery identity, outbox ID, and attempt generation; the outbound
native reference must name the same event/adapter/message. Storage MUST then
re-check the full `(event, plan, adapter, channel, outbox, attempt)` identity and
atomically commit three facts: the outbound native-message reference, the new
immutable `sent` receipt, and the outbox transition to `sent`. If the guarded
outbox row is no longer finalizable, or any insert fails, none of those writes
may commit. The unavoidable external-send-to-database boundary remains an
ambiguity boundary; MEDRE does not claim exactly-once transport delivery.

### 3.4.1 Attempt Identity Reservation

Attempt identity for callbacks is durably reserved when dispatch begins, not
when the retry worker claims the row. The outbox row carries a nullable
`active_attempt` column: when set, it is the in-flight attempt; when null,
the row's `attempt_number` is the live identity and also the last finalized
attempt.

- The retry worker reserves `attempt_number + 1` via a guarded storage
  update immediately before invoking the transport, after claim
  reconciliation and after the adapter-availability and capacity gates. A
  deferral before dispatch (unavailable adapter, capacity rejection)
  therefore consumes no attempt and leaves no reservation.
- The reservation is guarded on the claiming worker owning the `in_progress`
  row with no existing reservation, and on no sibling row for the same
  event-scoped delivery identity already representing that generation or a
  newer one. This closes the retry/replay allocation race in both write
  orderings. A worker that lost its claim (lease theft or reclaim), or whose
  next generation was superseded by replay, cannot reserve and MUST NOT invoke
  the transport.
- While the reserved dispatch runs, the worker renews the claimed row's
  lease: one awaited renewal immediately after the reservation — aborting
  transport when the claim is already lost and starting the dispatch on a
  fresh lease — then periodic renewal at half the poll interval for the
  claim's lease duration. A live worker's slow transport therefore does not
  outlive its claim; lease expiry during a dispatch implies worker death or
  a renewal/storage failure, and the fences below remain the authority for
  anything a superseded worker still commits.
- From the reservation commit onward, every callback validator — queued
  delivery finalization, queue terminal reporting, and post-handoff
  observations — admits the reserved attempt number and rejects earlier
  attempts.
- Finalization consumes the reservation atomically with its outcome
  transition: `attempt_number` advances to the reserved attempt and
  `active_attempt` clears in the same guarded statement. Explicit-attempt
  commits are fenced in both reservation states: a live reservation must
  match exactly, and an unreserved row rejects any explicit attempt lower
  than its already-finalized `attempt_number`. Retry-worker transitions are
  also fenced to the current claim owner. Live-pipeline finalization is
  likewise fenced to the pipeline worker that owns the row, so a pipeline
  result returning after lease expiry cannot clear or overwrite a retry worker
  that reclaimed the row. A worker finalizing after its lease expired therefore
  cannot consume another worker's reservation, release its claim, or regress
  finalized attempt identity. A rejected guard is reported to lifecycle code as
  an uncommitted transition; runtime observability MUST NOT project it as
  durable success, retry, or dead-letter state. Terminal
  transitions that pass no attempt number (abandonment, cancellation)
  consume a live reservation too, recording the reserved attempt as
  final.
- A dispatch stamps the reserved number onto the rendered result and every
  receipt it produces, so adapter callbacks echo exactly the identity the
  outbox will admit. Receipt lineage (`parent_receipt_id`) is independent
  and still derives from the previous receipt.

Claim reconciliation uses the reservation as the discriminator for crash
recovery: a claimed row with a live reservation and persisted receipt evidence
for that attempt commits the missing outbox transition (the transport is not
invoked again). A reservation without receipt evidence is ambiguous: the prior
process may have died before transport invocation, or the transport may have
accepted the send while receipt persistence was lost. Recovery therefore
**consumes** the reserved identity as an `adapter_transient` failed attempt and
moves the row to `retry_wait`, or `dead_lettered` when the retry budget is
exhausted. A later dispatch reserves a strictly newer number. Reserved attempt
identities are never reused.

A queue terminal callback can win a narrow race after a retry dispatch returns
a `queued` receipt but before the retry worker commits its own queued outbox
transition. If that CAS is rejected, lifecycle MAY re-read the authoritative
outbox row and project an already-committed outcome only when the row is
terminal at the exact same attempt number with no live reservation. This is
unambiguous because reserved attempt identities are never reused for another
dispatch. A different attempt or a still-reserved row remains superseded and
MUST NOT be reclassified by runtime code.

### 3.5 Stale Callback Protection

A stale callback is a delayed adapter callback that arrives after the outbox
item it refers to has been reclaimed by a retry or reached a terminal state.
Stale callbacks MUST NOT finalize a different delivery attempt.

Attempt correlation in every callback path compares against the outbox row's
effective attempt — `active_attempt` while a dispatch reservation is live,
otherwise the stored `attempt_number` — so a superseded attempt becomes
stale the moment the next dispatch reserves its identity, and the reserved
attempt stays admissible for the whole handoff.

When `finalize_queued_delivery` receives a callback with an `outbox_id`
whose outbox item has a status other than `queued` or `in_progress`, the
callback is rejected: a warning is logged and no supplemental receipt is
created. This prevents an old in-memory queue callback from corrupting a
newly retried delivery attempt.

### 3.6 Terminal Queue Outcome Reporting

When a queue-based adapter cannot deliver a previously-enqueued item, it
reports a terminal outcome to the pipeline via `QueueTerminalRecord` with one
of four outcomes:

| Outcome            | Meaning                                                 |
| ------------------ | ------------------------------------------------------- |
| `exhausted`        | Local retry budget exhausted after transient failures   |
| `permanent_failed` | Permanent send failure; no retry attempted              |
| `cancelled`        | Item cancelled while in-flight (e.g. task cancellation) |
| `abandoned`        | Adapter shutdown with unsent queued items remaining     |

The pipeline maps adapter-reported facts to distinct evidence layers:

- `exhausted` / `permanent_failed`: append a `failed` **attempt** receipt, then
  a linked `dead_lettered` **lifecycle** receipt at the same attempt number;
- `cancelled`: append `cancelled` lifecycle evidence linked to the queued
  attempt;
- `abandoned`: append `abandoned` lifecycle evidence linked to the queued
  attempt.

For outbox-backed callbacks, any newly proven failed-attempt receipt, the
terminal lifecycle receipt, and the terminal outbox transition MUST commit in
one guarded storage transaction. A stale callback therefore commits none of
those writes.

That transaction crosses the storage boundary as one validated
`TerminalOutboxFinalization` command. The lifecycle receipt carries the exact
delivery identity and generation being terminalized; status, identity, attempt,
failure kind, and the bounded mutable outbox error summary are derived from it
rather than repeated as independently mutable storage arguments. This keeps
orchestration evidence and storage authority structurally incapable of
disagreeing before the compare-and-set guard is evaluated.

Adapters MUST NOT directly mutate outbox state. They report facts; the
pipeline decides lifecycle transitions.

### 3.7 Structured Execution Evidence

The target-delivery → coordinator → lifecycle boundary carries one immutable
`DeliveryExecutionEvidence` value rather than independent receipt and failure
parameters. It contains an optional attempt receipt, an optional lifecycle
authority receipt, the canonical failure kind, and the error summary. When both
receipts are present, construction MUST reject mismatched event, plan, adapter,
channel, outbox, attempt, or parent lineage.

`TargetDeliveryService` produces this evidence, `DeliveryCoordinator` transports
it without interpreting lifecycle ownership, and `DeliveryLifecycleService`
decides which evidence may become mutable outbox authority. Compatibility
entry points that return a primary receipt MAY unwrap the structured value, but
MUST NOT recreate lifecycle interpretation outside the lifecycle layer.

---

## 4. Evidence Boundary

### 4.1 Receipts Are Append-Only Evidence

Every delivery attempt produces a new `DeliveryReceipt` row. Existing receipt
rows MUST NOT be updated or deleted after creation. The `DeliveryReceipt`
dataclass is `frozen=True`. Append order is historical evidence, not sufficient
current-state authority for outbox-backed delivery: the receipt becomes current
only when its guarded outbox transition commits and the outbox row points to its
`receipt_id`. A receipt appended by a stale worker after losing that transition
remains historical evidence. Outbox-less delivery continues to use latest
append order.

Receipts remain the authoritative immutable evidence trail for audit,
diagnostics, and operator inspection; the outbox pointer selects which receipt
is the current lifecycle projection. When one execution produces both a primary `failed` attempt receipt and linked
terminal lifecycle evidence, the delivery outcome MAY retain the failed receipt
as its attempt-facing result, but the guarded terminal outbox transition MUST
point at the lifecycle receipt. For outbox-backed terminalization the lifecycle
receipt and pointer transition commit atomically; the target-delivery layer does
not pre-append that authority receipt. This keeps attempt evidence distinct from mutable
lifecycle authority and prevents a terminal outbox from projecting the
preceding non-terminal failure as current. See
[state-machines.md](state-machines.md) §1.4.

### 4.2 Outbox Is Mutable Operational State

Outbox rows are mutable operational state. They track current work in progress
and MAY be transitioned through the statuses defined in
[state-machines.md](state-machines.md) §2. Outbox rows in terminal statuses
MUST NOT be transitioned or reclaimed; they are immutable for lifecycle purposes. The `delivery_receipts` table preserves the full
evidence trail independently of outbox lifecycle. See
[state-machines.md](state-machines.md) §2.4.

### 4.3 Causal Direction

The pipeline creates an outbox item before attempting adapter delivery. Attempt
evidence may be appended before mutable-state finalization, but terminal
lifecycle authority for an outbox-backed delivery MUST be committed atomically
with the terminal outbox transition. This prevents a crash from persisting a
terminal receipt that the outbox never selected, or terminal state with no
matching lifecycle receipt. See
[state-machines.md](state-machines.md) §3.1.

### 4.4 Projections Are Read-Only

`delivery_status` views, convergence diagnostics, recovery summaries, delivery
outcome ledgers, and report dict enrichment fields are read-only projections.
They MUST NOT drive state transitions, MUST NOT write to storage, and MUST NOT
be treated as evidence of what happened — only receipts and outbox state are
evidence.

All current-delivery projections MUST use the same event-scoped identity
`(event_id, delivery_plan_id, target_adapter, target_channel)` and the same
authority rule. Outbox-backed receipts are eligible only when a matching
outbox generation commits their `receipt_id`; outbox-less receipts remain
eligible by durable append order. `route_id`, receipt `source`, and
`replay_run_id` are provenance, not lifecycle-identity dimensions. The pure
`DeliveryAuthorityResolver` is the in-memory reference implementation; SQLite
projections MUST conform to the same vectors.

`ResolvedDeliverySnapshot` is the canonical in-memory read model for one full
`DeliveryIdentity`. It carries immutable receipt history, every loaded outbox
generation, the lifecycle-authoritative receipt, current operational outbox
generation, latest dispatch-attempt evidence, and the loaded causative receipt.
Diagnostics and operator projections consume this resolved value rather than
independently joining receipt authority, outbox state, and attempt history.

---

## 5. Replay Boundary

### 5.1 Replay Creates New Attempts

Replay re-processes stored canonical events through the pipeline. Accepted replay
delivery attempts produce receipt rows with `source="replay"` and a
`replay_run_id`. A non-empty run ID suppresses a target after visible `queued` or
`sent` acceptance evidence from that same run. Other replay attempts create new
receipt rows and never modify existing receipts.

### 5.1.1 Replay Attempt Identity

Replay asks storage to allocate and insert the outbox generation atomically as
`max(existing effective_attempt) + 1` across all outbox rows sharing the same
event-scoped delivery identity (`event_id`, `delivery_plan_id`,
`target_adapter`, normalized `target_channel`). `effective_attempt` is the
row's live `active_attempt` reservation when present, otherwise its finalized
`attempt_number`. SQLite computes this value while holding the same write
transaction that inserts the replay row. Replay therefore cannot allocate a
generation already reserved or finalized by a concurrent retry, cannot reclaim
a prior retry generation, and never mutates an existing live row. The same
ownership check that applies to live delivery also applies to replay after the
fresh row is created.

### 5.2 Replay Must Not Rewrite History

Replay MUST NOT update, delete, or modify existing receipt rows. Replay MUST
NOT alter existing outbox state for live-sourced entries. Replay receipts are
distinguishable from live receipts by the `source` and `replay_run_id` fields.

### 5.3 Replay Isolation

Replay deliveries are tagged with `source="replay"` and `replay_run_id` to
maintain isolation from live delivery. Queued callbacks finalize only through
exact `outbox_id` + `attempt_number` correlation against the authoritative
outbox row, which is validated for status, event, adapter, plan, channel, and
attempt before any candidate is selected (see
[routing-delivery.md](routing-delivery.md) §8.5). A matching queued receipt's
own durable `source` / `replay_run_id` lineage is the trusted provenance for
that one attempt, so a replay-sourced candidate is finalized exactly like a
live one and its replay lineage is carried onto the supplemental `sent`
receipt. When malformed history offers duplicate queued receipts across
sources for the same row and attempt (a row is single-sourced in normal
operation), non-replay candidates are preferred. Callbacks that do not match
the validated row — stale attempts, terminal or reclaimed rows — are still
rejected with a warning; replay isolation never overrides row validation. See
[diagnostics-evidence.md](diagnostics-evidence.md) §15 for the full
requirement set.

### 5.4 Replay Non-Guarantees

Replay is operator-initiated, in-memory, and non-durable. It is not a crash
recovery mechanism, not an exactly-once delivery guarantee, and not a substitute
for live delivery. Different or empty run IDs MAY produce duplicate sends, and
concurrent executions sharing a run ID can race before acceptance evidence commits.

---

## 6. Recovery Boundary

### 6.1 Recovery Classifies and Claims Work

Startup recovery classifies non-terminal outbox items and claims them for
re-processing. Recovery actions are documented outbox transitions — they are
not delivery confirmations.

### 6.2 Recovery Must Not Invent Success

Recovery MUST NOT fabricate successful delivery outcomes. Recovery moves
outbox items from resumable states (`pending`, `retry_wait`, `in_progress`,
`queued`) back into the delivery pipeline. The pipeline then attempts delivery
and records the honest outcome. Recovery does not skip this step and does not
assume prior success.

### 6.3 Recovery Must Not Block Startup

Recovery diagnostics are read-only projections. The runtime MUST NOT block,
delay, or modify startup sequencing based on convergence severity, recovery
ownership classifications, or lifecycle convergence findings. See
[state-machines.md](state-machines.md) §6.3 and
[diagnostics-evidence.md](diagnostics-evidence.md) §21.5.

### 6.4 Recovery Ownership Evidence

Recovery ownership evidence documents what work was recovered and why. It is
an accountability mechanism, not a correctness guarantee. Recovery actions
MUST NOT be presented as proof of delivery. See
[diagnostics-evidence.md](diagnostics-evidence.md) §22.

---

## 7. Conformance Rules

1. No component MAY define a status string not present in the authoritative
   vocabularies (§2.1). New statuses MUST be added to `delivery_state.py`
   first, then to [state-machines.md](state-machines.md).

2. Receipt rows MUST NOT be updated or deleted after creation. The append-only
   invariant is absolute. See [state-machines.md](state-machines.md) §1.4.

3. Outbox transitions MUST follow the legal transitions defined in
   [state-machines.md](state-machines.md) §2.3 and the `OUTBOX_TRANSITIONS`
   table in `delivery_state.py`.

4. Terminal statuses (`sent`, `dead_lettered`, `cancelled`, `abandoned`,
   `suppressed` for receipts; `sent`, `dead_lettered`, `cancelled`, `abandoned`
   for outbox) MUST NOT have outgoing transitions.

5. Adapters MUST NOT directly mutate outbox rows or append receipt rows. The
   pipeline owns lifecycle transitions.

6. Replay MUST NOT modify existing receipt rows or live-sourced outbox state.
   Replay creates new evidence; it does not rewrite history.

7. Recovery MUST NOT fabricate delivery outcomes. Recovery reclaims work for
   re-processing; it does not assume success.

8. Projections, views, convergence diagnostics, and report dicts MUST NOT
   drive state transitions or write to storage.

9. The `delivery_status` view is read-only. Status changes MUST be effected
   by appending new receipt rows. See [routing-delivery.md](routing-delivery.md)
   §9.

10. Convergence diagnostics and lifecycle convergence findings MUST NOT repair,
    mutate, or block startup. They are detection-only systems. See
    [diagnostics-evidence.md](diagnostics-evidence.md) §21.5 and §23.3.

---

## 8. Cross-Reference Index

This document normatively references the following specifications. Conflicts
between this document and any referenced specification are defects requiring
reconciliation in the same change. Neither document silently overrides the other.

| Document                                           | Domain                                                                                       |
| -------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| [state-machines.md](state-machines.md)             | Receipt and outbox state machines, transition graphs, invariants                             |
| [routing-delivery.md](routing-delivery.md)         | Route model, fanout, retry semantics, receipt schema, delivery_status view, failure taxonomy |
| [diagnostics-evidence.md](diagnostics-evidence.md) | Convergence diagnostics, recovery evidence, lifecycle convergence, evidence bundles          |
| `src/medre/core/engine/pipeline/delivery_state.py` | Internal code source for status vocabularies and transition tables                           |
