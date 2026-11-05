# Matrix↔XMPP reference bridge qualification

## Authority and purpose

The optional `reference` lab stage MUST run MEDRE's existing native fidelity
campaign against a disposable Synapse/Prosody test network. It MUST use
independent Matrix and XMPP peer accounts and MUST evaluate the actual runtime
against its SQLite ledger. A native local send receipt, offline readiness
report, preview, synthetic event, or successful process start alone MUST NOT
produce a positive reference verdict.

The reference journey MUST NOT create a parallel adapter execution path or a
second delivery/recovery authority. It inherits the native campaign's bounded
timeouts, refusal behavior, and exact committed-reference analysis.

## Execution and isolation

The lab's `reference-oauth` stage MUST qualify only the MEDRE Matrix OAuth
device. It MUST NOT require MMRelay OAuth. The `reference-provision` stage
MUST provision only Matrix and XMPP, retain separate outcomes for each, and
refuse the stage if either is not ready. It MUST reuse an existing ready
fixture rather than silently recreate its identity. These opt-in stages MUST
NOT become additional steps of the grouped `native` or `all` campaign.

The `reference` stage MUST require an already provisioned Matrix room and
XMPP MUC with independently authenticated peer identities. It MUST run the
standard ten-case bidirectional native fidelity campaign and recheck its
retained result through `scripts.analyze_fidelity_campaign.analyze`. The
analyzer MUST reconcile native source acknowledgments, independent recipient
records, canonical persisted events, committed native references, runtime
restart order, duplicate counts, and cleanup before returning `passed`.

## Content-free verdict

Only after the complete read-only analyzer returns success MAY the reference
stage write `reference-verdict.json`, alongside its private native campaign
captures. The verdict MUST use the exact `CASES` order and status, require both
source transports, and report only a closed evidence level, case count,
direction totals, and qualification limits. It MUST include the MEDRE, Matrix, and XMPP
source snapshot digests, commits, and dirty-state indicators from `sources.json`
while omitting checkout paths. Before and after the campaign, the snapshot
hashes MUST match the tracked-file bytes in the private build context, and
those tracked files MUST match the actual runner image's `/sources` tree.
Missing, altered, or malformed source bindings MUST refuse
the positive verdict. The inventory MUST be rechecked after the campaign; a
changed source snapshot MUST NOT produce a positive verdict. It MUST NOT
contain raw messages,
account credentials, recipient identifiers, native reference IDs, or exception
text. A missing, incomplete, stale, or failed campaign MUST NOT produce a
positive verdict.

Native captures, SQLite files, OAuth profiles, fixture YAML, and raw logs
remain private under the lab directory. A retained verdict is meaningful only
in conjunction with the private complete evidence and revision-bound source
snapshots in `sources.json`. It is not a transferable assertion that an
unrelated build or transport configuration passed.

## Explicit boundaries

The qualified path is two-way **plaintext** Matrix↔XMPP through the disposable
Docker network. It proves independent peer receipt for the ten cases under
those fixtures. It does not establish Matrix encrypted peer-device trust,
production account provisioning, live Internet behavior, attachment parity,
radio delivery, or permission to resend uncertain native submissions.

The legacy lab `prepare` operation still requires a development profile with
seven explicit adapter checkouts and an MMRelay candidate; these are build
provenance inputs, **not** native prerequisites of `reference-oauth`,
`reference-provision`, or `reference`. Reducing that historical lab preparation
requirement is separate from this reference stage.
