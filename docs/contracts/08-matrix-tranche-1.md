# Matrix Adapter Tranche 1: Protocol-Native Boundary Validation

> Contract version: 1
> Last updated: 2026-05-08


## Overview

This is the first real adapter that exercises the MEDRE runtime's architecture boundaries against a live protocol. The Matrix adapter uses `mindroom-nio` (a matrix-nio fork, imported as `nio`) as its Matrix client library. Everything in this tranche is designed to validate that the runtime's decode/render/deliver separation holds up when faced with real protocol quirks: synthetic events, unknown content types, partial metadata, and actual network delivery.

The adapter doesn't route, doesn't plan, and doesn't render fallback text. It decodes inbound Matrix events into canonical form and delivers outbound rendered content. That's it.


## Supported Features

- **Inbound text message reception.** Matrix `m.text`, `m.notice`, and `m.emote` events are decoded into canonical events by `MatrixCodec`.
- **Canonical event rendering.** `MatrixRenderer` turns canonical events into Matrix `m.room.message` content bodies.
- **MEDRE metadata envelope.** Embedded in Matrix event content under `content["medre"]["envelope"]` for cross-bridge correlation.
- **Reply detection.** Inbound `m.in_reply_to` references are resolved through storage (`resolve_native_ref`), with fallback text when the referenced event isn't found.
- **Reaction detection.** Inbound `m.annotation` rel_type events are decoded, with the annotation key stored as `EventRelation.key`.
- **Outbound delivery.** Messages are sent via `nio.AsyncClient.room_send`.
- **Native event ref correlation.** Storage-backed `store_native_ref` and `resolve_native_ref` map Matrix event IDs to canonical event IDs.
- **FakeMatrixAdapter.** A test double that requires no network and no `nio` installation. Used for unit and integration tests.
- **Deterministic failure injection.** The fake adapter supports controlled failure modes for hardening tests.


## Architecture Boundaries

These boundaries are enforced by design, not by convention. Tests verify them.

- `MatrixAdapter` does not route. No `Router` import.
- `MatrixAdapter` does not plan delivery. No `FallbackResolver`, no `DeliveryPlan` construction.
- `MatrixAdapter` does not render fallback text. Rendering lives in `MatrixRenderer`.
- `MatrixRenderer` does not perform delivery. No `nio` `RoomSend` calls.
- `MatrixCodec` does not route, plan, or render. It is a pure decode/encode layer.
- Storage remains the authoritative source for event correlation.
- The metadata envelope is secondary. Storage is the system of record.


## Configuration (MatrixConfig)

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `adapter_id` | `str` | Yes | Unique adapter instance ID |
| `homeserver` | `str` | Yes | Matrix homeserver URL (`https://...`) |
| `user_id` | `str` | Yes | Full Matrix user ID (`@user:server`) |
| `device_id` | `Optional[str]` | No | Persistent device identifier |
| `access_token` | `str` | Yes | Matrix access token. Never logged or embedded in events. |
| `room_allowlist` | `Optional[set[str]]` | No | Allowed room IDs. `None` means all rooms are accepted. |
| `metadata_embedding_mode` | `str` | No | `"safe"` (default) or `"rich"` |
| `store_path` | `Optional[str]` | No | State store path. Unused without E2EE. |
| `sync_timeout_ms` | `int` | No | Sync poll timeout. Default: 30000. |


## Metadata Envelope

`MatrixMetadataEnvelope` is embedded under `content["medre"]["envelope"]`. Fields:

- `schema_version`
- `canonical_event_id`
- `source_adapter`
- `source_channel`
- `provenance`
- `relation_info`
- `lineage_pointer`
- `metadata_mode`
- `native_source_summary`

The envelope is round-trip tolerant. Unknown fields are preserved on decode. Missing or corrupt envelopes return `None` rather than raising. No secrets are ever embedded: no access tokens, no private keys.


## Relation Behavior

**Inbound replies.** An `m.in_reply_to` event_id is resolved via `storage.resolve_native_ref`. If the referenced event is found in storage, the canonical event gets an `EventRelation` with `relation_type="reply"`. If resolution fails, the adapter falls back to rendering the reply context as text via `MatrixRenderer`.

**Reactions.** An `m.annotation` rel_type is decoded with the annotation key stored as `EventRelation.key`. The target event is resolved through storage the same way replies are.


## Storage / Correlation

**Outbound delivery.** After a successful `room_send`, a `NativeMessageRef` is stored with `matrix_room_id` and `matrix_event_id`. This links the canonical event to its Matrix-native counterpart.

**Inbound events.** The canonical `event_id` is system-generated. The native Matrix event_id is stored as a native ref for future correlation lookups.


## Testing Approach

- **FakeMatrixAdapter.** No real network, no `nio` dependency. Simulates the full inbound/outbound cycle against in-memory state.
- **Unit isolation.** `MatrixRenderer` and `MatrixCodec` are tested independently of the adapter.
- **Pipeline integration.** Tests combine `FakeMatrixAdapter` with `SQLiteStorage` to exercise the full decode/store/render/deliver path.
- **Boundary verification.** Tests assert that core imports don't leak into the adapter package, and that the adapter doesn't import routing or planning modules.
- **Optional dependency.** `mindroom-nio` is guarded by a `HAS_NIO` compat flag. Core tests pass without it installed.


## Dependency

```
pip install medre[matrix]
```

This installs `mindroom-nio>=0.25`. The core install (`pip install medre`) does not include it. All core tests pass without `mindroom-nio` present.


## Non-Goals (This Tranche)

These are explicitly out of scope for tranche 1:

- End-to-end encryption (E2EE)
- Attachments, files, images, media
- Room membership sync beyond basic join
- Admin API for Matrix configuration
- Webhooks or HTTP server
- Meshtastic, MeshCore, LXMF, Discord, Telegram adapters
- MMRelay compatibility mode
- Broad plugin ecosystem expansion
