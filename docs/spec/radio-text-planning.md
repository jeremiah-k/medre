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
return a partial plan. Input is bounded to one MiB and at most 128 parts.

The versioned plan records each part's ordinal, body, exact wire text, byte
length and SHA-256 digest. Its fingerprint covers the version, effective budget,
prefix, source digest and ordered wire payloads. The fingerprint is a content
identifier; it MUST NOT be treated as a native message ID or delivery evidence.
The JSON shape is declared in `../schemas/radio-text-plan.schema.json`.

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
