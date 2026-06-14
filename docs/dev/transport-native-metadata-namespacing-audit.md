# Transport-Native Metadata Namespacing Audit

Factual audit of how each MEDRE transport adapter namespaces the native
metadata it emits into `CanonicalEvent.metadata.native.data`, which keys
remain bare, and which bare keys are intentional versus legacy input
tolerance. This document is evidence of review, not normative authority.
The per-transport projection rules live in each
[transport profile](../spec/transport-profiles/) and the sender-identity
semantics live in [Routing and Delivery](../spec/routing-delivery.md).
Where this audit conflicts with the spec, the spec takes precedence.

Companion audits:

- [Relay Prefix and Provenance Audit](relay-prefix-attribution-audit.md)
  covers prefix-template rendering and provenance capture.
- [Transport-Native Identity Enrichment Audit](transport-native-identity-enrichment-audit.md)
  covers adapter-native state projected into generic sender fields.
- [Native Relations Audit](native-relations-audit.md) covers native-ref
  ownership, storage, and per-transport relation classification.

This audit focuses on one question: which native metadata keys are
namespaced by transport, which stay bare, and why.

## Scope and Boundaries

Namespacing here means a key carries a `<transport>.` prefix
(`meshtastic.from_id`, `meshcore.sender_id`, `lxmf.display_name`). Bare
keys carry no such prefix (`from_id`, `packet_id`, `source_hash`).

The identity-projection boundary is the structural reason namespacing
matters. Core rendering and core planning are transport-neutral: they
never inspect native transport keys. Each adapter owns a
`project_<transport>_attribution` helper that maps its native keys onto
the generic `RelayAttribution` sender fields, and
`_attribution_dispatch.project_source_fields` only detects the platform
and delegates. Namespacing the identity keys lets platform detection
(`detect_source_platform`) identify a sparse native dict unambiguously
and keeps per-transport identity from colliding across adapters.

Three constraints bound the current work package and shape what this
audit treats as in-scope versus deferred:

1. No stable public identity API. Sender identity is observational, not
   a contracted surface.
2. No topology or contact canonical events. Contact/pubkey/announce
   state stays adapter-local.
3. No receipt-semantics changes. Storage, outbox, and delivery-receipt
   behavior is unchanged by anything described here.

Two invariants carry through from the companion audits and are restated
because they govern how bare-key tolerance is interpreted:

- Old bare-key tolerance exists only to read stored events and existing
  test fixtures produced before namespacing, unless a bare key is
  intentionally retained for a documented non-identity consumer. It is
  not a license to emit bare identity keys from new code.
- Storage, outbox, and receipt evidence semantics are unchanged.
  Identity enrichment is observational and stale-able: it is not
  delivery evidence, not authoritative storage state, and may lag the
  transport SDK's live state.

Identity labels may appear in rendered text and in renderer-local
`RenderingResult.metadata` (including the normalized `relay_prefix_*`
keys). The authoritative machine-readable provenance source remains the
MEDRE metadata namespace (`medre.envelope` on Matrix, `fields[0xFD]` on
LXMF, `RenderingResult.metadata` on all transports).

---

## Matrix

### Matrix: Emitted native metadata keys

The Matrix codec (`MatrixCodec.decode` in
`src/medre/adapters/matrix/codec.py`) emits flat keys with no
`matrix.*` namespace. Matrix native data is identified by its
characteristic keys, not by a namespace prefix.

| Key                      | Source                                 | Namespaced? |
| ------------------------ | -------------------------------------- | ----------- |
| `room_id`                | Decode `room_id` argument              | No (flat)   |
| `event_id`               | Native event `event_id`                | No (flat)   |
| `sender`                 | Native event `sender` (MXID)           | No (flat)   |
| `formatted_body`         | Content `formatted_body` when present  | No (flat)   |
| `format`                 | Content `format` when present          | No (flat)   |
| `meshtastic_reply_id`    | MMRelay emote reaction only            | No (flat)   |
| `meshtastic_emoji`       | MMRelay emote reaction only            | No (flat)   |
| `meshtastic_*` wire keys | `_capture_mmrelay_fields` when present | No (wire)   |
| `displayname`            | Adapter post-codec enrichment          | No (flat)   |

The `meshtastic_*` captured keys are the mmrelay wire-format constants
(`meshtastic_id`, `meshtastic_replyId`, `meshtastic_text`,
`meshtastic_emoji`, `meshtastic_meshnet`, `meshtastic_portnum`,
`meshtastic_longname`, `meshtastic_shortname`,
`meshtastic_reaction_key`). They are an external wire contract owned by
`src/medre/interop/mmrelay.py`, not MEDRE-native keys; they are captured
verbatim from Matrix event content when an upstream mmrelay bridge
produced the event. `displayname` is added after codec decode by
`MatrixAdapter._on_room_message` from synced room-member state (MXID as
fallback when no member display name exists).

### Matrix: Namespaced keys

None. Matrix emits no `matrix.*` keys.

### Matrix: Remaining bare keys

All Matrix native keys are bare. The identity-relevant ones (`sender`,
`displayname`) and the native-ref keys (`event_id`, `room_id`) are flat
by design: they are also the platform-detection signal
(`_MATRIX_KEYS = {sender, event_id, room_id}` in
`_attribution_dispatch.py`) and they match the Matrix/MMRelay wire
shape. Namespacing them would break detection and diverge from the wire
contract.

### Matrix: Bare keys retained for non-identity consumers

- `event_id`, `room_id` feed `source_native_ref` (built at decode as
  `NativeRef(adapter, native_channel_id=room_id,
native_message_id=event_id)`) and relation `target_native_ref` for
  Matrix replies and true `m.annotation` reactions.
- `sender` feeds identity projection (`source_sender_id`,
  `source_sender_handle`, and the MXID localpart for
  `source_sender_short_label`).
- `displayname` feeds identity projection (`source_sender_label`).

### Matrix: Bare keys that are legacy input tolerance only

None for Matrix-native fields. The captured `meshtastic_*` wire keys are
external protocol fields, not MEDRE legacy tolerance.

### Matrix: Migration status

Deferred (and likely indefinite). Matrix flat keys are the
platform-detection signal and match the Matrix/MMRelay wire contract;
renaming them is out of scope for the current work package.

---

## Meshtastic

### Meshtastic: Emitted native metadata keys

The Meshtastic codec (`MeshtasticCodec.decode` in
`src/medre/adapters/meshtastic/codec.py`) emits a mix of namespaced
identity keys, namespaced non-identity packet metadata, and retained bare
forms for backward compatibility in the same dict.

Namespaced identity keys:

| Key                    | Source                                    |
| ---------------------- | ----------------------------------------- |
| `meshtastic.from_id`   | Classifier `from_id` (numeric node ID)    |
| `meshtastic.longname`  | `node_info["longname"]` passed at decode  |
| `meshtastic.shortname` | `node_info["shortname"]` passed at decode |

Namespaced non-identity packet metadata (emitted alongside retained bare
forms):

| Key                            | Source                            |
| ------------------------------ | --------------------------------- |
| `meshtastic.packet_id`         | Classifier `packet_id`            |
| `meshtastic.channel`           | Packet `channel` or config        |
| `meshtastic.portnum`           | Classifier portnum                |
| `meshtastic.to_id`             | Packet `toId`                     |
| `meshtastic.is_direct_message` | Classifier flag                   |
| `meshtastic.reply_id`          | Classifier from `decoded.replyId` |
| `meshtastic.emoji`             | Raw `decoded.emoji`               |
| `meshtastic.emoji_flag`        | Classifier flag                   |

Bare keys (retained alongside namespaced forms):

| Key                 | Source                                                   | Identity?       |
| ------------------- | -------------------------------------------------------- | --------------- |
| `packet_id`         | Same value as `meshtastic.packet_id` (duplicate)         | No              |
| `from_id`           | Same value as `meshtastic.from_id` (duplicate)           | Yes (duplicate) |
| `channel`           | Same value as `meshtastic.channel` (duplicate)           | No              |
| `portnum`           | Same value as `meshtastic.portnum` (duplicate)           | No              |
| `to_id`             | Same value as `meshtastic.to_id` (duplicate)             | No              |
| `is_direct_message` | Same value as `meshtastic.is_direct_message` (duplicate) | No              |
| `reply_id`          | Same value as `meshtastic.reply_id` (duplicate)          | No              |
| `emoji`             | Same value as `meshtastic.emoji` (duplicate)             | No              |
| `emoji_flag`        | Same value as `meshtastic.emoji_flag` (duplicate)        | No              |
| `packet`            | `snapshot_packet(packet)`                                | No              |
| `decoded`           | `snapshot_decoded(decoded)`                              | No              |
| `classification`    | Sub-dict (action/category/reason/flags)                  | No              |

The namespaced form is primary for new readers. The bare form is
retained for: non-identity consumers that read from the native dict
directly (evidence copies, diagnostics), inbound `NativeMessageRef.metadata`
shape preservation, and legacy stored-event/test-fixture tolerance.

`meshtastic.longname`/`meshtastic.shortname` are empty strings when node
info is unavailable; text packets carry no packet-level name field.

### Meshtastic: Namespaced keys

`meshtastic.from_id`, `meshtastic.longname`, `meshtastic.shortname` (identity),
plus `meshtastic.packet_id`, `meshtastic.channel`, `meshtastic.portnum`,
`meshtastic.to_id`, `meshtastic.is_direct_message`, `meshtastic.reply_id`,
`meshtastic.emoji`, `meshtastic.emoji_flag` (non-identity packet metadata).
These are the primary platform-detection signal
(`_MESHTASTIC_NAMESPACED_KEYS` in `_attribution_dispatch.py`) and the
primary source for the projection fallback chains (identity keys) and
for the Matrix renderer's mmrelay packet-ID resolution (non-identity
keys).

### Meshtastic: Remaining bare keys

Three categories:

1. Bare non-identity keys retained alongside their namespaced forms
   (`packet_id`, `channel`, `portnum`, `to_id`, `is_direct_message`,
   `reply_id`, `emoji`, `emoji_flag`). These bare forms feed the
   evidence/traceability chain, inbound `NativeMessageRef.metadata` shape
   preservation, and legacy stored-event tolerance. The namespaced form
   is primary for new readers.
2. Bare `from_id`, a transitional duplicate of `meshtastic.from_id`.
   The codec emits both with the same value. The namespaced form is
   primary; the bare form's only live reader is the projection legacy
   fallback.
3. Snapshot and classification keys (`packet`, `decoded`,
   `classification`) are un-namespaced by design; they carry full
   adapter-data snapshots for evidence/traceability and diagnostics.

### Meshtastic: Bare keys retained for non-identity consumers

- `packet_id` feeds `source_native_ref` at decode time
  (`NativeRef(adapter, native_channel_id=str(channel),
native_message_id=str(packet_id))`). It is read from the classifier
  result during decode, not read back out of the native dict by later
  stages.
- `reply_id` feeds the relation `target_native_ref` and relation
  `metadata["meshtastic_reply_id"]` for Meshtastic replies/reactions at
  decode time (also from the classifier result).
- The full flat dict (`packet_id`, `channel`, `portnum`, `to_id`,
  `reply_id`, `emoji`, `emoji_flag`, plus the `packet`/`decoded`/
  `classification` snapshots) is copied into inbound
  `NativeMessageRef.metadata` to preserve the codec's raw adapter-data
  shape for the inbound evidence chain. This copy intentionally keeps
  the flat structure so the evidence chain audits back to the transport
  SDK output without namespace transformation.
- Diagnostics logging in `MeshtasticAdapter` reads classifier fields
  (`classification.packet_id`, `classification.from_id`,
  `classification.portnum`), not the native dict keys.

### Meshtastic: Bare keys that are legacy input tolerance only

- `longname`, `shortname` are no longer emitted by the codec. They are
  read only as legacy input tolerance for stored events and test
  fixtures produced before namespacing, by:
  - `project_meshtastic_attribution` fallback chain (bare `longname`/
    `shortname` tried only after all namespaced candidates).
  - `MatrixRenderer._resolve_mmrelay_sender_names` (bare `longname`/
    `shortname` as the last resort before empty string, when injecting
    mmrelay wire fields into Matrix outbound content).
- Bare `from_id` is a transitional duplicate, not legacy-only: the
  codec still emits it. Its bare reader is the projection legacy
  fallback; the namespaced form wins whenever both are present.

The `relation_enricher` no longer reads bare `longname` directly. Core
planning sources sender labels exclusively from the generic
`SenderProjectionFn` callback wired by the runtime builder.

### Meshtastic: Migration status

Identity namespacing is complete. Non-identity packet metadata namespacing
is complete (namespaced forms emitted alongside retained bare forms).
Remaining work is deferred:

- Dropping the bare `from_id` duplicate is deferred until the
  projection fallback chain no longer reads it and stored-event
  tolerance is no longer required.
- Dropping the bare non-identity duplicates (`packet_id`, `channel`,
  `portnum`, `to_id`, `is_direct_message`, `reply_id`, `emoji`,
  `emoji_flag`) is deferred until all consumers (evidence copies,
  fixture assertions, diagnostics) are migrated to the namespaced forms.
  The namespaced form is already primary for new readers.

---

## MeshCore

### MeshCore: Emitted native metadata keys

The MeshCore codec (`MeshCoreCodec.decode` in
`src/medre/adapters/meshcore/codec.py`) emits only namespaced keys.

| Key                            | Source                                  |
| ------------------------------ | --------------------------------------- |
| `meshcore.packet_id`           | `sender_timestamp` from event           |
| `meshcore.sender_id`           | `pubkey_prefix` (6-byte hex)            |
| `meshcore.channel`             | `channel_idx` from event                |
| `meshcore.pubkey_prefix`       | Same value as `sender_id`               |
| `meshcore.txt_type`            | `txt_type` from event                   |
| `meshcore.is_direct_message`   | Classifier flag                         |
| `meshcore.contact_label`       | Adapter-resolved known-contact name     |
| `meshcore.contact_short_label` | Adapter-resolved short contact label    |
| `meshcore.classification`      | Sub-dict (action/category/reason/flags) |

Contact-label keys are enrichment layered on top of the core identity
keys. They are intentionally excluded from
`MESHCORE_NAMESPACED_KEYS` (the detection set) because a dict carrying
only contact labels lacks the core identity signals
(`pubkey_prefix`, `sender_id`, `channel`, `packet_id`).

### MeshCore: Namespaced keys

All MeshCore native keys are namespaced under `meshcore.*`.

### MeshCore: Remaining bare keys

None emitted. The codec produces no bare keys.

### MeshCore: Bare keys retained for non-identity consumers

None. `source_native_ref` is built at decode from the classifier result
(`NativeRef(adapter, native_channel_id=str(channel),
native_message_id=str(packet_id))`), not read back from the native
dict.

### MeshCore: Bare keys that are legacy input tolerance only

`project_meshcore_attribution` tolerates bare `pubkey_prefix` and bare
`channel_idx` as the last resort in its fallback chains, for older test
fixtures and stored events. The codec does not emit these bare forms.

### MeshCore: Migration status

Complete. No namespacing work remains for MeshCore. The bare-key
fallbacks in the projection helper can be dropped once stored-event
tolerance is no longer required.

---

## LXMF

### LXMF: Emitted native metadata keys

The LXMF codec (`LxmfCodec.decode` in `src/medre/adapters/lxmf/codec.py`)
emits bare identity keys and namespaced label keys.

| Key                 | Source                                       | Namespaced? |
| ------------------- | -------------------------------------------- | ----------- |
| `source_hash`       | Sender identity hash (hex, 32 chars)         | No (bare)   |
| `destination_hash`  | Recipient identity hash (hex, 32 chars)      | No (bare)   |
| `message_id`        | `LXMessage.hash` (hex)                       | No (bare)   |
| `timestamp`         | `LXMessage.timestamp`                        | No (bare)   |
| `title`             | `LXMessage.title`                            | No (bare)   |
| `delivery_method`   | `LXMessage.method` from event                | No (bare)   |
| `has_fields`        | Whether MEDRE fields envelope is present     | No (bare)   |
| `lxmf.display_name` | Captured `source_name` at ingress (when any) | Yes         |

The MEDRE fields envelope (`fields[0xFD]`) is placed in
`metadata.custom["medre_envelope"]`, not in `native.data`.

`lxmf.display_name` is set only when the session captures a non-empty
`source_name` at ingress. The current LXMF library does not populate
`source_name` on `LXMessage`, so this key is usually absent in live
ingest; fake-mode packets may carry it directly.

### LXMF: Namespaced keys

`lxmf.display_name`. The projection helper also reads
`lxmf.short_name`, but the codec never populates it: it is reserved for
future announce-based enrichment. `source_sender_short_label` therefore
falls through to the compact form of `lxmf.display_name` in practice.

### LXMF: Remaining bare keys

`source_hash`, `destination_hash`, `message_id`, `timestamp`, `title`,
`delivery_method`, `has_fields`. These are the current emitted shape,
not legacy tolerance. `source_hash` and `destination_hash` are also the
LXMF platform-detection signal (`_LXMF_KEYS` in
`_attribution_dispatch.py`).

### LXMF: Bare keys retained for non-identity consumers

- `message_id` feeds `source_native_ref` at decode
  (`NativeRef(adapter, native_channel_id=None,
native_message_id=str(message_id))`; LXMF has no channel concept).
- `source_hash` feeds identity projection (`source_sender_id` via
  `normalize_source_hash`). It is bare and current, not legacy.
- The flat dict is copied into inbound `NativeMessageRef.metadata` for
  evidence/traceability, matching the Meshtastic pattern.

### LXMF: Bare keys that are legacy input tolerance only

None. LXMF's bare keys are the current emitted shape.

### LXMF: Migration status

Deferred. The identity hash and message-id keys could be namespaced to
`lxmf.*` for consistency with the Meshtastic/MeshCore pattern, but
`source_hash`/`destination_hash` are the platform-detection signal and
renaming them requires coordinated codec, attribution, and dispatch
changes plus fixture updates. This asymmetry is the main remaining
namespacing gap and is out of scope for the current work package.
Announce-based `lxmf.short_name` population remains deferred per the
identity-enrichment audit.

---

## Consumer Mapping

How the native keys feed downstream consumers. "At decode" means the
value is taken from the classifier result during codec decode and
encoded into `source_native_ref` or relation `target_native_ref`; it is
not read back out of the native dict by a later stage.

| Consumer                                                | Matrix                                                               | Meshtastic                                                                                            | MeshCore                                          | LXMF                                              |
| ------------------------------------------------------- | -------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- | ------------------------------------------------- | ------------------------------------------------- |
| `source_native_ref`                                     | `event_id`, `room_id` (at decode)                                    | `packet_id`, `channel` (at decode)                                                                    | `packet_id`, `channel` (at decode)                | `message_id` (at decode)                          |
| Relation `target_native_ref`                            | `event_id`/`room_id`, MMRelay keys                                   | `reply_id`, `channel` (at decode)                                                                     | None (no native relations)                        | Envelope-only (`fields[0xFD]`)                    |
| Relation `metadata`                                     | `meshtastic_reply_id`, `meshtastic_emoji`, `meshtastic_reaction_key` | `meshtastic_reply_id`, `meshtastic_emoji`                                                             | None                                              | None                                              |
| Identity projection                                     | `sender`, `displayname`                                              | `meshtastic.from_id/longname/shortname` (+ bare legacy)                                               | `meshcore.sender_id`, `meshcore.contact_label`    | `source_hash`, `lxmf.display_name`                |
| Renderer metadata (`relay_prefix_*`, mmrelay injection) | `sender`, `displayname`, captured `meshtastic_*`                     | `meshtastic.packet_id` (via KEY_ID), `meshtastic.longname/shortname` (+ bare legacy via mmrelay path) | `meshcore.sender_id` (via projection)             | `source_hash` (via projection)                    |
| Diagnostics logging                                     | Classifier/adapter fields                                            | Classifier fields (`classification.*`), not native dict keys                                          | Classifier fields                                 | Classifier fields                                 |
| Inbound evidence (`NativeMessageRef.metadata`)          | Flat copy of `native.data`                                           | Flat copy of `native.data` (preserves codec shape with both forms)                                    | Namespaced copy of `native.data`                  | Flat copy of `native.data`                        |
| Outbound evidence (Meshtastic queue)                    | N/A                                                                  | Bare transport keys merged under `metadata["meshtastic"]`                                             | N/A                                               | N/A                                               |
| Core planning (relation enrichment)                     | Generic projected fields via `SenderProjectionFn`                    | Generic projected fields via `SenderProjectionFn`                                                     | Generic projected fields via `SenderProjectionFn` | Generic projected fields via `SenderProjectionFn` |

The renderer-metadata row covers two distinct paths. The
`relay_prefix_*` keys are generic rendering diagnostics produced by the
shared formatter from the projected `RelayAttribution` fields; they do
not expose native keys directly. The mmrelay injection path
(`MatrixRenderer._inject_mmrelay_metadata` and
`_resolve_mmrelay_sender_names`) reads Meshtastic-native namespaced
keys, then external mmrelay wire keys, then bare legacy keys as
input tolerance.

---

## Cross-Transport Summary

### Namespaced identity keys (current emitted shape)

| Transport  | Namespaced identity keys                                                                                 |
| ---------- | -------------------------------------------------------------------------------------------------------- |
| Meshtastic | `meshtastic.from_id`, `meshtastic.longname`, `meshtastic.shortname`                                      |
| MeshCore   | `meshcore.sender_id`, `meshcore.pubkey_prefix`, `meshcore.contact_label`, `meshcore.contact_short_label` |
| LXMF       | `lxmf.display_name` (label only; `lxmf.short_name` reserved, unpopulated)                                |
| Matrix     | None (flat by design)                                                                                    |

### Namespaced non-identity packet metadata (Meshtastic only)

| Transport  | Namespaced non-identity keys                                                                                                                                                               |
| ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Meshtastic | `meshtastic.packet_id`, `meshtastic.channel`, `meshtastic.portnum`, `meshtastic.to_id`, `meshtastic.is_direct_message`, `meshtastic.reply_id`, `meshtastic.emoji`, `meshtastic.emoji_flag` |
| MeshCore   | All keys are namespaced (see identity table above includes packet metadata)                                                                                                                |
| LXMF       | `lxmf.display_name` only (see identity table)                                                                                                                                              |
| Matrix     | None (flat by design)                                                                                                                                                                      |

### Bare keys retained for non-identity consumers

| Transport  | Bare keys                                                                                                                                                  | Consumer                                                                           |
| ---------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| Matrix     | `event_id`, `room_id`, `sender`, `displayname`                                                                                                             | `source_native_ref`, relations, identity projection                                |
| Meshtastic | `packet_id`, `channel`, `portnum`, `to_id`, `reply_id`, `emoji`, `emoji_flag`, `is_direct_message`, snapshots (bare duplicates alongside namespaced forms) | `source_native_ref`/relations (at decode), inbound evidence copy, legacy tolerance |
| MeshCore   | None                                                                                                                                                       | `source_native_ref` built at decode from classifier                                |
| LXMF       | `source_hash`, `destination_hash`, `message_id`, `timestamp`, `title`, `delivery_method`, `has_fields`                                                     | `source_native_ref` (at decode), identity projection, inbound evidence copy        |

### Bare keys that are legacy input tolerance only

| Transport  | Bare keys                      | Readers                                                                          |
| ---------- | ------------------------------ | -------------------------------------------------------------------------------- |
| Meshtastic | `longname`, `shortname`        | `project_meshtastic_attribution`, `MatrixRenderer._resolve_mmrelay_sender_names` |
| Meshtastic | `from_id` (duplicate)          | `project_meshtastic_attribution` legacy fallback (codec still emits both forms)  |
| MeshCore   | `pubkey_prefix`, `channel_idx` | `project_meshcore_attribution` fixture tolerance (codec does not emit these)     |
| LXMF       | None                           | Bare keys are current emitted shape                                              |
| Matrix     | None                           | Captured `meshtastic_*` are external wire fields, not MEDRE legacy               |

---

## Migration: Now versus Defer

### Migrate now

Nothing. The identity-projection and non-identity packet metadata namespacing
that the current work package requires is already in place: Meshtastic
identity keys are namespaced, Meshtastic non-identity packet metadata keys
are namespaced (emitted alongside retained bare forms), MeshCore is fully
namespaced, and LXMF display-name labels are namespaced. The remaining
native-ref and relation-mapping work in this branch is orthogonal to key
namespacing.

The namespacing rule to preserve in new code: identity keys that feed
`project_<transport>_attribution` belong under `<transport>.*`, and a
sparse native dict carrying any namespaced identity key is
unambiguously detectable as that platform. New identity keys added to
any codec should follow this rule.

### Defer

| Item                                                       | Reason                                                                                                                           |
| ---------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| Drop bare non-identity duplicates (Meshtastic)             | Evidence copies, fixtures, and diagnostics still read bare forms; namespaced form is already primary for new readers             |
| Drop bare `from_id` duplicate (Meshtastic)                 | Projection fallback still reads it; requires stored-event tolerance to lapse first                                               |
| LXMF identity keys → `lxmf.*`                              | `source_hash`/`destination_hash` are the detection signal; renaming needs coordinated codec, attribution, dispatch, fixture work |
| Matrix → `matrix.*` namespacing                            | Flat keys are the detection signal and match the Matrix/MMRelay wire contract; likely indefinite                                 |
| MeshCore drop bare `pubkey_prefix`/`channel_idx` fallbacks | Requires stored-event/fixture tolerance to lapse first                                                                           |
| `lxmf.short_name` population                               | Announce-based enrichment not implemented (see identity-enrichment audit)                                                        |

---

## Inspected Files

### Codecs and attribution

| File                                           | Status                                                                                            |
| ---------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| `src/medre/adapters/matrix/codec.py`           | Full                                                                                              |
| `src/medre/adapters/matrix/attribution.py`     | Full                                                                                              |
| `src/medre/adapters/matrix/metadata.py`        | Full                                                                                              |
| `src/medre/adapters/matrix/renderer.py`        | Full (`_resolve_mmrelay_sender_names`, `_resolve_mmrelay_packet_id`, `_build_source_attribution`) |
| `src/medre/adapters/meshtastic/codec.py`       | Full                                                                                              |
| `src/medre/adapters/meshtastic/attribution.py` | Full                                                                                              |
| `src/medre/adapters/meshtastic/adapter.py`     | Partial (outbound native-ref metadata merge, diagnostics logging)                                 |
| `src/medre/adapters/meshcore/codec.py`         | Full                                                                                              |
| `src/medre/adapters/meshcore/attribution.py`   | Full                                                                                              |
| `src/medre/adapters/lxmf/codec.py`             | Full                                                                                              |
| `src/medre/adapters/lxmf/attribution.py`       | Full                                                                                              |
| `src/medre/adapters/_attribution_dispatch.py`  | Full                                                                                              |
| `src/medre/interop/mmrelay.py`                 | Full                                                                                              |
| `src/medre/core/planning/relation_enricher.py` | Full (`SenderProjectionFn` wiring; no direct native-key reads)                                    |

### Companion audit docs

| File                                                     | Status |
| -------------------------------------------------------- | ------ |
| `docs/dev/relay-prefix-attribution-audit.md`             | Full   |
| `docs/dev/transport-native-identity-enrichment-audit.md` | Full   |
| `docs/dev/native-relations-audit.md`                     | Full   |
| `docs/dev/documentation-style.md`                        | Full   |
