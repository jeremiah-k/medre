# Canonical body identity — current prerelease contract

For each newly admitted canonical event, core commits exactly one
`event_body_identity` row with `(event_id, version=1, present_at_admission,
recorded_at)`. `present_at_admission` is strictly `0` or `1`: the payload has
string `body`, or string `text` only if there is no `body` key. Empty strings
count as present; `null` and invalid body values do not. The fact commits
atomically with the event, relations and durable ingress work (where present).

`get_body_presence_at_admission(event_id)` MUST return `None` only for an
unknown event, `True` for a present body and `False` for an absent body.
Missing or invalid persisted facts MUST raise an invariant error. This field
is explicitly **not** current retention, availability, a read grant, or
permission to send. It cannot change when future content expires.

MEDRE is pre-first release: previous SQLite shapes are refused. There is no
in-place migration or compatibility reader. Event bodies, reply excerpts,
rendering evidence, packet plans, attachments and provider-owned copies must
all be included before core-owned content can be collected. This contract
introduces no deletion, expiry clock, hold grants, read authorization,
delivery-deadline enforcement or replay permission. The existing runtime's
read and dispatch paths are unchanged.
