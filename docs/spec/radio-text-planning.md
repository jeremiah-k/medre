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

## Production integration boundary

This planner does not enable splitting in production adapters. MeshCore and
Meshtastic retain their existing bounded rendering until durable part progress
and exact-attempt feedback aggregation are implemented together.

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
