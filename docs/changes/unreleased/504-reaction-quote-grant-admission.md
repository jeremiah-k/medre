# Ceiling-governed admission issues the bounded reply-quote grant

Resolved-target reactions rendered `reacted KEY to an earlier message` even
when the reaction's target ref resolved to a stored canonical event. The
described reaction's preview comes from core relation enrichment only, and
its authorized quote read (`read_reply_context`) borrows either a live
source hold or a previously issued bounded `reply_quotes` body grant.
Durable ingress completion settles the target's source hold (terminal
content settlement), and a realistic reaction always arrives after its
target was fully processed — so the live-source branch was a sub-second
race the reaction could never win, and the grant branch could never fire
because no runtime code ever issued the grant. The enricher fail-closed to
the metadata projection, found no quote text, and the renderer degraded to
the honest wording. No configuration could provision the grant: issuance
existed only as a storage-level API exercised by tests.

Ceiling-governed admission is now the sanctioned issuance point. When an
admission carries a content deadline (the operator-enabled
`content_ceiling`), the admission transaction installs the per-event
ceiling row allowlisting exactly the `reply_quotes` consumer and issues one
exact bounded body grant for it, expiring with the ceiling. A reaction (or
reply) delivered after ingress completion then quotes the target's
canonical text for the grant's lifetime — bounded by the declared ceiling,
ended by expiry or revocation, independently of how long the canonical
content remains stored; the hard ceiling, the durable expiry
tombstones, and explicit revocation all remain enforced on the borrowed
read. Admissions without a configured ceiling install no ceiling row and
issue no grant, so their post-completion behavior is unchanged: the
described reaction still degrades honestly to `an earlier message`, never
to an empty quote. Reply and reaction enrichment still never mints grants.

The Matrix transport profile now documents when the described-reaction
preview is available; the content authority, availability, and retention
spec pages describe the producer-side issuance contract, and the
configuration reference notes that enabling the ceiling is what provisions
quote previews.
