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
load). Remote timestamps, provider TTL hints, retries, plugins, new
replays, and unresolved handoffs MUST NOT extend it. The ceiling row
(`event_content_access_policies.hard_ceiling`) is written once, on the
first authorized policy installation in the admission transaction, and
future grants cannot extend it. The effective delivery deadline is the
earlier of the event ceiling, the frozen plan deadline, and the adopted
target dependency deadline; the deadline machinery may only end earlier.

## 2. The invariant

When a ceiling (or a retention epoch, or a delivery deadline acting as the
effective bound) expires content, expiry enforcement MUST leave that
content unreachable and gone across every store enumerated in the census
below, according to that store's declared "gone" class, before the
enforcement receipt commits.

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

| Store                                                           | Content values attributable to one event                | Class                     | Enforcement action                                                                                                                                                                                                        |
| --------------------------------------------------------------- | ------------------------------------------------------- | ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `event_body_content`                                            | `body`                                                  | purged                    | rows deleted                                                                                                                                                                                                              |
| `event_relation_excerpts` (own event)                           | `excerpt`                                               | purged                    | rows deleted                                                                                                                                                                                                              |
| `event_relation_excerpts` (incoming, quoting the event)         | `excerpt`                                               | purged                    | rows deleted on the quoting events' relations                                                                                                                                                                             |
| `canonical_events`                                              | `payload`, `metadata`                                   | cleared-on-surviving-rows | values set to `'{}'`; identity columns retained                                                                                                                                                                           |
| `event_relations`                                               | `key`, `metadata`                                       | cleared-on-surviving-rows | `key` nulled, `metadata` set to `'{}'`; relation identity retained                                                                                                                                                        |
| `native_message_refs`                                           | `metadata`                                              | cleared-on-surviving-rows | set to `'{}'`; correlation identity retained                                                                                                                                                                              |
| `delivery_outbox`                                               | `metadata` (inline frozen dispatch context)             | cleared-on-surviving-rows | set to `'{}'`; generations never deleted                                                                                                                                                                                  |
| `delivery_observations`                                         | `metadata`                                              | cleared-on-surviving-rows | set to `'{}'`; evidence rows retained                                                                                                                                                                                     |
| `delivery_evidence_diagnostics`                                 | `diagnostic`                                            | purged                    | rows deleted for the event's receipts, observations, outbox rows, and ingress work                                                                                                                                        |
| `radio_attempt_progress`                                        | `plan_json`, `provenance_json`, `parts_json`            | purged                    | rows deleted for the event's outbox generations                                                                                                                                                                           |
| `delivery_outbox_snapshots`                                     | `context_json`                                          | purged                    | rows deleted for the event's outbox generations                                                                                                                                                                           |
| `event_attachment_associations`                                 | the load authority for retained bytes                   | purged                    | rows deleted                                                                                                                                                                                                              |
| `attachment_blobs`                                              | `data` (plaintext bytes)                                | purged                    | row deleted only when no other event still refers to the `content_ref`; shared bytes go with the last reference                                                                                                           |
| `delivery_receipts`                                             | none (registry: all columns identity/authority)         | retained-identity         | rows retained; G01/G02 settlement fences untouched                                                                                                                                                                        |
| `delivery_handoff_settlements`                                  | none (content-free append-order fence)                  | retained-identity         | rows retained; never touched by enforcement                                                                                                                                                                               |
| `operator_delivery_decisions`                                   | `target_channel` is conservatively unclassified         | retained-identity         | rows retained: immutable audit evidence whose schema contract makes them write-once; they hold delivery addressing, never message content; the copy family stays a declared wholesale blocker for the retention collector |
| `event_content_dependencies`                                    | none (authority)                                        | retained-identity         | all unsettled owners released at enforcement (the ceiling supersedes them)                                                                                                                                                |
| `event_content_access_grants`                                   | none (authority)                                        | retained-identity         | all live grants tombstoned `expired_at` at enforcement                                                                                                                                                                    |
| `event_content_access_policies`                                 | none (authority)                                        | retained-identity         | the immutable ceiling row is the enforcement trigger                                                                                                                                                                      |
| `event_body_identity`                                           | none (admission fact)                                   | retained-identity         | retained; coherence probes accept the ceiling receipt                                                                                                                                                                     |
| `event_routing_facts`                                           | none (bounded route identifiers)                        | retained-identity         | retained                                                                                                                                                                                                                  |
| `event_content_collection_log`                                  | none (receipt counters)                                 | retained-identity         | retained                                                                                                                                                                                                                  |
| `event_content_ceiling_log`                                     | none (receipt counters)                                 | retained-identity         | one row per enforced event; never rewritten                                                                                                                                                                               |
| `content_retention_epochs`                                      | none (activation boundary)                              | retained-identity         | retained                                                                                                                                                                                                                  |
| `conversation_membership`, `conversation_projection_state`      | none (projection linkage)                               | retained-identity         | retained                                                                                                                                                                                                                  |
| `event_relations.id`/targets, relation type                     | none                                                    | retained-identity         | retained                                                                                                                                                                                                                  |
| `durable_ingress_work`                                          | none (`provenance` is a closed marker)                  | retained-identity         | retained                                                                                                                                                                                                                  |
| `adapter_checkpoints`                                           | `cursor`, `metadata` — adapter-scoped, not event-scoped | unmeasured                | no event attribution exists; documented unmeasured scope                                                                                                                                                                  |
| `plugin_state`                                                  | `value` — plugin-scoped, not event-scoped               | unmeasured                | no event attribution exists; documented unmeasured scope                                                                                                                                                                  |
| `inbound_pressure_observations`, `inbound_pressure_outage_gaps` | none (counters only)                                    | retained-identity         | retained                                                                                                                                                                                                                  |
| `delivery_status`                                               | view; classified through `delivery_receipts`            | retained-identity         | follows its base table                                                                                                                                                                                                    |
| `_medre_schema_meta`                                            | none (sweep cursors, schema stamp)                      | retained-identity         | retained; cursor values are keyset positions, not content                                                                                                                                                                 |
| SQLite free pages and WAL frames                                | former page images of any of the above                  | unmeasured                | explicit operator erasure step (§5)                                                                                                                                                                                       |

The registry contract (`STORAGE_COLUMN_CLASSES`) is the column-level
authority for "content-potential": the enforcement purge and the residual
verification battery clear and re-check exactly those columns on
event-attributable rows. A new persistent table MUST be added to the
registry with reviewed classes and, if it can hold event-attributable
content-potential values, to the purge and the battery in the same
change.

## 4. Enforcement transition

The guarded ceiling-enforcement transition is a single mutation boundary:

- It MUST run under `BEGIN IMMEDIATE` with the storage write lock, so the
  purge, the verification, the authority tombstones, and the receipt
  commit atomically or not at all.
- Candidate discovery MUST be bounded per invocation with a durable keyset
  cursor that wraps at queue end, so backdated admissions are still
  enforced and one event can never starve others.
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
- Writes racing the sweep from the pre-ceiling authorization window MAY
  append evidence rows afterwards; the sweep's residual pass removes
  their content values, and the receipt stays valid. This is the honest
  boundary between "gone by enforcement" and writes already authorized
  before the instant.

Runtime invocation rides the retry poll cycle behind the default-off
`retry.ceiling_sweep` declaration, bounded and failure-isolated like the
deadline sweep. Storage-level invocation remains available to trusted
operator tooling.

## 5. SQLite-level guarantee and the explicit erasure requirement

Row deletion and value clearing are logical operations. SQLite does not
securely erase freed pages: after the purge, former content bytes can
remain recoverable in freelist pages and WAL frames until they are
overwritten, and `PRAGMA secure_delete` is NOT enabled by this product.
The storage layer MUST NOT claim byte-level erasure from logical
enforcement alone.

An operator who requires byte-level erasure MUST, after enforcement:

1. run `PRAGMA wal_checkpoint(TRUNCATE)` against the live database (or
   stop the runtime), and
2. run `VACUUM` on the database file (which rewrites the database image
   without freed content pages), or enable `PRAGMA secure_delete=ON`
   before enforcement so freed pages are zero-filled at delete time.

Until that step, the honest statement is exactly the standing
`sqlite_free_pages_and_wal` unmeasured scope: expired content is
unreachable through every read path and absent from every logical store,
while physical page images require the operator erasure step above.
Backups, exports, file-level snapshots, and provider copies remain
independently retained as documented by
[content-retention.md](content-retention.md) and
[security-privacy.md](security-privacy.md).

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
