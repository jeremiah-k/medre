# Radio text planning

## Immutable wire plan

The shared planner accepts already rendered plain text, a repeated attribution
prefix, an effective UTF-8 byte budget and a maximum part count. The effective
budget MUST exclude transport and firmware overhead before planning. Short text
MUST remain one unnumbered payload. Multipart text MUST repeat attribution and
an `(i/n) ` marker on every part, counting all overhead against the same budget.

The plan MUST preserve authored text exactly when concatenating part bodies,
including whitespace and newlines. The planner SHOULD prefer word boundaries
when possible. Oversized words MAY split at Unicode code-point boundaries;
grapheme clusters are not guaranteed indivisible. Encoded UTF-8 sequences MUST
NOT split. Marker digit growth MUST trigger replanning. An unusable budget or
exceeded part limit MUST reject the complete plan rather than truncate text or
return a partial plan. Source text and attribution prefix are each bounded to
one MiB of UTF-8, independently of the wire budget, with at most 128 parts.

The versioned plan records each part's ordinal, body, exact wire text, byte
length and SHA-256 digest. Its fingerprint covers the version, effective budget,
prefix, source digest and ordered wire payloads. The fingerprint is a content
identifier; it MUST NOT be treated as a native message ID or delivery evidence.
The JSON shape is declared in `../schemas/radio-text-plan.schema.json`.

## Attempt-bound progress and restart refusal

`medre.core.rendering.radio_progress.RadioAttemptProgress` now supplies a pure
versioned snapshot for binding the exact `RadioTextPlan` to one `outbox_id` and
attempt generation. Its ordered part records distinguish `unsent`, `submitting`,
`accepted` and `unknown`. A caller may begin only the first unsent part, and
only after all earlier parts have confirmed local handoff evidence. Every
accepted part must retain a non-empty scoped handoff identifier. Completion
means **all local handoffs accepted**, not remote RF or peer delivery.

The snapshot serializes as closed JSON-compatible data; loading requires the
outbox ID, generation and immutable plan fingerprint from an independent
authoritative outbox claim, and
recomputes source fragments, wire bytes and plan digests before accepting its
contents.
`from_data` converts a restored `submitting` part to `unknown` before returning,
blocking automatic replay and late-observation promotion. An
`unknown` part has no automatic retry transition. Explicit reconciliation
requires separate externally observed evidence and a future authorized API.
The progress shape is declared in `../schemas/radio-attempt-progress.schema.json`.
Recomputed digests establish internal consistency, not authenticity; loading MUST
compare the independently held plan fingerprint as well as attempt identity.
The pure transition object does not itself persist anything. **Production MUST
atomically bind the snapshot to an outbox attempt before native submission,
commit each in-flight transition before calling the SDK, and commit each
observation under compare-and-swap ownership.** No sending path may infer
acceptance merely from a snapshot supplied by an untrusted caller.

## Durable checkpoint boundary

`StorageBackend` now provides atomic binding, submission, observation, recovery
and read-only inspection for the pure progress model. The exact API, SQLite
schema, ownership guards and prerelease database compatibility are specified in
[storage.md §4.16](storage.md#416-radio_attempt_progress). Stored progress MUST
use the existing core database; adapters MUST NOT create a second canonical
outbox or fragment ledger. A new outbox row or generation MUST NOT bypass prior
native handoff evidence for the same delivery identity.

Inspection MUST leave retained submitting records unchanged. Explicit recovery
MUST commit their transition to unknown before exposing them to a sender. A
returned submitting checkpoint authorizes one local invocation under the
current claim; it does not make a call atomic with SQLite. A crash after its
commit but before invocation remains conservatively unknown after restart.

## Production integration boundary

MeshCore MAY opt into the synchronous guarded handoff described below. Meshtastic
retains single-packet bounded rendering; its deferred queue requires a separate
claim-ownership integration before it can participate. Physical transport
qualification remains separate from deterministic implementation tests.

Before sending any part, production integration MUST bind and persist the exact
plan to its outbox attempt. Every native handoff observation MUST retain part
ownership. One accepted part MUST NOT complete the parent delivery. Partial
acceptance, cancellation, failure and unknown handoff MUST remain distinguishable.
Recovery MUST reuse the persisted plan and known unsent/retryable progress rather
than rerendering or blindly repeating accepted parts. Ambiguous crash windows
can produce duplicate native delivery; exactly-once RF delivery is not promised.

All parts MUST pass through the adapter's serialized send owner and obey
message_delay_seconds across other queued messages and fanout. A parallel loop
that bypasses pacing is not an integration of this contract. Native emoji
tapbacks remain single packets. Replies MUST use proven destination-native
references; ambiguous whole-message targets degrade to readable text. Multiple
packet IDs per canonical event MUST retain their part ordinals and native scope.

Qualification MUST cover byte budgets, marker digit boundaries, multibyte names,
whitespace, long words, part limits, queue pressure, reconnect between parts,
shutdown, crashes around handoff, restart progress and exact/ambiguous relations.
Installed-SDK tests qualify API use; serial/BLE/TCP and MeshCore RF campaigns
are required to claim native transport qualification.

## Synchronous guarded handoff

A participating adapter MUST explicitly declare `supports_radio_multipart=True`.
The renderer MAY supply a typed `RenderingResult.radio_plan` containing the exact
wire text for every packet. Core MUST bind the complete plan to the durable
attempt before invoking the adapter and MUST preserve the original rendered
payload keys while substituting only the already-planned `text` for each part.
This is materialization of renderer-owned wire bytes, not a second rendering pass.

For each packet, core MUST supply a fresh, one-use `before_native_send` callback.
The adapter MUST invoke it inside its existing serialized send owner, after
pacing and immediately before the SDK command. The callback MUST commit
`begin_radio_part` under the dispatch's independently supplied worker identity;
reading the current worker from storage MUST NOT authorize an older dispatch.
An expired, superseded or reused grant MUST prevent the SDK call. The dispatch
deadline MUST be checked again at this boundary after pacing. Core MUST
discard any grant callback supplied by a renderer.

A granted invocation MUST NOT use an adapter-internal retry loop. A timeout or
other uncertain result after the grant MUST stop the plan and retain unknown
progress where claim ownership still permits the observation. If that write
fails or loses ownership, the persisted submitting record MUST remain blocking.
Cancellation MUST propagate. An outage proven to precede the first grant MAY
remain transient under the existing delivery policy. Failure after an accepted
prefix MUST be permanent for automatic retry purposes; no new generation may
blindly replay that prefix. This API does not authorize operator reconciliation.

Only synchronous `transport_handoff` results MAY accept a part. Each retained
handoff reference (the JSON shape in `../schemas/radio-local-handoff.schema.json`)
MUST include its ordinal, native channel, actual native ID
(or explicit null), and the declared confirmation level. Local acceptance with
no native ID MUST NOT manufacture a native message ID. Every part MUST have
committed acceptance before core returns a whole-plan handoff. Its confirmation
level MUST be the weakest declared part level, without promotion to recipient
delivery. A multipart aggregate MUST omit scalar native message/channel IDs so
one arbitrary packet cannot become the canonical message's native reply target.
The delivery lifecycle remains the sole owner of parent outbox completion.

`medre inspect radio-attempt OUTBOX_ID ATTEMPT --storage-path PATH` MUST open
storage read-only and report the selected snapshot together with current parent
identity/status. Inspection MUST leave submitting unchanged and MUST NOT start
an adapter, recover progress, infer recipient delivery or grant submission.
The output follows `../schemas/radio-attempt-inspection.schema.json`; absent
attempts exit 5 and invalid generations exit 2.
