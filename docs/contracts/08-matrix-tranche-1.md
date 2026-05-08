# Matrix Adapter Tranche 1: Protocol-Native Boundary Validation

> Contract version: 1
> Last updated: 2026-05-08


## Overview

This is the first real adapter that exercises the MEDRE runtime's architecture boundaries against a live protocol. The Matrix adapter uses `mindroom-nio` (imported as `nio`) as its Matrix client library. Everything in this tranche is designed to validate that the runtime's decode/render/deliver separation holds up when faced with real protocol quirks: synthetic events, unknown content types, partial metadata, and actual network delivery.

The adapter doesn't route, doesn't plan, and doesn't render fallback text. It decodes inbound Matrix events into canonical form and delivers outbound rendered content. That's it. The pipeline owns receipts, relation resolution, and storage. Adapters only transport messages and report native delivery metadata back to the pipeline. The Matrix-specific renderer lives inside the adapter package (`medre.adapters.matrix.renderer`), not in core. Core owns only the generic rendering protocol and pipeline machinery.

Matrix capabilities in tranche 1 are limited to text message reception and reply basics. Reactions, edits, deletes/redactions, attachments/media, and E2EE are all deferred.


## Supported Features

- **Inbound text message reception.** Matrix `m.text`, `m.notice`, and `m.emote` events are decoded into canonical events by `MatrixCodec`.
- **Canonical event rendering (adapter-owned).** `MatrixRenderer` turns canonical events into Matrix `m.room.message` content bodies. This renderer lives at `medre.adapters.matrix.renderer`, owned by the adapter/platform layer. Core owns the generic rendering protocol and pipeline machinery, not this implementation.
- **MEDRE metadata envelope.** Embedded in Matrix event content under `content["medre"]["envelope"]` for cross-bridge correlation. Secondary to storage; diagnostic only.
- **Inbound source native ref carry.** `MatrixCodec.decode()` carries the inbound Matrix event ID through `CanonicalEvent.source_native_ref`. The pipeline persists an inbound `NativeMessageRef(direction="inbound")` after canonical event storage, linking the Matrix event ID to the canonical event for future correlation lookups.
- **Reply detection (codec-level).** Inbound `m.in_reply_to` references are decoded directly into `EventRelation(relation_type="reply", target_native_ref=...)` by `MatrixCodec` without any storage lookup. The codec does not resolve native refs. The pipeline invokes `RelationResolver` during ingress to resolve these native relation refs to canonical event IDs where possible. Unresolved native relation refs are preserved and do not crash routing or rendering.
- **Outbound delivery with native event ID capture.** Messages are sent via `nio.AsyncClient.room_send`. The `RoomSendResponse.event_id` returned by the Matrix homeserver is the source of truth for outbound native correlation. This value is reported as generic adapter delivery result metadata and persisted by the pipeline as `NativeMessageRef.native_message_id` (direction `outbound`). Adapters do not manage their own storage; they transport and report.
- **Native event ref correlation.** Pipeline-owned storage (`store_native_ref`, `resolve_native_ref`) maps Matrix event IDs to canonical event IDs.
- **FakeMatrixAdapter.** A test double that requires no network and no `nio` installation. Enforces `deliver(RenderingResult)` on the outbound path; inbound simulation uses `simulate_inbound(CanonicalEvent)`. Used for unit and integration tests.
- **Deterministic failure injection.** The fake adapter supports controlled failure modes for hardening tests.


## Architecture Boundaries

These boundaries are enforced by design, not by convention. Tests verify them.

- `MatrixAdapter` does not route. No `Router` import.
- `MatrixAdapter` does not plan delivery. No `FallbackResolver`, no `DeliveryPlan` construction.
- `MatrixAdapter` does not render fallback text. Rendering lives in `MatrixRenderer`.
- `MatrixRenderer` does not perform delivery. No `nio` `RoomSend` calls.
- `MatrixRenderer` is adapter/platform-owned. It lives at `medre.adapters.matrix.renderer`. Core owns the generic rendering protocol (interface, pipeline dispatch), not this Matrix-specific implementation. Core never imports from the adapter package.
- `MatrixCodec` does not route, plan, or render. It is a pure decode/encode layer. It does not resolve native refs or query storage.
- `RelationResolver` runs during pipeline ingress, after decode and before storage, to resolve native relation refs to canonical event IDs where possible.
- Storage remains the authoritative source for event correlation. The pipeline owns receipts and persistence. Adapters only transport and report native delivery metadata.
- The metadata envelope is secondary. Storage is the system of record.
- `RoomSendResponse.event_id` from `nio` is the sole source of truth for outbound Matrix native correlation. No synthetic or locally-generated IDs are used as native refs.
- `FakeMatrixAdapter.deliver()` accepts `RenderingResult`, not raw `CanonicalEvent`. Inbound simulation goes through `simulate_inbound(CanonicalEvent)`.


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

**Inbound replies.** `MatrixCodec` decodes `m.in_reply_to` into an `EventRelation(relation_type="reply", target_native_ref=NativeRef(...))` directly, with no storage lookup in the codec or adapter. The pipeline invokes `RelationResolver` during ingress to resolve the `target_native_ref` to a canonical `target_event_id` via `resolve_native_ref` where the referenced event has already been stored. If resolution fails (the referenced event hasn't been seen yet), the native relation ref is preserved on the relation. Unresolved relations do not crash routing or rendering; `fallback_text` is used by the delivery stage when the target adapter lacks native relation support.

**Reactions: deferred.** Matrix reaction delivery and `m.annotation` decoding are not part of tranche 1. Reaction semantics are deferred to a later tranche. No reaction-related event processing, storage, or rendering occurs in this tranche.

**Edits, deletes/redactions: deferred.** Matrix `m.replace` (edits) and `m.redaction` (deletes/redactions) are not part of tranche 1. No edit or redaction event processing, storage, or rendering occurs in this tranche.

**Attachments/media: deferred.** File, image, audio, video, and other media attachments (`m.file`, `m.image`, `m.audio`, `m.video`) are not part of tranche 1.


## Storage / Correlation

The pipeline owns all receipt and persistence logic. Adapters transport messages and report delivery metadata. They do not manage their own storage. Storage is the authoritative source of truth. The metadata envelope remains secondary and diagnostic.

**Outbound delivery.** After a successful `room_send`, the `RoomSendResponse.event_id` returned by the Matrix homeserver is the source of truth for outbound native correlation. The adapter reports this value through generic adapter delivery result metadata. The pipeline then persists it as `NativeMessageRef(native_message_id=..., direction="outbound")`, linking the canonical event to its Matrix-native counterpart. No synthetic or locally-generated event IDs are ever used as native refs.

**Inbound events.** The canonical `event_id` is system-generated. `MatrixCodec.decode()` carries the inbound Matrix event ID on `CanonicalEvent.source_native_ref`. After canonical event storage, the pipeline persists `NativeMessageRef(native_message_id=<matrix_event_id>, direction="inbound")` to map the native Matrix event ID to the canonical event for future correlation lookups (e.g., resolving reply targets).


## Testing Approach

- **FakeMatrixAdapter.** No real network, no `nio` dependency. Enforces `deliver(RenderingResult)` on the outbound path. Inbound simulation uses `simulate_inbound(CanonicalEvent)`. Simulates the full inbound/outbound cycle against in-memory state.
- **Unit isolation.** `MatrixRenderer` and `MatrixCodec` are tested independently of the adapter.
- **Pipeline integration.** Tests combine `FakeMatrixAdapter` with `SQLiteStorage` to exercise the full decode/store/render/deliver path.
- **Boundary verification.** Tests assert that core imports don't leak into the adapter package, and that the adapter doesn't import routing or planning modules.
- **Optional dependency.** `mindroom-nio` is guarded by a `HAS_NIO` compat flag. Core tests pass without it installed.


## Dependency

```
pip install medre[matrix]
```

This installs `mindroom-nio>=0.25`. The core install (`pip install medre`) does not include it. All core tests pass without `mindroom-nio` present.

### Why `mindroom-nio` instead of upstream `matrix-nio`

- **Distribution name:** `mindroom-nio>=0.25` on PyPI.
- **Python import name:** `nio` (same as upstream `matrix-nio`).
- **Rationale.** `mindroom-nio` is a maintained fork of `matrix-nio`, selected because it is under active development. It is tracking the vodozemac migration path and other improvements that upstream `matrix-nio` has not shipped. This positions the adapter for future compatibility without requiring a library swap later.
- **E2EE scope.** Selecting `mindroom-nio` for maintenance and future-compatibility reasons does not implement or expand E2EE in tranche 1. No `[e2e]` configuration, key upload/claim, device verification, SAS/QR, key backup, or encryption internals are part of this tranche. The dependency choice is about library health, not feature activation.


## Non-Goals (This Tranche)

These are explicitly out of scope for tranche 1:

- End-to-end encryption (E2EE)
- Attachments, files, images, media (`m.file`, `m.image`, `m.audio`, `m.video`)
- Matrix edits (`m.replace`)
- Matrix deletes and redactions (`m.redaction`)
- Matrix reactions (`m.annotation`). Reaction delivery and decoding are deferred to a later tranche.
- Room membership sync beyond basic join
- Admin API for Matrix configuration
- Webhooks or HTTP server
- Meshtastic, MeshCore, LXMF, Discord, Telegram adapters
- MMRelay compatibility mode
- Broad plugin ecosystem expansion
- Live Synapse integration tests
