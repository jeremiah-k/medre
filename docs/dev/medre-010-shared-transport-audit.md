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
| Portnum / detection metadata | `metadata.native.data["meshtastic"]["portnum"]`   | **Not included in default Matrix safe-mode bridge context**             |

**Proven transport mechanisms:** Matrix embeds MEDRE provenance and bridge
context in JSON event content; LXMF embeds bounded bridge context inside its
`0xFD` custom fields; XMPP encodes the same bounded context in its stanza
extension. None of these embedded claims confer sender authority. Non-Matrix
peers may exchange relation hints without a Matrix account or homeserver.

## Unresolved end-to-end projection issue — pre-tag gate

MEDRE's core event can store Meshtastic portnum, sender names and channel under
its versioned source-native namespace, but Matrix `safe` mode intentionally
omits unbounded native metadata. There is currently no source-adapter-approved,
portable safe projection for portnum over `bridge_context`. Likewise, the
`bridge_context` relation hints do not constitute authorization to mutate
native messages. Do not claim that a Matrix shared-room peer reconstructs all
original source-native facts, or that arbitrary upstream provenance has been
verified. Add a bounded, validated source-native transport projection _with
adapter-owned allowlists_ before relying on portnum-sensitive behavior in a
remote peer. Exercise Matrix and LXMF/XMPP representations with the same
corpus and malformed/oversized inputs. This is a functional gate before the
first freeze tag, not a speculative addition to an already published schema.

The Matrix adapter's 0.1.0 native schema must not contain `interop.mmrelay`.
Old MMRelay content keys are neither canonical fields nor relation authority.
The Matrix adapter's legacy opt-in presentation code remains outside the frozen
canonical/native schema and must not be enabled by MMRelay 1.5. This renderer
may still fill a missing structured reaction key using payload/body/placeholder
fallback; that is **not** an approved way to form an MMRelay 1.5 structured
reaction. Exact nonempty `EventRelation.key` enforcement is an approved
requirement; implement and test it in the renderers before qualifying the full
0.1 runtime, including native Meshtastic empty-emoji classification. Retiring that
standalone opt-in API is a separate code-removal decision, not proof that it
belongs in the frozen contract.
