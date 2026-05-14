# Adapter Baseline Consolidation: Consistency Audit

> Contract version: 1
> Last updated: 2026-05-08
> Track: 2 (Adapter Baseline Consolidation)

This document audits all four MEDRE adapters across twelve structural dimensions. For each dimension, every adapter's current behavior is tabulated, and inconsistencies are classified as either **intentional** (driven by protocol semantics) or **accidental** (likely copy-paste drift or uneven implementation progress).

The four adapters are:

- **Matrix** (`medre.adapters.matrix`) - a PRESENTATION adapter for Matrix chat rooms
- **Meshtastic** (`medre.adapters.meshtastic`) - a TRANSPORT adapter for Meshtastic radio
- **MeshCore** (`medre.adapters.meshcore`) - a TRANSPORT adapter for MeshCore radio
- **LXMF** (`medre.adapters.lxmf`) - a TRANSPORT adapter for LXMF/Reticulum messaging


## 1. Platform String Value

The `platform` class attribute on each adapter's `BaseAdapter` subclass.

| Adapter | `platform` value | Notes |
|---|---|---|
| Matrix | `"matrix"` | Real protocol; fake adapter reports same platform |
| Meshtastic | `"meshtastic"` | Real protocol; fake adapter reports same platform |
| MeshCore | `"meshcore"` | Real protocol; fake adapter reports same platform |
| LXMF | `"lxmf"` | Real protocol; fake adapter reports same platform |

**Verdict: Consistent.** All adapters use a lowercase string matching their protocol family name. Both real and fake adapters report the same `platform` value (e.g., `"matrix"`, `"meshtastic"`, `"meshcore"`, `"lxmf"`). The `platform` field answers "what protocol family does this adapter speak?", never "is this adapter fake?". Fake/test mode is conveyed by class name, module name, and `config.connection_type="fake"` where applicable — never by the `platform` field.


## 2. Role (TRANSPORT vs PRESENTATION)

| Adapter | `AdapterRole` | Rationale |
|---|---|---|
| Matrix | `PRESENTATION` | Chat platform, high-level, rich features |
| Meshtastic | `TRANSPORT` | Low-level radio transport |
| MeshCore | `TRANSPORT` | Low-level radio transport |
| LXMF | `TRANSPORT` | Low-level mesh messaging transport |

**Verdict: Consistent, intentional.** The split is correct. Matrix is a presentation layer (rooms, reactions, formatted content). The other three are constrained transports with limited payload sizes and no rich message semantics.


## 3. Config Shape

How configuration is structured and validated.

| Adapter | Config class | Type | Required fields | Validation |
|---|---|---|---|---|
| Matrix | `MatrixConfig` | `@dataclass(frozen=True)` | `adapter_id`, `homeserver`, `user_id`, `access_token` | `.validate()` checks URL scheme, user ID prefix, token non-empty |
| Meshtastic | `MeshtasticConfig` | `@dataclass(frozen=True)` | `adapter_id` | `.validate()` checks connection_type enum, host for TCP |
| MeshCore | `MeshCoreConfig` | `@dataclass(frozen=True)` | `adapter_id` | `.validate()` checks connection_type enum, host for TCP |
| LXMF | `LxmfConfig` | `@dataclass(frozen=True)` | `adapter_id` | `.validate()` checks connection_type is `"fake"`, delivery_method enum, stamp_cost >= 0 |

**Verdict: Mostly consistent.** All four use `@dataclass(frozen=True)` and expose a `.validate()` method returning `Self`. Matrix has the most validation logic because its homeserver URL and user ID have well-known structural constraints. LXMF restricts `connection_type` to `Literal["fake"]` in tranche 1, while Meshtastic and MeshCore accept `Literal["fake", "tcp", "serial", "ble"]` but only implement fake mode.

**Accidental drift: none significant.** The field names differ because the protocols differ (Matrix has `homeserver`, `user_id`; Meshtastic has `host`, `serial_port`; LXMF has `stamp_cost`, `display_name`). These are protocol-shaped, not drift.


## 4. Fake Adapter Behavior

Each adapter has a corresponding fake for testing.

| Adapter | Fake class | Config source | `deliver()` returns | Inbound path | Test helpers |
|---|---|---|---|---|---|
| Matrix | `FakeMatrixAdapter` | Bare strings (`adapter_id`, `channel`) | `AdapterDeliveryResult(native_message_id=f"$fake_{result.event_id}", ...)` | `simulate_inbound(CanonicalEvent)` | `make_event()`, `make_reply_event()`, `make_reaction_event()` |
| Meshtastic | `FakeMeshtasticAdapter` | `MeshtasticConfig` (full config) | `AdapterDeliveryResult(native_message_id=str(packet_id), ...)` via `FakeMeshtasticClient` | `simulate_inbound(packet_dict)` through real codec+classifier | `make_text_event()` |
| MeshCore | `FakeMeshCoreAdapter` | `MeshCoreConfig` (full config) | `AdapterDeliveryResult(native_message_id=str(packet_id), ...)` via `FakeMeshCoreClient` | `simulate_inbound(packet_dict)` through real codec+classifier | `make_text_event()` |
| LXMF | `FakeLxmfAdapter` | `LxmfConfig` (full config) | `AdapterDeliveryResult(native_message_id=sha256_hex, ...)` via `FakeLxmfClient` | `simulate_inbound(packet_dict)` through real codec+classifier | `make_text_event()` |

**Verdict: Intentional differences, with some drift.**

- Matrix's fake takes bare strings for config because Matrix has many required fields that don't map to test scenarios. The transport fakes take real config objects, which is the more rigorous approach.
- Meshtastic, MeshCore, and LXMF fakes all follow the same pattern: real codec + classifier + fake client with sequential/deterministic IDs. This is consistent and correct.
- Matrix's fake generates IDs as `"$fake_{event_id}"` (Matrix-style event IDs). Meshtastic and MeshCore use sequential integers. LXMF uses SHA-256 hex. All are intentionally shaped to match their protocol's native ID format.

**Accidental drift: none.** The differences are protocol-driven.


## 5. Renderer Selection

How the renderer decides whether it handles a given target adapter.

| Adapter | Selection strategies (in order) | `can_render` signature |
|---|---|---|
| Matrix | 1. `target_platform == "matrix"`, 2. `target_adapter.startswith("matrix")` | `(event, target_adapter, target_platform=None)` |
| Meshtastic | 1. `target_platform == "meshtastic"`, 2. `target_adapter.startswith("meshtastic")`, 3. `target_adapter in known_adapters` | `(event, target_adapter, target_platform=None)` |
| MeshCore | 1. `target_platform == "meshcore"`, 2. `target_adapter.startswith("meshcore")`, 3. `target_adapter in known_adapters` | `(event, target_adapter, target_platform=None)` |
| LXMF | 1. `target_platform == "lxmf"`, 2. `target_adapter.startswith("lxmf")`, 3. `target_adapter in known_adapters` | `(event, target_adapter, target_platform=None)` |

**Verdict: Intentional inconsistency.**

Matrix only has two strategies (platform match, prefix). The three transport renderers have three strategies, adding the `known_adapters` set for realistic IDs like `"local-radio"`.

This is intentional: Matrix adapters are expected to follow the `"matrix"` naming convention (or be registered by platform). Transport adapters are more likely to use arbitrary IDs in production, so the `known_adapters` fallback matters more.

If desired, a `known_adapters` parameter could be added to `MatrixRenderer.can_render()` for consistency, but it isn't needed yet.


## 6. Codec/Classifier Ownership

Whether each adapter owns its own codec and packet classifier.

| Adapter | Codec class | Classifier class | Codec inherits `AdapterCodec`? | Codec `encode()` implemented? |
|---|---|---|---|---|
| Matrix | `MatrixCodec` | N/A (no classifier needed) | Yes | Raises `NotImplementedError` (use renderer) |
| Meshtastic | `MeshtasticCodec` | `MeshtasticPacketClassifier` | No (standalone class) | N/A (decode only) |
| MeshCore | `MeshCoreCodec` | `MeshCorePacketClassifier` | No (standalone class) | N/A (decode only) |
| LXMF | `LxmfCodec` | `LxmfPacketClassifier` | No (standalone class) | N/A (decode only) |

**Verdict: Intentional differences with one accidental inconsistency.**

- Matrix's codec inherits from `AdapterCodec` because Matrix is a PRESENTATION adapter where the encode/decode symmetry makes sense architecturally (even though `encode()` is deprecated in favor of the renderer). The three transport codecs are standalone classes without the `AdapterCodec` base. This is protocol-shaped: transports only decode inbound packets.
- Matrix doesn't need a packet classifier because nio already dispatches by event type.
- **Accidental inconsistency:** Matrix's codec inherits `AdapterCodec` while the others don't. This is minor but could cause confusion. If the framework adds generic codec registry logic later, only Matrix would be picked up automatically. Not urgent, but worth noting.


## 7. Native Ref Behavior (Inbound)

How each adapter populates `source_native_ref` on inbound events.

| Adapter | Source of native ID | `native_channel_id` | `native_message_id` | Notes |
|---|---|---|---|---|
| Matrix | `event.event_id` from nio | `room_id` | Matrix event ID (e.g. `$xyz`) | Set in codec when `event_id` is non-empty |
| Meshtastic | `packet_id` from `classification` | Channel index as string | Packet ID as string | Set in codec when `pkt_id is not None` |
| MeshCore | `sender_timestamp` from `classification` | Channel index as string | Timestamp as string | Set in codec when `pkt_id is not None` |
| LXMF | `message_id` from `classification` | `None` (no channel concept) | Message ID hex string | Set in codec when `pkt_id is not None` |

**Verdict: Consistent, protocol-shaped.**

All adapters follow the same pattern: conditionally set `source_native_ref` when the native ID is available. The difference in ID sources (event_id, packet_id, sender_timestamp, message_id) is entirely driven by protocol semantics. LXMF having `native_channel_id=None` is correct because LXMF messages are addressed by destination hash, not by channel.


## 8. Fake Delivery Result Behavior

What the fake adapter returns from `deliver()`.

| Adapter | Returns from `deliver()` | `native_message_id` | `native_channel_id` |
|---|---|---|---|
| Matrix | `AdapterDeliveryResult` | `f"$fake_{result.event_id}"` | `result.target_channel or ""` |
| Meshtastic | `AdapterDeliveryResult` | `str(packet_id)` (sequential int) | `str(channel_index)` |
| MeshCore | `AdapterDeliveryResult` | `str(packet_id)` (sequential int) | `str(channel_index)` |
| LXMF | `AdapterDeliveryResult` | SHA-256 hex of counter | `None` |

**Verdict: Consistent.** All fakes return `AdapterDeliveryResult` with deterministic IDs. The ID format matches the protocol's native ID format (Matrix event IDs start with `$`, Meshtastic/MeshCore use integer packet IDs, LXMF uses hex hashes). This is intentional.


## 9. Relation Behavior

How each adapter handles reply/threading relations.

| Adapter | Inbound relation source | Outbound relation support | Notes |
|---|---|---|---|
| Matrix | `m.in_reply_to` from `content["m.relates_to"]` | Renderer builds `m.relates_to` for replies | Full native reply support |
| Meshtastic | `replyId` from `decoded` dict | Not rendered in tranche 1 | Codec detects `replyId`, no outbound reply rendering |
| MeshCore | None | None | No relation support in MeshCore protocol |
| LXMF | Fields envelope (deferred) | Fields envelope (deferred) | `LxmfCodec` explicitly defers relation reconstruction: "EventRelation objects are NOT created from envelope relations during decode" |

**Verdict: Protocol-shaped.** Each protocol has fundamentally different relation capabilities:

- Matrix has first-class threaded replies via `m.relates_to`.
- Meshtastic has a `replyId` field in decoded packets but no rich threading.
- MeshCore has no relation mechanism at all.
- LXMF could carry relations in its fields dict, but decoding them from the envelope is explicitly deferred.

The Meshtastic `replyId` detection in the codec without outbound rendering is a reasonable tranche 1 compromise. It preserves inbound information without committing to an outbound strategy.


## 10. Metadata Envelope Behavior

How each adapter embeds and extracts MEDRE metadata.

| Adapter | Envelope location | Envelope class | Inbound extraction | Outbound embedding |
|---|---|---|---|---|
| Matrix | `content["medre"]["envelope"]` | `MatrixMetadataEnvelope` (frozen dataclass) | In `_on_room_message` via `MatrixMetadataEnvelope.from_content(content)` | In renderer via `envelope.to_content()` |
| Meshtastic | None | None | None | None |
| MeshCore | None | None | None | None |
| LXMF | `fields[0xFD]["medre"]` | `LxmfFieldsHelper` (static methods) | In codec via `LxmfFieldsHelper.extract_envelope(fields)` | In renderer via `LxmfFieldsHelper.embed_envelope()` |

**Verdict: Intentional, protocol-shaped.**

Only Matrix and LXMF embed metadata envelopes. Meshtastic and MeshCore have no envelope mechanism, which makes sense for constrained radio transports where every byte matters and the metadata would bloat payloads.

The two envelope implementations differ in structure:

- Matrix uses a content-dict subtree (`medre.envelope`) which is natural for JSON.
- LXMF uses a numeric field key (`0xFD`) which is natural for the LXMF fields dict.

Both are correct for their protocols.


## 11. Optional Dependency Behavior

How each adapter handles missing dependencies.

| Adapter | Dependency | Import name | Guard module | Behavior when missing |
|---|---|---|---|---|
| Matrix | `mindroom-nio` | `nio` | `matrix/compat.py` (`HAS_NIO`) | Raises `MatrixConnectionError` on `start()` |
| Meshtastic | `mtjk` (Meshtastic fork) | `meshtastic` | `meshtastic/compat.py` (`HAS_MESHTASTIC`) | Raises `MeshtasticConnectionError` on `start()` if not fake |
| MeshCore | None (scaffolded) | N/A | N/A | Raises `MeshCoreConnectionError` on `start()` if not fake |
| LXMF | None (scaffolded) | N/A | N/A | Raises `LxmfConnectionError` on `start()` if not fake |

**Verdict: Intentional differences reflecting maturity.**

- Matrix and Meshtastic have real Python packages with compat guards. The codecs and renderers don't import the dependency directly, keeping them testable in isolation.
- MeshCore and LXMF don't have real dependencies yet. They're scaffolded: the adapter raises on non-fake connection types. When real SDKs are integrated, they'll need their own compat modules following the same pattern.

**Accidental note:** The Meshtastic compat module is more complex than Matrix's because it also provides a `get_portnum_table()` helper. This is fine but worth knowing about.


## 12. Production Connectivity Status

Whether any adapter can make real network calls.

| Adapter | Real connectivity | What works in production mode | Status |
|---|---|---|---|
| Matrix | Code exists, untested | `nio.AsyncClient` login + sync + room_send code is written | Needs real nio integration testing |
| Meshtastic | Code is scaffolded | Connection code deferred; `self._client = None` even for non-fake types | Needs real mtjk callback/send testing |
| MeshCore | Not implemented | Raises `MeshCoreConnectionError` for any non-fake type | Needs production SDK verification |
| LXMF | Not implemented | Raises `LxmfConnectionError` for any non-fake type | Needs Reticulum/LXMF integration planning |

**Verdict: All scaffolded in tranche 1.** This is by design.

Matrix is the closest to real operation. It has actual `nio` client code in `start()` and `deliver()`. But this code hasn't been exercised against a real homeserver yet. The other three are further away.

See contract 16 (`16-production-connectivity-readiness.md`) for a detailed readiness assessment per adapter.


## 13. Adapter Delivery Error Hierarchy

Base adapter delivery failure exceptions in `src/medre/adapters/base.py`:

```python
class AdapterSendError(Exception):
    """Transient delivery failure — retryable subject to RetryPolicy."""
    transient: bool = True

class AdapterPermanentError(Exception):
    """Permanent delivery failure — not retryable."""
    transient: bool = False
```

Each adapter's `SendError` inherits from the appropriate base so that `classify_failure` can use `error.transient` to decide `ADAPTER_TRANSIENT` vs `ADAPTER_PERMANENT`:

| Adapter | Transient base | Permanent base | Notes |
|---|---|---|---|
| Matrix | `MatrixSendError(AdapterSendError)` | `MatrixSendError(AdapterPermanentError)` (raised for auth/house-rule failures) | Single `MatrixSendError` class; transient flag set per-instance based on failure cause. `CancelledError` propagates (re-raised before broad catch). Broad catch is `Exception`, not `BaseException`. |
| Meshtastic | `MeshtasticSendError(AdapterSendError)` | `MeshtasticSendError(AdapterPermanentError)` (payload encoding failures) | Session retries transient up to 3×. Queue-full is transient. |
| MeshCore | `MeshCoreSendError(AdapterSendError)` | `MeshCoreSendError(AdapterPermanentError)` (invalid address) | Session retries transient up to 3×. |
| LXMF | `LxmfSendError(AdapterSendError)` | `LxmfSendError(AdapterPermanentError)` (invalid destination) | Session retries transient up to 3×. |

**Per-adapter error classification decisions:**

| Error condition | Matrix | Meshtastic | MeshCore | LXMF |
|---|---|---|---|---|
| not-connected / SDK not initialized | **Permanent** | **Permanent** | **Permanent** | **Permanent** |
| queue-full (local queue) | N/A (no local queue) | **Transient** | N/A (no local queue) | N/A (no local queue) |
| Connection / network failure | **Transient** | **Transient** | **Transient** | **Transient** |
| Timeout | **Transient** | **Transient** | **Transient** | **Transient** |
| Auth failure | **Permanent** | N/A | N/A | N/A |
| Invalid address / destination | **Permanent** | N/A | **Permanent** | **Permanent** |
| Payload encoding failure | **Permanent** | **Permanent** | **Permanent** | N/A |
| Config error | **Permanent** | **Permanent** | **Permanent** | **Permanent** |
| Rate limit (HTTP 429) | **Transient** | N/A | N/A | N/A |

All four adapters treat not-connected and SDK-not-initialized as **permanent** errors. An adapter that cannot reach its transport SDK should not be retried without operator intervention. Queue-full (where applicable) is transient because the queue may drain.

**Verdict: Consistent hierarchy.** All four adapters share the same transient/permanent split at the base level. The pipeline's `RetryExecutor.classify_failure` uses `getattr(error, 'transient', True)` to map to `DeliveryFailureKind.ADAPTER_TRANSIENT` (retryable) or `DeliveryFailureKind.ADAPTER_PERMANENT` (dead-letter). Exceptions without a `transient` attribute fall back to the standard Python type check (`TimeoutError`, `ConnectionError`, `OSError` → transient; everything else → permanent). The Matrix adapter catches `Exception` (not `BaseException`) with explicit `CancelledError` re-raise to preserve asyncio cancellation semantics. No adapter may swallow `CancelledError`.


## 14. Delivery Result Semantics

### 14.1 What "sent" Means

When `deliver()` returns an `AdapterDeliveryResult`, the adapter accepted the delivery — the handoff from pipeline to adapter succeeded. This is a **local acceptance** guarantee only. "Sent" means adapter handoff/acceptance succeeded, not final native-platform delivery. It does not mean the message reached any remote recipient, except for Matrix where the homeserver confirms storage.

### 14.2 native_message_id

Populated only when the adapter returns a platform-native message identifier from the transport SDK. This value must be platform-provided — adapters must never fabricate, synthesize, or locally-generate a `native_message_id`. Queue-based or fire-and-forget adapters may return `native_message_id=None` when no native send confirmation is available. In that case, `delivery_note` (see below) documents the local-acceptance status. Native refs are persisted only when `native_message_id` is not `None`.

### 14.3 delivery_note

`AdapterDeliveryResult` gains an optional `delivery_note: str | None = None` field. This is a human-readable context string explaining the delivery outcome when the native IDs alone are insufficient. Use cases:

- Queue-based adapters noting local-acceptance without platform ACK (e.g., `"no end-to-end ACK; status reflects local acceptance only"`).
- MeshCore alpha noting the lack of end-to-end confirmation.
- Any adapter providing diagnostic context about why `native_message_id` is `None`.
- Explaining that "sent" means adapter handoff succeeded, not final delivery — documenting the limited ACK semantics of constrained transports.

`delivery_note` is informational only. Consumers must not parse it for control-flow decisions.

### 14.4 Per-Adapter Result Summary

| Adapter | native_message_id | native_channel_id | delivery_note | What deliver() return means |
|---|---|---|---|---|
| Matrix | Matrix event ID (from `room_send` response, platform-provided) | Room ID string (platform-provided) | `None` (server confirmation is authoritative) | Homeserver accepted and stored the message. Adapter handoff succeeded. |
| Meshtastic | `None` (no native send confirmation from queue) | Channel index as string (platform-provided) | May describe local-acceptance if adapter returns a result | Message was enqueued to outbound queue; actual radio send is async. Adapter handoff succeeded, not final delivery. |
| MeshCore | MeshCore message reference (from SDK send, platform-provided) | Channel slot index (platform-provided, may be `None`) | `"no end-to-end ACK; status reflects local acceptance only"` | SDK accepted the message for local transmission. Adapter handoff succeeded, not final delivery. |
| LXMF | LXMF message hash (from message creation, platform-provided) | `None` (LXMF uses destination-hash addressing) | `None` (hash is authoritative) | LXMF message was created and dispatched to LXMRouter. Adapter handoff succeeded, not final delivery. |

The pipeline must not backfill `native_channel_id` or any other native ref field from route configuration. All native ref values reflect what the platform returned.

See Contract 22, Section 4.8 for the full per-adapter delivery table.


## Summary of Findings

### Intentional Inconsistencies (protocol-shaped)

1. **Role split:** Matrix is PRESENTATION, the others are TRANSPORT.
2. **Config fields:** Each protocol has different required fields.
3. **Fake adapter config input:** Matrix takes bare strings; transports take real config objects.
4. **Renderer selection strategies:** Transport renderers have `known_adapters`; Matrix doesn't.
5. **Native ref sources:** Different ID formats per protocol.
6. **Relation support:** Ranges from full native (Matrix) to none (MeshCore).
7. **Metadata envelopes:** Matrix and LXMF have them; Meshtastic and MeshCore don't.
8. **Dependency maturity:** Matrix and Meshtastic have real packages; MeshCore and LXMF are scaffolded.

### Accidental Inconsistencies (drift)

1. **Codec base class:** Matrix's codec inherits `AdapterCodec`; transport codecs don't. Minor, could cause issues with a future codec registry.
2. **None found beyond this.** The transport adapters (Meshtastic, MeshCore, LXMF) are structurally very similar because MeshCore and LXMF were clearly patterned after Meshtastic. This is appropriate and not a problem.

### Recommendations

1. **Keep the current structure.** The inconsistencies are overwhelmingly protocol-shaped. Forcing them into uniformity would add abstraction layers without benefit.
2. **Consider adding `known_adapters` to MatrixRenderer** if Matrix adapters ever need arbitrary IDs. Low priority.
3. **Decide on codec base class strategy** before building a codec registry. Either make all codecs inherit `AdapterCodec`, or remove the inheritance from Matrix's codec and treat the base class as optional.
4. **Document the "copy Meshtastic" pattern** for future transport adapters. The Meshtastic adapter is the template; MeshCore and LXMF followed it closely, and this consistency is valuable.
