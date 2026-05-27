# State Machines

Formal definition of the receipt and outbox state machines, their
transition graphs, and the causal relationship between them.

See also: [architecture.md](architecture.md), [storage.md](storage.md),
[routing-delivery.md](routing-delivery.md).

---

## 1. Receipt State Machine

### 1.1 Statuses

The receipt state machine has five terminal and non-terminal statuses:

| Status          | Terminal | Meaning                                                                    |
| --------------- | -------- | -------------------------------------------------------------------------- |
| `queued`        | No       | Adapter accepted the event into a local send queue.                        |
| `sent`          | Yes      | Adapter confirmed delivery to the transport.                               |
| `failed`        | No       | Delivery attempt failed. May be followed by retry or dead letter.          |
| `dead_lettered` | Yes      | All retry attempts exhausted or terminal failure.                          |
| `suppressed`    | Yes      | Delivery was suppressed by loop prevention, policy, or capacity rejection. |

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
or deleted — the latest receipt for a given `(event_id, target_adapter)` pair
determines the current delivery status.

### 1.3 Legal Transitions

Receipts are append-only. There is no explicit transition table because every
receipt is a new row. The implicit transition is temporal: receipt N+1
supersedes receipt N for the same delivery chain.

| From (prev receipt status) | To (next receipt status) | Condition                                     |
| -------------------------- | ------------------------ | --------------------------------------------- |
| —                          | `queued`                 | Queue-based adapter accepts event             |
| —                          | `sent`                   | Synchronous adapter confirms delivery         |
| —                          | `failed`                 | Adapter raises transient or permanent error   |
| —                          | `suppressed`             | Loop/policy/capacity suppression              |
| `queued`                   | `sent`                   | Queue-based adapter reports native message ID |
| `failed`                   | `failed`                 | Retry attempt also fails                      |
| `failed`                   | `dead_lettered`          | Retry exhausted                               |

Receipts with `parent_receipt_id = None` are initial attempts. Subsequent
receipts in a retry chain set `parent_receipt_id` to the previous receipt's
`receipt_id` and increment `attempt_number`.

### 1.4 Invariant: Append-Only

> Every receipt is append-only. No `DeliveryReceipt` row is ever updated or
> deleted after creation. The `DeliveryReceipt` struct is `frozen=True`
> (immutable at the Python level). Current delivery status is derived by
> reading the latest receipt for a given delivery chain, not by mutation.

### 1.5 Historical Note: `update_retry_due()` Removal

Prior to this specification, `update_retry_due()` mutated the `next_retry_at`
field on existing receipt rows. This was removed. Capacity rejection now
creates a new receipt row with `status="suppressed"` instead of mutating the
original receipt. The `next_retry_at` field is populated only at receipt
creation time and is never modified.

### 1.6 Receipt Statuses Not in Active Use

The `DeliveryReceipt` type definition includes `"accepted"` and `"confirmed"`
in its status literal. These statuses are reserved for future use and are not
produced by any current code path. They MUST NOT be treated as valid current
statuses for conformance or diagnostic purposes.

---

## 2. Outbox State Machine

### 2.1 Statuses

The outbox state machine has eight statuses:

| Status          | Terminal | Meaning                                                      |
| --------------- | -------- | ------------------------------------------------------------ |
| `pending`       | No       | Work exists but has not started.                             |
| `in_progress`   | No       | Claimed by a worker for active delivery processing.          |
| `queued`        | No       | Handed to adapter-local queue (e.g., Meshtastic send queue). |
| `sent`          | Yes      | Adapter confirmed delivery to the transport.                 |
| `retry_wait`    | No       | Transient failure; awaiting next scheduled retry attempt.    |
| `dead_lettered` | Yes      | Retries exhausted or terminal failure.                       |
| `cancelled`     | Yes      | Operator or shutdown cancelled the delivery.                 |
| `abandoned`     | Yes      | Drain timeout or ambiguous loss during shutdown.             |

### 2.2 Transition Graph

```text
  ┌─────────┐     claim      ┌──────────────┐
  │ pending │ ──────────────► │ in_progress  │ ◄─── lease renewal
  └────┬────┘                └──┬─┬─┬─┬─┬────┘
       │                        │ │ │ │ │
       │ cancel / abandon       │ │ │ │ │
       ▼                        │ │ │ │ │
  ┌────────────┐               │ │ │ │ │
  │ cancelled  │               │ │ │ │ │
  │ (terminal) │               │ │ │ │ │
  └────────────┘               │ │ │ │ │
                               │ │ │ │ │
  ┌────────────┐               │ │ │ │ │
  │ abandoned  │ ◄── drain ────┘ │ │ │ │
  │ (terminal) │                │ │ │ │
  └────────────┘                │ │ │ │
                                │ │ │ │
  ┌────────────┐  cancel/abandon │ │ │ │
  │  queued    │ ◄──────────────┘ │ │ │
  └────┬───────┘                  │ │ │
       │  adapter confirms        │ │ │
       ▼                          │ │ │
  ┌────────────┐                  │ │ │
  │    sent    │ ◄────────────────┘ │ │
  │ (terminal) │  (success)         │ │
  └────────────┘                    │ │
                                    │ │
  ┌────────────┐                    │ │
  │ retry_wait │ ◄── transient ─────┘ │
  │            │     failure           │
  └────┬───────┘                       │
       │  claim (retry worker)         │
       └──────► in_progress            │
       │                               │
       │  cancel / abandon             │ dead-letter
       ▼                               ▼
  ┌────────────┐               ┌──────────────┐
  │ cancelled  │               │ dead_lettered │ ◄── in_progress
  │ (terminal) │               │  (terminal)   │     or retry_wait
  └────────────┘               └───────────────┘
```

### 2.3 Legal Transitions

| From          | To              | Method                        | Condition                           |
| ------------- | --------------- | ----------------------------- | ----------------------------------- |
| —             | `in_progress`   | `create_outbox_item()`        | Pipeline claims delivery slot       |
| `in_progress` | `queued`        | `mark_outbox_queued()`        | Adapter-local queue acceptance      |
| `in_progress` | `sent`          | `mark_outbox_sent()`          | Adapter confirms delivery           |
| `queued`      | `sent`          | `mark_outbox_sent()`          | Queue-based adapter confirms send   |
| `in_progress` | `retry_wait`    | `mark_outbox_retry_wait()`    | Transient failure, retry scheduled  |
| `in_progress` | `dead_lettered` | `mark_outbox_dead_lettered()` | Terminal failure or no retry policy |
| `retry_wait`  | `dead_lettered` | `mark_outbox_dead_lettered()` | Terminal failure after retry        |
| `pending`     | `cancelled`     | `mark_outbox_cancelled()`     | Operator or shutdown cancellation   |
| `in_progress` | `cancelled`     | `mark_outbox_cancelled()`     | Operator or shutdown cancellation   |
| `retry_wait`  | `cancelled`     | `mark_outbox_cancelled()`     | Operator or shutdown cancellation   |
| `queued`      | `cancelled`     | `mark_outbox_cancelled()`     | Operator or shutdown cancellation   |
| `pending`     | `abandoned`     | `mark_outbox_abandoned()`     | Drain timeout or ambiguous loss     |
| `in_progress` | `abandoned`     | `mark_outbox_abandoned()`     | Drain timeout or ambiguous loss     |
| `retry_wait`  | `abandoned`     | `mark_outbox_abandoned()`     | Drain timeout or ambiguous loss     |
| `queued`      | `abandoned`     | `mark_outbox_abandoned()`     | Drain timeout or ambiguous loss     |
| `retry_wait`  | `in_progress`   | `claim_due_outbox_items()`    | Retry worker reclaims the item      |

Terminal statuses (`sent`, `dead_lettered`, `cancelled`, `abandoned`) have no
outgoing transitions. The storage layer enforces `allowed_from` guards on
every transition method.

### 2.4 Mutable Operational State

> Outbox rows are mutable operational state. They MAY be reclaimed or replaced
> for terminal statuses. The `delivery_receipts` table preserves the full
> evidence trail independently of outbox lifecycle.

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

| Outbox Terminal | Receipt Terminal  | Condition                                              |
| --------------- | ----------------- | ------------------------------------------------------ |
| `sent`          | `sent`            | Successful delivery                                    |
| `sent`          | `queued` → `sent` | Queue-based: initial queued, then sent on confirmation |
| `dead_lettered` | `dead_lettered`   | Retry exhaustion or terminal failure                   |
| `cancelled`     | —                 | No receipt produced (pre-delivery)                     |
| `abandoned`     | —                 | No receipt produced (pre-delivery)                     |

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

## 4. Conformance Requirements

1. The pipeline MUST NOT create receipt rows with statuses not listed in
   §1.1.
2. The pipeline MUST NOT transition outbox items through paths not listed in
   §2.3.
3. Every receipt row MUST have `frozen=True` semantics — no mutation after
   creation.
4. Outbox items in terminal statuses MUST NOT have outgoing transitions.
5. Implicit suppression paths (§3.4) MUST NOT produce `DeliveryReceipt` rows.
