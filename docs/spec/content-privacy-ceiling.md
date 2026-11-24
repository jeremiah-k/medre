# The hard content privacy ceiling

The operator ceiling is an absolute, immutable bound on how long the
relay's own retained content may be used at all, including for delivery.
This page is the normative ceiling invariant and the store-by-store census
that defines what "gone" means everywhere the store can hold content or
bytes derivable from it.

See also: [content-retention.md](content-retention.md),
[content-runtime-authority.md](content-runtime-authority.md),
[content-collection-guard.md](content-collection-guard.md),
[security-privacy.md](security-privacy.md), and the developer-facing
design history in `docs/dev/content-lifecycle.md` and
`docs/dev/delivery-deadline-hard-ceiling.md`.

---

## 1. The ceiling value and its producers

The ceiling for one admitted event is one absolute UTC instant derived at
admission from the trusted local admission time plus the operator's
declared window (`content_ceiling.max_age_seconds`, bounded to one year at
load). Remote timestamps, transport TTL hints, retries, plugins, new
replays, and unresolved handoffs MUST NOT extend it. The ceiling row
(`event_content_access_policies.hard_ceiling`) is written once, on the
first authorized policy installation in the admission transaction, and
future grants cannot extend it. The effective delivery deadline is the
earlier of the event ceiling, the frozen plan deadline, and the adopted
target dependency deadline; the deadline machinery may only end earlier.

## 2. The invariant

When an event's immutable **hard content ceiling** elapses, enforcement MUST
leave its content unreachable and gone across every store enumerated in the
census below, according to that store's declared "gone" class, before its
enforcement receipt commits. A delivery deadline can expire one target's send
authority earlier without expiring content used by another target. A retention
epoch permits guarded best-effort collection; neither trigger promises the
event-wide ceiling purge before the hard ceiling itself elapses.

The invariant has two layers, and both MUST hold:

1. **Unreachable at the instant.** From the ceiling instant onward, every
   authorized content read path MUST refuse the event's content: the
   mandatory/optional authority observation, the reply-quote grant, the
   outbox and replay reads, and the native-submission fence all derive
   from storage-authoritative rows that compare against the ceiling.
   Unauthorized paths MUST fail closed.
2. **Gone by enforcement.** The guarded ceiling-enforcement transition
   MUST purge every enumerated content store for the event, verify with
   the residual battery that no content-potential value remains
   attributable to the event, and only then record the content-free
   enforcement receipt. A verification failure MUST roll the entire sweep
   back: a partial purge MUST NOT commit.

Enforcement is the ceiling completing; it is not a new policy decision.
The receipt (`event_content_ceiling_log`) is the durable, content-free
fact that purge and verification completed for one event. Receipt
presence MUST NOT be read as a physical-erasure certificate.

The two layers distinguish two states that operators and reviewers must
not conflate. **Expired** content is read-denied from the ceiling instant
onward, but its values can still be stored until a sweep reaches the
event: writes authorized before the ceiling may add residual values in
that window, and the read-only audit reports them per store until
enforcement revisits them. **Purged** content is logically absent: a
successful purge plus the residual battery establishes absence, and the
same battery re-checks it at every later receipt verification. Physical
erasure of freed pages and WAL frames is a further, separate step and is
never implied by either state (§5).

## 3. Census: every store that can hold content or derivable bytes

The census enumerates every persistent store in the core SQLite catalog
plus the SQLite-level remnants. Coverage classes:

- **purged** — enforcement deletes the rows (or their content values).
- **cleared-on-surviving-rows** — the row survives as linkage or evidence;
  every registry-declared content-potential column is cleared.
- **retained-identity** — no content-potential value is attributable to
  the event; identity/authority facts remain by design.
- **unmeasured** — outside the logical store; covered by the explicit
  operational requirement in §5.

| Store                                                           | Content values attributable to one event                                                                                                                                             | Class                     | Enforcement action                                                                                                                                                                                                        |
| --------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `event_body_content`                                            | `body`                                                                                                                                                                               | purged                    | rows deleted                                                                                                                                                                                                              |
| `event_relation_excerpts` (own event)                           | `excerpt`                                                                                                                                                                            | purged                    | rows deleted                                                                                                                                                                                                              |
| `event_relation_excerpts` (incoming, quoting the event)         | `excerpt`                                                                                                                                                                            | purged                    | rows deleted on the quoting events' relations                                                                                                                                                                             |
| `canonical_events`                                              | `payload`, `metadata`                                                                                                                                                                | cleared-on-surviving-rows | values set to `'{}'`; identity columns retained                                                                                                                                                                           |
| `event_relations`                                               | `key`, `metadata`                                                                                                                                                                    | cleared-on-surviving-rows | `key` nulled, `metadata` set to `'{}'`; relation identity retained                                                                                                                                                        |
| `native_message_refs`                                           | `metadata`                                                                                                                                                                           | cleared-on-surviving-rows | set to `'{}'`; correlation identity retained                                                                                                                                                                              |
| `delivery_outbox`                                               | `metadata` (inline frozen dispatch context), `payload_hash` (render-input digest)                                                                                                    | cleared-on-surviving-rows | metadata set to `'{}'` and payload hash set to `NULL`; generations never deleted                                                                                                                                          |
| `delivery_observations`                                         | `metadata`                                                                                                                                                                           | cleared-on-surviving-rows | set to `'{}'`; evidence rows retained                                                                                                                                                                                     |
| `delivery_evidence_diagnostics`                                 | `diagnostic`                                                                                                                                                                         | purged                    | rows deleted for the event's receipts, observations, outbox rows, and ingress work                                                                                                                                        |
| `radio_attempt_progress`                                        | `plan_json`, `provenance_json`, `parts_json`, `plan_fingerprint`                                                                                                                     | purged                    | rows deleted for the event's outbox generations; the payload-derived plan fingerprint is measured into the receipt's logical bytes and removed with the row                                                               |
| `delivery_outbox_snapshots`                                     | `context_json`                                                                                                                                                                       | purged                    | rows deleted for the event's outbox generations                                                                                                                                                                           |
| `event_attachment_associations`                                 | the load authority for retained bytes                                                                                                                                                | purged                    | rows deleted                                                                                                                                                                                                              |
| `attachment_blobs`                                              | `data` (plaintext bytes)                                                                                                                                                             | purged                    | row deleted only when no other event still refers to the `content_ref`; shared bytes go with the last reference                                                                                                           |
| `delivery_receipts`                                             | none (registry: all columns identity/authority)                                                                                                                                      | retained-identity         | rows retained; G01/G02 settlement fences untouched                                                                                                                                                                        |
| `delivery_handoff_settlements`                                  | none (content-free append-order fence)                                                                                                                                               | retained-identity         | rows retained; never touched by enforcement                                                                                                                                                                               |
| `operator_delivery_decisions`                                   | `target_channel` is conservatively unclassified                                                                                                                                      | retained-identity         | rows retained: immutable audit evidence whose schema contract makes them write-once; they hold delivery addressing, never message content; the copy family stays a declared wholesale blocker for the retention collector |
| `event_content_dependencies`                                    | none (authority)                                                                                                                                                                     | retained-identity         | all unsettled owners released at enforcement (the ceiling supersedes them)                                                                                                                                                |
| `event_content_access_grants`                                   | none (authority)                                                                                                                                                                     | retained-identity         | all live grants tombstoned `expired_at` at enforcement                                                                                                                                                                    |
| `event_content_access_policies`                                 | none (authority)                                                                                                                                                                     | retained-identity         | the immutable ceiling row is the enforcement trigger                                                                                                                                                                      |
| `event_body_identity`                                           | none (admission fact)                                                                                                                                                                | retained-identity         | retained; coherence probes accept the ceiling receipt                                                                                                                                                                     |
| `event_routing_facts`                                           | none (bounded route identifiers)                                                                                                                                                     | retained-identity         | retained                                                                                                                                                                                                                  |
| `event_content_collection_log`                                  | none (receipt counters)                                                                                                                                                              | retained-identity         | retained                                                                                                                                                                                                                  |
| `event_content_ceiling_log`                                     | none (receipt counters)                                                                                                                                                              | retained-identity         | one row per enforced event; never rewritten                                                                                                                                                                               |
| `content_retention_epochs`                                      | none (activation boundary)                                                                                                                                                           | retained-identity         | retained                                                                                                                                                                                                                  |
| `conversation_membership`, `conversation_projection_state`      | none (projection linkage)                                                                                                                                                            | retained-identity         | retained                                                                                                                                                                                                                  |
| `event_relations.id`/targets, relation type                     | none                                                                                                                                                                                 | retained-identity         | retained                                                                                                                                                                                                                  |
| `durable_ingress_work`                                          | none (`provenance` is a closed marker)                                                                                                                                               | retained-identity         | retained                                                                                                                                                                                                                  |
| `adapter_checkpoints`                                           | `cursor`, `metadata` — adapter-scoped, not event-scoped                                                                                                                              | unmeasured                | no event attribution exists; documented unmeasured scope                                                                                                                                                                  |
| `plugin_state`                                                  | `value` — plugin-scoped, not event-scoped                                                                                                                                            | unmeasured                | no event attribution exists; documented unmeasured scope                                                                                                                                                                  |
| `inbound_pressure_observations`, `inbound_pressure_outage_gaps` | none (counters only)                                                                                                                                                                 | retained-identity         | retained                                                                                                                                                                                                                  |
| `delivery_handoff_uncertainty`, `radio_accepted_part_facts`     | exact attempt identity plus a closed live/superseded uncertainty state; accepted packet identity; no wire text                                                                       | retained-identity         | survives the ceiling to validate delayed evidence without authorizing sends; an explicit non-uncertain failure records supersession so purging its diagnostic text cannot revive uncertainty                              |
| `delivery_status`                                               | view; classified through `delivery_receipts`                                                                                                                                         | retained-identity         | follows its base table                                                                                                                                                                                                    |
| `_medre_schema_meta`                                            | none (sweep cursors, schema stamp)                                                                                                                                                   | retained-identity         | retained; cursor values are keyset positions, not content                                                                                                                                                                 |
| SQLite free pages and WAL frames                                | former page images of any of the above                                                                                                                                               | unmeasured                | explicit operator erasure step (§5)                                                                                                                                                                                       |
| `radio_attempt_evidence_summaries`                              | Exact attempt provenance, packet states and native correlation, revision, or a closed unreadable-checkpoint marker. No wire bytes, grants, diagnostic text, or payload fingerprints. | retained-identity         | Preserve only the typed evidence projection; an undecodable checkpoint keeps a non-authorizing blocker so it cannot become message-only proof.                                                                            |
| `delivery_observation_scopes`                                   | Verified original outbox and attempt identity, packet ordinal, or an unreadable-scope marker.                                                                                        | retained-identity         | Preserve mutable, content-free operational scope while clearing transport metadata; changing scope advances its attempt revision.                                                                                         |
| `delivery_observation_scope_versions`                           | Exact-attempt monotonic revision for every packet-scope mutation, without transport text.                                                                                            | retained-identity         | Three atomic triggers advance the revision on scope insertion, change, or deletion; a row count alone cannot fence an in-place rewrite.                                                                                   |
| `delivery_route_retry_bounds`                                   | none (numeric/boolean retry-policy bounds captured at admission)                                                                                                                     | retained-identity         | retained; after enforcement retires the frozen dispatch context, post-erasure reconciliation reads these bounds instead of any current configuration; they never authorize another native send                            |

Two attribution boundaries bound the incoming-quote rows. An excerpt whose
quoted target resolves to no event of this installation is attributed to no
event: no other event's ceiling hides it and no other event's enforcement
purges it; the quoting event's own ceiling governs it as its own content. A
native message identity claimed by more than one event (SQLite treats NULLs
as distinct in the native identity constraint) fails closed on both the read
and the purge: expiry of any claiming event hides the excerpt from every
authorized read, and the enforcement of any claiming event purges it, so an
ambiguous identity can over-hide and over-purge the quoting event's excerpt
but cannot leave expired content readable.

A published native reference outranks a canonical source alias. When both
name the same native identity, the excerpt is attributed to the referenced
event alone: only that event's ceiling hides it on read, and only that
event's enforcement purges it. An event that holds the identity only as a
source alias governs no excerpt of it once a reference for that identity
exists.

The registry contract (`STORAGE_COLUMN_CLASSES`) is the column-level
authority for "content-potential": the enforcement purge and the residual
verification battery clear and re-check event-attributable content-potential
columns except expressly retained or unmeasured entries. In particular,
`operator_delivery_decisions.target_channel` is an expressly retained
operator-decision identity field, not part of the residual battery. A new persistent table MUST be added to the
registry with reviewed classes and, if it can hold event-attributable
content-potential values, to the purge and the battery in the same
change.

## 4. Enforcement transition

The guarded ceiling-enforcement transition is a single mutation boundary:

- It MUST run under `BEGIN IMMEDIATE` with the storage write lock, so the
  purge, the verification, the authority tombstones, and the receipt
  commit atomically or not at all.
- Candidate discovery MUST be bounded per invocation with a durable keyset
  cursor and a finite traversal high-water mark. A completed cycle MUST wrap
  to revisit backdated admissions and residuals even under a sustained stream
  of newer entries. Independent pending/residual budgets ensure progress on
  **successful sweeps**; an event that persistently fails verification MUST
  roll the transaction back and requires operator intervention.
- Each sweep MUST bound the number of elapsed policies examined for first-time
  enforcement, including policies that already have a receipt. This budget
  MUST be independent of the residual-repair receipt page. The residual cursor
  MUST wrap to detect late writes behind its prior position. Each audit finding
  MUST distinguish sole-owner attachment blobs from shared associations.
- For each elapsed ceiling without a receipt it MUST: measure the values
  it removes; purge per the census classes; tombstone every live optional
  grant and release every unsettled dependency (the ceiling supersedes
  both); verify the residual battery is zero; and record the receipt with
  content-free counters.
- For an already-enforced event that regained content-potential values
  through a write authorized before its ceiling (for example a delivery
  observation completing an in-flight send), it MUST re-purge the
  residual values without rewriting the immutable receipt. Read
  mediation keeps such content unreachable in the interim.
- It MUST never delete delivery identity or evidence rows: receipts,
  observations, outbox generations, native references, handoff
  settlements, operator decisions, conversation projections, and the
  admission identity all survive as linkage, with census-declared content
  values cleared.
- After enforcement retires an event's frozen dispatch context, remaining
  retry decisions for its outbox generations MUST use the persisted
  admission-time route bounds (`delivery_route_retry_bounds`) and MUST NOT
  consult current global or route configuration for authority over the
  erased route. Generations admitted without bounds reconcile without
  them: persisted evidence settles exactly as before, and no further
  attempt is scheduled. The bounds are content-free arithmetic facts, not
  send authority: recovery may settle existing evidence through them, but
  they never authorize another native send.
- Receipt, observation, ingress and outbox writes MUST keep exact durable
  evidence after expiry without recreating free-form diagnostic content.
  Their diagnostic follow-ups check the installed ceiling and immutable
  enforcement receipt within the same SQLite write transaction. Content-free
  failure classifications and retained handoff facts remain independently
  authoritative. Legacy writes and unrelated writers are still subject to
  the residual sweep and its verification battery.
- Writes racing the sweep from the pre-ceiling authorization window MAY
  append evidence rows afterwards; the sweep's residual pass removes
  their content values, and the receipt stays valid. An authorized read of
  event B also withholds an excerpt quoted from A as soon as A expires,
  even if B remains unexpired and the excerpt has not yet been purged.
  This includes canonical and native-target quote references. This is the honest
  boundary between "gone by enforcement" and writes already authorized
  before the instant.
- A terminal evidence fence MUST cover every retained evidence version that
  can change the decision: the exact-attempt observation count, receipt count,
  checkpoint revision, and retained packet-scope mutation revision. A sweep that restores
  packet correlation for a legacy acknowledgment therefore invalidates a
  stale abandonment decision rather than letting it commit.
- When content is denied after a receipt was persisted, a committed `sent`
  receipt for the exact delivery identity may settle the outbox `sent`
  without another dispatch. A `queued` receipt proves only local queue
  acceptance: reconciliation commits the row to the durable, content-free
  confirmation-pending disposition and leaves deferred completion
  outstanding. Receipts with contradictory event, plan, adapter, destination,
  origin, or replay-run provenance cannot settle either state. Positive
  exact-attempt native evidence also settles `sent` alongside a historical
  failed receipt; both rows remain immutable. A receipt-less evidence
  settlement fences the exact-attempt packet-scope mutation revision so an
  in-place repair invalidates stale conclusions. No settlement fabricates a
  receipt or restores send authority.
- A confirmation-pending row (outbox status `queued`) is never claimable,
  never reclaimed, and never re-dispatched: queued never implies sent, and
  no retry scan, resolver, or maintenance wake dispatches it. Its only
  exits are evidence-driven settlement and the explicit operator
  resolution to `delivered`, `failed`, or `cancelled`. The deadline sweep
  never resolves a confirmation-pending row, so a pending row can outlive
  its delivery deadline until the operator acts.
- Before the first sweep, a checkpoint's wire plan is not returned by
  `get_radio_attempt` once its installed ceiling elapses. Evidence-only
  reconciliation can still inspect a validated, content-free projection of
  that checkpoint; accepted handoff facts do not reauthorize submission.
  Invalid checkpoints remain unresolved rather than becoming no-checkpoint
  whole-message proof.

Runtime invocation rides the existing LIVE-mode outbox poll cycle behind
an independent, default-off `retry.ceiling_sweep` declaration, bounded and
failure-isolated like the deadline sweep. It remains available even when
`retry.enabled=false`: LIVE startup constructs the worker for initial
dispatch, and that same worker continues as the maintenance worker for the
sweep without claiming retry work. Storage-level invocation remains
available to trusted operator tooling.

## 5. SQLite-level guarantee and the explicit erasure requirement

Row deletion and value clearing are logical operations. SQLite does not
securely erase freed pages: after the purge, former content bytes can
remain recoverable in freelist pages and WAL frames until they are
overwritten, and `PRAGMA secure_delete` is NOT enabled by this product.
The storage layer MUST NOT claim byte-level erasure from logical
enforcement alone.

An operator who requires byte-level erasure MUST, after enforcement:

1. quiesce all runtime and other SQLite writers and readers (including any
   open transaction or connection that can block a checkpoint),
2. audit the stopped logical store, complete any needed ceiling enforcement or
   residual repair, and verify all affected events. Resolve pagination via
   `*_has_more` or verify events explicitly; a bounded clean page is not a
   global logical-cleanliness certificate,
3. while the store stays quiescent, run `VACUUM` on the database file to
   rebuild it without deleted content pages, **then** run
   `PRAGMA wal_checkpoint(TRUNCATE)` outside any transaction. Verify its busy
   result is zero and the WAL is truncated before claiming completion.

`PRAGMA secure_delete=ON` enabled **before** enforcement can zero freed
pages but does **not** remove content-bearing WAL frames. Even with that
setting, a successful `wal_checkpoint(TRUNCATE)` is required; secure delete
is not an alternative to WAL cleanup. Neither method erases backups, earlier
file snapshots, or provider-owned copies.

Expiration denies authorized content reads at the installed hard ceiling;
it does **not** prove logical removal while the default-off sweep has not
processed the event. Logical absence is established only by a completed
purge and a clean residual verification over the affected event or complete
paginated receipt set. Writes authorized before expiry or from legacy
paths can leave late residuals, so a previous clean result is not a
permanent certificate. The `sqlite_free_pages_and_wal` scope remains
unmeasured even when logical absence has been verified: physical page
images require the operator erasure step above.
Backups, exports, file-level snapshots, and provider copies remain
independently retained as documented by
[content-retention.md](content-retention.md) and
[security-privacy.md](security-privacy.md). A developer-facing inventory of
which provider-owned copies exist around one installation, with
per-transport ownership, preservation, and erase-ability matrices, is in
[the provider retention inventory](../dev/provider-retention-inventory.md);
it records discovery only and adds no guarantee to this page.

## 6. Coherence requirements

- The admission-identity probes and the guarded collector MUST treat the
  ceiling receipt as a completed collection: a ceiling-purged event is
  `already_collected` for the retention collector, and recreated content
  on an enforced event is a loud inconsistency, never silently
  re-collected.
- The enforcement receipt is immutable: it MUST NOT be rewritten,
  deleted, or duplicated for one event.
- Re-enforcement of residual values MUST NOT relax any guard: the
  fail-closed verification battery is the same battery that proves the
  first purge.

## 7. Operator audit and receipt verification contract

The audit surface is strictly read-only. It MUST NOT purge, tombstone,
release, record, or rewrite anything, and it MUST NOT expose content
values in its reports, diagnostics or logs. Database-side residual predicates
MAY inspect content-bearing fields without returning their values: event
identifiers and census store labels are content-free identity; message text,
excerpts, blob bytes, and other content-potential values never appear in an
audit report. Both modes MUST derive their
probes from the same battery table the enforcement transition verifies
with, so the audit cannot drift from what enforcement proves.

1. **Audit** (`medre storage ceiling audit`, storage method
   `audit_content_ceilings`). Walks the residual battery for content
   whose ceiling has elapsed and reports, per census store, the residual
   rows observed for each event in the audit snapshot (`elapsed_pending`),
   plus enforced events that regained content-potential values through
   writes authorized before their ceiling (`residual_enforced` — the
   documented late-write case). Counts are per-event observations, not an
   additive forecast: a shared blob is attributed only to the event that is
   currently its sole owner, even though a sweep can delete it after removing
   another event's last association. Reports are bounded windows with
   explicit `*_has_more` truncation flags and per-list continuation event
   IDs; a single-event report states one of `unknown`, `not_elapsed`,
   `elapsed_pending`, `residual_enforced`, `enforced_clean`.
2. **Verify** (`medre storage ceiling verify`, storage method
   `verify_content_ceiling_receipts`). Re-runs the residual battery
   against the current database for every enforcement receipt in a
   bounded window in event ID order with a continuation event ID and reports pass/fail per receipt with the receipt's
   recorded content-free counters. A receipt fails exactly when its event
   holds residual content-potential values at verification time; the
   verdict is `passed` only when every examined receipt re-checks clean.
   A receipt's recorded counters MUST NOT be read as claims about the
   current state — the battery re-run is the only claim the verification
   makes about now.

An explicitly requested event without a receipt MUST fail closed: absence
of proof is not proof. A failing verification reports the late-write
boundary, not a broken receipt — remediation is the sweep re-purging the
residual values, never an audit-side mutation, and the receipt stays
immutable. The audit observes the same honest boundary as §5: it
measures the logical census stores only, and receipt presence is not a
physical-erasure certificate.

### Closed acknowledgment evidence after enforcement

The sweep preserves only exact-attempt uncertainty, read-only radio progress
without any wire plan or payload-derived fingerprint, and previously accepted
original radio packet identities in content-free tables. These facts allow
authenticated delayed observations to be recorded during `retry_wait` after
free-form failure diagnostics and radio wire plans are erased. They confer
**no** packet submission authority. Packet observations retain only their
closed ordinal/outbox/attempt scope; clearing transport metadata must not
turn a packet ACK into whole-message acceptance. When a re-sweep revisits an
observation, the retained scope is re-derived from the validated packet
claim and re-fenced: the scope column carries closed identity only, so the
rewrite tightens legacy pre-fence scopes rather than exposing content.

An explicit non-uncertain failure classification for the same attempt
transitions the retained uncertainty state to its closed superseded value in
the writing transaction. Supersession is therefore durable content-free
evidence, not merely a newer diagnostic row: when the ceiling later deletes
the diagnostic text, the admission and read guards still reject the closed
attempt and cannot revive an obsolete uncertainty label.
