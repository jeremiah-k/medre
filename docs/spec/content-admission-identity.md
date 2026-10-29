# Canonical body identity — current prerelease contract

For each newly admitted canonical event, core commits exactly one
`event_body_identity` row with `(event_id, version=1, availability, recorded_at)`.
`event_id` is a foreign key and primary key. `availability` is `retained` only
when the accepted canonical payload explicitly contains string `body`, or a
string `text` when no `body` key exists. An empty string is _present_;
missing, `null`, or invalid explicit body is _unavailable_. The manifest is
committed alongside the event, relations and durable ingress work when admitted
through `admit_ingress`, or alongside the event when written with `append`.

`get_body_availability(event_id)` MUST accept every non-empty string allowed
by the canonical event identity contract. It returns `None` only when no
canonical event exists. An admitted event with a missing or invalid identity
row MUST raise a storage invariant error. It returns `unavailable` when the
admitted event has no body, and `retained` for a present body. The lookup
returns no bytes or permission.
`retained` is an immutable **admission observation**, not evidence that a
later caller is authenticated or that content remains after eventual expiry.

MEDRE is pre-first release: previous SQLite shapes are refused. There is no
in-place migration or compatibility reader. Event bodies, reply excerpts,
rendering evidence, packet plans, attachments and provider-owned copies must
all be included before core-owned content can be collected. This contract
introduces no deletion, expiry clock, hold grants, read authorization,
delivery-deadline enforcement or replay permission. The existing runtime's
read and dispatch paths are unchanged.
