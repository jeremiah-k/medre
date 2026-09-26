# Matrix Event Shape

This document defines the Matrix ingress representation used by MEDRE. It is a
serialization contract, not an adapter-runtime API. A producer can emit this
shape without importing MEDRE or mindroom-nio.

The canonical event envelope remains transport-neutral. Matrix-specific facts
MUST live in generic relations/native references when a generic concept exists,
or under the versioned Matrix native metadata namespace when the fact is
protocol-specific.

See also: [event-model.md](event-model.md), [metadata.md](metadata.md), and the
[Matrix transport profile](transport-profiles/matrix.md).

## 1. Canonical Identity Projection

For an inbound Matrix event, the producer MUST project identity as follows:

| Matrix fact               | Canonical field                       |
| ------------------------- | ------------------------------------- |
| Sender MXID               | `source_transport_id`                 |
| Room ID                   | `source_channel_id`                   |
| Event ID                  | `source_native_ref.native_message_id` |
| Thread root, when present | `source_native_ref.native_thread_id`  |
| Origin server timestamp   | `timestamp`                           |

`source_native_ref.adapter` identifies the Matrix adapter instance and
`source_native_ref.native_channel_id` repeats the room ID. The original Matrix
event ID, room ID, and sender are repeated inside the Matrix native namespace.
When the producer receives a valid nonnegative Matrix origin-server timestamp,
it MUST also retain those milliseconds as `native.matrix.origin_server_ts_ms`.
A producer that genuinely lacks the source timestamp MAY omit that native field;
the canonical `timestamp` still MUST contain the best event time it can establish.

## 2. Event Kind and Relation Mapping

Matrix lifecycle semantics MUST use existing canonical event kinds and
`EventRelation` values. Matrix-specific event kinds MUST NOT be added to the
transport-neutral registry solely to represent Matrix wire structures.

| Matrix input                              | Canonical event kind | Canonical relation  |
| ----------------------------------------- | -------------------- | ------------------- |
| Ordinary room message                     | `message.created`    | none unless related |
| `m.in_reply_to` without `m.thread`        | `message.created`    | `reply`             |
| `m.annotation`                            | `message.reacted`    | `reaction`          |
| `m.replace`                               | `message.edited`     | `edit`              |
| `m.thread`                                | message/media kind   | `thread`            |
| `m.room.redaction`                        | `message.deleted`    | `delete`            |
| `m.image`, `m.audio`, `m.video`, `m.file` | `message.file`       | as applicable       |

Relation targets SHOULD use `target_native_ref` until the pipeline resolves the
Matrix event ID to a canonical event ID. The native relation descriptor under
`native.matrix.relation` retains Matrix wire details that are not generic core
semantics.

When `m.thread` includes `m.in_reply_to`, the thread root is the canonical
`thread` target. `native.matrix.relation.reply_to_event_id` stores the parent
event ID from `m.in_reply_to.event_id`; it does not replace the thread root.
An explicit `m.in_reply_to` additionally yields a SECOND canonical relation:
a `reply` relation whose target is the explicit parent. Thread-only events
carry no reply relation. Downstream, an explicit reply-in-thread therefore
inherits plain-reply capability semantics (a destination with
`replies="unsupported"` skips the delivery even when `threads="fallback"`
would degrade it to inline text), while a plain thread event keeps the pure
`thread` capability path. The same co-carriage applies to `m.replace` events
that carry `m.in_reply_to` (the conventional in-thread edit shape): the edit
relation targets the original event and a `reply` relation targets the
carried parent.

Redactions use canonical relation type `delete`. Their Matrix-native descriptor
uses `kind="redaction"` to preserve the source wire concept without expanding the
core relation vocabulary.

## 3. Native Metadata Namespace

`CanonicalEvent.metadata.native.data` MUST contain a `matrix` object conforming
to
[`matrix-native-metadata.schema.json`](../schemas/matrix-native-metadata.schema.json).
Version 1 has this top-level shape:

```json
{
  "matrix": {
    "schema_version": 1,
    "room_id": "!room:example.org",
    "event_id": "$event",
    "event_type": "m.room.message",
    "sender": "@alice:example.org",
    "encryption": {
      "event_encrypted": true,
      "decrypted": true,
      "verified": true
    }
  }
}
```

Optional Matrix-native fields include display name, transaction ID, message
type, formatted-body information, relation context, media descriptors, relay
provenance, and room encryption state. `origin_server_ts_ms` is optional only
when the producer lacks a valid source timestamp; when supplied by Matrix it
MUST be retained and MUST be a nonnegative integer.

Raw Matrix event content MUST NOT be copied wholesale into native metadata.

## 4. Media Descriptors

Image, audio, video, and file messages use `message.file`. Matrix media details
remain under `native.matrix.media` because MXC addressing and Matrix encrypted
attachment structure are protocol-specific.

The descriptor MAY contain:

- `kind`
- `mxc_uri`
- `filename`
- `mime_type`
- `size_bytes`
- `width` and `height`
- `duration_ms`
- `thumbnail_mxc_uri`
- `encrypted`

`media.encrypted` means the attachment file uses Matrix encrypted-media
metadata. It is distinct from `encryption.event_encrypted`, which means the
room event itself was received through Matrix room encryption.

Encrypted-media keys, IVs, hashes, and thumbnail decryption material MUST NOT be
persisted in the canonical native namespace.

## 5. Encryption and Verification Provenance

The Matrix namespace records only bounded facts needed to explain how the event
was obtained:

- `room_encrypted`: whether the room is known to be encrypted, when known
- `event_encrypted`: whether the normalized event arrived through Matrix room
  encryption
- `decrypted`: whether the SDK successfully decrypted that event
- `verified`: the SDK verification result, when available

Sender keys, Olm/Megolm session IDs, device private material, access tokens, and
other cryptographic secrets MUST NOT be persisted in this namespace.

`metadata.transport.transport_encrypted` carries the generic encryption fact.
Matrix verification remains native because its exact semantics are provider
specific.

## 6. Relay and MMRelay Interoperability

MEDRE relay provenance embedded in Matrix content is projected to
`native.matrix.relay.medre_envelope`. This preserves the safe relay envelope
without treating it as Matrix identity.

Known MMRelay compatibility fields are projected separately to
`native.interop.mmrelay`. They MUST NOT be mixed into `native.matrix` because
they describe a cross-project wire convention rather than Matrix itself.

Existing cross-transport relation metadata used for Meshtastic reply/reaction
rendering remains unchanged. This contract does not require MMRelay to import
MEDRE runtime code; an external producer can validate emitted metadata against
the standalone JSON Schema.

## 7. Versioning

`native.matrix.schema_version` is the version of this native metadata contract.
A change to accepted property names, required fields, value types, enum values,
or field semantics MUST increment the version and update the JSON Schema,
examples, tests, and this specification together.

The versioned namespace is the only supported Matrix native metadata shape.
Root-level Matrix identity fields and alternate field aliases are outside this
contract and MUST NOT be interpreted as an equivalent representation.
Consumers MUST require `schema_version` to match the contract version they
implement; missing or unsupported versions are not Matrix-native metadata for
that consumer.

## 8. Non-Goals

This document defines an ingress serialization contract; outbound wire behavior
is owned by the [Matrix transport profile](transport-profiles/matrix.md), which
now documents native edits, deletes, and threads alongside their mutation
eligibility rules.

This contract also does not integrate MMRelay into MEDRE. It defines a stable
serialization boundary that MMRelay or another producer can adopt independently.

## 9. Outbound Native Lifecycle Wire Shapes

The Matrix renderer emits closed outbound operations instead of magic content
keys. The old `_matrix_event_type` convention is removed.

### 9.1 Operation envelope

Every outbound Matrix render carries exactly one operation under the single
payload key `_matrix_operation` (module
`medre.adapters.matrix.outbound`, struct `MatrixOutboundOperation`):

```json
{
  "_matrix_operation": {
    "kind": "send_event",
    "event_type": "m.room.message",
    "content": { "msgtype": "m.text", "body": "..." },
    "redacts_event_id": null,
    "reason": null
  }
}
```

- `kind` is `send_event` (text, reply, reaction, thread, edit — anything with
  wire content) or `redact_event`.
- `send_event` requires non-empty `event_type` and `content`; `redact_event`
  requires `redacts_event_id`. Mixing per-kind fields is invalid, unknown
  fields are rejected, and a missing envelope is a permanent delivery error.
  Nothing under `_matrix_operation` ever reaches the homeserver.
- The envelope deliberately carries no room identity; routing stays in
  `RenderingResult.target_channel`.

### 9.2 Edits (`m.replace`)

Sent as `send_event` with `event_type="m.room.message"`. Per the spec's event
replacements rules, the top-level `body` is the `"* "` fallback, and the real
new message lives in `m.new_content` with exactly one relay attribution:

```json
{
  "msgtype": "m.text",
  "body": "* edited text",
  "format": "org.matrix.custom.html",
  "formatted_body": "<p>* edited text</p>",
  "m.new_content": {
    "msgtype": "m.text",
    "body": "edited text",
    "format": "org.matrix.custom.html",
    "formatted_body": "<p>edited text</p>"
  },
  "m.relates_to": { "rel_type": "m.replace", "event_id": "$original_copy" }
}
```

The edit target is the bound ORIGINAL destination copy native id
(`rel.target_fact.native_message_id` — the renderer fail-closes on any
non-`bound_owned` fact and never falls back to relation metadata or
`target_native_ref`). Bound reply/thread relations carried by the edit event
itself are mirrored into `m.new_content["m.relates_to"]`. `m.new_content` is
sent inside the encrypted payload for encrypted rooms (edits are room
messages and encrypt exactly like normal sends); only `m.relates_to` stays
cleartext, per the spec.

### 9.3 Redactions (`m.room.redaction`)

Deletes render `send_event`-free `redact_event` operations:

```json
{
  "_matrix_operation": {
    "kind": "redact_event",
    "redacts_event_id": "$owned_copy",
    "reason": "Deleted by original author via MEDRE relay"
  }
}
```

The adapter delivers them through the dedicated
`PUT /rooms/{roomId}/redact/{eventId}/{txnId}` endpoint
(`MatrixSession.room_redact`) with a deterministic transaction id that folds
in the operation kind and target, so a redaction never shares a transaction
with a send or a different target. `m.room.redaction` is an intentionally
plaintext event type: the redaction request carries no message content, so
there is nothing to encrypt; the same is true for `m.reaction` annotations,
which the pinned SDK sends unencrypted even in encrypted rooms.

### 9.4 Threads (`m.thread`)

Sent as `send_event` with `event_type="m.room.message"`:

```json
{
  "msgtype": "m.text",
  "body": "...",
  "...": "...",
  "m.relates_to": {
    "rel_type": "m.thread",
    "event_id": "$thread_root",
    "is_falling_back": false,
    "m.in_reply_to": { "event_id": "$explicit_parent" }
  }
}
```

The root is the bound destination thread root. An explicit bound reply
relation on the same event becomes the parent with `is_falling_back=false`;
without one, the root itself is the fallback parent and `is_falling_back`
is `true` (spec fallback-parent semantics). An unbound root degrades to a
plain message without `m.relates_to` — source-platform IDs are never placed
into destination content.
