# LXMF Adapter Tranche 1: Message Transport Validation

> **Status:** Active
> **Classification:** Normative
> **Authority:** Current contract for LXMF adapter features, fields, config, and boundaries
> **Last reviewed:** 2026-05-24
>
> Contract version: 1
> Last updated: 2026-05-08

## Overview

This is a constrained message transport adapter. The LXMF (Lightweight eXtensible Messaging Format) adapter declares `AdapterRole.TRANSPORT` and uses the platform name `lxmf`. No real Reticulum or LXMF library dependency is required in tranche 1. The fake adapter provides deterministic IDs and full lifecycle simulation without any network or library dependency. Tranche 1 validates that the MEDRE runtime's decode/render/deliver pipeline works against a message transport with structured fields, native title support, and protocol-native message IDs.

The adapter does not route, does not plan, and does not render fallback text. It decodes inbound LXMF messages into canonical events and delivers outbound rendered content. The pipeline owns receipts, relation resolution, and storage. Adapters transport messages and report native delivery metadata back to the pipeline. The LXMF-specific renderer lives inside the adapter package (`medre.adapters.lxmf.renderer`), not in core. Core owns the generic rendering protocol and pipeline machinery. Core never imports from the LXMF adapter package.

LXMF capabilities in tranche 1 are limited to text message ingress and egress with fields-based metadata. Real Reticulum connectivity, resource/attachment transfer, identity management, propagation node interaction, and LXST are all deferred.

Reticulum is an internal implementation dependency when real connectivity is active. MEDRE core does not import or depend on Reticulum. Reticulum `Identity` and `Destination` classes are never exposed to the pipeline. LXMF addresses (16-byte hashes) are plain strings as far as MEDRE is concerned. No raw Reticulum adapter support is planned. LXST is explicitly out of scope.

## Supported Features

- **Inbound text decoding.** LXMF messages are decoded into canonical events by `LxmfCodec`. The message's `content` field becomes `payload["body"]`. The `title` field becomes `payload["title"]` when present. The LXMF `fields` dict is inspected for the `FIELD_CUSTOM_META` (0xFD) key; when found, its contents are extracted as MEDRE metadata. Packet metadata (message_id, source_hash, destination_hash, is_direct_message) is stored in `metadata.native.data` as a flat dict.
- **Fields metadata envelope.** MEDRE metadata can be embedded in the LXMF `fields` dict under the `FIELD_CUSTOM_META` (0xFD) key. This namespace carries a structured dict with keys like `schema_version`, `event_id`, `source_adapter`, and relation metadata. This is a MEDRE convention, not native LXMF semantics. Other LXMF clients will see the field but may ignore it. The adapter does not validate or enforce schema conformance within the envelope.
- **Title support.** Titles are natively preserved. Unlike the Meshtastic and MeshCore adapters, which have no native title concept, LXMF carries `title` as a first-class field. `LxmfCodec.decode()` maps it to `payload["title"]`. `LxmfRenderer` includes it in outbound content when present.
- **Outbound text rendering.** `LxmfRenderer` turns canonical events into LXMF content payloads: a dict with keys `content` (the body string), `title` (optional title string), and `fields` (optional dict for the MEDRE metadata envelope). The renderer lives at `medre.adapters.lxmf.renderer`, owned by the adapter layer. Core never imports from the adapter package.
- **Native refs via message IDs.** LXMF message IDs are 32-byte SHA-256 hashes, represented as hex strings. `LxmfCodec.decode()` sets `source_native_ref` with the message's hex ID as `native_message_id`. The pipeline's `_persist_inbound_native_ref` persists this as a `NativeMessageRef(direction="inbound")`. Outbound: `FakeLxmfAdapter.deliver()` returns an `AdapterDeliveryResult` with the deterministic hex message ID.
- **Fake adapter for testing.** `FakeLxmfAdapter` is a full adapter (not a client-facing test utility) that mirrors the real adapter's lifecycle and inbound/outbound flow. It generates deterministic sequential message IDs using SHA-256 hashes of sequential counter values. It tracks all sent messages in `sent_messages`. The fake adapter's `deliver()` returns an `AdapterDeliveryResult` with the deterministic message ID. `set_deliver_failure(True)` triggers an `AdapterSendError` (transient) on the next delivery for error testing. No real Reticulum dependency, no network required.
- **Metadata embedding in fields.** The adapter supports embedding MEDRE metadata into the LXMF fields dict. The envelope contains relation info, provenance data, and schema version. Known MEDRE envelope fields round-trip through `LxmfFieldsHelper`: the renderer embeds them, the codec extracts them. Unknown LXMF fields dict entries are preserved in the envelope only because `LxmfFieldsHelper` copies the fields dict. MEDRE canonical events do not preserve arbitrary unknown LXMF field data. The adapter does not interpret or transform the metadata contents beyond the known envelope keys.
- **Relation metadata in fields (transport only).** Relation metadata is embedded in the fields envelope. MEDRE-native relations are carried as structured data inside the `FIELD_CUSTOM_META` envelope. The adapter treats this as opaque metadata transport. Canonical `EventRelation` reconstruction from fields is **deferred**. Relation resolution remains pipeline-owned.

## Architecture Boundaries

These boundaries are enforced by design, not by convention. Tests verify them.

- `LxmfAdapter` does not route. No `Router` import.
- `LxmfAdapter` does not plan delivery. No `FallbackResolver`, no `DeliveryPlan` construction.
- `LxmfAdapter` does not render fallback text. Rendering lives in `LxmfRenderer`.
- `LxmfRenderer` does not perform delivery. No LXMF client calls.
- `LxmfRenderer` is adapter-owned. It lives at `medre.adapters.lxmf.renderer`. Core owns the generic rendering protocol (interface, pipeline dispatch), not this LXMF-specific implementation. Core never imports from the adapter package.
- `LxmfCodec` does not route, plan, or render. It is a pure decode layer. It does not resolve native refs or query storage.
- Storage remains the authoritative source for event correlation. The pipeline owns receipts and persistence. Adapters transport and report native delivery metadata.
- No real Reticulum or network is required for default tests. `FakeLxmfAdapter` simulates the full cycle.
- Reticulum `Identity` and `Destination` classes are never exposed to the pipeline. They are internal implementation details when real connectivity is active.
- **Platform identity.** A fake LXMF adapter declares `platform='lxmf'`, not `'fake_lxmf'`. The fake/testing nature is indicated by class name and `config.connection_type='fake'`, not by the platform string. This ensures the platform registry correctly maps fake LXMF adapters to `LxmfRenderer` for all LXMF routes.

## Capability Declaration

```python
AdapterCapabilities(
    text=True,
    title=True,
    replies="unsupported",
    reactions="unsupported",
    edits="unsupported",
    deletes="unsupported",
    attachments=False,
    metadata_fields=True,
    delivery_receipts=False,
    store_and_forward=False,
    direct_messages=True,
    max_text_bytes=16384,
    max_text_chars=16384,
)
```

This is an honest declaration. The adapter does what it says and nothing more. `title=True` reflects LXMF's native title field. `metadata_fields=True` enables the fields dict as a metadata transport, which is a key advantage over adapters without structured field support. `direct_messages=True` because LXMF is inherently DM-oriented: messages are addressed to specific identities. `replies="unsupported"` because LXMF has no native `replyId` field, though MEDRE relation metadata can be carried through the fields dict. `max_text_bytes=16384` and `max_text_chars=16384` advertise the transport's willingness to handle payloads up to that size. The renderer does not enforce truncation in tranche 1.

## Configuration (LxmfConfig)

`LxmfConfig` is a frozen dataclass with a `validate()` method that checks field constraints. Invalid configuration raises `LxmfConfigError` before the adapter starts.

| Field                     | Type                                                        | Required | Description                                                                                                                                                                                                                      |
| ------------------------- | ----------------------------------------------------------- | -------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `adapter_id`              | `str`                                                       | Yes      | Unique adapter instance ID. Must be non-empty.                                                                                                                                                                                   |
| `connection_type`         | `Literal["fake", "reticulum"]`                              | No       | Connection mode. `"fake"` is the default. `"reticulum"` is accepted at config level but requires optional dependencies; `start()` raises `LxmfConnectionError` for non-fake modes when SDK is unavailable. Defaults to `"fake"`. |
| `default_delivery_method` | `Literal["direct", "opportunistic", "propagated", "paper"]` | No       | Default LXMF delivery method. Defaults to `"direct"`. This is a configuration hint for future real connectivity; the fake adapter ignores it.                                                                                    |
| `display_name`            | `str`                                                       | No       | Optional display name for LXMF announces. Defaults to `""`.                                                                                                                                                                      |
| `stamp_cost`              | `int`                                                       | No       | Default stamp cost (0 = no stamp required). Defaults to `8`.                                                                                                                                                                     |
| `meshnet_name`            | `str`                                                       | No       | Human-readable meshnet name. Informational. Defaults to `""`.                                                                                                                                                                    |
| `default_channel`         | `int`                                                       | No       | Default radio channel index for outbound messages. Defaults to `0`.                                                                                                                                                              |
| `message_delay_seconds`   | `float`                                                     | No       | Minimum delay between outbound messages (pacing). Defaults to `0.5`.                                                                                                                                                             |
| `metadata_embedding`      | `bool`                                                      | No       | Whether to embed MEDRE metadata envelopes in LXMF fields. Defaults to `True`.                                                                                                                                                    |
| `identity_path`           | `str \| None`                                               | No       | Path to identity file. Placeholder for future use. Defaults to `None`.                                                                                                                                                           |

## Fields Envelope Convention

LXMF messages carry a `fields` dict that can hold arbitrary key-value pairs. The MEDRE adapter uses a reserved namespace within this dict to transport MEDRE metadata across the LXMF network.

- **Namespace key:** `FIELD_CUSTOM_META` (0xFD). This is a custom field key chosen to avoid collision with standard LXMF field keys.
- **Contents:** A structured dict containing `schema_version` (string), `event_id` (UUID string), `source_adapter` (adapter ID string), and optional `relations` (list of relation dicts with `relation_type`, `target_event_id`, `target_native_ref`).
- **MEDRE convention, not LXMF semantics.** This envelope is a MEDRE-specific convention. Other LXMF clients will see the field key in the fields dict but may ignore it. The adapter does not validate schema conformance within the envelope beyond extracting known keys.
- **Known envelope keys round-trip through LxmfFieldsHelper.** Metadata embedded by the renderer into fields is extracted by the codec on decode. This works reliably for the known MEDRE envelope keys (schema_version, event_id, source_adapter, relations, metadata_keys). It is not a guarantee that arbitrary or future LXMF field data survives the MEDRE pipeline. Unknown fields dict entries are preserved only because `LxmfFieldsHelper` copies the fields dict, not because canonical events preserve all LXMF field data.

## Native Ref Flow

### Inbound

1. An LXMF message arrives at the adapter with a 32-byte SHA-256 message ID (hex string).
2. `LxmfCodec.decode()` converts the message into a `CanonicalEvent` with `source_native_ref=NativeRef(adapter=<adapter_id>, native_channel_id="", native_message_id=<hex_message_id>)`.
3. The adapter calls `ctx.publish_inbound(event)`, pushing the canonical event into the pipeline.
4. The pipeline's `_persist_inbound_native_ref` reads `event.source_native_ref` and persists a `NativeMessageRef(direction="inbound")` mapping the LXMF message ID to the canonical event ID.

### Outbound (Fake Adapter)

1. The pipeline renders a canonical event into a `RenderingResult` via `LxmfRenderer`.
2. The pipeline calls `adapter.deliver(result)` on the `FakeLxmfAdapter`.
3. The fake adapter generates a deterministic SHA-256 hex message ID from an internal counter.
4. The fake adapter returns `AdapterDeliveryResult(native_message_id=<hex_message_id>, native_channel_id="")`.
5. The pipeline reads the `AdapterDeliveryResult` and persists `NativeMessageRef(direction="outbound")`.

### Outbound (Real Adapter)

Not implemented in tranche 1. The real adapter's `deliver()` is scaffolded and returns `None`. No outbound native ref is persisted.

## Relation and Reply Behavior

LXMF has no native `replyId` field. The adapter declares `replies="unsupported"` in its capabilities. The adapter does not produce `EventRelation` objects from native LXMF message data.

MEDRE relation metadata can be carried through the fields dict envelope (see Fields Envelope Convention above). When present on an inbound message, the codec extracts relation data from the `FIELD_CUSTOM_META` envelope. This is metadata transport, not native reply semantics. Relation resolution remains pipeline-owned.

Outbound reply delivery is not supported. Future tranches may add structured relation handling through the fields envelope, but the adapter will never claim native reply support because LXMF does not provide it.

## Relationship to Reticulum

LXMF is built on Reticulum, a self-configuring mesh networking stack. The relationship between the LXMF adapter and Reticulum is strictly internal and **deferred** in tranche 1.

- The LXMF adapter will use Reticulum internally when real connectivity is active. This is an implementation detail, not a MEDRE concern. In tranche 1, Reticulum is not used at all.
- MEDRE core does not import or depend on Reticulum. No `import reticulum` appears outside the adapter package.
- Reticulum `Identity` and `Destination` classes are never exposed to the pipeline. They stay inside the adapter's internal client code.
- LXMF addresses (16-byte hashes) are plain strings as far as MEDRE is concerned. The adapter handles any necessary conversion between string addresses and Reticulum destination objects internally.
- No raw Reticulum adapter support is planned. Reticulum is a transport substrate for LXMF, not a first-class MEDRE adapter target.
- LXST (LXMF Streaming Transport) is explicitly out of scope. No streaming, file transfer, or bulk data transport is planned for the LXMF adapter.

## Dependency

```bash
pip install medre[lxmf]
```

This installs `lxmf` and `reticulum` packages. The core install (`pip install medre`) does not include them. All core tests pass without either package present. The adapter's own tests use `FakeLxmfAdapter` and do not require `lxmf` or `reticulum`.

- **Distribution names:** `lxmf` and `reticulum` on PyPI.
- **Optional.** The compat module sets `HAS_LXMF = False` when the packages are not installed. The adapter's `start()` raises `LxmfConnectionError` for non-fake connection types when the libraries are missing.
- **Tranche 1 does not require either package.** All tests use the fake adapter. Real connectivity is deferred. The adapter's `start()` raises `LxmfConnectionError` if `connection_type` is anything other than `"fake"`.

```python
# medre/adapters/lxmf/compat.py
HAS_LXMF: bool

try:
    import lxmf  # noqa: F401
    HAS_LXMF = True
except ImportError:
    HAS_LXMF = False
```

## Testing Approach

- **FakeLxmfAdapter.** No real Reticulum dependency, no network, no LXMF library. Generates deterministic sequential SHA-256 hex message IDs. Tracks all sent messages in `sent_messages`. `set_deliver_failure()` triggers errors for pipeline error handling tests.
- **Unit isolation.** `LxmfRenderer` and `LxmfCodec` are tested independently of the adapter.
- **Pipeline integration.** Tests combine `FakeLxmfAdapter` with `SQLiteStorage` to exercise the full decode/store/render/deliver path.
- **Boundary verification.** Tests assert that core imports don't leak into the adapter package, and that the adapter doesn't import routing, planning, or storage modules.
- **Fields round-trip.** Tests verify that known MEDRE envelope keys embedded by `LxmfFieldsHelper` survive a full render/decode cycle. This covers the defined envelope fields only, not arbitrary LXMF field data.
- **No real Reticulum or network required.** No test in the default suite requires a running Reticulum instance, an LXMF propagation node, any network connectivity, or the `lxmf`/`reticulum` packages installed. All tests use the fake adapter and hand-crafted packet dicts.

## Not Implemented in Tranche 1

The following are explicitly **not implemented**. Code may exist as
scaffolding or placeholder, but none of these are functional:

- **Real LXMF/Reticulum connectivity.** The adapter only operates in `connection_type="fake"` mode. No live Reticulum transport is established.
- **Resource/attachment transfer.** No `RNS.Resource` usage. `FIELD_FILE_ATTACHMENTS`, `FIELD_IMAGE`, and `FIELD_AUDIO` are not processed.
- **Identity file loading.** No `RNS.Identity()` recall or creation. The `identity_path` config field is a placeholder.
- **Announce/advertisement handling.** No processing of LXMF or Reticulum announce packets. No `destination.announce()` calls.
- **Path discovery.** No Reticulum path requests or routing table interaction.
- **Propagation node interaction.** No `lxmf.propagation` destination handling. No store-and-forward through LXMF's distributed propagation network.
- **Relation reconstruction from fields envelope.** The fields dict can carry relation metadata, but the codec does not extract and reconstruct `EventRelation` objects from it. This is deferred.
- **LXST.** LXMF Streaming Transport is out of scope. No streaming, no bulk transfer.

## Non-Goals (This Tranche)

These are explicitly out of scope for tranche 1:

- **Real Reticulum connectivity.** No connection to a running Reticulum network. The adapter only operates in fake mode. Real connectivity requires the optional `lxmf` and `reticulum` packages and is deferred.
- **Real LXMF delivery modes.** No DIRECT, OPPORTUNISTIC, PROPAGATED, or PAPER message delivery. These are LXMF-native delivery concepts that require a live Reticulum transport.
- **Resource/attachment transfer.** No file transfer, image handling, or binary resource support. LXMF supports resources for large payloads, but the adapter does not handle them.
- **Identity file loading.** No loading or management of Reticulum identity files. Identity management is deferred.
- **Announce/advertisement handling.** No processing of LXMF or Reticulum announce packets. Presence and discovery are deferred.
- **Path discovery.** No Reticulum path discovery or routing table interaction. The adapter does not participate in network path resolution.
- **Propagation node interaction.** No interaction with LXMF propagation nodes. Message propagation and store-and-forward through LXMF's distributed propagation network are deferred.
- **Ticket-based reply correlation.** LXMF supports ticket-based reply correlation in real mode. This is not implemented and not relevant to the fake adapter.
- **LXST.** LXMF Streaming Transport is explicitly out of scope. No streaming, no bulk transfer, no LXST protocol handling.
- **Reticulum as a standalone adapter.** No raw Reticulum adapter. Reticulum is an internal transport substrate for LXMF, not a first-class MEDRE adapter target.
- **Reactions, edits, deletes.** No native support for any of these relation types. The fields envelope can carry relation metadata, but the adapter does not interpret it as native LXMF semantics.
- **Store-and-forward.** No LXMF store-and-forward integration.
- **Matrix changes.** No modifications to the Matrix adapter, renderer, or configuration. The LXMF adapter is a separate TRANSPORT adapter that interacts with the pipeline, not with the Matrix adapter directly.
- **Webhooks, admin APIs.** No HTTP endpoints, webhook handlers, or administrative interfaces.
- **Renderer truncation enforcement.** The renderer notes LXMF payload size limits but does not truncate in tranche 1.

---

## Tranche 5: Delivery Semantics Verification and Session Boundary Hardening

> **Added:** 2026-05-26
> **Scope:** Delivery semantics hardening. Threading bridge added (session.py `call_soon_threadsafe` for Reticulum→asyncio, post-stop callback guard), honest delivery_note in adapter.py, plus test coverage and doc hardening. Source was changed for delivery/threading hardening.

### Delivery Semantics Verification

Tranche 5 adds tests verifying that the delivery semantics documented in this contract are actually enforced:

1. **Honest OUTBOUND return.** `send_text()` in fake mode returns `LxmfDeliveryState.OUTBOUND` — never `DELIVERED`, `SENT`, or `SENDING`. Tests assert the state is exactly `OUTBOUND`.

2. **Terminal-state untracking.** When a delivery callback transitions a tracked message to a terminal state (`DELIVERED`, `FAILED`, `REJECTED`, `CANCELLED`), the entry is removed from `_outbound_deliveries`. Tests verify each terminal state triggers cleanup.

3. **Failure counter accuracy.** `permanent_delivery_failures` increments exactly once per `FAILED` or `REJECTED` callback. Tests verify the count before and after each transition.

4. **Full transition chain.** Tests simulate the complete `OUTBOUND → SENDING → SENT → DELIVERED` progression via sequential delivery callbacks, verifying the entry stays tracked through intermediate states and is untracked on terminal state.

### Session Boundary Hardening

5. **Callback dispatch safety.** Tests verify both sync and async message callbacks work correctly through `inject_inbound()`. Async callbacks are scheduled on the running event loop; sync callbacks are called directly. Callback exceptions do not crash the session.

6. **Concurrent send isolation.** Concurrent `send_text()` calls produce distinct message IDs with no tracking corruption.

7. **Unknown message hash safety.** Delivery callbacks for untracked hashes are silently ignored — no crash, no tracking corruption.

### Source Changes in Tranche 5

Tranche 5 includes source changes to `session.py` (threading bridge via `call_soon_threadsafe`, post-stop callback guard clearing `_message_callback`/`_loop` on stop, early return in `_on_lxmf_delivery`) and `adapter.py` (honest delivery_note). The codec, renderer, and config modules are unchanged. Tests verify both pre-existing and new behaviour.

---

_This contract describes the implemented LXMF adapter tranche 1. If the implementation diverges from this document, the document should be updated to match the implementation's actual behavior._
