# State Machines

Formal definition of the receipt and outbox state machines, their
transition graphs, and the causal relationship between them.

See also: [architecture.md](architecture.md), [storage.md](storage.md),
[routing-delivery.md](routing-delivery.md).

---

## 1. Receipt State Machine

### 1.1 Statuses

The receipt state machine has five terminal and non-terminal statuses:

| Status          | Terminal | Meaning                                                                                        |
| --------------- | -------- | ---------------------------------------------------------------------------------------------- |
| `queued`        | No       | Adapter accepted the event into a local send queue.                                            |
| `sent`          | Yes      | Adapter reported successful handoff to the transport layer.                                    |
| `failed`        | No       | Delivery attempt failed. May be followed by retry or dead letter.                              |
| `dead_lettered` | Yes      | All retry attempts exhausted or terminal failure.                                              |
| `suppressed`    | Yes      | Delivery was suppressed by loop prevention, policy, capacity rejection, or shutdown rejection. |

### 1.2 Transition Graph

```text
                        ┌──────────┐
                        │  queued  │
                        └────┬─────┘
                             │ (queue-based adapter confirms send)
                             ▼
              ┌──────────────────────────┐
              │          sent            │ ◄── terminal
              └──────────────────────────┘

              ┌──────────────────────────┐
              │         failed           │──┐
              └──────────────────────────┘  │
                     │                      │
                     │ (retry scheduled)    │ (retries exhausted
                     ▼                      │  or terminal error)
              ┌──────────────────────────┐  │
              │         failed           │  │  (subsequent attempt)
              └──────────────────────────┘  │
                     │                      │
                     │                      ▼
              ┌──────────────────────────┐
              │      dead_lettered       │ ◄── terminal
              └──────────────────────────┘

              ┌──────────────────────────┐
              │       suppressed         │ ◄── terminal
              └──────────────────────────┘
```

Each delivery attempt produces a new receipt row. Receipts are never updated
or deleted — the latest receipt for a given **delivery chain** determines the
current delivery status of that chain. A delivery chain is identified by
`delivery_plan_id`, `target_adapter`, and `target_channel`; retry lineage
is linked by `parent_receipt_id`. Event-level status is an aggregate
reporting view, not the primitive current-status key.

### 1.3 Legal Transitions

Receipts are append-only. There is no explicit transition table because every
receipt is a new row. The implicit transition is temporal: receipt N+1
supersedes receipt N for the same delivery chain.

| From (prev receipt status) | To (next receipt status) | Condition                                      |
| -------------------------- | ------------------------ | ---------------------------------------------- |
| —                          | `queued`                 | Queue-based adapter accepts event              |
| —                          | `sent`                   | Synchronous adapter reports successful handoff |
| —                          | `failed`                 | Adapter raises transient or permanent error    |
| —                          | `suppressed`             | Loop/policy/capacity/shutdown suppression      |
| `queued`                   | `sent`                   | Queue-based adapter reports native message ID  |
| `failed`                   | `failed`                 | Retry attempt also fails                       |
| `failed`                   | `dead_lettered`          | Retry exhausted                                |

Receipts with `parent_receipt_id = None` are initial attempts. Subsequent
receipts in a retry chain set `parent_receipt_id` to the previous receipt's
`receipt_id` and increment `attempt_number`.

### 1.4 Invariant: Append-Only

> Every receipt is append-only. No `DeliveryReceipt` row is ever updated or
> deleted after creation. The `DeliveryReceipt` struct is `frozen=True`
> (immutable at the Python level). Current delivery status is derived by
> reading the latest receipt for a given delivery chain, not by mutation.

### 1.5 Retry Scheduling

Receipt rows are append-only. The storage API exposes no method that mutates
existing delivery-receipt rows. Retry scheduling is represented by fields
(`next_retry_at`, `retry_max_attempts`, `retry_backoff_base`) set when a
receipt row is appended — these fields are never modified after creation.

### 1.6 Dead-Letter Attempt Convention

When retries are exhausted and a dead-letter receipt is created, the
`attempt_number` field follows a chain-closing convention:

- `should_dead_letter()` is called with the `attempt_number` of the **failed**
  receipt (the attempt that just failed).
- `RetryExecutor.is_exhausted(attempt_number)` determines whether that
  attempt exhausts the configured `max_attempts`.
- The dead-letter receipt receives `attempt_number + 1` — one more than the
  failed attempt that triggered dead-lettering. This makes the dead-letter
  receipt the chain-closing row whose attempt number accounts for the
  dead-lettering step itself.

Example: if `max_attempts = 3`, the failed receipt at `attempt_number = 3`
triggers `should_dead_letter() → True`, and the dead-letter receipt is
appended with `attempt_number = 4`.

### 1.7 Status Vocabulary

The receipt status vocabulary is closed: `queued`, `sent`, `failed`,
`dead_lettered`, `suppressed`. No other status labels are valid in current
MEDRE receipt semantics. Status values are enforced by the `DeliveryReceipt`
type at construction time.

---

## 2. Outbox State Machine

### 2.1 Statuses

The outbox state machine has eight statuses:

| Status          | Terminal | Meaning                                                            |
| --------------- | -------- | ------------------------------------------------------------------ |
| `pending`       | No       | Work exists but has not started.                                   |
| `in_progress`   | No       | Claimed by a worker for active delivery processing.                |
| `queued`        | No       | Handed to adapter-local queue (e.g., Meshtastic send queue).       |
| `sent`          | Yes      | Operational work item completed after adapter-reported handoff.    |
| `retry_wait`    | No       | Transient failure; awaiting next scheduled retry attempt.          |
| `dead_lettered` | Yes      | Retries exhausted or terminal failure.                             |
| `cancelled`     | Yes      | Explicit operator cancellation (not automatic shutdown).           |
| `abandoned`     | Yes      | Drain-timeout abandonment of tracked in-flight adapter deliveries. |

### 2.2 Transition Graph

```text
  ┌─────────┐     claim      ┌──────────────┐
  │ pending │ ──────────────► │ in_progress  │ ◄─── lease renewal
  └─┬───┬───┘                └──┬─┬─┬─┬─┬────┘
    │   │                       │ │ │ │ │
    │   │ cancel                │ │ │ │ │
    │   ▼                       │ │ │ │ │
    │  ┌────────────┐          │ │ │ │ │
    │  │ cancelled  │          │ │ │ │ │
    │  │ (terminal) │          │ │ │ │ │
    │  └────────────┘          │ │ │ │ │
    │                          │ │ │ │ │
    │  ┌────────────┐          │ │ │ │ │
    │  │ abandoned  │ ◄─ drain ─┘ │ │ │ │
    │  │ (terminal) │             │ │ │ │
    │  └────────────┘             │ │ │ │
    │                             │ │ │ │
    │  abandon (explicit)         │ │ │ │
    └─►┌────────────┐             │ │ │ │
       │ abandoned  │             │ │ │ │
       │ (terminal) │             │ │ │ │
       └────────────┘             │ │ │ │
                                  │ │ │ │
  ┌────────────┐  cancel          │ │ │ │
  │  queued    │ ◄────────────────┘ │ │ │
  └─┬────┬─────┘                    │ │ │
     │    │  stale queued reclaim   │ │ │
     │    └────────────────────────►│ │ │
     │                               │ │ │
     │  adapter reports handoff      │ │ │
     ▼                               │ │ │
  ┌────────────┐                     │ │ │
  │    sent    │ ◄───────────────────┘ │ │
  │ (terminal) │  (success)            │ │
  └────────────┘                       │ │
                                        │ │
  ┌────────────┐  cancel (explicit)    │ │
  │ cancelled  │ ◄─── retry_wait ──────┤ │
  │ (terminal) │                       │ │
  └────────────┘                       │ │
                                        │ │
  ┌────────────┐                       │ │
  │ retry_wait │ ◄── transient ────────┘ │
  │            │     failure               │
  └──┬──┬──────┘                          │
     │  │  claim_due_outbox_items()        │
     │  └──────► in_progress              │
     │                                    │
     │  cancel (explicit)                 │ dead-letter
     ▼                                    ▼
  ┌────────────┐                ┌──────────────┐
  │ cancelled  │                │ dead_lettered │ ◄── in_progress
  │ (terminal) │                │  (terminal)   │     or retry_wait
  └────────────┘                └───────────────┘

  ┌────────────┐
  │ abandoned  │ ◄── abandon (explicit) ── retry_wait
  │ (terminal) │ ◄── abandon (explicit) ── queued
  └────────────┘
```

### 2.3 Legal Transitions

| From          | To              | Method                        | Condition                                                                                               |
| ------------- | --------------- | ----------------------------- | ------------------------------------------------------------------------------------------------------- |
| —             | `in_progress`   | `create_outbox_item()`        | Pipeline claims delivery slot                                                                           |
| `in_progress` | `queued`        | `mark_outbox_queued()`        | Adapter-local queue acceptance                                                                          |
| `in_progress` | `pending`       | `release_outbox_claim()`      | Worker releases claim without delivery                                                                  |
| `in_progress` | `sent`          | `mark_outbox_sent()`          | Adapter reports successful handoff                                                                      |
| `queued`      | `sent`          | `mark_outbox_sent()`          | Queue-based adapter confirms send                                                                       |
| `in_progress` | `retry_wait`    | `mark_outbox_retry_wait()`    | Transient failure, retry scheduled                                                                      |
| `in_progress` | `dead_lettered` | `mark_outbox_dead_lettered()` | Terminal failure or no retry policy                                                                     |
| `retry_wait`  | `dead_lettered` | `mark_outbox_dead_lettered()` | Terminal failure after retry                                                                            |
| `pending`     | `cancelled`     | `mark_outbox_cancelled()`     | Explicit operator action (not automatic shutdown)                                                       |
| `in_progress` | `cancelled`     | `mark_outbox_cancelled()`     | Explicit operator action (not automatic shutdown)                                                       |
| `retry_wait`  | `cancelled`     | `mark_outbox_cancelled()`     | Explicit operator action (not automatic shutdown)                                                       |
| `queued`      | `cancelled`     | `mark_outbox_cancelled()`     | Explicit operator action (not automatic shutdown)                                                       |
| `in_progress` | `abandoned`     | `mark_outbox_abandoned()`     | Drain-timeout abandonment of tracked in-flight adapter delivery (not automatic shutdown)                |
| `pending`     | `abandoned`     | `mark_outbox_abandoned()`     | Drain-timeout abandonment of orphaned or unrecoverable delivery state (not automatic graceful shutdown) |
| `retry_wait`  | `abandoned`     | `mark_outbox_abandoned()`     | Drain-timeout abandonment of orphaned or unrecoverable delivery state (not automatic graceful shutdown) |
| `queued`      | `abandoned`     | `mark_outbox_abandoned()`     | Drain-timeout abandonment of orphaned or unrecoverable delivery state (not automatic graceful shutdown) |
| `retry_wait`  | `in_progress`   | `claim_due_outbox_items()`    | Due retry_wait outbox item reclaimed                                                                    |
| `pending`     | `in_progress`   | `claim_due_outbox_items()`    | Worker claims pending outbox item                                                                       |
| `queued`      | `in_progress`   | `claim_due_outbox_items()`    | Stale queued reclaim after grace period                                                                 |

Terminal statuses (`sent`, `dead_lettered`, `cancelled`, `abandoned`) have no
outgoing transitions. The storage layer enforces `allowed_from` guards on
every transition method.

> **Authoritative source:** The `OUTBOX_TRANSITIONS` table in
> `delivery_state.py` (§4) is the authoritative internal transition table.
> §2.3 is a human-readable rendering that must be kept in sync when
> transitions are added or changed.

#### Stale Queued Reclaim

The `queued` → `in_progress` transition is a **reclaim** path, not a direct
claim. It occurs when `claim_due_outbox_items()` detects outbox rows in
`status = 'queued'` whose `updated_at` is older than the configured grace
threshold (`STALE_QUEUED_GRACE_SECONDS`, default 300 s). This reclaims items
that were handed to an adapter-local queue but never reached `sent` — for
example because the worker crashed or the adapter lost the queued message.

**Direct claimability and stale reclaim are different concepts:**

- **Directly claimable** statuses (`pending`, `retry_wait`) can be claimed by
  any worker at any time. `is_claimable_outbox_status()` returns `True`.
- **Stale reclaim** statuses (`in_progress` with expired lease, `queued` past
  the grace threshold) require storage-level staleness queries and are not
  reflected by `is_claimable_outbox_status()`. `queued` remains **not**
  directly claimable.

### 2.4 Mutable Operational State

> Outbox rows are mutable operational state for **non-terminal** statuses.
> Terminal outbox rows (`sent`, `dead_lettered`, `cancelled`, `abandoned`)
> MUST NOT be transitioned, reclaimed, deleted, or replaced. A new delivery
> after terminal state MUST use a new attempt identity (new
> `delivery_plan_id` and/or new `attempt_number`). The `delivery_receipts`
> table preserves the full evidence trail independently of outbox lifecycle.

### 2.5 Graceful Shutdown Behavior

When the runtime shuts down gracefully, the outbox is not mutated. Non-terminal
outbox rows survive in SQLite and are processed on next startup through the
normal reclaim and dispatch paths: due retry receipts (where `next_retry_at` has passed) by the RetryWorker,
due outbox items (`pending`, `retry_wait`, expired `in_progress`, stale `queued`) by `claim_due_outbox_items()`. The shutdown evidence model
(`ShutdownEvidence`) records `resume_expected=True` when pending non-terminal
work exists, and `outbox_shutdown_policy="resumable"` to signal the resumable
policy is active.

The table below describes what happens to each outbox status during graceful
shutdown:

| Outbox status at shutdown | Classification       | Shutdown action                                                  | Restart recovery                                                      |
| ------------------------- | -------------------- | ---------------------------------------------------------------- | --------------------------------------------------------------------- |
| `pending`                 | Resumable            | No mutation. Row preserved.                                      | Claimed by `claim_due_outbox_items()` on next startup.                |
| `retry_wait`              | Resumable            | No mutation. Row preserved.                                      | Due retry_wait outbox items reclaimed via `claim_due_outbox_items()`. |
| `in_progress`             | Resumable            | No mutation. Row preserved. Lease may expire during shutdown.    | Expired lease reclaimed by `claim_due_outbox_items()`.                |
| `queued`                  | Resumable            | No mutation. Row preserved.                                      | Stale queued reclaim after grace period.                              |
| `sent`                    | Terminal (no action) | Already final. No shutdown interaction.                          | N/A.                                                                  |
| `dead_lettered`           | Terminal (no action) | Already final. No shutdown interaction.                          | N/A.                                                                  |
| `cancelled`               | Terminal (no action) | Already final. Set by explicit operator action, not by shutdown. | N/A.                                                                  |
| `abandoned`               | Terminal (no action) | Already final. Set during drain timeout for in-flight items.     | N/A.                                                                  |

Cancellation (`cancelled`) and abandonment (`abandoned`) are distinct terminal
states. They are not automatically applied to non-terminal outbox work during
graceful shutdown. Cancellation requires explicit operator action; abandonment
is set for in-flight deliveries that exceed the drain timeout.

For in-flight adapter deliveries (those actively executing an adapter `deliver()`
call when shutdown begins), the drain period allows completion. Deliveries
that complete during drain produce normal receipts. Deliveries abandoned after
the drain timeout expires produce suppressed receipts with error
`shutdown_drain_timeout`.

New deliveries submitted to the pipeline after shutdown has begun are rejected
immediately. These produce suppressed receipts with error
`delivery_rejected_shutdown` — no outbox item is created and no adapter
interaction occurs.

---

## 3. Relationship Between Machines

### 3.1 Causal Direction

Outbox transitions drive receipt creation, never the reverse. The pipeline
creates an outbox item (status `in_progress`) before attempting adapter
delivery. On completion, it:

1. Appends a `DeliveryReceipt` to storage.
2. Updates the outbox item status based on the delivery outcome.

A receipt is the immutable evidence record. An outbox item is the mutable
operational tracker.

### 3.2 Foreign Key Linkage

The `receipt_id` field on `DeliveryOutboxItem` is the causal link between the
two machines. Each outbox status transition records the `receipt_id` of the
corresponding receipt. This enables:

- Tracing from outbox state to the evidence that produced it.
- Reconstructing the full delivery history from receipts alone (outbox is
  secondary).

### 3.3 Terminal State Correspondence

| Outbox Terminal | Receipt Terminal  | Condition                                                                                                                                       |
| --------------- | ----------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| `sent`          | `sent`            | Successful delivery                                                                                                                             |
| `sent`          | `queued` → `sent` | Queue-based: initial queued, then sent on confirmation                                                                                          |
| `dead_lettered` | `dead_lettered`   | Retry exhaustion or terminal failure                                                                                                            |
| `cancelled`     | —                 | No receipt produced (pre-delivery)                                                                                                              |
| `abandoned`     | `suppressed`      | **Shutdown drain-timeout** abandonment produces a suppressed receipt with `failure_kind="shutdown_rejection"`, `error="shutdown_drain_timeout"` |
| —               | `suppressed`      | New delivery rejected during shutdown (no outbox item created); receipt with `error="delivery_rejected_shutdown"`                               |

### 3.4 Implicit Suppression Paths

Events may be suppressed without producing a receipt. These paths are by
design and are documented here for completeness.

#### Native-Ref Dedup (Stage: Dedup)

When the dedup stage detects a duplicate native-message ref, the event is
suppressed **before storage**. No `DeliveryReceipt` is created because the
event was never stored. Evidence of suppression is recorded in
`RuntimeAccounting` counters only.

```text
ingress → dedup (duplicate detected) → return []  [no receipt, no storage]
```

#### Reaction-to-Reaction Suppression (Stage: Post-Store)

When a `MESSAGE_REACTED` event targets another reaction event, the pipeline
stores the event but skips routing. No `DeliveryReceipt` is created because
no delivery was attempted. The event is visible in storage for audit purposes.

```text
ingress → dedup → resolve_relations → store → reaction-to-reaction check → return []  [no receipt]
```

---

## 4. Internal Source of Truth

### 4.1 delivery_state Module

The module `src/medre/core/engine/pipeline/delivery_state.py` is the internal source of truth for status vocabularies, terminal/claimable/accepted classification sets, and observed transition tables. It is a leaf module with no external imports.

The module defines four status vocabularies as `frozenset` constants:

| Constant                    | Values                                                                                              | Used by                          |
| --------------------------- | --------------------------------------------------------------------------------------------------- | -------------------------------- |
| `RECEIPT_STATUSES`          | `queued`, `sent`, `failed`, `dead_lettered`, `suppressed`                                           | `DeliveryReceipt.status`         |
| `OUTBOX_STATUSES`           | `pending`, `in_progress`, `queued`, `sent`, `retry_wait`, `dead_lettered`, `cancelled`, `abandoned` | `DeliveryOutboxItem.status`      |
| `OUTCOME_STATUSES`          | `success`, `queued`, `transient_failure`, `permanent_failure`, `skipped`                            | `DeliveryOutcome.status`         |
| `ADAPTER_DELIVERY_STATUSES` | `sent`, `enqueued`                                                                                  | `OutboundResult.delivery_status` |

Classification subsets:

| Constant                        | Subset of          | Values                                            |
| ------------------------------- | ------------------ | ------------------------------------------------- |
| `TERMINAL_RECEIPT_STATUSES`     | `RECEIPT_STATUSES` | `sent`, `dead_lettered`, `suppressed`             |
| `NON_TERMINAL_RECEIPT_STATUSES` | `RECEIPT_STATUSES` | `queued`, `failed`                                |
| `TERMINAL_OUTBOX_STATUSES`      | `OUTBOX_STATUSES`  | `sent`, `dead_lettered`, `cancelled`, `abandoned` |
| `NON_TERMINAL_OUTBOX_STATUSES`  | `OUTBOX_STATUSES`  | `pending`, `in_progress`, `queued`, `retry_wait`  |
| `CLAIMABLE_OUTBOX_STATUSES`     | `OUTBOX_STATUSES`  | `pending`, `retry_wait`                           |
| `ACCEPTED_OUTCOME_STATUSES`     | `OUTCOME_STATUSES` | `success`, `queued`                               |

Transition tables are declarative `dict[str, frozenset[str]]` mappings. Terminal statuses have no outgoing entries. The tables are consumed by `validate_receipt_transition()` and `validate_outbox_transition()` helpers, which return `bool` without raising exceptions.

The outbox transition table is aligned with §2.3 Legal Transitions. Notable entries include: `pending` → `cancelled` (explicit operator action, not automatic shutdown); `in_progress` → `abandoned` (drain-timeout abandonment of tracked in-flight adapter deliveries); `pending` → `abandoned`, `retry_wait` → `abandoned`, `queued` → `abandoned` (drain-timeout abandonment of orphaned or unrecoverable delivery state — not automatic graceful shutdown); `in_progress` → `pending` (claim release); `queued` → `cancelled` (explicit operator action); `queued` → `in_progress` (stale queued reclaim after grace period); `retry_wait` → `cancelled` (explicit operator action). Graceful shutdown does **not** automatically transition non-terminal outbox rows to `cancelled` or `abandoned`. While the code permits `abandoned` transitions from any non-terminal state (`pending`, `in_progress`, `retry_wait`, `queued`), in practice they represent explicit drain-timeout or unrecoverable-state handling, not automatic graceful shutdown behavior.

### 4.2 Design Constraints

The `delivery_state` module follows these constraints:

1. **No enums.** Statuses are plain strings throughout the codebase.
2. **No state-machine engine.** Transition tables are declarative; they do not drive behavior.
3. **No exceptions.** Validation helpers return `bool`; callers decide how to handle invalid states.
4. **No external imports.** The module must not import from storage, pipeline, or planning layers. This avoids circular import chains.

---

## 5. Conformance Requirements

1. The pipeline MUST NOT create receipt rows with statuses not listed in
   §1.1.
2. The pipeline MUST NOT transition outbox items through paths not listed in
   §2.3.
3. Every receipt row MUST have `frozen=True` semantics — no mutation after
   creation.
4. Outbox items in terminal statuses MUST NOT have outgoing transitions.
5. Implicit suppression paths (§3.4) MUST NOT produce `DeliveryReceipt` rows.

## 6. Startup Ownership of Persisted State

### 6.1 Persisted State Machines

The two persisted state machines that survive across process restarts are:

1. **Outbox items** (`delivery_outbox`): mutable operational state with status transitions.
2. **Delivery receipts** (`delivery_receipts`): append-only evidence trail.

Receipts are never updated or deleted. Outbox items transition through the statuses defined in §2.1. Both are stored in SQLite and survive process crashes and graceful shutdowns.

### 6.2 Startup Ownership Rules

When the runtime starts, it reclaims ownership of non-terminal outbox items according to their status:

| Outbox status at startup | Startup ownership action                                                                                              |
| ------------------------ | --------------------------------------------------------------------------------------------------------------------- |
| `pending`                | Eligible for immediate claim by `claim_due_outbox_items()`. No grace period required.                                 |
| `retry_wait`             | Due retry_wait outbox items reclaimed by `claim_due_outbox_items()` when `next_retry_at` has passed. Otherwise waits. |
| `in_progress`            | Lease may have expired during prior shutdown. Reclaimed by `claim_due_outbox_items()` after lease expiry.             |
| `queued`                 | Reclaimed by stale queued reclaim after `STALE_QUEUED_GRACE_SECONDS` (default 300 s) has elapsed.                     |
| `sent`                   | Terminal. No startup action.                                                                                          |
| `dead_lettered`          | Terminal. No startup action.                                                                                          |
| `cancelled`              | Terminal. No startup action.                                                                                          |
| `abandoned`              | Terminal. No startup action.                                                                                          |

Startup does not block on convergence diagnostics. Non-terminal items are reclaimed lazily through the normal `claim_due_outbox_items()` path, not by a startup-time state sweep.

### 6.3 Recovery Convergence and Startup

Convergence diagnostics (see Diagnostics and Evidence Specification §21) are read-only projections derived from outbox and receipt state. They do not drive startup behavior. The runtime does not block, delay, or modify startup sequencing based on convergence severity. Operators inspect convergence diagnostics output after startup to identify and manually address state discrepancies.

Lifecycle delivery convergence diagnostics (see Diagnostics and Evidence Specification §23) provide finer-grained detection of specific contradictions between the two state machines: receipt/outbox status mismatches (§23.6), retry metadata anomalies in `retry_wait` outbox items (§23.7), stalled non-terminal outbox items (§23.9), attempt count regressions in receipt chains (§23.10), and receipt sequence gaps (§23.11). Like convergence summary diagnostics, lifecycle convergence findings are detection-only and do not drive startup or worker behavior.
