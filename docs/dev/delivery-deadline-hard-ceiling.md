# Delivery deadline and hard content privacy ceiling

This page is the design for register row
[G03](contract-gap-status.md#register): a policy-set delivery deadline and a
hard content privacy ceiling that hold across outbox admission, provider
queues and pending work. It inventories the deadline machinery that already
exists on `main`, then fixes where the hard deadline is checked, how it
coexists with accepted and uncertain native attempts, and what the ceiling
means for the availability preview. The implemented content authority it
extends is
[content runtime authority](../spec/content-runtime-authority.md); the
retention background is
[content lifecycle](content-lifecycle.md#deadlines-and-handoff-safety).

## Current state

Two deadline families exist. They are enforced at different places and are
stored in different rows, and neither is fully connected to an operator
policy source.

| Hook | Location | What it enforces today | What it does not |
| --- | --- | --- | --- |
| Mandatory dependency deadline | `event_content_dependencies.deadline` (`src/medre/core/storage/sqlite/schema.py:84`); validated at `admit_ingress` (`src/medre/core/storage/sqlite/_ingress.py:160-192`) | An optional operator-supplied `content_deadline` written at admission; expiry is observed durably and cannot be undone by clock rollback | No production caller passes `content_deadline` (`src/medre/core/engine/pipeline/runner.py:820`, `src/medre/runtime/app.py:3137`), so the column is NULL in live admission and the gate is dormant |
| Outbox adoption fence | `insert_outbox_dependency` (`src/medre/core/storage/sqlite/_content_identity.py:112-144`) inside the `create_outbox_item` transaction (`src/medre/core/storage/sqlite/_outbox.py:449-452, 483-485`) | A generation staged after its source deadline elapsed receives no content dependency; the adoption copies the source deadline into the target row | With a NULL source deadline there is nothing to compare; the fence exists but never fires in production |
| Durable expiry observation | `_observe_mandatory_expiry` (`src/medre/core/storage/sqlite/_content_grants.py:44-58`) | Releases expired source and target dependencies logically and durably; every authorized read and native fence observes it first | Observation is a permission transition; it removes no bytes and cancels no obligation rows |
| Plan deadline | `DeliveryPlan.deadline` (`src/medre/core/planning/delivery_plan.py:174`), `DEADLINE_EXCEEDED` taxonomy (`delivery_plan.py:320`); checked pre-render and rechecked before native handoff (`src/medre/core/engine/pipeline/target_delivery.py:585-607, 950-977, 1021`) | Refuses a target whose plan deadline passed before rendering and again immediately before handoff, persisting failure evidence each time | No production planner sets a non-None deadline (only constructors at `fallback_resolution.py:89` and `retry_plan.py:568`), so the hook is also dormant; outbox admission does not consult it |
| Native-start fence | `authorize_native_submission` (`src/medre/core/storage/sqlite/_content_grants.py:560-621`); call sites `target_delivery.py:613-642, 1008-1019` | Fuses in-progress ownership, exact attempt reservation, unexpired lease, newer-sibling exclusion and live content dependency into one SQLite decision just before adapter invocation | A local dispatch-start fence, not atomic with the SDK; with a NULL dependency deadline the content leg cannot refuse |
| Radio native-start deadline | `_Submission.begin` (`src/medre/core/engine/pipeline/radio_delivery.py:114-137`) and the packet-start checkpoint (`src/medre/core/storage/sqlite/_radio.py:379-391`) | Checks the plan deadline around every packet start and fuses deadline, content authority and claim into one transaction | Same dormant deadline input; post-send observation intentionally requires no live content authority |
| Retry-claim cleanup | Retry worker refusal path (`src/medre/runtime/retry.py:1200-1216`) and claim release guard (`_outbox.py:1345-1362`) | A claimed row whose content authority is denied is reconciled against persisted evidence, then abandoned; a reserved attempt is never released back to the queue | Cleanup is lazy: a `pending` or `retry_wait` row sits until a dispatcher claims it, and nothing sweeps unclaimed rows when content expires |
| Availability preview | `preview_content_access` (`src/medre/core/storage/content_availability.py:181-182`) and the immutable per-event ceiling row (`schema.py:91-99`) | Reports `unavailable/hard_ceiling_elapsed` for held consumers and refuses optional holds past the ceiling | Pure, non-mutating and dispatch-blind; it never blocks a send and is not the enforcement contract |

The gap in one sentence: every enforcement site a hard deadline needs already
exists and is transactionally correct, but the two timestamps that would
make them fire — a policy-derived plan deadline and a content ceiling written
into the dependency deadline — have no production producer.

## Enforcement points

The design adds no second SQLite authority, no new queue framework and no new
fence site. It supplies producers and cleanup on top of the rows and
transactions listed above.

- The hard privacy ceiling is an operator policy input expressed as an
  absolute UTC instant. Admission writes it as the mandatory dependency
  deadline in `event_content_dependencies.deadline`, in the same transaction
  that admits the event.
- The delivery deadline is an operator policy input expressed as a maximum
  attempt window. Planning computes `DeliveryPlan.deadline` from a trusted
  local admission time and the selected policy; remote timestamps or
  provider TTL hints never create local authority.
- At every site, the effective deadline is the earlier of the two. A policy
  may shorten the content window below the ceiling; nothing may lengthen it.
- The plan deadline rides the existing snapshot: staging freezes it into the
  dispatch snapshot context (`outbox_manager.py:157-159`), retry
  reconstruction restores it and rejects timezone-naive values
  (`retry_plan.py:534-552`), so a restart or reclaim cannot lose it.

Outbox admission is the first check. The design keeps `insert_outbox_dependency`
as the single admission gate: once the ceiling is written as the source
deadline, any generation staged after the ceiling — replay, retry staging or a
new sibling target — commits as a row but adopts no content dependency, and
`_outbox_content_authority`
(`src/medre/core/storage/sqlite/_content_grants.py:121-151`) then denies every
read and native start for that generation. The obligation stays durable and
auditable; the content authority does not exist.

The native-submit fence is the second check. `authorize_native_submission`
already observes expiry and requires a live dependency before every adapter
invocation; `check_dispatch_deadline` rechecks the plan deadline after
rendering and after awaited authorization; the radio packet start fuses both
inside one checkpoint transaction. A deadline that has elapsed produces the
existing refusal semantics: `DEADLINE_EXCEEDED` failure evidence with
`content_authority_denied` where the content leg refused, an
`AdapterPermanentError` on the delivery path, no transport invocation, and no
delivery observation or receipt that could be read as provider acceptance.

Pending-outbox cleanup is the third check. A bounded sweep rides the retry
worker's poll cycle — the component that already owns claim, release and
abandonment decisions for outbox rows — and uses only the existing lifecycle
transitions:

- It retires `pending` and `retry_wait` rows whose event ceiling has elapsed
  through `mark_outbox_abandoned` with a `content_authority_denied` summary,
  letting the guarded terminal settlement release the target dependency in
  the same transaction
  (`src/medre/core/storage/sqlite/connection.py:365-411`).
- It skips rows with a live `active_attempt` reservation and rows in
  `queued` status entirely; their authority is the attempt and feedback
  machinery, not the ceiling sweep.
- It never deletes outbox rows, receipts, observations or native references;
  terminal rows are immutable history.

The sweep's write is guarded by the transition's own compare-and-set
(`src/medre/core/storage/sqlite/_outbox.py:967-1129`): the sweep passes the
attempt identity it read, so a reservation committed between its read and its
write fails the attempt fence and the row stays reserved for evidence
reconciliation. Because `allowed_from` on the abandonment transition also
admits `in_progress`, a claimed row that is still unreserved may be settled
by the sweep — that is safe by construction, since an unreserved claim never
authorized a native send and the dispatcher's subsequent reservation or
finalization simply fails its guard and reconciles.

## Attempt authority interaction

An `active_attempt` reservation can represent an uncertain native send even
when no transport was observably invoked, and a `queued` row represents a
transport-local acceptance whose external outcome is unresolved. The ceiling
is a content-retention and dispatch rule; it is not evidence about what the
provider did.

- A deadline that has elapsed does not imply non-acceptance. Expired work is
  settled as an unsent refusal only when the reservation is provably
  unstarted; otherwise the existing evidence reconciliation runs first and
  the outcome comes from receipts, feedback facts or conservative uncertainty.
- Cleanup refuses reserved and queued rows. `release_outbox_claim` already
  refuses to release a reservation, and the sweep adds no path around it;
  resolving a `queued` row stays with `DeferredHandoffCompleted`,
  `DeferredHandoffFailed` and `PostHandoffObservation` feedback under the
  correlation contract in
  [delivery lifecycle](../spec/delivery-lifecycle.md#34-deferred-hand-off-correlation).
- The stale-queued reclaim after the five-minute grace
  (`src/medre/core/storage/sqlite/_outbox.py:760-763`,
  `src/medre/core/storage/sqlite/constants.py:15`) remains the recovery path
  for a crashed process's queue-only rows. A reclaimed row past the ceiling
  reaches the native fence and is refused there with
  `content_authority_denied`; it is never re-sent, and the refusal settles it
  through the same guarded abandonment.
- Already submitted or accepted operations keep their ownership fences.
  Content removal never erases attempt evidence, native references or
  duplicate-prevention records; logical expiry does not rewrite a
  `queued` receipt into `failed` or `cancelled`.

## Content ceiling semantics

The availability preview and the hard ceiling answer different questions. The
preview classifies what a consumer may read now; the ceiling bounds how long
the content may be used at all, including for delivery.

- The ceiling is a floor in the strictness sense: retention profiles —
  relay-only, recent context, history — and retry scheduling derive their
  windows from it and may only end earlier. A profile, plugin hold or replay
  cannot extend past it, and grant renewal cannot move it
  (`content-lifecycle.md#consumer-holds`).
- The per-event policy row is installed once and is immutable
  (`src/medre/core/storage/sqlite/_content_grants.py:221-226`); a later
  admission or retry observes the
  same ceiling. Rolling the wall clock backward reopens nothing: observed
  expiry writes `released_at`, and the next observation re-releases before
  any permission check.
- Pending replay ambitions after the ceiling are refused, never
  reconstructed. Replay generations adopt no dependency of their own and
  dispatch on the still-live admission hold
  (`content-runtime-authority.md#transactional-dependencies`), so
  `read_event_for_replay` and `read_body_for_outbox` fail closed once the
  ceiling elapsed; a content-dependent replay stage reports its named denied
  outcome instead of producing an empty send.
- The ceiling governs the relay's own retained content. Provider-side copies,
  attachments already transferred and native stores are outside it, and no
  provider deletion is implied; that boundary is G06 work.
- The ceiling is not a per-message TTL negotiated with providers. It is
  local authority derived from a trusted local admission time, and the
  inclusive expiry boundary uses aware UTC instants exactly like the
  existing grant checks.

## Qualification boundaries

The implementation qualifies on real SQLite, using the race families the
content-authority suites already established
(`tests/test_content_dispatch_authority_races.py`,
`tests/test_content_grants_storage.py`) plus new ones for the cleanup path.

- Real-transaction races: admission versus expiry on the same event; the
  ceiling sweep racing a live `claim_due_outbox_items` on the same row, with
  the transition guard deciding; two workers allocating generations across
  the ceiling in one `BEGIN IMMEDIATE` window.
- Deadline-elapsed with a live claim: a reservation taken before the ceiling
  that reaches the native fence after it — refused, evidence persisted, and
  the reservation consumed only by lifecycle finalization.
- Queued native handoff at expiry: a deferred disposition outstanding when
  the ceiling passes — the row stays `queued`, the sweep skips it, feedback
  facts still finalize it, and a stale-queued reclaim after the grace is
  refused at the fence without a transport call.
- Restart: cleanup idempotent across close/reopen; observed expiry tombstones
  survive clock rollback; the frozen plan deadline survives retry
  reconstruction and rejects a naive value.
- Per-transport honesty: fake and local tiers drive deterministic clocks
  through the families above; native SDK and hardware tiers prove only that
  the refusal happens before invocation and that accepted prefixes stay
  intact. No tier claims that a provider deleted or held back queued work,
  and native handoff is never equated with recipient receipt.

## Non-goals

- No distributed lease or multi-worker fencing beyond the existing
  single-writer SQLite leases and sibling-exclusion guards.
- No hot reload of ceilings or deadlines; configuration is startup-compiled
  and a changed ceiling applies to later admissions.
- No physical-erasure claim: WAL, backups, logs and provider stores remain
  unmeasured copies.
- No provider-side deletion or provider-queue introspection in this stack;
  per-provider copy retention stays a separate gap.
