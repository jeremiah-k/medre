# Meshtastic Relations and MMRelay Metadata

> Contract version: 1
> Last updated: 2026-05-18

## Meshtastic relation mapping

- `decoded.replyId` on a text packet is the native Meshtastic packet ID being replied to.
- `decoded.replyId` plus `decoded.emoji == 1` is a Meshtastic tapback/reaction.
- MEDRE canonicalizes these as `EventRelation` entries:
  - reply packets become `relation_type="reply"` with `target_native_ref.native_message_id=str(replyId)`.
  - tapback packets become `event_kind="message.reacted"` with `relation_type="reaction"`, `key` set to the emoji/text payload, and `target_native_ref.native_message_id=str(replyId)`.
- Empty tapback text is retained safely with a fallback reaction key of `"?"`.

## Packet snapshot storage

Meshtastic packet snapshots are stored without adding a new table:

- `CanonicalEvent.metadata.native.data["packet"]` contains a JSON-safe snapshot of the full packet.
- `CanonicalEvent.metadata.native.data["decoded"]` contains a JSON-safe snapshot of the decoded payload.
- `CanonicalEvent.metadata.native.data["classification"]` contains classifier output, including reply/reaction flags.
- Inbound `NativeMessageRef.metadata` mirrors `event.metadata.native.data` for lookup/debug/plugin use.
- Outbound `NativeMessageRef.metadata` mirrors `AdapterDeliveryResult.metadata` when a target adapter returns native delivery metadata.

Bytes in snapshots are represented as `{"encoding": "base64", "data": "..."}`. Unknown non-JSON objects are represented with `repr(value)` instead of failing ingestion.

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

## Matrix behavior

- True Matrix reactions render as `m.reaction` with `m.relates_to.rel_type="m.annotation"` when a Matrix target event ID is available and MMRelay compatibility is disabled.
- MMRelay compatibility, or missing Matrix target IDs, renders reactions as `m.room.message` emotes with `meshtastic_replyId`, `meshtastic_text`, and `meshtastic_emoji=1` metadata.
- Matrix inbound content carrying MMRelay keys preserves those fields in `CanonicalEvent.metadata.native.data`.

## Cross-adapter native target resolution

The pipeline may enrich an event per delivery target before rendering:

1. A relation has `target_event_id` after canonical resolution.
2. The target adapter needs its own native ID for structured reply/reaction rendering.
3. The pipeline looks up `NativeMessageRef` records for the target event and chooses the first ref owned by the target adapter.
4. The enriched relation is used only for rendering to that target; the stored source event is not mutated.
