# MeshCore Transport Profile

## Purpose and Role

The MeshCore adapter is a **transport adapter** (`AdapterRole.TRANSPORT`) that connects to a MeshCore node via TCP, serial, BLE, or a test fake. It bridges inbound event payloads into the MEDRE canonical event stream and delivers outbound rendered payloads directly via the session for local acceptance.

The adapter delegates SDK client lifecycle to `MeshCoreSession`. The session owns the `MeshCore` SDK instance; the adapter owns semantic conversion (classification, codec decode, event publishing).

**Platform identifier:** `meshcore`

**Maturity note:** MeshCore integration is at an **alpha** stage. Delivery status reflects local acceptance only; there is no end-to-end ACK mechanism.

---

## Configuration Fields

| Field                   | Type                                   | Default      | Description                                                   |
| ----------------------- | -------------------------------------- | ------------ | ------------------------------------------------------------- |
| `adapter_id`            | `str`                                  | _(required)_ | Unique adapter instance identifier                            |
| `connection_type`       | `Literal["fake","tcp","serial","ble"]` | `"fake"`     | Connection mode                                               |
| `host`                  | `str \| None`                          | `None`       | Hostname/IP for TCP (required when `connection_type="tcp"`)   |
| `port`                  | `int \| None`                          | `None`       | Port for TCP (default 4403)                                   |
| `serial_port`           | `str \| None`                          | `None`       | Serial device path (required when `connection_type="serial"`) |
| `serial_baudrate`       | `int`                                  | `115200`     | Baud rate for serial                                          |
| `ble_address`           | `str \| None`                          | `None`       | BLE MAC address (required when `connection_type="ble"`)       |
| `meshnet_name`          | `str`                                  | `""`         | Human-readable meshnet name                                   |
| `default_channel`       | `int`                                  | `0`          | Default channel index for outbound                            |
| `channel_mapping`       | `dict[int, str]`                       | `{}`         | Channel index → display name map                              |
| `message_delay_seconds` | `float`                                | `0.5`        | Minimum delay between outbound messages                       |
| `sync_timeout_ms`       | `int`                                  | `30000`      | Sync operation timeout                                        |
| `identity`              | `str \| None`                          | `None`       | Optional node identity string                                 |
| `pubkey`                | `str \| None`                          | `None`       | Optional public key (hex string)                              |
| `node_config`           | `dict[str, object]`                    | `{}`         | Opaque node settings; must not contain secret keys            |
| `max_text_bytes`        | `int`                                  | `512`        | UTF-8 byte budget for rendered radio text                     |

---

## Capabilities

Machine-readable capability declaration: [`meshcore-capabilities.json`](meshcore-capabilities.json)

> Capability levels map to the CapabilityLevel enum (adapter-runtime.md §6.2): `"unsupported"` = `FALSE`.

| Capability        | Value                                    |
| ----------------- | ---------------------------------------- |
| text              | `True`                                   |
| replies           | `"unsupported"`                          |
| reactions         | `"unsupported"`                          |
| edits             | `"unsupported"`                          |
| deletes           | `"unsupported"`                          |
| attachments       | `False`                                  |
| metadata_fields   | `False`                                  |
| delivery_receipts | `False`                                  |
| store_and_forward | `False`                                  |
| direct_messages   | `False` (outbound; inbound PRIV relayed) |
| channels          | `True`                                   |
| async_delivery    | `True`                                   |
| mesh_routing      | `True`                                   |
| max_text_bytes    | Configurable (default 512)               |

`max_text_bytes` is the adapter's advertised end-to-end text budget, not a
single MeshCore packet payload cap. MeshCore per-packet payload constraints are
lower (approximately 184 bytes depending on SDK framing). MEDRE does not
implement fragmentation or reassembly — outbound text exceeding the budget is
truncated by the renderer before delivery.

---

## Supported Inbound Event Kinds

The packet classifier (`MeshCorePacketClassifier`) applies a structured policy:

| Condition                             | Action       | Category         | Reason                  |
| ------------------------------------- | ------------ | ---------------- | ----------------------- |
| `code` field present                  | **ignore**   | `ack`            | `"ack_packet"`          |
| Non-empty text + `type="PRIV"`        | **relay**    | `direct_message` | `"direct_text_packet"`  |
| Non-empty text + `type="CHAN"`        | **relay**    | `text`           | `"channel_text_packet"` |
| Non-empty text + unknown/missing type | **relay**    | `text`           | `"channel_text_packet"` |
| Empty/whitespace text                 | **ignore**   | (preserved)      | `"empty_text_packet"`   |
| Unrecognised type (not PRIV/CHAN)     | **deferred** | `unknown`        | `"unknown_packet"`      |
| No text, no code                      | **drop**     | `malformed`      | `"malformed_packet"`    |

Relayed packets are decoded by `MeshCoreCodec` into:

- **`MESSAGE_CREATED`** — all text-shaped packets (channel and direct message).

---

## Supported Outbound Event Kinds

The MeshCore renderer (`MeshCoreRenderer`) produces:

- **Plain text** — body text with UTF-8 byte-budget truncation (no prefix support in current release scope).
- **Channel messages** — `channel_index` determines target channel; sends via `session.send_text(channel_index=…)`.
- **Direct messages** — `contact_id` (pubkey prefix) determines recipient; sends via `session.send_text(contact_id=…)`.

No reply or reaction rendering — capabilities declare both as `"unsupported"`.

---

## Native Reference Format

- **Inbound native ref:** `NativeRef(adapter=<id>, native_channel_id=<str(channel_idx)>, native_message_id=<str(sender_timestamp)>)`
  - `packet_id` is the `sender_timestamp` (4-byte LE Unix timestamp).
  - `sender_id` is the `pubkey_prefix` (6-byte hex prefix of sender's public key).
- **Outbound native ref:** `native_message_id` extracted from SDK send result when available; `delivery_status="local_accepted"`.

---

## Delivery Semantics

**Local acceptance:** `deliver()` delegates to `session.send_text()`. The session sends via the SDK with bounded retry (3 attempts, linear backoff 0.1 s × attempt). Success returns a `native_message_id` (if the SDK provides one) with `delivery_note="MeshCore alpha — no end-to-end ACK"`.

**No end-to-end confirmation.** The current MeshCore SDK does not provide delivery ACKs. The adapter honestly reports `delivery_status="local_accepted"`. Consumers MUST be tolerant of delivery uncertainty.

**Fake mode:** Returns `None` (no real delivery).

---

## Session Lifecycle

1. **Disconnected** — Initial state; `_meshcore=None`.
2. **Connecting** — `session.start()` calls `_connect_real()` which uses SDK factory methods (`MeshCore.create_tcp`, `MeshCore.create_serial`, `MeshCore.create_ble`). Subscribes to `CONTACT_MSG_RECV`, `CHANNEL_MSG_RECV`, and `DISCONNECTED` event types.
3. **Connected** — Client created and subscribed; `_diag.connected=True`. Inbound events flow via `_on_sdk_event` → `_message_callback`.
4. **Reconnecting** — SDK `DISCONNECTED` event triggers bounded exponential backoff (1 s → 2 s → 4 s → … capped at 30 s, ±25 % jitter, max 10 attempts). On success, re-subscribes.
5. **Stopped** — `stop()` sets `_stop_requested`, unsubscribes, disconnects SDK client, nulls references. Idempotent.

**Callback bridging:** SDK callbacks are async; the session invokes the adapter callback directly and awaits short callbacks inline. Long-running callbacks are scheduled as fire-and-forget tasks.

---

## Diagnostics Keys

`adapter.diagnostics()` returns (no secrets, no raw SDK objects):

| Key                                     | Type          | Description                    |
| --------------------------------------- | ------------- | ------------------------------ |
| `adapter_id`                            | `str`         | Adapter identifier             |
| `started`                               | `bool`        | Adapter started flag           |
| `mode`                                  | `str`         | Config connection type         |
| `classifier_packets_seen`               | `int`         | Total classified               |
| `classifier_packets_relayed`            | `int`         | Relay action count             |
| `classifier_packets_ignored`            | `int`         | Ignore action count            |
| `classifier_packets_dropped`            | `int`         | Drop action count              |
| `classifier_packets_deferred`           | `int`         | Deferred action count          |
| `classifier_packets_ack_ignored`        | `int`         | ACK sub-counter                |
| `classifier_packets_empty_text_ignored` | `int`         | Empty text sub-counter         |
| `classifier_packets_unknown_deferred`   | `int`         | Unknown packet sub-counter     |
| `classifier_packets_dm_relayed`         | `int`         | DM relay sub-counter           |
| `classifier_packets_malformed`          | `int`         | Malformed sub-counter          |
| `inbound_published`                     | `int`         | Events published inbound       |
| `session.connected`                     | `bool`        | Session connected              |
| `session.reconnecting`                  | `bool`        | Reconnect in progress          |
| `session.reconnect_attempts`            | `int`         | Consecutive reconnect attempts |
| `session.last_error`                    | `str \| None` | Last error description         |
| `session.transient_delivery_failures`   | `int`         | Transient send errors          |
| `session.permanent_delivery_failures`   | `int`         | Permanent send errors          |

---

## Relation Degradation Behavior

MeshCore is a transport adapter with no native relation support. All relation types are unsupported.

| Relation type | Capability level | Strategy | Rendering path                                                            |
| ------------- | ---------------- | -------- | ------------------------------------------------------------------------- |
| Replies       | `"unsupported"`  | `skip`   | No delivery. Reply-carrying events targeting this adapter are suppressed. |
| Reactions     | `"unsupported"`  | `skip`   | No delivery. Reaction events targeting this adapter are suppressed.       |
| Edits         | `"unsupported"`  | `skip`   | No delivery. Edit events targeting this adapter are suppressed.           |
| Deletes       | `"unsupported"`  | `skip`   | No delivery. Delete events targeting this adapter are suppressed.         |
| Threads       | _deferred_       | —        | Reserved. MeshCore has no thread concept.                                 |

MeshCore does not currently declare the `"fallback"` capability level for any relation type in its capability JSON. All relations are unsupported. Events carrying relation context are skipped at the planning stage. Because the capability profile does not advertise fallback, the live planner will not normally select `fallback_text` for this adapter. Only `message.created` and `message.text` kinds are delivered, as they do not require relation support.

If a future profile revision or a directly constructed `RenderingContext` supplies `fallback_text` for a relation, the MeshCore renderer would produce its native payload format with the relation context embedded as inline text. This is a renderer contract, not a test-only quirk; any code path that populates `fallback_text` on a routed relation triggers the same inline-text rendering path.

**Thread deferral:** The `"thread"` relation type is defined in the canonical event model (`VALID_RELATION_TYPES`), but no adapter currently renders thread relations. Thread capability requires a future `AdapterCapabilities.threads` field and planner-level thread routing. Until then, thread relations are reserved and not capability-driven.

**Payload requirement:** The MeshCore renderer produces MeshCore-native payloads (text body, truncated to `max_text_bytes`, with `channel_index` or `contact_id`). The adapter dispatches these payloads via `session.send_text` without modification.

---

## Known Limitations

- **Alpha maturity.** No end-to-end delivery confirmation; `delivery_status` is always `"local_accepted"`.
- **No reply or reaction support.** Capabilities declare both as `"unsupported"`. MeshCore has no built-in threading/reply mechanism.
- **Sender identity is a pubkey prefix.** 6-byte hex prefix is not human-readable; downstream consumers must map to display names externally.
- **Duplicate-send risk from retry.** The session retries transient send failures up to 3 times. If the first attempt was received by the remote node but the ACK was lost, the message will be sent again.
- **No outbound DM initiation in capabilities.** `direct_messages=False` means MEDRE does not model outbound DM targeting. Inbound PRIV messages are still relayed.
- **BLE mode is future/not yet validated.** The `create_ble` factory is wired but not production-tested.
- **Radio metadata (SNR/RSSI) only in V3 protocol messages.** Not available for all packet types.

---

## Duplicate-Send Risk Level

**Medium.** Session-level retry on transient failures can produce duplicates when the first attempt succeeded at the radio but the response was lost. There is no application-level dedup or idempotent send ID.

---

## Validation Status

- Config validation enforces: non-empty `adapter_id`, valid `connection_type`, connection-type-specific required fields, non-negative `max_text_bytes` (int, not bool), optional `identity`/`pubkey` format, `node_config` secret-key prohibition.
- Classifier tests cover all classification branches (ACK, text, DM, empty, unknown, malformed).
- Codec tests cover text and DM decode.
- Renderer tests cover text rendering and UTF-8 truncation.
- Session tests cover lifecycle, reconnect, and send retry.

---

## Reference Libraries

| Library    | Purpose                                                           | Optional                |
| ---------- | ----------------------------------------------------------------- | ----------------------- |
| `meshcore` | MeshCore Python SDK (`MeshCore` factory, `EventType`, `commands`) | Yes (`medre[meshcore]`) |
