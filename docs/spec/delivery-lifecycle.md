# Delivery Lifecycle Authority Model

> **Status:** Active
> **Classification:** Normative
> **Authority:** Authoritative specification for the MEDRE delivery lifecycle authority hierarchy, vocabulary provenance, boundary definitions, replay/recovery constraints, and conformance rules.
> **Last reviewed:** 2026-06-04

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

---

## 2. Authoritative vs Derived Vocabularies

### 2.1 Authoritative Vocabularies

These vocabularies are defined in `delivery_state.py` (§4 of
[state-machines.md](state-machines.md)) and are the normative source:

| Vocabulary                    | Constant                        | Values                                                                                              |
| ----------------------------- | ------------------------------- | --------------------------------------------------------------------------------------------------- |
| Receipt statuses              | `RECEIPT_STATUSES`              | `queued`, `sent`, `failed`, `dead_lettered`, `suppressed`                                           |
| Outbox statuses               | `OUTBOX_STATUSES`               | `pending`, `in_progress`, `queued`, `sent`, `retry_wait`, `dead_lettered`, `cancelled`, `abandoned` |
| Outcome statuses              | `OUTCOME_STATUSES`              | `success`, `queued`, `transient_failure`, `permanent_failure`, `skipped`                            |
| Adapter delivery statuses     | `ADAPTER_DELIVERY_STATUSES`     | `sent`, `enqueued`                                                                                  |
| Terminal receipt statuses     | `TERMINAL_RECEIPT_STATUSES`     | `sent`, `dead_lettered`, `suppressed`                                                               |
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

| Derived artifact                                        | Source                                                   | Defined in                                               |
| ------------------------------------------------------- | -------------------------------------------------------- | -------------------------------------------------------- |
| `delivery_status` SQL view                              | Latest receipt per `(delivery_plan_id, target_adapter)`  | [routing-delivery.md](routing-delivery.md) §9            |
| Convergence severity (`safe`/`degraded`/`inconsistent`) | Cross-reference of outbox + receipt statuses             | [diagnostics-evidence.md](diagnostics-evidence.md) §21   |
| Recovery ownership statuses                             | Classification of outbox items at startup                | [diagnostics-evidence.md](diagnostics-evidence.md) §22   |
| Health vocabulary (`healthy`/`degraded`/etc.)           | Adapter diagnostics projection                           | [diagnostics-evidence.md](diagnostics-evidence.md) §5    |
| Report dict enrichment fields                           | Parsed from receipt `error` and `rendering_evidence`     | [diagnostics-evidence.md](diagnostics-evidence.md) §17.2 |
| Delivery outcome ledger                                 | Grouped projection over receipts and outbox              | [diagnostics-evidence.md](diagnostics-evidence.md) §19   |
| Lifecycle convergence findings                          | Detection-only analysis of receipt/outbox contradictions | [diagnostics-evidence.md](diagnostics-evidence.md) §23   |

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

---

## 4. Evidence Boundary

### 4.1 Receipts Are Append-Only Evidence

Every delivery attempt produces a new `DeliveryReceipt` row. Existing receipt
rows MUST NOT be updated or deleted after creation. The `DeliveryReceipt`
dataclass is `frozen=True`. Current delivery status is derived by reading the
latest receipt for a delivery chain, not by mutation.

Receipts are the authoritative evidence trail for audit, diagnostics, and
operator inspection. See [state-machines.md](state-machines.md) §1.4.

### 4.2 Outbox Is Mutable Operational State

Outbox rows are mutable operational state. They track current work in progress
and MAY be transitioned through the statuses defined in
[state-machines.md](state-machines.md) §2. Outbox rows in terminal statuses
MUST NOT be transitioned or reclaimed; they are immutable for lifecycle purposes. The `delivery_receipts` table preserves the full
evidence trail independently of outbox lifecycle. See
[state-machines.md](state-machines.md) §2.4.

### 4.3 Causal Direction

Outbox transitions drive receipt creation, never the reverse. The pipeline
creates an outbox item before attempting adapter delivery. On completion, it
appends a receipt and then updates the outbox. See
[state-machines.md](state-machines.md) §3.1.

### 4.4 Projections Are Read-Only

`delivery_status` views, convergence diagnostics, recovery summaries, delivery
outcome ledgers, and report dict enrichment fields are read-only projections.
They MUST NOT drive state transitions, MUST NOT write to storage, and MUST NOT
be treated as evidence of what happened — only receipts and outbox state are
evidence.

---

## 5. Replay Boundary

### 5.1 Replay Creates New Attempts

Replay re-processes stored canonical events through the pipeline. Each replay
delivery produces new receipt rows with `source="replay"` and a `replay_run_id`.
Replay MAY create new delivery attempts, but each attempt is a new receipt row
— it does not modify existing receipts.

### 5.1.1 Replay Attempt Identity

Replay computes the outbox attempt number as `max(existing attempt_number) + 1`
across all outbox rows sharing the same delivery identity (delivery_plan_id,
target_adapter, target_channel). This ensures replay never reclaims or mutates
live rows, which have lower attempt numbers. The same ownership check that
applies to live delivery also applies to replay: if the freshly-created outbox
row comes back terminal, active, or owned by another worker, the pipeline skips
delivery with `failure_kind=outbox_not_owned`.

### 5.2 Replay Must Not Rewrite History

Replay MUST NOT update, delete, or modify existing receipt rows. Replay MUST
NOT alter existing outbox state for live-sourced entries. Replay receipts are
distinguishable from live receipts by the `source` and `replay_run_id` fields.

### 5.3 Replay Isolation

Replay deliveries are tagged with `source="replay"` and `replay_run_id` to
maintain isolation from live delivery. When all matching queued receipt
candidates are replay-sourced, the pipeline MUST NOT create supplemental sent
receipts or transition the outbox from `queued` to `sent`. See
[routing-delivery.md](routing-delivery.md) §8.5 and
[diagnostics-evidence.md](diagnostics-evidence.md) §15.

### 5.4 Replay Non-Guarantees

Replay is operator-initiated, in-memory, and non-durable. It is not a crash
recovery mechanism, not an idempotent delivery guarantee, and not a substitute
for live delivery. Replay MAY produce duplicate sends.

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

4. Terminal statuses (`sent`, `dead_lettered`, `suppressed` for receipts;
   `sent`, `dead_lettered`, `cancelled`, `abandoned` for outbox) MUST NOT have
   outgoing transitions.

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
