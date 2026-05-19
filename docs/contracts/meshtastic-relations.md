# Meshtastic Relations and MMRelay Metadata

> Contract version: 2
> Last updated: 2026-05-19

## Meshtastic relation mapping

- `decoded.replyId` on a text packet is the native Meshtastic packet ID being replied to.
- `decoded.replyId` plus `decoded.emoji == 1` is a Meshtastic tapback/reaction.
- MEDRE maps these through `NativeMessageRef` records, not a message_map table.
- Canonicalisation:
  - reply packets become `relation_type="reply"` with `target_native_ref.native_message_id=str(replyId)`.
  - tapback packets become `event_kind="message.reacted"` with `relation_type="reaction"`, `key` set to the emoji/text payload, and `target_native_ref.native_message_id=str(replyId)`.
- Empty tapback text is retained safely with a fallback reaction key of `"?"`.

## Outbound native ref (delayed callback)

Meshtastic is a queued, paced transport. The adapter cannot return a native packet ID synchronously from `deliver()`.

1. The renderer produces a content payload and the adapter enqueues it with the canonical `event_id` stored alongside the payload (never in the radio data).
2. The queue's `_process_queue` background loop dequeues items, applies pacing, and calls the real send function.
3. When the send returns a real Meshtastic packet ID, the adapter builds an `OutboundNativeRefRecord` with both `event_id` and `native_message_id`.
4. It calls the `AdapterContext.record_outbound_native_ref` callback, wired by the pipeline runner.
5. The pipeline persists a `NativeMessageRef(direction="outbound")` linking the canonical event ID to the Meshtastic packet ID.
6. Failures in the callback are caught and logged so they never crash the queue drain.

This is the only path that establishes the Matrix-event-ID to Meshtastic-packet-ID mapping for outbound messages.

## Cross-adapter native target resolution

Before rendering to a target adapter, the pipeline enriches relations:

1. A relation has `target_event_id` after canonical resolution.
2. The pipeline looks up `NativeMessageRef` records for that target event and selects the first ref owned by the target adapter.
3. The enriched `target_native_ref` is used only for rendering to that adapter. The stored source event is not mutated.

## Meshtastic -> Matrix replies

A Meshtastic inbound reply (with `replyId`) becomes a Matrix reply (`m.in_reply_to`) only when a matching `NativeMessageRef` exists that links the original Matrix event ID to the referenced Meshtastic packet ID. When the mapping is missing (e.g. the original message was sent before the bridge started), the reply renders as plain text without `m.in_reply_to`. The fallback is safe and silent.

## Meshtastic -> Matrix reactions

MMRelay-compatible `m.emote` descriptive rendering. The Matrix renderer produces:

```
\n {prefix}reacted {emoji} to "{preview}"
```

- `prefix` is the formatted relay prefix template (e.g. `[MeshUser]`). When no prefix is configured, the line starts with `\n `.
- `emoji` is the reaction symbol extracted from the relation key.
- `preview` is the abbreviated original text (up to 40 chars, newlines collapsed to spaces, `...` appended if truncated).
- Longname/shortname from Meshtastic are preserved exactly as received: spaces, casing, and emoji characters are not altered.

When `mmrelay_compat` is false and a Matrix-native target event ID is available, a true `m.reaction` (`m.annotation`) is emitted instead.

## Matrix -> Meshtastic reactions

Cross-platform reactions (Matrix origin, Meshtastic target) use MMRelay-compatible descriptive text:

```
{compact_prefix}{sep}reacted {emoji} to "{preview}"
```

- `compact_prefix` strips spaces from display name tokens while preserving casing. Example: `"Display Name"` becomes `"DisplayName"`.
- `sep` is a single space inserted between the prefix and `reacted` only when the prefix is non-empty and does not already end with whitespace.
- `reply_id` is set when the original message has a known Meshtastic packet ID (via `target_native_ref` or `meshtastic_reply_id` metadata), making the descriptive text a structured reply.
- `emoji` is **not** set to `1`. The payload is plain text, not a native Meshtastic tapback.

Native Meshtastic-originated reactions (same adapter) continue to use `emoji=1` + `reply_id` for proper tapback round-tripping.

## Display name handling

| Direction | Rule | Example |
|---|---|---|
| Matrix -> Meshtastic (compact prefix) | Spaces removed, casing preserved | `"Display Name"` -> `"DisplayName"` |
| Meshtastic -> Matrix | Longname preserved exactly | `"Mesh User 📡"` -> `"Mesh User 📡"` |
| Meshtastic -> Matrix | Shortname preserved exactly | `"Mesh"` -> `"Mesh"` |

## Packet snapshot storage

Meshtastic packet snapshots are stored without adding a new table:

- `CanonicalEvent.metadata.native.data["packet"]` contains a JSON-safe snapshot of the full packet.
- `CanonicalEvent.metadata.native.data["decoded"]` contains a JSON-safe snapshot of the decoded payload.
- `CanonicalEvent.metadata.native.data["classification"]` contains classifier output, including reply/reaction flags.
- Inbound `NativeMessageRef.metadata` mirrors `event.metadata.native.data` for lookup/debug/plugin use.
- Outbound `NativeMessageRef.metadata` carries packet snapshot keys (`id`, `channel`, `reply_id`, `to`) from the delivery result plus send context (`text`, `meshnet_name`, `reply_id`, `emoji`).

Bytes in snapshots are represented as `{"encoding": "base64", "data": "..."}`. Unknown non-JSON objects are represented with `repr(value)` instead of failing ingestion.

Snapshots and native metadata are stored for future plugin use and are not consumed by the core reply/reaction pipeline.

## MMRelay compatibility keys

MEDRE keeps MMRelay wire metadata at the adapter/interop edge. It is not the canonical data model.

Supported Matrix content keys are:

- `meshtastic_id`
- `meshtastic_replyId`
- `meshtastic_text`
- `meshtastic_emoji`
- `meshtastic_longname`
- `meshtastic_shortname`
- `meshtastic_meshnet`
- `meshtastic_portnum`

`meshtastic_emoji == 1` is the MMRelay-compatible reaction flag.

## Matrix inbound MMRelay metadata

Matrix inbound content carrying MMRelay keys preserves those fields in `CanonicalEvent.metadata.native.data`. The Matrix renderer and codec use these keys for MMRelay-compatible rendering when `mmrelay_compat` is enabled.
