# Fence retention writes against epoch rotation and short policy gaps

A LIVE retention worker now passes its selected activation epoch through to
the existing guarded per-event collector. Under the collector's own SQLite
`BEGIN IMMEDIATE` transaction, the epoch must still be newest, in relay-only
mode, and the event must lie strictly after that epoch's admission watermark.
An intervening operator activation refuses the old write without collecting
content or incrementing an integrity-refusal count; the next cycle starts
from the new epoch's keyset head. An old worker heartbeat likewise cannot
advance a superseded epoch.

On a newly started LIVE worker with a still-fresh predecessor, MEDRE opens a
new append-only activation epoch rather than assuming its previous heartbeat
proves the policy was continuously enabled. This conservatively excludes any
short disabled/re-enabled interval. A stale predecessor continues to require
explicit operator activation. A failed opening transaction does not permit
fallback to the previous epoch.

Manual, confirmed exact-event collection retains its prior API and authority.
This change does not add physical erasure, multi-process worker coordination,
or a global retention deadline.

The retention preview now implements its advertised bounded continuation:
`preview_content_retention(after=(created_at, event_id))` pages the same
watermark-scoped admission order and emits a pair-shaped `next_after` with
`has_more`. Its JSON Schema, normative documentation, and aggregate-only
operator status are aligned. The runtime-config schema now enforces the
loader's `relay-only` / `enabled=true` condition. The new policy tests use
function-style pytest declarations.

Direct calls to the runtime worker's `sweep()` now respect disabled, restricted,
and abandoned gates, even when the background scheduler was never started.
