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
construction. Malformed selection input is the only caller-input condition
that raises: incomplete identities, empty-string channels, and malformed
expected observations raise `ValueError` before storage is touched. Storage
errors propagate under their own authority as specified below.

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
| Outbox `sent`                                              | `completed`             |
| Outbox `cancelled`                                         | `cancelled`             |
| Outbox `abandoned`                                         | `inspect_native`        |
| Failed or dead-lettered with a retryable persisted failure kind       | `preview_replay`        |
| Failed or dead-lettered otherwise (including no persisted kind)       | `needs_operator_action` |
| Event present but source content no longer reads as retained          | `content_unavailable`   |
| Frozen staged plan contradicts the row's persisted plan binding       | `route_or_plan_changed` |

Classification precedence is independent of the table's row order. An absent
event is `not_found`; missing or multiply live generations are `ambiguous`.
For rows classified as `pending`, `retry_scheduled`, `preview_replay` or
`needs_operator_action`, a source no longer retained is `content_unavailable`
before evaluating expectations and plan binding. Other outcomes do not need
source bytes to report their persisted state. A superseded expected observation
is then `stale`, followed by `route_or_plan_changed` for a contradictory staged
binding or `ambiguous` for an uninterpretable snapshot, then status mapping.

Terminal persisted outcomes (`sent`, `cancelled`, `abandoned`) remain visible
after content collection; active-owner and uncertain-native states
(`in_progress`, `queued`) likewise remain visible without borrowing source
content. Terminal settlement clears the reservation atomically. A live
`active_attempt` reservation on a non-terminal row means a dispatch owner
exists, so the row classifies as `in_progress` exactly where cancellation
would refuse it. Any unrecognized status fails closed to `ambiguous`.

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

The preview reports metadata only, using event existence, metadata-only
outbox rows, a typed owner-scoped dispatch snapshot read, and (only for
outcomes needing source content) the replay read's authorization. It never
returns stored message text, excerpt bytes, or error summaries. The preview
issues no lifecycle or content mutation of its own. The replay authorization
read takes a short `BEGIN IMMEDIATE` transaction and may durably record an
expiry observation under content authority.

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

Any persisted state the preview cannot classify deterministically becomes
`ambiguous` in the returned model. For an uninterpretable frozen snapshot,
`snapshot_present` is `true` and `snapshot_kind` is unknown rather than falsely
reporting a missing row. `ValueError` is reserved for malformed selection
input, mirroring the untyped preview's refusal semantics. Storage errors
other than `OutboxSnapshotIntegrityError` MUST propagate under their own
authority and MUST NOT become selection outcomes. The storage
adapter raises `OutboxSnapshotReadError` for snapshot I/O failures and
`OutboxSnapshotIntegrityError` for a present but malformed or unknown-version
snapshot; only the latter becomes an `ambiguous` observation.

The model carries fixed values: `execution_authorized` is always `False` and
`scope` is always the read-only current-generation scope shared with the
untyped preview. `excluded_sibling_count` reports the outbox rows for the
same event that the exact selection did not select, and is zero when no
single row was selected — sibling deliveries are never widened into the
result.

## Execution selection at delivery admission

Replay execution consumes the same selection key for its delivery admission.
`ReplayRequest.exact_target_selections` in
`src/medre/core/engine/replay/types.py` carries a tuple of
`ExactTargetSelection` instances; the empty tuple is the unrestricted default
and changes no existing behavior. When the tuple is non-empty, a delivery
plan reaches delivery admission in the replay deliver stage only when its
full `(event_id, delivery_plan_id, target_adapter, target_channel)` identity,
read from the plan with the authority's channel normalization, names one
requested selection. A plan whose full identity cannot be read is treated as
unselected rather than passed through conservatively: exact-target admission
never widens. Unselected targets are skipped with the same semantics as
`target_adapters` exclusion, and when no plan survives, the deliver stage is
skipped with the reason `No delivery plans matched exact_target_selections
filter`. Where `target_adapters` or `route_ids` are also set, each filter
applies in turn and the deliverable set is the intersection.
`ReplayRequest` construction refuses malformed selection input — a
non-selection element or a repeated identity — with `ValueError` before any
replay runs; the selection class itself refuses incomplete identities and
the empty-string channel.

Each plan that names a selection is additionally verified against persisted
state at admission time, before any outbox generation is admitted or a send
is staged. The verification calls `preview_exact_target` with the same
selection, so execution admission and the read-only preview resolve exactly
the same target, or both refuse, for the same storage state. Admission
refuses — records the refusal and stages no send for that target — exactly
when the observation classifies the identity as `not_found`, `ambiguous`, or
`route_or_plan_changed`: an event absent from canonical storage, no
persisted outbox row for the identity, contested live generations, an
uninterpretable frozen snapshot, or a staged binding that contradicts the
row's persisted plan. Every other observed category admits the target to the
delivery lifecycle, whose own outbox admission revalidates identity,
generation, dispatch source and ownership in its write transaction; the
selection gate constrains which targets replay execution may touch and never
replaces that revalidation. The gate accepts no caller-supplied expected
fingerprint or generation, so a superseded observation can never authorize a
send: the persisted facts are read fresh at admission. A target whose
persisted row is `completed` or actively owned is admitted as new replay
work under the generation allocation rules, unchanged. The verification's
reads are the preview's own read contract, including the authorized replay
read for outcomes that need source content.

Refusals surface in the replay result. When every plan for an event is
refused, the deliver result is `skipped` and its error is one
`exact_target_refused: <category> (<event_id>, <delivery_plan_id>,
<target_adapter>, <target_channel>)` string per refused target, joined with
a semicolon and a space, where `no-channel` stands in for the `None` channel. Diagnostic skip
counts use the bounded `exact_target_selection_refused` reason rather than
target-specific identifiers; full details remain in the result. When some targets
are admitted, the deliver result carries the refusals in its output under
`exact_target_refusals`, each record holding the identity, the observed
category, and the same reason string. This refusal evidence also survives when
every admitted target is subsequently capability-suppressed, capacity-rejected,
or fails in delivery. Storage errors from the verification
propagate under their own authority and never become refusals.

The execution selection adds no CLI surface, no decision or audit write
path, and no outbox generation semantics. One exact target never widens into
sibling channels; a route or plan drift refusal never falls back to
re-planning from current router configuration — the refusal is final for
that admission; and a native handoff is not recipient receipt.

## Qualification boundaries

The exact-target property is qualified on the fake/core tier against real
on-disk SQLite state: competing writers hold genuine `BEGIN IMMEDIATE`
transactions on separate connections, storage handles are closed and
reopened, and scoped replay stages genuine outbox generations and receipts
through the fake presentation adapter. This qualification proves, and is
limited to, the transactional and storage-level properties of the contract:

* the write authorities serialize — cancellation and the audited decision
  observe a typed refusal when a superseding write wins; a recorded
  decision does not mutate the outbox and therefore does not prevent a
  later guarded cancellation, while no half-applied state or lost update
  survives;
* recorded decisions, persisted outbox state, and the preview's
  classification survive a storage-handle restart, and the normal
  claim/attempt fences still refuse superseded observations afterwards;
* scoped replay refuses named targets whose persisted state drifted,
  records the bounded `exact_target_selection_refused` evidence, and
  stages no generation, dispatch, or receipt for refused or excluded
  siblings of the same event;
* a stale observation never sends: superseded expectations classify as
  `stale` evidence only, and execution re-validates persisted state
  through the shared selector before anything is staged.

This tier does NOT prove native acceptance by any real provider: the fake
adapter's handoff result is transport simulation, and a fake-tier handoff
MUST NOT be read as recipient receipt. It does NOT prove independently
observed recipient delivery, which is reserved for evidence tiers with an
independent recipient, and it does NOT qualify current-pin provider
behavior. Native and current-pin qualification of the exact-target
property remains reserved to those tiers; a lower tier never stands in for
a higher one.

## Native local-integration qualification

The evidence ladder for this contract is fake/core, local
SDK/integration, independent recipient, and hardware. The fake/core tier
above is the first rung. The LXMF propagation leg supplies the native
local-SDK rung, with the recipient verdict observed by a genuinely
independent second instance; external-network and hardware rungs remain
above it and are not claimed.

### LXMF propagation leg

`tests/integration/test_lxmf_exact_target_propagation.py` qualifies the
exact-target property on the pinned LXMF/RNS SDKs over a loopback-only
Reticulum UDPInterface pair. Two process-isolated instances participate:
the MEDRE side drives the installed adapter distribution's real session
configured with the exact 16-byte destination hashes the other side
published at readiness, and the recipient side runs a real propagation
node on the raw pinned SDKs in its own process, recording its own
delivery observations. No observation on the recipient side touches
MEDRE code, so the recipient verdict cannot be MEDRE reading back its own
receipts.

This tier proves, and is limited to, the following native properties:

* **A propagated handoff is not recipient receipt.** The MEDRE side's
  honest terminal state tops out at `sent` — the propagation node
  accepted the message — and the qualification asserts that state
  separately from the recipient verdict. The two facts are recorded in
  different processes, and the recipient fact is the only receipt
  evidence; a propagation handoff MUST NOT be reported as recipient
  receipt anywhere downstream.
* **Exact destination, independently observed.** The recipient instance
  observes the message addressed to exactly its announced delivery
  destination hash, sourced from the MEDRE side's announced delivery
  destination hash, carried with the propagation delivery method, and
  delivered exactly once — no duplicate and no widening to any other
  destination.
* **Content-addressed identity survives the native path.** The native
  message identifier the MEDRE side handed off equals the message hash
  the recipient instance observed.
* **No handoff without an observed exact destination.** A propagated
  send to a destination hash the MEDRE side has never observed natively
  is refused before any message is constructed, and the propagation node
  observes nothing. Native discovery of the exact destination gates the
  handoff.

This tier does NOT prove delivery through a network of propagation nodes,
store-and-forward pickup by a destination that is not itself the
propagation node, propagation peering or sync flows, or delivery over an
external network or physical radio. The recipient instance and the
propagation node being one harness instance is a local-tier boundary; a
multi-node topology is a higher-tier claim.

Collection boundary: the required `transport-local-integration` CI job
runs provider-owned suites at their pinned revisions and does not collect
core-owned `local_integration` tests, so this leg's CI execution is not
wired today. The qualification asset is core-owned and runs wherever the
pinned SDK extra is installed. Closing that delta requires either a CI
step that runs core-owned local-integration tests alongside the provider
suite, or an equivalent propagation scenario in the provider-owned suite
at its pin; until one lands, this section records the asset, not a CI
execution.

### Remaining native legs

No other transport currently offers a CI-executable native tier for this
property. Chatmail and XMPP are recorded `local_integration: false` in
the adapter compatibility manifest, and neither provider suite at its pin
defines a Docker or local-integration tier: both carry only opt-in
live-marked tests that require private credentials outside the repository
(a two-profile Chatmail fixture, respectively a two-account XMPP
configuration with server host, port, JIDs, and MUC domain). A second
native leg therefore needs, precisely: a CI-executable service tier —
either a Chatmail or XMPP server in the integration compose with the
provider harness promoted to a deterministic local-integration scenario,
or repository-secret-backed private fixtures — and a provider-owned
scenario at its pin that observes delivery through an independent second
account, so the exact-target verdict is never MEDRE reading back its own
receipts. Other transports' native legs and the hardware rungs remain
outstanding for the full qualification of this property.

## Schema and stability

Preview instances serialize through `to_dict` and MUST conform to
`docs/schemas/exact-target-preview.schema.json`. The category vocabulary, the
required field set, the fixed scope and authorization values, and the
snapshot-kind vocabulary are pinned by parity tests against the Python model;
the three surfaces (model, schema, this page) change together or not at all.
