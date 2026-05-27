# Source Audit Evidence and Review Notes

This document consolidates evidence from pre-production source audits across
MEDRE's four transport adapters. These audits are historical snapshots of
point-in-time review, not normative authority. They document what was verified,
what remains unverified, and where gaps exist between MEDRE assumptions and
real transport behavior.

The normative adapter contracts live in [docs/spec/](../spec/). If this
document conflicts with the spec, the spec takes precedence.

## Audit Sources

| Audit                                          | Scope                                       | Date       |
| ---------------------------------------------- | ------------------------------------------- | ---------- |
| Meshtastic Source-of-Truth Audit               | mtjk SDK, MMRelay reference, packet shapes  | 2026-05-26 |
| LXMF Source-of-Truth Audit                     | LXMF/Reticulum SDKs, wire format            | 2026-05-08 |
| MeshCore Source-of-Truth Audit                 | MeshCore SDK, wire format, send semantics   | 2026-05-26 |
| Metadata Normalization Audit                   | Cross-transport metadata flow               | 2026-05-10 |
| Diagnostics Consistency Audit                  | Cross-adapter diagnostics, sessions         | 2026-05-09 |
| Dependency Reality Audit                       | Install friction, optional imports          | 2026-05-10 |

## Meshtastic

### What was verified

The Meshtastic audit verified MEDRE's adapter assumptions against the
`mtjk` (meshtastic-python fork) SDK and the MMRelay reference codebase.

**Packet shapes confirmed from source:**

| Field               | Source                                     | Status      |
| ------------------- | ------------------------------------------ | ----------- |
| `from` (int)        | protobuf `MeshPacket.from`                 | Confirmed   |
| `to` (int)          | protobuf `MeshPacket.to`                   | Confirmed   |
| `id` (int)          | protobuf `MeshPacket.id`                   | Confirmed   |
| `channel` (int)     | protobuf `MeshPacket.channel`              | Confirmed   |
| `decoded.portnum`   | `PortNum.Name()` string                    | Confirmed   |
| `decoded.text`      | Added by `_on_text_receive` from payload   | Confirmed   |
| `decoded.replyId`   | protobuf `Data.reply_id`, optional int     | Confirmed   |
| `decoded.emoji`     | protobuf `Data.emoji`, optional int        | Confirmed   |
| `fromId`/`toId`     | `_enrich_packet_identity` lookup           | Confirmed   |
| `rxTime`            | protobuf `MeshPacket.rx_time`              | Confirmed   |
| `rxSnr`/`rxRssi`    | Signal quality metrics                     | Confirmed   |
| `encrypted` (bool)  | Protobuf field on real packets             | Confirmed   |

**PortNum fallback map verified:** The `_NUMERIC_PORTNUM_FALLBACK` in
`packet_classifier.py` is protocol-correct. All values match the protobuf
`PortNum` enum exactly. When the `mtjk` SDK is installed, the classifier
uses the real enum. When absent, the fallback provides correct resolution
for all included entries.

**Send API verified:** `sendText()` returns a `MeshPacket` with populated
`id` field. The returned packet ID is usable as `native_message_id` for
delivery correlation.

### What remains unverified

| Area                                      | Risk   |
| ----------------------------------------- | ------ |
| Real TCP/serial/BLE connection lifecycle  | Medium |
| Packet shapes with real hardware captures | Medium |
| ACK tracking and correlation              | Low    |
| Telemetry/position payload shapes         | Low    |
| Node database / name cache behavior       | Medium |

### Gaps closed in hardening

Several gaps identified in the initial audit were closed in subsequent
hardening:

- `encrypted` field: classifier now extracts and assigns `drop` action
- `hopStart`/`hopLimit`: extracted for diagnostic use
- `rxTime`: extracted for startup backlog suppression
- `rxSnr`/`rxRssi`: extracted for radio-quality tracking
- `priority`: extracted for diagnostic use

All new field extractions are diagnostic-only. They do not change
classification decisions or canonical event structure.

### Classifier comparison with MMRelay

MEDRE uses a 4-action model (`relay`, `ignore`, `drop`, `deferred`) instead
of MMRelay's 3-action model (`RELAY`, `PLUGIN_ONLY`, `DROP`). Key differences:

- **`deferred` action**: replaces MMRelay's `PLUGIN_ONLY` for packets that
  may have future handlers
- **Encrypted packets**: MEDRE drops them (no decryption infrastructure)
- **Unknown portnums**: MEDRE defers them for future handlers
- **Explicit reason strings**: every classification includes a human-readable
  reason
- **Diagnostic counters**: per-action and per-reason counters exposed via
  adapter diagnostics

## LXMF

### What was verified

The LXMF audit verified assumptions against the LXMF Python library (v0.9.6)
and Reticulum network stack.

**Identity/addressing confirmed:**

- Identity hash: 16-byte truncated SHA-256 of public key, displayed as 32
  hex chars
- Destination hash: one-way derivation from identity hash and app aspect
- No numeric node ID concept; everything is hash-based
- Two LXMF aspects: `lxmf.delivery` and `lxmf.propagation`

**Message structure confirmed:**

- `LXMessage` wire format: `dest_hash` + `src_hash` + signature + msgpack payload
- Message ID: 32-byte SHA-256 hash (content-addressed)
- Separate `title` and `content` fields
- `fields` dict uses integer keys, not string keys
- `FIELD_TICKET` carries reply permission with expiry
- `FIELD_THREAD` groups conversation messages

**Delivery methods confirmed:**

| Method        | Characteristics                                       |
| ------------- | ----------------------------------------------------- |
| DIRECT        | Link-based, reliable, up to 319 bytes per packet      |
| OPPORTUNISTIC | Single packet, fire-and-forget, max 295 bytes          |
| PROPAGATED    | Store-and-forward via propagation node                 |
| PAPER         | Offline transfer via QR code or URI                    |

### What remains unverified

| Area                                  | Risk   |
| ------------------------------------- | ------ |
| Real Reticulum thread callbacks       | Medium |
| Delivery state progression to final   | Medium |
| Propagation node operation            | Low    |
| Multi-hop delivery                    | Low    |
| Message signing verification          | Low    |

### Session hardening findings

The LXMF session underwent specific hardening:

- Delivery state model maps all 9 LXMF states with explicit untracking tests
- Bounded outbound tracking (`_MAX_OUTBOUND_DELIVERIES = 1000`) with FIFO eviction
- Inbound normalization handles bytes, str, and missing attributes for all fields
- Thread-to-asyncio bridge uses `call_soon_threadsafe` (not `create_task`)
- Send returns `(native_id, OUTBOUND)` in fake mode, never claiming delivered

## MeshCore

### What was verified

The MeshCore audit verified assumptions against the MeshCore Python library
(v2.2.5) and firmware source.

**Identity/addressing confirmed:**

- Ed25519 keypair identity, 32-byte public key as hex string (64 chars)
- No numeric node ID; identity is always pubkey hex
- Contact list is dict keyed by pubkey hex
- No broadcast address concept; send to specific pubkey or use flood

**Packet shapes confirmed:**

- Direct messages: `CONTACT_MSG_RECV` with `pubkey_prefix` (truncated sender key)
- Channel messages: `CHANNEL_MSG_RECV` with `channel_idx`
- Both carry `sender_timestamp`, `txt_type`, `text`
- No native reply mechanism
- No native reaction mechanism
- No protobuf at any layer

**Send API confirmed:**

- `send_msg()` returns `Event` with `expected_ack` (4-byte hex) and
  `suggested_timeout`
- `send_msg_with_retry()` has built-in retry loop
- ACK events arrive separately via `EventType.ACK`
- Always-on E2EE (AES-128 + HMAC, no toggle)

### What remains unverified

| Area                                          | Risk   |
| --------------------------------------------- | ------ |
| Real callback packet shapes with hardware     | Medium |
| Connection lifecycle nuances                   | Medium |
| ACK correlation and timeout behavior           | Medium |
| Contact-based sender resolution                | Low    |
| Flood message handling                         | Low    |

### Gaps closed in hardening

- Connection lifecycle: session wires `create_tcp`, `create_serial`,
  `create_ble` factory calls
- Event subscription: session subscribes to CONTACT_MSG_RECV,
  CHANNEL_MSG_RECV, DISCONNECTED
- Outbound send with retry: session retries transient failures up to 3 times
- Reconnection: bounded exponential backoff (1s to 30s cap, max 10 attempts)
- Inbound callback normalization: extracts dict from SDK Event, normalizes
  non-dict payloads
- Sync callback handling: checks `asyncio.iscoroutine()` before awaiting

## Metadata Normalization

### Core finding

Transport-rich metadata stays in namespaced envelopes. Canonical events
remain transport-agnostic. The `core/` package never imports adapter packages.

### Metadata architecture

All event metadata lives in a structured `EventMetadata` with six namespaces:
`transport`, `routing`, `radio`, `telemetry`, `native`, `custom`. Each
adapter stores transport-specific details under `metadata.native.data[<transport>]`.

| Transport    | Native namespace                 |
| ------------ | -------------------------------- |
| Matrix       | `metadata.native.data["matrix"]` |
| Meshtastic   | `metadata.native.data["meshtastic"]` |
| MeshCore     | `metadata.native.data["meshcore"]` |
| LXMF         | `metadata.native.data["lxmf"]`   |

### What cannot be normalized

These are fundamentally different across transports and cannot be abstracted:

1. **Delivery confirmation**: sync HTTP (Matrix) vs async radio ACK vs
   store-and-forward (LXMF)
2. **Message ordering**: only Matrix has server-assigned ordering
3. **Persistence**: Matrix and LXMF persist; Meshtastic and MeshCore are
   ephemeral
4. **Reply rendering**: only Matrix and Meshtastic have native reply
5. **Native ID semantics**: globally unique vs session-scoped vs
   content-addressed vs collision-prone
6. **Payload limits**: ~100KB (Matrix) vs ~228B (Meshtastic) vs 184B
  (MeshCore) vs multi-KB (LXMF)

### Outbound metadata embedding

Only LXMF and Matrix support metadata embedding in outbound messages.
Meshtastic and MeshCore have payload limits that preclude structured
metadata. This means metadata round-tripping across transports is
best-effort, not contractual.

## Diagnostics Consistency

### Common diagnostic fields

All four adapters expose these fields:

- `connected` (bool)
- `reconnecting` (bool)
- `reconnect_attempts` (int, bounded to max 10)
- `transient_delivery_failures` (int counter)
- `permanent_delivery_failures` (int counter)
- `last_error` / `last_sync_error` (string or None)

### Safety guarantees

All four adapters guarantee that diagnostics contain:

- No secrets, access tokens, keys, or private device material
- No raw SDK objects, protobuf, or `LXMessage` instances
- All exceptions converted to `str()` before inclusion

### Session pattern consistency

All four sessions follow the same lifecycle: construction, `start()`,
`stop()`, `diagnostics()`. All own their callbacks and reconnect logic.
Parameters are nearly identical:

| Session           | Max attempts | Backoff cap | Jitter  |
| ----------------- | ------------ | ----------- | ------- |
| Matrix            | 10           | 60s         | +-25%   |
| Meshtastic        | 10           | 30s         | +-25%   |
| MeshCore          | 10           | 30s         | +-25%   |
| LXMF              | 10           | 30s         | +-25%   |

### Observational caveat

Diagnostics are snapshot observations, not authoritative state. A
`connected: true` diagnostic does not guarantee the next operation will
succeed. Delivery failure counters are cumulative since adapter start.
Use the delivery receipt system for authoritative delivery state.

## Dependency Reality

### Core dependency

- `msgspec==0.21.1` (exact pin): required for all MEDRE installations.
  Binary wheels available for standard platforms.

### Optional transport dependencies

| Distribution      | Import name   | Friction | Docker (TCP) |
| ----------------- | ------------- | -------- | ------------ |
| `mindroom-nio`    | `nio`         | Low      | Good         |
| `mindroom-nio[e2e]` | (same)      | High     | Moderate     |
| `mtjk`            | `meshtastic`  | Low      | Good         |
| `meshcore`        | `meshcore`    | Low      | Good         |
| `lxmf`            | `LXMF`        | Moderate | Moderate     |

Key observations:

- Distribution name and import name differ for `mtjk` (imports as
  `meshtastic`) and `mindroom-nio` (imports as `nio`)
- Two of five dependencies are forks (`mindroom-nio`, `mtjk`)
- E2EE has the highest install friction (Rust dependency via `vodozemac`)
- Reticulum uses a non-standard license (review for your use case)
- All transport dependencies are optional; core MEDRE installs only `msgspec`

### Versioning strategy

MEDRE uses minimum-version floor pins (`>=`) for transport dependencies.
The core dependency (`msgspec`) is exact-pinned. No lockfile is committed.
No upper-bound caps are used.

## Known Documentation Gaps

A gap analysis is maintained at `/tmp/medre-spec-impl-gaps.md`. Notable items:

1. **TOML key inconsistency** -- some runbook examples use `type = "fake"` for
   adapters and `source`/`target` keys for routes, but the actual config schema
   uses `adapter_kind` and `source_adapters`/`dest_adapters` (arrays).
2. **Truncation enforcement status** -- some runbooks claim truncation is not
   enforced, but `MeshtasticConfig.max_text_bytes = 227` exists and the
   renderer applies it.
3. **failure_kind casing** -- some docs use UPPERCASE (`"RENDERER_FAILURE"`)
   while others use lowercase snake_case (`renderer_failure`). The lowercase
   form is correct.
4. **Container evidence references** -- some runbooks still reference
   `docs/contracts/` paths instead of `docs/spec/`.
