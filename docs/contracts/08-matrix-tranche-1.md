# Matrix Adapter Tranche 1: Protocol-Native Boundary Validation

> Contract version: 2
> Last updated: 2026-05-09


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


### Lifecycle Hardening (Tranche 2)

MatrixAdapter lifecycle was hardened for deterministic startup/shutdown:

- **Authentication verification.** After `restore_login`, the adapter checks `AsyncClient.logged_in`. If the client did not authenticate, the adapter closes the connection and raises `MatrixConnectionError`. This prevents silent failures where the adapter appears to start but never connects.
- **Sync task resilience.** The `sync_forever` task creation is wrapped in `try/except`. If task creation fails (e.g., no running event loop), the adapter cleans up the client and raises `MatrixConnectionError` with the underlying cause.
- **Idempotent stop.** `stop()` is safe to call multiple times, before `start()`, or after a failed `start()`. All client operations are guarded with `except Exception: pass` to prevent partial-cleanup crashes. The sync task cancellation uses `asyncio.wait_for` with the adapter's timeout to prevent hangs.
- **Health reporting.** `health_check()` returns `"unknown"` when no client exists, `"healthy"` when the client is logged in, and `"failed"` when the client exists but `logged_in` is false.
- **Mock-based lifecycle tests.** 21 dedicated tests in `tests/test_matrix_lifecycle.py` exercise all start/stop/health edge cases using mock nio objects. No real Matrix server or nio installation required.
  - `TestMatrixAdapterStart` (5 tests): successful client creation, event callback registration, missing-nio error, login failure, sync failure.
  - `TestMatrixAdapterStop` (4 tests): sync task cancellation, double-stop idempotency, stop-before-start, client close.
  - `TestMatrixAdapterHealthCheck` (4 tests): unknown/healthy/failed states across lifecycle.
  - `TestMatrixAdapterRestart` (1 test): full start->stop->start cycle.
  - `TestMatrixAdapterLifecycleEdgeCases` (2 tests): no orphaned tasks, stop after failed start.
  - `TestMatrixAdapterSyncFailure` (5 tests): sync_forever exception recording, health after failure, clean stop after failure, restart recovery, default failure state.


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


## Fixture Coverage (Tranche 2)

A centralized fixture module was added at `tests/fixtures/matrix_packets.py` providing factory functions for duck-typed nio event and response objects. These factories require no `nio` import and are shared across Matrix test modules.

Available factories:

| Factory | Produces |
|---------|----------|
| `make_room_message` | Duck-typed `RoomMessageText` with `.sender`, `.event_id`, `.body`, `.source` |
| `make_reply_event` | Room message with `m.in_reply_to` in content |
| `make_self_message` | Room message where `sender` matches the bot user |
| `make_notice_message` | Duck-typed `RoomMessageNotice` (`msgtype="m.notice"`) |
| `make_emote_message` | Duck-typed `RoomMessageEmote` (`msgtype="m.emote"`) |
| `make_medre_envelope_message` | Room message with valid MEDRE envelope |
| `make_corrupt_envelope_message` | Room message with malformed envelope (string, not dict) |
| `make_room` | Duck-typed `MatrixRoom` with `.room_id` |
| `make_room_send_response` | Duck-typed `RoomSendResponse` with `.event_id` |
| `make_room_send_response_none_event_id` | Duck-typed `RoomSendResponse` with `event_id=None` (malformed success) |
| `make_room_send_response_empty_event_id` | Duck-typed `RoomSendResponse` with `event_id=""` (malformed success) |
| `make_room_send_error` | nio error response stand-in (no `.event_id`, `__str__` returns error message) |

All factories use `SimpleNamespace` or minimal classes. No mocks, no patches, no nio imports. The module style matches existing fixture files (`meshtastic_packets.py`, `lxmf_packets.py`, `meshcore_packets.py`).


## Live Test Harness (Tranche 2)

A skipped-by-default live test harness at `tests/test_matrix_live.py` provides optional real-homeserver validation.  A companion runbook exists at `docs/runbooks/matrix-live-smoke.md`.

**The live harness is optional.**  It does not gate CI.  Default `pytest` runs are fake-only and always pass without a homeserver.  The harness exists to give developers a fast way to verify real Matrix connectivity during development, not to replace deterministic unit tests.

### What live smoke proves

- The adapter can connect to a real Matrix homeserver using an access token.
- `health_check()` transitions correctly: `"unknown"` before start, `"healthy"` after start, `"unknown"` after stop.
- Outbound `room_send` produces a real `event_id` starting with `$`.
- The adapter starts, sends, and stops cleanly without leaking asyncio tasks.
- The lifecycle round-trip (start → send → healthy → stop → unknown) works as an ordered sequence.

### What live smoke does NOT prove

- **Inbound message reception.**  Validating that a real Matrix event flows through the sync loop, through `_on_room_message`, through the codec, and into `publish_inbound` requires a second Matrix account (or a second device) to send a message.  With only one account, this is not reliably testable without polling loops and timeouts that make the suite flaky.  Inbound codec correctness is covered by deterministic unit tests instead.
- **Self-message suppression with real echoes.**  The homeserver echoes outbound messages back via the sync stream.  A live test would need to wait for that echo and assert `publish_inbound` was not called — but timing is unreliable without a second actor.  Self-message suppression is covered by deterministic unit tests (`test_matrix_lifecycle.py`, `test_matrix_adapter.py`).
- **MEDRE-origin envelope suppression.**  This secondary suppression path is unit-tested.  Live validation would require injecting an event with a matching envelope, which needs a second account or homeserver-level tricks.
- **E2EE, reactions, edits, deletes, attachments, media.**  None of these features are implemented in tranche 1.
- **Admin API, webhooks, HTTP server.**  Out of scope.
- **Non-Matrix connectivity.**  Meshtastic, MeshCore, LXMF adapters are out of scope.
- **Auth command / credential storage.**  The current tranche uses environment-variable access tokens.  A future mmrelay-like `auth` command for interactive login may be useful but is not implemented.

### Default behavior

Live tests are excluded from the default pytest run via:

.. code-block:: toml

    [tool.pytest.ini_options]
    markers = [
        "live: tests that connect to a real Matrix homeserver (skipped by default)",
    ]
    addopts = "-m 'not live'"

### Running live tests

Set the required environment variables and use the `live` marker:

.. code-block:: bash

    export MATRIX_HOMESERVER="https://matrix.example.com"
    export MATRIX_USER_ID="@bot:example.com"
    export MATRIX_ACCESS_TOKEN="syt_...your_token..."
    export MATRIX_ROOM_ID="!room:example.com"
    pip install -e ".[matrix]"
    pytest tests/test_matrix_live.py -m live -v

If any variable is unset, all live tests skip cleanly with a descriptive message.

### Required environment variables

======================== =====================================================
Variable                 Description
======================== =====================================================
``MATRIX_HOMESERVER``    Full URL of the Matrix homeserver
``MATRIX_USER_ID``       Fully-qualified Matrix user ID (``@user:server``)
``MATRIX_ACCESS_TOKEN``  Access token for the bot account
``MATRIX_ROOM_ID``       Room ID to send test messages to
======================== =====================================================

### Available tests

- `test_adapter_starts_and_reports_healthy` -- connects to the homeserver, verifies `health_check()` returns `"healthy"` and `platform == "matrix"`.
- `test_adapter_health_unknown_after_stop` -- verifies `health_check()` returns `"unknown"` after `stop()`.
- `test_adapter_health_unknown_before_start` -- verifies `health_check()` returns `"unknown"` on a never-started adapter.
- `test_send_text_message_captures_event_id` -- delivers an `m.text` message, asserts `event_id` starts with `$`, asserts `native_channel_id` matches.
- `test_full_lifecycle_start_send_stop` -- ordered round-trip: start → send → healthy → stop → unknown.
- `test_self_message_suppression_note` -- documents why live suppression testing is limited (always passes).
- `test_medre_origin_envelope_suppression_note` -- documents MEDRE-origin suppression scope (always passes).

### Local homeserver setup (no Docker required)

Synapse via pip (recommended):

.. code-block:: bash

    pip install matrix-synapse
    python -m synapse.app.homeserver \
      --server-name localhost \
      --config-path homeserver.yaml \
      --generate-config \
      --report-stats=no
    python -m synapse.app.homeserver --config-path homeserver.yaml
    register_new_matrix_user -c homeserver.yaml -u bot -p secret http://localhost:8008
    curl -s -X POST \
      -d '{"type":"m.login.password","user":"bot","password":"secret"}' \
      http://localhost:8008/_matrix/client/v3/login

Conduit (lightweight Rust homeserver):

.. code-block:: bash

    # Download from https://conduit.rs or build from source
    ./conduit  # port 6167 by default

Docker (optional, not required):

.. code-block:: bash

    docker run -d --name synapse -p 8008:8008 \
      -e SYNAPSE_SERVER_NAME=localhost \
      -e SYNAPSE_REPORT_STATS=no \
      matrixdotorg/synapse:latest

### Known limitations

- No E2EE. Tests target unencrypted rooms only. E2EE is deferred to a future release. When implemented, `mindroom-nio[e2e]` will be required (installable via `pip install -e ".[matrix-e2e]"`) and both `store_path` and `device_id` will become mandatory. The `.[matrix-e2e]` optional dependency group now exists in `pyproject.toml` as a scaffold; runtime encryption is not yet implemented. An `e2ee_required` config mode is being introduced that will refuse startup when E2EE deps are absent, but encrypted message operation remains unsupported until a future tranche. See the runbook (`docs/runbooks/matrix-alpha-operation.md`, section 8) and the E2EE readiness contract (`docs/contracts/25-matrix-e2ee-readiness.md`) for posture details.
- Cross-signing/verification and room key backup/import/export remain deferred. No implementation timeline.
- No reactions, edits, deletes, or attachments.
- No production credential storage or auth command.
- No admin API.
- No inbound reception test (requires second actor).
- Storage is authoritative; metadata envelope is secondary.
- Real operation remains Matrix-tranche-1-limited.


## Operational Safety

These mechanisms prevent message loops and ensure clean delivery in a bridge environment where the adapter's own user account appears as a room participant.


### Self-Message Suppression

In a bridge scenario, the adapter's Matrix user is present in rooms it bridges. When the adapter sends an outbound message, the Matrix homeserver echoes that event back via the sync stream. Without suppression, the adapter would decode its own outbound message as a new inbound event, creating an echo loop.

**Primary defense: adapter-level sender check, before decode.** In `_on_room_message`, before any codec work begins, the incoming event's `sender` field is compared against `config.user_id`. If they match, the event is discarded. No decoding, no storage, no pipeline processing.

**Missing sender allowed through.** Events without a `sender` field are not suppressed. These are synthetic or malformed events that should reach the codec for logging and diagnostic handling, even if they are ultimately discarded downstream.

**Secondary defense: MEDRE-origin envelope check.** After decode, if an inbound event carries a `medre` envelope whose `source_adapter` matches this adapter's ID, the event is recognized as a loop hint. This is a secondary check, not the primary suppression path. It exists as defense in depth for cases where the sender field is absent or spoofed in a bridged environment.


### MEDRE-Origin Loop Hint Suppression

The `envelope.source_adapter` check is non-authoritative. A missing or corrupt envelope does not suppress a legitimate inbound event. Storage remains the authoritative source for deduplication and correlation. The envelope check is a performance optimization: it lets the adapter cheaply discard events it clearly produced, without requiring a storage round trip.

Corrupt or partial envelopes are tolerated. If the envelope is present but malformed, the adapter does not crash. The event passes through to normal pipeline processing, and storage-level deduplication handles any actual duplicates.


### RelationResolver API Alignment

`RelationResolver` methods consume and produce relation data using a split-field storage contract: `(adapter, channel, message_id) -> event_id`. The `resolve_relation()` method aligns with this contract, accepting a triple of adapter ID, channel, and native message ID to look up the corresponding canonical event ID. All resolver methods use the same field decomposition for consistency. No monolithic "native ref string" parsing is performed inside the resolver; the caller provides the already-split fields.


### Delivery Hygiene

Outbound message delivery follows strict hygiene rules to prevent protocol violations and aid debugging:

- **`target_channel` is the preferred routing field.** The delivery target is read from `target_channel` on the delivery instruction. This is the canonical routing field.
- **`room_id` is stripped from content before `room_send`.** If `room_id` appears in the rendered content dict, it is removed before the content is passed to `nio.AsyncClient.room_send`. The room ID belongs in the API call's routing parameter, not in the event content body. Including it there is a protocol violation that some homeservers reject or silently ignore.
- **Fail-fast on missing room.** If no valid room ID can be determined for an outbound delivery, the adapter raises an error immediately rather than attempting delivery with a null or empty room identifier.


## Configuration (MatrixConfig)

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `adapter_id` | `str` | Yes | Unique adapter instance ID |
| `homeserver` | `str` | Yes | Matrix homeserver URL (`https://...`) |
| `user_id` | `str` | Yes | Full Matrix user ID (`@user:server`) |
| `device_id` | `Optional[str]` | No | Persistent device identifier. Unused in plaintext alpha; will be required for E2EE production. |
| `access_token` | `str` | Yes | Matrix access token. Never logged or embedded in events. |
| `room_allowlist` | `Optional[set[str]]` | No | Allowed room IDs. `None` means all rooms are accepted. |
| `metadata_embedding_mode` | `str` | No | `"safe"` (default) or `"rich"` |
| `store_path` | `Optional[str]` | No | State store path. Optional in plaintext alpha (no crypto state to persist). Will be required for E2EE production to persist Olm/Megolm session keys and device data across restarts. |
| `sync_timeout_ms` | `int` | No | Sync poll timeout. Default: 30000. |


## Metadata Envelope

`MatrixMetadataEnvelope` is a frozen Python `dataclass`, not a `msgspec` struct. This is intentional. The envelope is an adapter-internal data structure. It is never part of the canonical event model, never passed through `msgspec` serialization directly, and never round-trips through the canonical pipeline as a typed object. The codec handles its own JSON serialization/deserialization when embedding and extracting it from Matrix event content. Using a frozen dataclass gives immutability guarantees without imposing a `msgspec` dependency on an adapter-internal type. This design is documented and tested.

Fields:

- `schema_version`
- `canonical_event_id`
- `source_adapter`
- `source_channel`
- `provenance`
- `relation_info`
- `lineage_pointer`
- `metadata_mode`
- `native_source_summary`

The envelope is round-trip tolerant. Unknown fields are tolerated/ignored on decode. Missing or corrupt envelopes return `None` rather than raising. No secrets are ever embedded: no access tokens, no private keys.


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
- **Lifecycle tests.** `tests/test_matrix_lifecycle.py` covers start/stop/health edge cases with mock nio objects. 21 tests across 6 classes, no real server required.
- **Centralised fixtures.** `tests/fixtures/matrix_packets.py` provides reusable duck-typed nio event/response factories across all Matrix test modules.
- **Live test harness.** `tests/test_matrix_live.py` provides optional real-homeserver validation, skipped by default. Run with `pytest -m live`.


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
- Live Synapse integration tests. All testing uses `FakeMatrixAdapter` and in-memory storage. No test requires a running Synapse homeserver.
- Self-message suppression bypass or selective echo. The sender check is unconditional for matched senders. There is no configuration to disable or selectively allow self-echoes.
- Reconnect logic or automatic recovery from sync disconnection
- E2EE key management or encrypted room participation
