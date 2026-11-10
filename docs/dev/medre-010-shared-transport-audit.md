# Pre-0.1.0 shared-event boundary audit

This is a design and verification record, not approval to tag. The canonical
`CanonicalEvent` model is transport-neutral; Matrix, LXMF, and XMPP carry
**bounded projections** of it over their respective native protocols. The
`medre.envelope` in Matrix content is a provenance projection, **not** a
serialized, independently authenticated `CanonicalEvent`. Matrix text is
still presented in the Matrix `body`; an envelope is never permission to
replace that body. The same distinction applies to other transports.

## Decisions (owner-approved)

- **D1 — distribution: direction CLOSED.** Publish stable core and adapter
  packages to an index while supporting Git branches, tags, full commits,
  forks, and editable development checkouts. Adapters release independently.
  Official stable/development channel selection and remembered custom sources
  belong in package-management commands. The source-channel interface and
  release dependency migration require implementation and qualification;
  no package publication is authorized by this record.
- **D3 — metadata/trust: CLOSED.** MMRelay 1.5 emits only canonical MEDRE
  metadata, never flat `meshtastic_*` keys. The visible canonical body is
  authoritative. Other senders' metadata is trusted only for the relay itself
  or globally allowlisted explicit peer MXIDs; no wildcard/TOFU, no
  power-level-based authorization, no legacy compatibility mode. The MMRelay
  implementation is a separate reviewed change on PR #835.
- **D4 — node identifiers: CLOSED.** Canonical `source_transport_id` retains
  the radio node ID. On newer Meshtastic firmware (2.8+) this ID changes with
  key rotation, while older firmware may retain it; it is already broadcast on
  the mesh. No extra disclosure toggle is required by this decision.
- **Signing: later schema version.** Claims in current envelopes are hints,
  never verification or authorization facts. A versioned MEDRE signing feature
  will be proposed after 0.1.0 and consumed by MMRelay later.

## Generic interoperability contract

| Purpose                      | Canonical field                                   | Wire representation / limits                                            |
| ---------------------------- | ------------------------------------------------- | ----------------------------------------------------------------------- |
| Authored text                | `payload.body`                                    | Destination transport's authored text; never a native metadata override |
| Event kind / emote           | `event_kind`, `payload.msgtype`                   | Matrix text/emote representation; not inferred from packet IDs          |
| Source node                  | `source_transport_id`                             | Generic `bridge_context.source.sender_id`                               |
| Logical mesh/channel         | `source_channel_id`                               | `bridge_context.source.channel`                                         |
| Packet ID (including `0`)    | `source_native_ref.native_message_id`             | `bridge_context.source.native_ref`                                      |
| Reply / reaction             | `relations[]`                                     | `bridge_context.relations[]` (bounded to 32)                            |
| Reaction emoji               | `EventRelation.key`                               | `bridge_context.relations[].key`                                        |
| Target packet ID             | `relations[].target_native_ref.native_message_id` | Scoped bridge relation target, no implicit ID guessing                  |
| Display long/short names     | Adapter-native namespace, attribution projection  | `bridge_context.source.sender_name` / `sender_short_name` when supplied |
| Portnum / detection metadata | `metadata.native.data["meshtastic"]["portnum"]`   | Explicit `bridge_context.source.native_facts.fields.portnum` projection |

**Proven transport mechanisms:** Matrix embeds MEDRE provenance and bridge
context in JSON event content; LXMF embeds bounded bridge context inside its
`0xFD` custom fields; XMPP encodes the same bounded context in its stanza
extension. None of these embedded claims confer sender authority. Non-Matrix
peers may exchange relation hints without a Matrix account or homeserver.

## Source projection and qualification gates

Source adapters explicitly select public scalar facts in their native
`public_facts` map. Core validates and bounds the generic `source.native_facts`
projection. Matrix, LXMF, and XMPP carry that same context; sender names remain
generic attribution fields. This permits Meshtastic portnum delivery without
exposing the entire radio packet or adding a Matrix-specific namespace.

Runtime and malformed-input tests must cover each adapter's actual serializer.
A passing digest inventory records schema bytes; it does not qualify a live
shared-room round trip, native relation authority, hardware, or E2EE recovery.
Do not claim a completed freeze until the coordinated consumers, final
compatibility pins, clean installs, and live integration gates pass.

The Matrix adapter's native schema excludes `interop.mmrelay`. Its explicit
legacy presentation API remains outside the canonical contract and must not be
enabled by MMRelay 1.5. Canonical reactions require an exact nonempty relation
key; Matrix rejects payload/body/default-symbol substitutions. Meshtastic
classification drops empty or malformed tapbacks, and its codec and renderer
reject fabricated or implicit keys. Exact-key tests qualify these paths. Signing remains deferred.
