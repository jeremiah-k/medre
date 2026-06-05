# Lifecycle Authority Audit Guide

Compact developer reference for auditing lifecycle status vocabulary,
consistency across surfaces, and classification correctness. This doc
complements the normative lifecycle authority spec at
`docs/spec/delivery-lifecycle.md` (with supporting details in
`docs/spec/state-machines.md` and `docs/spec/routing-delivery.md`).

## Canonical Status Vocabularies

Source of truth: `src/medre/core/engine/pipeline/delivery_state.py`.

| Vocabulary       | Values                                                                                                                          | Enforced on                             |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------- |
| Receipt          | `queued`, `sent`, `failed`, `dead_lettered`, `suppressed`                                                                       | `DeliveryReceipt.status`                |
| Outbox           | `pending`, `in_progress`, `queued`, `sent`, `retry_wait`, `dead_lettered`, `cancelled`, `abandoned`                             | `DeliveryOutboxItem.status`             |
| Outcome          | `success`, `queued`, `transient_failure`, `permanent_failure`, `skipped`                                                        | `DeliveryOutcome.status`                |
| Adapter delivery | `sent`, `enqueued`                                                                                                              | `AdapterDeliveryResult.delivery_status` |
| Operator         | `disabled`, `not_configured`, `configured`, `starting`, `connected`, `degraded`, `unavailable`, `stopping`, `failed`, `stopped` | `AdapterStatusEvidence.operator_status` |

Classification subsets (all defined in `delivery_state.py`):

| Classification       | Values                                            | Partition of       |
| -------------------- | ------------------------------------------------- | ------------------ |
| Terminal receipt     | `sent`, `dead_lettered`, `suppressed`             | `RECEIPT_STATUSES` |
| Non-terminal receipt | `queued`, `failed`                                | `RECEIPT_STATUSES` |
| Terminal outbox      | `sent`, `dead_lettered`, `cancelled`, `abandoned` | `OUTBOX_STATUSES`  |
| Non-terminal outbox  | `pending`, `in_progress`, `queued`, `retry_wait`  | `OUTBOX_STATUSES`  |
| Claimable outbox     | `pending`, `retry_wait`                           | `OUTBOX_STATUSES`  |
| Accepted outcome     | `success`, `queued`                               | `OUTCOME_STATUSES` |

## Audit Checklist: Status Update Correctness

Before changing any status value, vocabulary, or transition:

1. **Vocabulary frozenset** -- update the `frozenset` constant in `delivery_state.py`.
2. **Terminal / non-terminal / claimable / accepted sets** -- update `TERMINAL_*`, `NON_TERMINAL_*`, `CLAIMABLE_*`, `ACCEPTED_*` if membership changes. `NON_TERMINAL_*` constants are computed as `VOCABULARY - TERMINAL_*` and must partition cleanly (disjoint, union equals vocabulary).
3. **Transition table** -- add/remove entries in `RECEIPT_TRANSITIONS` or `OUTBOX_TRANSITIONS`.
4. **Spec tables** -- update `docs/spec/state-machines.md` sections 1.1, 1.3, 2.1, 2.3.
5. **Test coverage** -- update `tests/test_delivery_state.py` (vocabulary, classification, transition tests).
6. **Safe-update guidance** -- follow the checklist in `delivery_state.py` module docstring (six items).

## How to Audit Each Surface

### Producer (pipeline writes)

- `DeliveryLifecycleService` owns retry decisions, dead-letter progression, supplemental queued-to-sent receipts, suppression receipt creation, and outbox finalization.
- `TargetDeliveryService` owns per-target execution: rendering, adapter invocation, primary single-attempt receipt construction.
- `PipelineRunner` owns orchestration: route planning, outbox creation, lease renewal.
- Check: every status written to a receipt row or outbox row must come from the closed vocabulary above.

### Consumer (reads / projections)

- `delivery_status` SQL view (`docs/spec/routing-delivery.md` section 9) is a projection, not a stored value. It reads `MAX(sequence)` per `(delivery_plan_id, target_adapter)`.
- `delivery_status()` storage method normalizes `NULL` and empty-string channels.
- Diagnostics (adapter, convergence, recovery) are read-only snapshots, not authoritative state.
- Check: no consumer should write or derive status values outside the vocabulary.

### Storage (schema and views)

- `delivery_receipts` table: append-only. No `UPDATE` or `DELETE` after row creation.
- `delivery_outbox` table: mutable operational state with `allowed_from` guards on every transition method.
- `delivery_status` view: groups by `(delivery_plan_id, target_adapter)`, picks `MAX(sequence)`.
- Check: storage methods (`mark_outbox_sent`, `mark_outbox_queued`, etc.) enforce `allowed_from` guards via `OUTBOX_TRANSITIONS`.

### Evidence (diagnostics and bundles)

- `AdapterStatusEvidence` (in `src/medre/core/evidence/adapter_status.py`) is observational and pure -- no I/O, no async, no SDK imports.
- `RecoveryOwnershipAction.observed_status` is the observed outbox status at analysis time. In snapshot diagnostics it equals `prior_status` because no storage mutation occurs.
- Rendering evidence (`rendering_evidence` column) is populated only for `sent` and `queued` receipt statuses; `None` for `suppressed`, `failed`, or pre-outbox skip paths.
- Check: evidence fields are `str` or `None`, never SDK objects or enums.

### Operator visibility

- `OPERATOR_STATUSES` tuple in `adapter_status.py` defines the 10 canonical operator-facing strings.
- `derive_operator_status()` maps `AdapterState` enum values to operator strings (e.g., `READY` -> `connected`, `DEGRADED` and `BACKPRESSURED` both map to `degraded`).
- Diagnostics keys (`connected`, `health`, `reconnecting`, etc.) are contractual across all four adapters. See `docs/spec/diagnostics-evidence.md` section 2.
- Check: operator-facing diagnostics contain no secrets, no raw SDK objects, no protobuf.

## Known Derived Surfaces

These surfaces derive status from receipt/outbox state rather than storing it independently:

| Surface                                    | Derivation                                                  | Source                              |
| ------------------------------------------ | ----------------------------------------------------------- | ----------------------------------- |
| `delivery_status` SQL view                 | `MAX(sequence)` projection                                  | `schema.py`                         |
| `delivery_status()` storage method         | Reads view, normalizes channel                              | `_receipt.py`                       |
| `DeliveryLifecycleService` retry decisions | Reads `failed` receipt + `adapter_transient` failure kind   | `delivery_lifecycle.py`             |
| `RecoveryOwnershipStatus` classification   | Reads outbox status + lease state at startup                | `recovery/models.py`                |
| `AdapterStatusEvidence.operator_status`    | Derives from enabled/configured/lifecycle state             | `adapter_status.py`                 |
| `CapabilityDecision`                       | Derives from `AdapterCapabilities` + event kind + relations | `routing-delivery.md` section 6.3   |
| `RenderingEvidence.capability_level`       | Carried from `CapabilityDecision` into rendering context    | `routing-delivery.md` section 6.3.7 |

## Dead-Letter Attempt Convention

When `should_dead_letter()` returns `True` for a failed receipt at attempt N,
the dead-letter receipt is appended with `attempt_number = N + 1`. This
chain-closing convention means the dead-letter receipt records one more than
the exhausted attempt. Example: `max_attempts = 3`, failed receipt at
`attempt_number = 3` triggers dead-lettering; the dead-letter receipt receives
`attempt_number = 4`. See `state-machines.md` §1.6.

## Adapter Metadata Naming Rule

Adapters report delivery outcome via `AdapterDeliveryResult.delivery_status` (field: `delivery_status`, values: `"sent"` or `"enqueued"`). This is the **adapter-level** lifecycle field.

Do not confuse this with:

- `DeliveryReceipt.status` -- the pipeline-level receipt status (`queued`, `sent`, `failed`, `dead_lettered`, `suppressed`).
- `DeliveryOutboxItem.status` -- the operational outbox status.
- `AdapterStatusEvidence.operator_status` -- the operator-facing health string.

The pipeline maps adapter `delivery_status` to receipt status: `"sent"` maps to receipt `"sent"`, `"enqueued"` maps to receipt `"queued"`. The adapter never sets receipt status directly.

When adding new adapter-level status evidence fields, name them `adapter_status` or `adapter_*` to keep the namespace clear. Do not introduce `delivery_status` in metadata dicts or evidence bundles as a synonym for receipt status.

## Related Validation Surfaces

When auditing or modifying lifecycle vocabulary, these files are the most relevant validation surfaces:

- `tests/test_delivery_state.py` — vocabulary frozensets, classification sets, transition tables
- `tests/test_docs_lifecycle_authority.py` — docs/code vocabulary alignment, adapter metadata naming
- `tests/conformance/test_delivery_lifecycle_conformance.py` — delivery lifecycle contract behavior
- `tests/conformance/test_recovery_conformance.py` — recovery classification and ownership
- `tests/test_storage_receipts.py` and split outbox test files — receipt/outbox storage behavior
- Adapter parity tests — adapter `delivery_status` and metadata key naming

## Deferred Refactors

From lifecycle authority research. These are not bugs; they are larger cleanup
items tracked for future work:

| Item                      | Scope                                                 | Notes                                                                                                                                                                                                                                                                                                                        |
| ------------------------- | ----------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| RetryWorker unification   | `src/medre/runtime/retry.py`, `delivery_lifecycle.py` | Retry logic is split between `RetryWorker` (scheduling, capacity) and `DeliveryLifecycleService` (retry decisions, dead-letter progression). A future refactor could consolidate the retry decision boundary so `RetryWorker` owns scheduling and capacity while `DeliveryLifecycleService` owns all state transition logic. |
| Replay outbox attribution | `src/medre/core/engine/pipeline/`                     | Replay BEST_EFFORT mode produces receipts but does not create outbox items. Attribution comes from the receipt alone. A future refactor could give replay its own lightweight attribution mechanism instead of relying on live-path outbox patterns.                                                                         |
| Capability caching        | `src/medre/core/planning/`                            | `CapabilityDecisionResolver` is stateless and re-evaluates on every call. For high-throughput scenarios, caching resolved decisions per `(event_kind, target_adapter)` could reduce repeated lookups against static `AdapterCapabilities`. Not currently a bottleneck.                                                       |
| Frozen DeliveryPlan       | `src/medre/core/planning/delivery_plan.py`            | `DeliveryPlan` is a mutable dataclass used as an operational artifact. Making it frozen (or adding a frozen variant) would align with the immutability pattern used by `DeliveryReceipt` and `CapabilityDecision`. This requires updating all construction sites.                                                            |

Note: the `observed_status` rename in `src/medre/core/recovery/models.py` (formerly `recovered_status`) was completed during the lifecycle-consolidation branch — it is no longer deferred.
