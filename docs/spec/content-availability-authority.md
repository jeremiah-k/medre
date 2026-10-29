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

## Required migration and integration gates

Before enabling an operator policy, implement a separately reviewed, transactional
storage migration with versioned availability rows and bounded grant rows.
Migration must validate every old shape, distinguish absent/expired/denied
content, record mandatory ingress/outbox dependencies at admission, and never
rewrite native attempt fences. A write-capable collector must account for
relations, observations, radio packet plans, attachment content, provider
journals, WAL/backups and external copies, with optimistic read/dispatch
revalidation and race tests. Plugin consumers must use the same bounded grant
authority after plugin execution permissions are independently qualified.

The installed runtime retains its prior behavior. **No retention default,
expiry, erasure guarantee, data migration or plugin grant is introduced here.**

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
