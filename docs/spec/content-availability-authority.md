# Content availability and holds: phase-one policy preview

This module introduces a **versioned, pure policy preview** in
`medre.core.storage.content_availability`. It does not alter storage, run at
message admission, grant access, enforce a runtime deadline or delete content.

The explicit results are `retained`, `unavailable` (bytes absent or hard ceiling
elapsed) and `denied` (access is refused without revealing whether bytes are present).
A bounded optional consumer must be in the policy allowlist, request the exact
scope (`body`, `reply_excerpt`, `attachment`), and remain within the configured
hold window. Even mandatory dependency holds are limited by an operator-supplied
hard ceiling in this **preview**; hard ceilings require future dispatch/admission
coordination so outstanding work cannot be sent without its required content.

`preview_release_blockers` reports outstanding core delivery, inbound reply
relations, and unknown provider copies separately. An empty blocker list is
**not** permission to delete; the inventory is incomplete by construction.

## New opt-in persisted body-read grants (not whole-runtime authority)

Core SQLite now implements `issue_optional_body_hold`,
`revoke_optional_body_hold`, and `read_body_for_consumer`. One immutable
per-event ceiling and one exact `(event_id, body, consumer)` grant are stored
transactionally; no event receives an implicit grant. An optional hold MUST
name an allowlisted consumer, be issued at or before the observation time,
expire within the policy maximum and the event-level ceiling, and MUST NOT be
reissued after revocation or observed expiry. SQLite separately records
`revoked_at` for explicit revocation and `expired_at` for a permanently
observed deadline; clock rollback cannot restore either closed grant. Bounds MUST compare UTC instants and elapsed
durations, including across daylight-saving transitions. Non-retained read
results MUST carry no body value. Incompatible policy attempts fail closed.

A `read_body_for_consumer` call returns `denied` without body or existence
information when the authenticated internal caller lacks a live grant, and
returns `unavailable` only to a granted requester whose admitted body was
absent. An empty string remains a present body. The grant predicate and
content selection MUST share one SQLite statement/snapshot. This low-level
storage API MUST report SQLite failures and payload-integrity violations as
`StorageError`, preserving SQLite exception causes. Invalid caller requests
remain `ValueError`. The API is for trusted core code; a consumer string is not authentication
and must never be exposed to an untrusted extension as a capability.

**This is not operational content expiry.** The prerelease SQLite shape now
separates canonical body/text and relation excerpts and persists mandatory
content dependencies atomically with ingress/outbox writes. `read_event_for_ingress`,
`read_event_for_outbox`, `read_body_for_outbox`, and
`authorize_native_submission` provide held-owner checks used in selected
runtime paths. Neither the optional-consumer grants nor these dependency rows
authorize collection. Generic event/relation reads and ordinary operator
inspection now use metadata-only projections. Replay and reply content reads
require a live source dependency; native starts require the exact worker,
reserved attempt and lease. Retained recovery records, packet wire plans,
attachments and provider-owned copies remain sensitive. See
[content runtime authority](content-runtime-authority.md).

## Required runtime integration gates

Before enabling an operator policy, implement one persisted content-reference
and availability model with bounded grant rows and validated constraints.
Admission now captures mandatory ingress/outbox dependencies transactionally;
remaining readers MUST distinguish retained, unavailable and denied content
without rewriting native attempt fences. Incompatible prerelease layouts MUST be
refused and replaced with fresh development state; legacy readers, backfills
and upgrade paths are not part of this contract.

A write-capable collector MUST account for relations, observations, radio
packet plans, attachment content, provider journals, WAL/backups and external
copies, with read/dispatch revalidation and race tests. Plugin consumers MUST
use the same bounded grant authority after plugin execution permissions are
independently qualified.

Selected ingress and outbox runtime paths now use owner-scoped reads. **No
retention default, collection, erasure guarantee or plugin grant is introduced.**

## Preview trust and serialization boundary

The `requester` argument MUST be supplied explicitly. `requester=None` is an
internal inspection context; it MUST NOT be treated as
authenticated access for an omitted external principal. Mandatory holds are
trusted internal dependency facts, not optional read grants. An optional
requester MUST have its own live optional hold before the preview reveals byte
presence or hard-ceiling expiry. A future-issued hold MUST NOT be active,
including a mandatory hold. Runtime authentication and persisted grant creation
remain separate integration requirements.

Policy version `1` MUST be an integer, not a boolean. Dependency counts MUST be
nonnegative integers. Optional allowlists are immutable snapshots. Serialized
decisions MUST conform to `content-availability-decision.schema.json`; they
carry no content and do not authorize deletion.

This pure model assumes its caller has selected holds for one content object;
it does not carry or validate a persisted event/content identity. Runtime
integration MUST bind every grant to the selected content reference and reject
cross-object reuse before invoking this policy. This is an integration gate,
not authority supplied by the preview's metadata.

Direct decision construction MUST enforce the version, result/reason pairing,
held-until presence and timezone awareness before serialization. UTC metadata
MUST NOT depend on the host's local timezone or optional format-checker packages.
