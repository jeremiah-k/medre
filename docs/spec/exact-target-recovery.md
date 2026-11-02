# Exact-target recovery observation contract

## Scope and authority

The typed exact-target contract in
`src/medre/core/observability/exact_target.py` selects exactly one persisted
delivery by its full `(event_id, delivery_plan_id, target_adapter,
target_channel)` identity and observes its current state as a typed preview.
Consumers import directly from that module; the observability package does not
re-export it. The contract is additive to the untyped exact-recovery preview
and to the [storage cancellation authority](operator-exact-resolution.md): it
changes no existing output, schema, or behavior, and it exposes no CLI
surface.

The preview is a **read-only observation**. It is never a replay execution
grant, never a cancellation permit, and never a compare-and-swap token. Any
future write path MUST revalidate the exact identity, generation, status and
ownership under storage authority inside its own transaction before mutating
anything.

## Selection key

`ExactTargetSelection` wraps one complete `DeliveryIdentity`. The four
identity components are supplied explicitly so the channel decision is made
before authority normalization applies:

* a nonempty channel string selects that channel;
* `None` selects the deliberate no-channel identity;
* the empty string MUST be rejected with a `ValueError` at construction.
  Storage and authority resolution normalize the empty string to the
  no-channel identity, so accepting it would silently widen the caller's
  stated intent.

An identity missing any of event, plan, or adapter MUST be rejected at
construction. Malformed selection input is the only condition under which the
preview raises: incomplete identities, empty-string channels, and malformed
expected observations raise `ValueError` before storage is touched.

## Category vocabulary

The category is a closed 14-token vocabulary, frozen across the Python
`Literal`, the JSON Schema enum in `exact-target-preview.schema.json`, and the
mapping below. Nine tokens reuse the shared recovery disposition vocabulary so
one authority classifies delivery state (`pending`, `in_progress`,
`awaiting_feedback`, `retry_scheduled`, `completed`, `cancelled`,
`inspect_native`, `preview_replay`, `needs_operator_action`); five tokens
classify exact-target selection outcomes (`not_found`, `ambiguous`, `stale`,
`content_unavailable`, `route_or_plan_changed`).

`ambiguous` means delivery-identity selection ambiguity: the identity has no
persisted outbox row, or more than one currently-live generation, or a frozen
context that cannot be interpreted. It does NOT describe relation-binding
ambiguity, which is a different token in the canonical event namespace.

| Observation                                                          | Category                |
| -------------------------------------------------------------------- | ----------------------- |
| Event absent from canonical storage                                   | `not_found`             |
| No outbox row for the identity, or more than one live generation      | `ambiguous`             |
| Expected fingerprint or generation superseded by current state        | `stale`                 |
| Outbox `pending` without a reservation                                | `pending`               |
| Outbox `in_progress`, or any non-terminal row with `active_attempt`   | `in_progress`           |
| Outbox `queued`                                                       | `awaiting_feedback`     |
| Outbox `retry_wait`                                                   | `retry_scheduled`       |
| Outbox or receipt `sent`                                              | `completed`             |
| Outbox or receipt `cancelled`                                         | `cancelled`             |
| Outbox or receipt `abandoned`                                         | `inspect_native`        |
| Failed or dead-lettered with a retryable persisted failure kind       | `preview_replay`        |
| Failed or dead-lettered otherwise (including no persisted kind)       | `needs_operator_action` |
| Event present but source content no longer reads as retained          | `content_unavailable`   |
| Frozen staged plan contradicts the row's persisted plan binding       | `route_or_plan_changed` |

Classification precedence follows that table's observation order: `not_found`,
then selection ambiguity, then `content_unavailable`, then `stale`, then
`route_or_plan_changed`, then the status mapping. Terminal persisted outcomes
are authoritative and classify regardless of a residual reservation, which
terminal settlement clears atomically. A live `active_attempt` reservation on
a non-terminal row means a dispatch owner exists, so the row classifies as
`in_progress` exactly where the cancellation authority would refuse it. Any
status outside the persisted outbox vocabulary fails closed to `ambiguous`.

The persisted plan-binding contradiction is checked only when a
`delivery_outbox_snapshots` row exists for the selected generation. Rows
created before that table keep their inline metadata and carry no snapshot
binding; the model's binding fields are therefore nullable. Reduced `direct`
documents carry no plan or route identity fields, so their binding is the
row's own identity columns, which the exact selection already matched. A
present staged document that cannot be interpreted fails closed to
`ambiguous`.

`cancel_eligible` is a derived boolean, not a category: it reports whether the
selected row matches the exact-cancellation eligibility predicate
(`pending`/`retry_wait` status without a reservation or claim), aligned with
the audit table's observed-status constraint. Like every other field it is an
observation; the write transaction revalidates everything.

## Generation semantics

`attempt_number` is the raw persisted finalized attempt counter, including
zero for staged work that has never been dispatched. `active_attempt` is the
raw persisted in-flight reservation. `observed_generation` mirrors
`attempt_number` exactly. No field in this contract may carry a
display-normalized effective generation: write paths consume these fields, and
a display that normalizes staged work to an effective attempt of one MUST NOT
feed them.

The `stale` classification compares a caller-supplied expected observation
against current state. The expected generation is the persisted finalized
counter; it is superseded when it differs from the raw `attempt_number` or
when the row now carries an `active_attempt` reservation, mirroring the
cancellation transaction's revalidation. The expected fingerprint must be a
fingerprint this contract previously reported: a SHA-256 hash over a
canonical statement of the identity and the row's raw metadata counters,
status, scheduling deadline and persisted payload binding. A fingerprint is
observation evidence only and never authorizes anything.

## Read-only and metadata-only guarantees

The preview performs metadata reads only, through the storage read paths
other operator surfaces use: event existence via metadata, outbox rows via
the metadata-only projection, content availability via the replay read's
authorization, and the frozen dispatch snapshot via its typed owner-scoped
read. It never returns stored message text, excerpt bytes, or error
summaries. It acquires no lock beyond ordinary reads and performs no storage
mutation.

The content-availability check reuses the replay read's own authority. That
read creates no hold and grants no claim. Where a mandatory dependency has
already expired, it durably records the expiry observation — a permission
transition under the content authority, never a content or lifecycle
mutation.

A `content_unavailable` classification MUST NOT be resolved by reconstructing
bytes. Collected or expired content returns to a replayable state only
through the content admission authority — a fresh admission — never by
reassembly from previews, projections, or any other partial observation.

## Fail-closed behavior and error authority

Any state the preview cannot classify deterministically becomes `ambiguous`
in the returned model. `ValueError` is reserved for malformed selection
input, mirroring the untyped preview's refusal semantics. Storage errors
propagate with their own authority and MUST NOT be converted into selection
outcomes; a failed snapshot read is a storage error, while a present but
uninterpretable frozen document is an `ambiguous` observation.

The model carries fixed values: `execution_authorized` is always `False` and
`scope` is always the read-only current-generation scope shared with the
untyped preview. `excluded_sibling_count` reports the outbox rows for the
same event that the exact selection did not select, and is zero when no
single row was selected — sibling deliveries are never widened into the
result.

## Schema and stability

Preview instances serialize through `to_dict` and MUST conform to
`docs/schemas/exact-target-preview.schema.json`. The category vocabulary, the
required field set, the fixed scope and authorization values, and the
snapshot-kind vocabulary are pinned by parity tests against the Python model;
the three surfaces (model, schema, this page) change together or not at all.
