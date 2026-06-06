# Native Relations Audit

Branch: `native-relations-closure`

Compact audit of how native references flow through MEDRE — inbound storage,
outbound recording, field ownership, transport-specific metadata, and per-transport
classification. Source and test changes on this branch implement and verify the
findings documented here (see `test_cross_adapter_relation_fallback.py`,
adapter renderers, and codec relation handling).

Reference repos inspected:

| Repo                      | Description                                |
| ------------------------- | ------------------------------------------ |
| `mtjk`                    | Meshtastic Python SDK fork/reference       |
| `mindroom-nio`            | Matrix nio fork/reference                  |
| `meshtastic-matrix-relay` | Matrix/Meshtastic relay reference          |
| `meshcore_py`             | MeshCore Python client reference           |
| `MeshCore`                | MeshCore protocol/reference implementation |
| `LXMF`                    | LXMF reference implementation              |

---

## 1. Ownership Model

| Component            | Responsibility                                                                                                   |
| -------------------- | ---------------------------------------------------------------------------------------------------------------- |
| **RelationResolver** | Resolves inbound `target_native_ref` → canonical `target_event_id` via `storage.resolve_native_ref()` lookup     |
| **RelationEnricher** | Resolves target canonical event → target-adapter `NativeRef` via `list_native_refs_for_event()` before rendering |
| **Renderer**         | Chooses native ref vs fallback text for outbound payload                                                         |
| **Adapter**          | Reports native facts (IDs, metadata) _after_ delivery only                                                       |

---

## 2. Core Types

### NativeRef (inline, lightweight)

| Field               | Type          | Populated?                   | Owner         |
| ------------------- | ------------- | ---------------------------- | ------------- |
| `adapter`           | `str`         | always                       | adapter fact  |
| `native_channel_id` | `str \| None` | transport-dependent          | adapter fact  |
| `native_message_id` | `str`         | always when present          | adapter fact  |
| `native_thread_id`  | `str \| None` | **RESERVED — always `None`** | not populated |

Used inline on `CanonicalEvent.source_native_ref` and `EventRelation.target_native_ref`.

### NativeMessageRef (persisted mapping)

| Field                | Type                      | Populated?                   | Owner                         |
| -------------------- | ------------------------- | ---------------------------- | ----------------------------- |
| `id`                 | `str`                     | always (framework-generated) | **authoritative**             |
| `event_id`           | `str`                     | always                       | **authoritative**             |
| `adapter`            | `str`                     | always                       | adapter fact                  |
| `native_channel_id`  | `str \| None`             | transport-dependent          | adapter fact                  |
| `native_message_id`  | `str`                     | always when present          | adapter fact                  |
| `native_thread_id`   | `str \| None`             | **RESERVED — always `None`** | not populated                 |
| `native_relation_id` | `str \| None`             | **RESERVED — always `None`** | not populated                 |
| `direction`          | `"inbound" \| "outbound"` | always                       | **authoritative** (framework) |
| `metadata`           | `dict`                    | always (may be empty)        | adapter fact, JSON-safe       |
| `created_at`         | `datetime`                | always                       | **authoritative** (framework) |

Persisted in `native_message_refs` SQLite table with `UNIQUE(adapter, native_channel_id, native_message_id)`.

### OutboundNativeRefRecord (delayed queue ref)

| Field                | Type          | Populated?                     | Owner                   |
| -------------------- | ------------- | ------------------------------ | ----------------------- |
| `event_id`           | `str`         | always                         | **authoritative**       |
| `adapter`            | `str`         | always                         | adapter fact            |
| `native_channel_id`  | `str \| None` | transport-dependent            | adapter fact            |
| `native_message_id`  | `str`         | required (validated non-empty) | adapter fact            |
| `native_thread_id`   | `str \| None` | **RESERVED — always `None`**   | not populated           |
| `native_relation_id` | `str \| None` | **RESERVED — always `None`**   | not populated           |
| `delivery_plan_id`   | `str`         | always                         | **authoritative**       |
| `metadata`           | `dict`        | always (may be empty)          | adapter fact, JSON-safe |

Used _only_ by queue-based adapters (Meshtastic) to report delayed native IDs.

---

## 3. Inbound Native Refs — Where Stored

| Storage Layer               | Columns                                                                                                                            | Source                                   |
| --------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------- |
| `canonical_events` table    | `source_native_adapter`, `source_native_channel_id`, `source_native_message_id`, `source_native_thread_id`                         | `CanonicalEvent.source_native_ref`       |
| `event_relations` table     | `target_native_adapter`, `target_native_channel_id`, `target_native_message_id`, `target_native_thread_id`                         | `EventRelation.target_native_ref`        |
| `native_message_refs` table | `adapter`, `native_channel_id`, `native_message_id`, `native_thread_id`, `native_relation_id`, `direction`, `metadata`, `event_id` | Pipeline `_persist_inbound_native_ref()` |

Codec sets `source_native_ref` on decode → pipeline persists `NativeMessageRef(direction="inbound")`.

Codec sets `target_native_ref` on `EventRelation` for replies/reactions → pipeline persists relation with split native columns.

---

## 4. Outbound Native Refs — Where Stored

| Path                                              | When                                                                               | Storage                                                                                                                                                                                                 |
| ------------------------------------------------- | ---------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Synchronous adapters** (Matrix, MeshCore, LXMF) | `AdapterDeliveryResult.delivery_status="sent"` AND `native_message_id is not None` | `TargetDeliveryService` stores `NativeMessageRef(direction="outbound")`                                                                                                                                 |
| **Queue-based adapters** (Meshtastic)             | Adapter returns `delivery_status="enqueued"`, `native_message_id=None`             | Pipeline records receipt as `status="queued"`. Queue drain later obtains real native ID via `OutboundNativeRefRecord` → `NativeMessageRef(direction="outbound")` + supplemental `status="sent"` receipt |

**Current implementation risk**: if the in-memory Meshtastic queue is accepted but does not drain before shutdown, MEDRE may never receive the delayed packet ID and therefore cannot persist the outbound native ref for that delivery attempt. Future durable queue recovery would mitigate this.

---

## 5. Authoritative vs Adapter Facts

### Authoritative (framework-owned)

- `CanonicalEvent.event_id` — THE identity
- `EventRelation.target_event_id` — once resolved, authoritative
- `NativeMessageRef.id` — mapping record identity
- `NativeMessageRef.direction` — `"inbound"` or `"outbound"`
- `NativeMessageRef.created_at` — framework timestamp
- `DeliveryReceipt.status` — pipeline-determined from adapter `delivery_status`
- `OutboundNativeRefRecord.delivery_plan_id` — plan correlation

### Adapter Facts (transport-originated)

- `NativeRef.adapter` — which adapter owns the namespace
- `NativeRef.native_channel_id` — adapter channel/room
- `NativeRef.native_message_id` — adapter message/event ID
- `AdapterDeliveryResult.delivery_status` — `"sent"` or `"enqueued"`
- `AdapterDeliveryResult.metadata` — namespaced transport data
- `NativeMessageRef.metadata` — namespaced transport data

### Reserved (schema exists, always `None`)

- `native_thread_id` — on `NativeRef`, `NativeMessageRef`, `OutboundNativeRefRecord`
- `native_relation_id` — on `NativeMessageRef`, `OutboundNativeRefRecord`

---

## 6. Transport-Specific Metadata Keys

### Metadata in `EventRelation.metadata`

| Key                           | Set by                                         | Purpose                                  |
| ----------------------------- | ---------------------------------------------- | ---------------------------------------- |
| `meshtastic_reply_id`         | Matrix codec (MMRelay path), Meshtastic codec  | Wire-format reply ID for cross-transport |
| `meshtastic_emoji`            | Matrix codec (MMRelay emote), Meshtastic codec | Emoji/reaction flag (value: `1`)         |
| `meshtastic_reaction_key`     | Matrix codec (MMRelay)                         | Structured MMRelay reaction key          |
| `original_text`               | RelationEnricher Phase 2                       | Target event text for inline fallback    |
| `original_sender_displayname` | RelationEnricher Phase 2                       | Target sender display name               |
| `original_sender`             | RelationEnricher Phase 2                       | Target sender identity                   |

### Metadata in `AdapterDeliveryResult.metadata`

Transport-specific data MUST live under `metadata[<transport>]`.
Convention: `metadata.matrix`, `metadata.meshtastic`, `metadata.meshcore`, `metadata.lxmf`.

### Metadata in `CanonicalEvent.metadata.native.data` (per-adapter)

| Transport  | Keys                                                                                                                                                                       |
| ---------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Matrix     | `room_id`, `event_id`, `sender`, plus MMRelay keys (`meshtastic_replyId`, `meshtastic_text`, `meshtastic_emoji`)                                                           |
| Meshtastic | `packet_id`, `from_id`, `channel`, `portnum`, `to_id`, `longname`, `shortname`, `reply_id`, `emoji`, `emoji_flag`, `packet` snapshot, `decoded` snapshot, `classification` |
| MeshCore   | `meshcore.packet_id`, `meshcore.sender_id`, `meshcore.channel`, etc.                                                                                                       |
| LXMF       | `source_hash`, `destination_hash`, `message_id`, `timestamp`, `title`, `delivery_method`, `has_fields`                                                                     |

### Metadata in `NativeMessageRef.metadata`

- **Inbound**: copy of `event.metadata.native.data` — preserves the per-adapter shape from the inbound codec, which may be flat (e.g. Meshtastic `packet_id`, `from_id`, `channel` at top level of the adapter data dict). This is NOT namespaced as `metadata[<transport>]`; it mirrors whatever the codec produced in `native.data`.
- **Outbound (synchronous adapters)**: transport-namespaced from `AdapterDeliveryResult.metadata` — follows the `metadata[<transport>]` convention.
- **Outbound (Meshtastic)**: enriched merge under `meshtastic` namespace — `text`, `meshnet_name`, `channel_name`, `reply_id`, `emoji`, plus defensively normalised legacy/non-namespaced delivery keys.

---

## 7. Delayed Native Refs — Queued Transports

Applies to: **Meshtastic only** (queue-based adapter).

```text
1. Adapter.deliver() → enqueues payload, returns delivery_status="enqueued", native_message_id=None
2. TargetDeliveryService records receipt with status="queued"
3. PipelineRunner._process_queue() background task drains queue
4. On successful send, adapter obtains real native_message_id from platform
5. Adapter builds OutboundNativeRefRecord → calls ctx.record_outbound_native_ref
6. PipelineRunner stores NativeMessageRef(direction="outbound")
7. PipelineRunner appends supplemental status="sent" receipt
```

**Current implementation risk**: if the in-memory queue is accepted but does not drain before process exit, the outbound native mapping for those items cannot be persisted. No flush/retry on shutdown.

---

## 8. When Native IDs Are Unavailable

| Scenario                                            | What happens                                                                                                            |
| --------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| Inbound: codec cannot extract native ID             | `source_native_ref=None`, pipeline skips `_persist_inbound_native_ref()`                                                |
| Inbound: already-seen native ref (loop)             | `handle_ingress()` suppresses entire event (dedup)                                                                      |
| Relation: `target_native_ref` present, lookup fails | Relation preserved with native ref for future retry; `target_event_id` remains `None`                                   |
| RelationEnricher: no target-adapter native ref      | Relation keeps whatever `target_native_ref` it had (possibly `None`); renderer falls back                               |
| RelationEnricher: native ref wrong channel          | Strips incompatible `target_native_ref` (sets to `None`)                                                                |
| Renderer: no target native ref for outbound         | Falls back: `fallback_text` → abbreviated `target_event_id` → `target_native_ref.native_message_id` → "unknown message" |

---

## 9. Native-Ref Source Classification

| Classification                             | Meaning                                                                                                                           |
| ------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------- |
| **Immediate native ref**                   | Native ID available at decode/delivery time; persisted synchronously                                                              |
| **Delayed native ref**                     | Native ID not available at enqueue; recorded after queue drain                                                                    |
| **Unavailable by transport design**        | Transport has no durable message ID for this path                                                                                 |
| **Unavailable due to failure/uncertainty** | Decode or delivery failed; no native ID obtainable                                                                                |
| **Best-effort outbound native ref**        | Local evidence/correlation when expected_ack is returned; not durable protocol identity; relation rendering remains fallback-only |
| **Fallback-only**                          | No native ref; renderer uses `fallback_text` or degraded display                                                                  |

---

## 10. Per-Transport Sections

### 10.1 Matrix

**Native ID**: `event_id` (string, globally unique, assigned by homeserver, permanent).

| Aspect                    | Detail                                                                                                                                          |
| ------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| Inbound source ref        | `NativeRef(adapter, native_channel_id=room_id, native_message_id=event_id)`                                                                     |
| Inbound reply relation    | `target_native_ref` with `native_message_id=reply_event_id` from `m.relates_to.m.in_reply_to.event_id`                                          |
| Inbound reaction relation | `target_native_ref` with `native_message_id=target_mx_id` from `ReactionEvent.reacts_to`                                                        |
| MMRelay emote reaction    | `target_native_ref=None`; relies on metadata keys `meshtastic_reply_id`, `meshtastic_emoji`, `meshtastic_reaction_key`                          |
| Outbound native ref       | Immediate; `AdapterDeliveryResult.native_message_id` from `RoomSendResponse.event_id`                                                           |
| `txn_id` (transaction_id) | Idempotency metadata only — used in PUT path for dedup, never stored as native ID. Visible only to sending device in `unsigned.transaction_id`. |
| Thread ID                 | **RESERVED** — Matrix `m.thread` relations not yet decoded into `native_thread_id`                                                              |

**Classification**: inbound source = **immediate native ref**. Inbound relations = **immediate native ref**. Outbound = **immediate native ref**. MMRelay emote cross-transport = **fallback-only** (no Matrix native target).

**Reference repo findings**:

- nio: `Event.event_id` from top-level JSON, always present. `transaction_id` from `unsigned.transaction_id`, optional, ephemeral.
- MMRelay: uses `event.event_id` for bidirectional mapping in SQLite `message_map`. Sends with stable `txn_id` for idempotent retry.
- Matrix relation wire format: `content.m.relates_to.m.in_reply_to.event_id` (replies), `content.m.relates_to.rel_type=m.annotation` + `event_id` + `key` (reactions).

---

### 10.2 Meshtastic

**Native ID**: `MeshPacket.id` (uint32, hybrid 10-bit sequential + 22-bit random, assigned before queue).

| Aspect                        | Detail                                                                                                      |
| ----------------------------- | ----------------------------------------------------------------------------------------------------------- |
| Inbound source ref            | `NativeRef(adapter, native_channel_id=str(channel_index), native_message_id=str(packet_id))`                |
| Inbound reply relation        | `target_native_ref` with `native_message_id=str(reply_id)` from `Data.reply_id` (uint32)                    |
| Inbound reaction relation     | Same as reply; `Data.emoji == 1` flags reaction                                                             |
| Outbound native ref (sync)    | N/A — Meshtastic is queue-based                                                                             |
| Outbound native ref (delayed) | `OutboundNativeRefRecord` after queue drain; `native_message_id` from `MeshPacket.id` obtained at send time |
| Thread ID                     | **RESERVED** — Meshtastic has no threading model                                                            |
| `Data.reply_id`               | Single-level reply reference to previous `MeshPacket.id`. No chaining.                                      |

**Classification**: inbound source = **immediate native ref**. Inbound relations = **immediate native ref**. Outbound = **delayed native ref** (queue-based).

**Delayed ref lifecycle**:

1. `deliver()` returns `delivery_status="enqueued"`, `native_message_id=None`
2. Queue drain → `_sendPacket()` → `_generatePacketId()` → real uint32 ID
3. `_record_delayed_outbound_ref()` builds `OutboundNativeRefRecord`
4. Stored as `NativeMessageRef(direction="outbound")`

**Reference repo findings**:

- meshtastic-python: ID assigned in `sendData()` before `_sendToRadio()` queue. TX queue is `OrderedDict` drained FIFO. `QueueStatus.mesh_packet_id` confirms ack.
- MMRelay: two-phase pattern — `mapping_info` dict attached to `QueuedMessage`, persisted to `message_map` only after send completes. `reply_id` targets known previous ID; sender's own ID unknown until dequeue.
- Protobuf: `Data.reply_id` (uint32), `Data.emoji` (uint32). Ephemeral fields: `rx_time`, `rx_snr`, `rx_rssi`, `priority`.

---

### 10.3 MeshCore

**Native ID**: No protocol-level durable message identifier.

| Aspect                    | Detail                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| ------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Inbound source ref        | `NativeRef(adapter, native_channel_id=str(channel), native_message_id=str(packet_id))` — packet_id is `sender_timestamp` or derived, NOT a protocol-guaranteed ID                                                                                                                                                                                                                                                                                     |
| Inbound reply relation    | **None** — codec comment: "No reply relation support in MeshCore"                                                                                                                                                                                                                                                                                                                                                                                     |
| Inbound reaction relation | **None**                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| Outbound native ref       | `AdapterDeliveryResult.native_message_id` from send result; may be `None`                                                                                                                                                                                                                                                                                                                                                                             |
| `expected_ack`            | MeshCore has no durable protocol-level message ID. MEDRE currently records `expected_ack` as a best-effort outbound `native_message_id` when the SDK returns it. This is useful for local send correlation/evidence but is NOT a durable MeshCore message identity and must NOT be relied on for long-lived cross-transport relation resolution. Relation rendering to MeshCore remains fallback-only unless a future durable native ID is available. |
| Channel ID                | `channel_idx` (0-255), durable per channel config                                                                                                                                                                                                                                                                                                                                                                                                     |
| Contact identity          | 32-byte Ed25519 public key, durable                                                                                                                                                                                                                                                                                                                                                                                                                   |
| `tx_hash` (Python client) | Client-side derivation `SHA256(timestamp \|\| text)[0:4]` — NOT protocol-level, collision-prone                                                                                                                                                                                                                                                                                                                                                       |

**Classification**:

| Path                                | Classification                                                          |
| ----------------------------------- | ----------------------------------------------------------------------- |
| Contact message received            | **Unavailable by transport design** — no message ID in protocol         |
| Channel message received            | **Unavailable by transport design** — no message ID, no sender identity |
| Contact message sent (expected_ack) | **Best-effort outbound native ref** — local evidence/correlation only   |
| Channel message sent                | **Unavailable by transport design** — no ACK expected at all            |
| Channel definition                  | Immediate native ref (durable `channel_idx`)                            |
| Contact/peer identity               | Immediate native ref (durable public key)                               |

**Reference repo findings**:

- MeshCore C++: `expected_ack` computed in `BaseChatMesh::composeMsgPacket()`, stored in `AckTableEntry` circular buffer (8 slots RAM only).
- Python client: `MSG_SENT` event carries `expected_ack` (4 raw bytes). `CONTACT_MSG_RECV` carries `pubkey_prefix` + `sender_timestamp` + `text` (no message hash). `CHANNEL_MSG_RECV` carries `channel_idx` + `sender_timestamp` + `text`.
- No native threading, no native relation fields, no message dedup.

---

### 10.4 LXMF

**Native ID**: `LXMessage.hash` / `LXMessage.message_id` — 32-byte SHA-256 of (destination_hash \|\| source_hash \|\| msgpack(timestamp, title, content, fields)). Never transmitted; always computable from message content.

| Aspect                          | Detail                                                                                                                                                                                                                                                                              |
| ------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Inbound source ref              | `NativeRef(adapter, native_channel_id=None, native_message_id=str(message_id))` — NO channel                                                                                                                                                                                        |
| Inbound reply relation          | **None** — MEDRE does not decode LXMF native relations                                                                                                                                                                                                                              |
| Inbound reaction relation       | **None**                                                                                                                                                                                                                                                                            |
| Outbound native ref             | Immediate; `AdapterDeliveryResult.native_message_id` from `LXMessage.hash`                                                                                                                                                                                                          |
| `native_channel_id` on outbound | `str(destination_hash)` if available, else `None`                                                                                                                                                                                                                                   |
| MEDRE fields envelope           | Embedded in `LXMessage.fields` under key `0xFD` — carries `target_native_ref` (adapter, native_channel_id, native_message_id) for cross-transport relation rendering                                                                                                                |
| `delivery_state`                | Stored as `metadata["lxmf"]["delivery_state"]`. The adapter computes this from `LXMessage.state` (integer constants: GENERATING→OUTBOUND→SENDING→SENT→DELIVERED or FAILED/REJECTED/CANCELLED) returned by `send_text` and records the `.value` under the `lxmf` metadata namespace. |
| `transient_id`                  | Used only by propagation nodes for encrypted storage. Not the same as `message_id`.                                                                                                                                                                                                 |
| `FIELD_THREAD` (0x08)           | Defined in LXMF.py but **unspecified and unused** — no documentation, no enforcement, no known client implements it                                                                                                                                                                 |

**MEDRE current behavior**: LXMF is treated as **unsupported/fallback/envelope-only** for native relations. MEDRE embeds outbound relation data in the `0xFD` fields envelope for round-trip correlation, but does NOT read or write LXMF's native `FIELD_THREAD`. Do not imply MEDRE supports LXMF native relations.

**Classification**:

| Path                        | Classification                                                           |
| --------------------------- | ------------------------------------------------------------------------ |
| Inbound source ref          | **Immediate native ref** (`message_id` is deterministic and stable)      |
| Inbound relation            | **Fallback-only** — MEDRE does not decode LXMF native relations          |
| Outbound source ref         | **Immediate native ref** (`hash` computed on send)                       |
| Outbound relation rendering | **Fallback-only** — uses `0xFD` envelope, not LXMF native `FIELD_THREAD` |
| `FIELD_THREAD` (0x08)       | **Unavailable by transport design** — defined but unspecified in LXMF    |

**Reference repo findings**:

- LXMF: `message_id` = `SHA-256(destination_hash + source_hash + msgpack(payload))`, never transmitted, always computable. `transient_id` for propagation node storage only.
- `FIELD_THREAD = 0x08` exists in field constants but has zero documentation, zero implementation. Roadmap lists threading as planned.
- `FIELD_EMBEDDED_LXMS = 0x01` allows embedding full LXMF messages within messages (could support quoting).
- State machine: `LXMessage.state` uses integer constants (0x00–0xFF), not a namespaced metadata dict.
- `LXMRouter.delivery_packet` and `lxmf_delivery` handle inbound delivery; `fail_message()` sets `state = FAILED`.

---

## 11. Uncertainties

1. **MeshCore outbound `native_message_id`**: The MeshCore adapter records `expected_ack` as a best-effort outbound `native_message_id` when available from the SDK send result. This is useful for local send correlation/evidence but is volatile/local ACK correlation only — it is NOT a durable MeshCore protocol identity and must NOT be relied on for long-lived cross-transport relation resolution.

2. **MeshCore `pkt_id` in codec**: The MeshCore codec extracts `pkt_id` from incoming events and stores it as `native_message_id`. This appears to be `sender_timestamp` (4-byte Unix timestamp). Collision risk is moderate for high-volume channels.

3. **LXMF `0xFD` envelope round-trip fidelity**: The fields envelope carries `target_native_ref` outbound, but there is no evidence of MEDRE reading it back on inbound decode. If a receiving MEDRE instance needs to resolve cross-transport relations from the envelope, additional decode logic may be needed.

4. **MMRelay cross-transport metadata (`meshtastic_reply_id`, etc.)**: These keys are set by the Matrix codec when detecting MMRelay-formatted emotes. They live in `EventRelation.metadata` and are used by the Meshtastic renderer as a fallback. This path is specific to MMRelay-bridged Matrix rooms and will not work for native Matrix reactions.
