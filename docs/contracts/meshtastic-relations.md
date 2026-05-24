# Meshtastic Relations and MMRelay Metadata

> Contract version: 3
> Last updated: 2026-05-24

## Meshtastic relation mapping

- `decoded.replyId` on a text packet is the native Meshtastic packet ID being replied to.
- `decoded.replyId` plus `decoded.emoji == 1` is a Meshtastic tapback/reaction.
- MEDRE maps these through `NativeMessageRef` records, not a message_map table.
- Canonicalisation:
  - reply packets become `relation_type="reply"` with `target_native_ref.native_message_id=str(replyId)`.
  - tapback packets become `event_kind="message.reacted"` with `relation_type="reaction"`, `key` set to the emoji/text payload, and `target_native_ref.native_message_id=str(replyId)`.
- Empty tapback text is retained safely with a fallback reaction key of `"?"`.

## Per-adapter renderer config resolution

Renderers resolve transport-specific parameters by looking up adapter config at render time. The lookup direction differs between renderers.

### MeshtasticRenderer (target-adapter config)

The MeshtasticRenderer receives a mapping of adapter IDs to `MeshtasticConfig` instances at construction. At render time, it resolves config by the **target adapter ID** (the adapter the rendered payload will be delivered to).

- The mapping must contain at least one entry. An empty mapping is a construction error.
- Resolution is a direct key lookup: `configs[target_adapter]`. Unknown target adapters raise `KeyError` with a diagnostic listing known adapters.
- The resolved config provides `radio_relay_prefix`, `meshnet_name`, and `max_text_bytes` for the render.
- No fallback config exists. Every target adapter must have a registered config.

This is target-driven because radio parameters (prefix, meshnet name, byte budget) belong to the destination radio, not the message source.

### MatrixRenderer (source-adapter config)

The MatrixRenderer receives optional scalar defaults (`mmrelay_compat`, `meshnet_name`, `matrix_relay_prefix`) and an optional `source_configs` mapping of adapter IDs to config objects. At render time, it resolves config by the **source adapter ID** (the adapter that produced the original event).

- If `source_configs` is populated, the renderer looks up `event.source_adapter` in the mapping and reads per-source `meshnet_name` and `mmrelay_compatibility` from the matched config object.
- If the source adapter is not in the mapping, or if `source_configs` is empty, the renderer falls back to the scalar defaults (`_meshnet_name`, `_mmrelay_compat`, `_matrix_relay_prefix`).
- **Runtime assembly** passes only `source_configs` — no scalar defaults from any Meshtastic config. Unknown or non-Meshtastic sources render plain Matrix output without Meshtastic prefix or metadata. Scalar constructor parameters remain available for direct constructor use in unit tests.

This is source-driven because Matrix rendering embeds mesh provenance metadata (meshnet name, MMRelay wire keys) that depends on where the message originated.

## NativeMessageRef storage and resolution

`NativeMessageRef` records are the link between native transport IDs and canonical event IDs. They are stored in SQLite.

### Storage schema

The `native_message_refs` table stores one row per observed native message:

| Column               | Type    | Notes                                                  |
| -------------------- | ------- | ------------------------------------------------------ |
| `id`                 | TEXT PK | Unique record ID                                       |
| `event_id`           | TEXT FK | References `canonical_events.event_id`                 |
| `adapter`            | TEXT    | Adapter that owns this native ID (e.g. `radio-alpha`)  |
| `native_channel_id`  | TEXT    | Channel ID, or `NULL` for channelless transports       |
| `native_message_id`  | TEXT    | Native transport message ID (packet ID, event ID, etc) |
| `native_thread_id`   | TEXT    | Thread ID, or `NULL`                                   |
| `native_relation_id` | TEXT    | Reserved, currently unused                             |
| `direction`          | TEXT    | `"inbound"` or `"outbound"`                            |
| `metadata`           | TEXT    | JSON metadata blob                                     |
| `created_at`         | TEXT    | ISO timestamp                                          |

A `UNIQUE(adapter, native_channel_id, native_message_id)` constraint prevents duplicate mappings. Because SQL treats `NULL != NULL`, the storage layer performs a resolve-before-insert check to handle `NULL` channel IDs correctly.

### Resolution: native ID to canonical event ID

`resolve_native_ref(adapter, native_channel_id, native_message_id)` queries the table and returns the linked `event_id`, or `None` if no mapping exists. The query uses `IS ?` for the channel column so that `NULL` values match correctly under SQL three-valued logic.

The `RelationResolver` component calls this method to turn `target_native_ref` (a native-space reference) into `target_event_id` (a canonical event ID). When resolution fails, the relation is preserved with an unresolved `target_native_ref`, and renderers fall back to degraded output.

### Pipeline enrichment: canonical event ID to native ID

Before rendering, the pipeline enriches each relation with a target-adapter-specific `target_native_ref`. Given a resolved `target_event_id`, the pipeline calls `list_native_refs_for_event()` and selects the first ref owned by the target adapter. Channel-aware matching is strict: when a target channel is specified, only a ref with an exact channel match is used.

The enriched `target_native_ref` is scoped to the render call. The stored source event is not mutated.

### Idempotent storage

`store_native_ref()` is idempotent. Inserting a duplicate `(adapter, channel, message_id)` triple is a no-op. This handles re-delivery and retry scenarios without requiring upsert logic.

### Dedup suppression

When an inbound event arrives with a native triple that already exists in `native_message_refs`, the pipeline suppresses the event entirely (empty delivery outcomes). The original event is not re-processed.

## Outbound native ref (delayed callback)

Meshtastic is a queued, paced transport. The adapter cannot return a native packet ID synchronously from `deliver()`.

1. The renderer produces a content payload and the adapter enqueues it with the canonical `event_id` stored alongside the payload (never in the radio data).
2. The queue's `_process_queue` background loop dequeues items, applies pacing, and calls the real send function.
3. When the send returns a real Meshtastic packet ID, the adapter builds an `OutboundNativeRefRecord` with both `event_id` and `native_message_id`.
4. It calls the `AdapterContext.record_outbound_native_ref` callback, wired by the pipeline runner.
5. The pipeline persists a `NativeMessageRef(direction="outbound")` linking the canonical event ID to the Meshtastic packet ID.
6. Failures in the callback are caught and logged so they never crash the queue drain.

This is the only path that establishes the Matrix-event-ID to Meshtastic-packet-ID mapping for outbound messages.

## Meshtastic -> Matrix replies

A Meshtastic inbound reply (with `replyId`) becomes a Matrix reply (`m.in_reply_to`) only when a matching `NativeMessageRef` exists that links the original Matrix event ID to the referenced Meshtastic packet ID. When the mapping is missing (e.g. the original message was sent before the bridge started), the reply renders as plain text without `m.in_reply_to`. The fallback is safe and silent.

## Meshtastic -> Matrix reactions

MMRelay-compatible `m.emote` descriptive rendering. The Matrix renderer produces:

```text
\n {prefix}reacted {emoji} to "{preview}"
```

- `prefix` is the formatted relay prefix template (e.g. `[MeshUser]`). When no prefix is configured, the line starts with `\n` followed by a space.
- `emoji` is the reaction symbol extracted from the relation key.
- `preview` is the abbreviated original text (up to 40 chars, newlines collapsed to spaces, `...` appended if truncated).
- Longname/shortname from Meshtastic are preserved exactly as received: spaces, casing, and emoji characters are not altered.

When `mmrelay_compat` is false and a Matrix-native target event ID is available, a true `m.reaction` (`m.annotation`) is emitted instead.

The reaction symbol is extracted with the following precedence: relation `key`, then `payload["key"]`, then `payload["body"]`, then a warning-emoji fallback.

## Matrix -> Meshtastic reactions

Cross-platform reactions (Matrix origin, Meshtastic target) use MMRelay-compatible descriptive text:

```text
{compact_prefix}{sep}reacted {emoji} to "{preview}"
```

- `compact_prefix` strips spaces from display name tokens while preserving casing. Example: `"Display Name"` becomes `"DisplayName"`.
- `sep` is a single space inserted between the prefix and `reacted` only when the prefix is non-empty and does not already end with whitespace.
- `reply_id` is set when the original message has a known Meshtastic packet ID (via `target_native_ref` or `meshtastic_reply_id` metadata), making the descriptive text a structured reply.
- `emoji` is **not** set to `1`. The payload is plain text, not a native Meshtastic tapback.

Native Meshtastic-originated reactions (same adapter) continue to use `emoji=1` + `reply_id` for proper tapback round-tripping.

## Reaction-to-reaction suppression

The pipeline suppresses reactions that target another reaction. This prevents cascading reaction loops across transports.

A reaction event is suppressed when:

1. The target event's `event_kind` is `MESSAGE_REACTED` (itself a reaction), or
2. The target event carries a `reaction`-type relation.

Suppressed events are stored but not delivered. The pipeline returns empty delivery outcomes with no `DeliveryReceipt`. Inbound `NativeMessageRef` records for the suppressed event are still persisted.

Storage errors during target-event lookup are caught and skipped so they never block delivery of a non-suppressed event.

Normal reactions targeting a regular message are unaffected.

## Multi-radio reply and reaction examples

When multiple Meshtastic adapters are configured (e.g. `radio-alpha` on meshnet `AlphaNet` and `radio-beta` on meshnet `BetaNet`), each adapter carries independent config for `meshnet_name`, `default_channel`, `radio_relay_prefix`, and `max_text_bytes`.

### Reply across radios

1. A user on `radio-alpha` sends packet 2728143522. MEDRE stores an inbound `NativeMessageRef` linking `radio-alpha` + packet 2728143522 to canonical event `evt_001`.
2. The event is relayed to Matrix as a regular message.
3. Later, a user on `radio-beta` sends a reply with `replyId=2728143522`. The codec creates a relation with `target_native_ref(adapter="radio-beta", native_message_id="2728143522")`.
4. The `RelationResolver` looks up `resolve_native_ref("radio-beta", None, "2728143522")`. Because the original packet was on `radio-alpha`, not `radio-beta`, the lookup returns `None`. The relation stays unresolved.
5. The reply renders as plain text on both Matrix and `radio-beta`, without structured reply metadata.

If the reply had targeted a packet that was originally sent via `radio-beta` (same adapter), the resolution would succeed and structured reply metadata would be included.

### Reaction across radios

1. A message from `radio-alpha` is bridged to Matrix, producing canonical event `evt_050` with an outbound `NativeMessageRef` for the Matrix event ID.
2. A Matrix user reacts to `evt_050`. The pipeline enriches the reaction's relations with a `target_native_ref` for the target Meshtastic adapter (say, `radio-beta`).
3. If `radio-beta` has an outbound or inbound ref for `evt_050` (meaning the message was also relayed to `radio-beta`), the reaction renders as a descriptive reply with `reply_id`. Otherwise, it renders as descriptive text without `reply_id`.

### Same-radio reaction

When a Meshtastic tapback targets a packet from the same radio adapter, the resolution succeeds and the reaction round-trips as `emoji=1` + `reply_id`, preserving native tapback semantics.

## Fallback behavior when mappings are missing

All resolution and enrichment failures degrade gracefully:

| Scenario                                      | Fallback behavior                                               |
| --------------------------------------------- | --------------------------------------------------------------- |
| Inbound reply with unknown `replyId`          | Plain text, no `m.in_reply_to`, no `reply_id`                   |
| Inbound reaction with unknown target          | Emote or plain text without structured reply metadata           |
| Cross-platform reaction, no target native ref | Descriptive text without `reply_id`                             |
| Target adapter not in renderer configs        | `KeyError` at render time (configuration error, not data error) |
| Storage lookup failure during enrichment      | Relation preserved unchanged, degraded rendering                |
| Native triple already seen (dedup)            | Event suppressed entirely, no delivery outcomes                 |

Missing mappings never crash the pipeline. Every resolution path has a safe text-only fallback.

## Display name handling

| Direction                             | Rule                             | Example                              |
| ------------------------------------- | -------------------------------- | ------------------------------------ |
| Matrix -> Meshtastic (compact prefix) | Spaces removed, casing preserved | `"Display Name"` -> `"DisplayName"`  |
| Meshtastic -> Matrix                  | Longname preserved exactly       | `"Mesh User 📡"` -> `"Mesh User 📡"` |
| Meshtastic -> Matrix                  | Shortname preserved exactly      | `"Mesh"` -> `"Mesh"`                 |

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
- `meshtastic_reaction_key` _(MEDRE extension, not part of standard MMRelay; used for structured reaction symbol round-tripping)_

`meshtastic_emoji == 1` is the MMRelay-compatible reaction flag.

## Matrix inbound MMRelay metadata

Matrix inbound content carrying MMRelay keys preserves those fields in `CanonicalEvent.metadata.native.data`. The Matrix renderer and codec use these keys for MMRelay-compatible rendering when `mmrelay_compat` is enabled.

## SDK import boundaries

Transport SDK imports are isolated within adapter boundary modules. The `meshtastic` package is imported only within `adapters/meshtastic/` (primarily `compat.py` and `session.py`). The `nio` Matrix client library is imported only within `adapters/matrix/` (primarily `compat.py`). Both use optional-import guards so the SDK is required only when the corresponding adapter is active.

Core modules (`core/`, `interop/`, `config/`, `runtime/`) never import transport SDK packages. The `interop/mmrelay.py` module defines wire-format string constants only. Renderers import from `medre.core.*` and `medre.config.*` but never from SDK packages.

## Non-goals

These capabilities are outside the current scope:

- **Message backfill** on startup. The bridge processes events that arrive after it starts.
- **Edit and delete** propagation across transports.
- **Thread creation** across transports. Meshtastic has no native thread concept.
- **Durable delivery guarantees** (at-least-once, exactly-once). Delivery is best-effort per adapter.
- **Cross-radio native ID resolution**. A `replyId` from radio A is not resolvable through radio B's adapter namespace. Replies and reactions across different Meshtastic radios render as plain text.
