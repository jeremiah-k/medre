# MeshCore Transport Profile

## Purpose and Role

The MeshCore adapter is a **transport adapter** (`AdapterRole.TRANSPORT`) that connects to a MeshCore node via TCP, serial, BLE, or a test fake. It bridges inbound event payloads into the MEDRE canonical event stream and delivers outbound rendered payloads directly via the session for local acceptance.

The adapter delegates SDK client lifecycle to `MeshCoreSession`. The session owns the `MeshCore` SDK instance; the adapter owns semantic conversion (classification, codec decode, event publishing).

**Platform identifier:** `meshcore`

**Maturity note:** MeshCore integration is at a **pre-release** stage. Delivery status reflects local acceptance only; there is no end-to-end ACK mechanism.

---

## Configuration Fields

| Field                   | Type                                   | Default      | Description                                                                                                                                                        |
| ----------------------- | -------------------------------------- | ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `adapter_id`            | `str`                                  | _(required)_ | Unique adapter instance identifier                                                                                                                                 |
| `connection_type`       | `Literal["fake","tcp","serial","ble"]` | `"fake"`     | Connection mode                                                                                                                                                    |
| `host`                  | `str \| None`                          | `None`       | Hostname/IP for TCP (required when `connection_type="tcp"`)                                                                                                        |
| `port`                  | `int \| None`                          | `4000`       | Port for TCP (defaults to 4000 when `connection_type="tcp"`)                                                                                                       |
| `serial_port`           | `str \| None`                          | `None`       | Serial device path (required when `connection_type="serial"`)                                                                                                      |
| `serial_baudrate`       | `int`                                  | `115200`     | Baud rate for serial                                                                                                                                               |
| `ble_address`           | `str \| None`                          | `None`       | BLE MAC address (required when `connection_type="ble"`)                                                                                                            |
| `ble_pin`               | `str \| None`                          | `None`       | Optional BLE pairing PIN (used only for BLE); forwarded to MeshCore.create_ble(pin=...) only when configured; sensitive — never exposed in diagnostics/logs/errors |
| `origin_label`          | `str`                                  | `""`         | Platform-neutral operator-defined source label for relay prefixes                                                                                                  |
| `default_channel`       | `int`                                  | `0`          | Default channel index for outbound                                                                                                                                 |
| `message_delay_seconds` | `float`                                | `0.5`        | Minimum delay between outbound messages                                                                                                                            |
| `identity`              | `str \| None`                          | `None`       | Optional node identity string                                                                                                                                      |
| `pubkey`                | `str \| None`                          | `None`       | Optional public key (hex string)                                                                                                                                   |
| `node_config`           | `dict[str, object]`                    | `{}`         | Opaque node settings; must not contain secret keys                                                                                                                 |
| `max_text_bytes`        | `int`                                  | `512`        | UTF-8 byte budget for rendered radio text                                                                                                                          |
| `meshcore_relay_prefix` | `str`                                  | `""`         | Relay prefix template for outbound text (empty = no prefix; see §Relay Attribution Prefix)                                                                         |

---

## Capabilities

Machine-readable capability declaration: [`meshcore-capabilities.json`](meshcore-capabilities.json)

> Capability levels map to the CapabilityLevel enum (adapter-runtime.md §6.2):
> `"native"` = `TRUE`, `"fallback"` = degraded inline text, and
> `"unsupported"` = `FALSE`.

| Capability        | Value                                    |
| ----------------- | ---------------------------------------- |
| text              | `True`                                   |
| title             | `False`                                  |
| threads           | `"fallback"`                             |
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

- **Plain text** — body text with optional `meshcore_relay_prefix`, UTF-8 byte-budget truncation.
- **Channel messages** — `channel_index` determines target channel; sends via `session.send_text(channel_index=…)`.
- **Direct messages** — `contact_id` (pubkey prefix) determines recipient; sends via `session.send_text(contact_id=…)`.

No reply or reaction rendering — capabilities declare both as `"unsupported"`.

---

## Relay Attribution Prefix

The MeshCore renderer prepends a human-readable relay attribution prefix to
outbound text when `meshcore_relay_prefix` is non-empty on the target
adapter's `MeshCoreConfig`.

**Configuration:** `meshcore_relay_prefix` (string, default `""`). When
empty, no prefix is prepended.

**Template syntax:** `{placeholder}` variables resolved by the shared core
formatter (`format_relay_prefix`) against `RelayAttribution` extracted from
the source event. Operators SHOULD prefer `{origin_label}` for
cross-platform prefix templates — `origin_label` is the MEDRE-generic source
label. `{origin_label}` is resolved through a precedence chain: route-level
`source_origin_label` (or `dest_origin_label` for reverse legs) takes
priority over the source adapter's config-level `origin_label`. See the
Meshtastic Transport Profile §Relay Attribution Prefix for the authoritative
list of supported template variables.

**Default:** `""` (no prefix). MeshCore sender identity is a hex pubkey
prefix, not a human-readable name. Templates referencing `{sender}` or
`{sender_short}` resolve to empty strings unless the sender is a
locally-known contact whose advertised name enriches those fields (see
§Sender Identity Projection). Operators SHOULD prefer `{origin_label}` or
`{sender_id}` for MeshCore-bound prefixes that need a stable value.

**Truncation:** The prefix is prepended before UTF-8 byte-budget truncation (`max_text_bytes`, default 512). The rendered prefix counts toward the byte budget. Multi-byte UTF-8 codepoints are never split.

**Metadata keys** (conditional, only when prefix is configured):

| Key                              | Value                                                       |
| -------------------------------- | ----------------------------------------------------------- |
| `relay_prefix_template`          | The original template string                                |
| `relay_prefix_rendered`          | The rendered prefix string                                  |
| `relay_prefix_variables_used`    | Variables resolved (value found, even if empty)             |
| `relay_prefix_missing_variables` | Variables in template whose value was `None` or empty       |
| `relay_prefix_unknown_variables` | Unknown placeholders left unchanged in the rendered prefix  |
| `relay_prefix_formatting_error`  | Error description when unknown placeholders are encountered |

**Attribution caveat:** The prefix is human-readable attribution only. It does not constitute delivery evidence. The MEDRE metadata namespace remains the authoritative source for machine-readable provenance. Local send acceptance does not confirm MeshCore RF delivery.

---

## Sender Identity Projection

The MeshCore adapter projects MeshCore-native sender identity into the
generic `RelayAttribution` sender fields (see
[Routing and Delivery §17.5.9](../routing-delivery.md#1759-generic-sender-identity-semantics)).
Projection is owned by `project_meshcore_attribution`; core rendering
consumes only the generic fields.

At ingress, each event carries a sender public-key prefix (`pubkey_prefix`,
a 6-byte hex truncation of the Ed25519 public key) and a
`sender_timestamp`. MeshCore identity is always pubkey-based; there is no
numeric node ID.

### Contact-Label Enrichment

At ingress the adapter resolves a known contact's advertised name via
`session.resolve_contact_label(pubkey_prefix)`. The session calls
`MeshCore.get_contact_by_key_prefix(prefix)` against the SDK's in-memory
`contacts` dict, populated by `CONTACTS` events. The lookup is
synchronous, issues no network call, never raises, and returns the
contact's `adv_name` (stripped) or `None`. The codec records the result
as `meshcore.contact_label` and `meshcore.contact_short_label` in native
metadata.

Contact keys are intentionally excluded from
`MESHCORE_NAMESPACED_KEYS`. Platform detection relies on the core
identity keys (`pubkey_prefix`, `sender_id`, `channel`, `packet_id`);
contact labels are enrichment layered on top.

### Projection Rules

| Generic field               | Source                                                                                                        |
| --------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `source_sender_id`          | `meshcore.pubkey_prefix` → `meshcore.sender_id` → bare `pubkey_prefix`                                        |
| `source_sender_label`       | `meshcore.contact_label` only (human label; opaque pubkey never becomes label)                                |
| `source_sender_short_label` | `meshcore.contact_short_label`, else compact derivation of `contact_label` (first whitespace-delimited token) |
| `source_sender_handle`      | Not produced (the pubkey prefix is exposed via `source_sender_id`)                                            |

When the sender is not a locally-known contact, both label fields are
`None`. The opaque pubkey prefix never populates `source_sender_label`,
so `{sender}` renders empty rather than a truncated hex string.
Operators who want the pubkey prefix in a prefix use `{sender_id}`. The
projection also emits `source_native_channel_id` and
`source_native_message_id`.

Per the opacity rule ([§17.5.9](../routing-delivery.md#1759-generic-sender-identity-semantics)),
an opaque pubkey is not a label. When contact enrichment resolves a
label, `{sender}` and `{sender_short}` populate for MeshCore-origin
events; otherwise they resolve to empty strings.

### No Topology or Contact Canonical Events

The adapter emits no topology or contact canonical events and models no
contact reachability or RF delivery. Contact enrichment is limited to
the local SDK contact cache; there is no cross-transport
pubkey-to-name resolution.

Identity labels may appear in rendered messages and renderer-local
metadata; enrichment is observational and is not delivery evidence.
Diagnostics expose no secrets (including BLE pairing PINs) and no raw
SDK objects. See
[Routing and Delivery §17.5.10](../routing-delivery.md#17510-identity-enrichment-diagnostics-and-privacy)
for the cross-transport policy.

---

## Native Reference Format

- **Inbound native ref:** `NativeRef(adapter=<id>, native_channel_id=<str(channel_idx)>, native_message_id=<str(sender_timestamp)>)`
  - `packet_id` is the `sender_timestamp` (4-byte LE Unix timestamp).
  - `sender_id` is the `pubkey_prefix` (6-byte hex prefix of sender's public key).
- **Outbound native ref:** `native_message_id` extracted from SDK send result when available; `delivery_status="sent"` (default), with `metadata["meshcore"]["local_acceptance"]=True`.

---

## Delivery Semantics

**Local acceptance:** `deliver()` delegates to `session.send_text()`. The session
sends via the SDK with bounded retry (3 attempts). DM sends MAY use the
`suggested_timeout` value returned by the meshcore_py SDK for the target contact;
this value is in milliseconds from the SDK and is converted to seconds internally,
then clamped. `suggested_timeout` hints are cached per normalized contact. Channel
sends do not use DM `suggested_timeout` hints. The pinned SDK reports
command-response timeout/no-event conditions as `ERROR` events with reasons
`"timeout"` or `"no_event_received"`; MEDRE treats those two reasons as transient
transport uncertainty and keeps them on the bounded retry path. Other explicit SDK
`ERROR` responses remain permanent rejections. Local acceptance does not constitute
RF end-to-end delivery. Success returns a `native_message_id` (if the SDK provides
one). The `delivery_note` varies by send type: channel sends report `"MeshCore:
channel send local-accepted only (no ACK protocol)"`; DM sends report `"MeshCore:
DM sent with expected_ack captured as native_id; delivery confirmation not tracked"`.

**No end-to-end confirmation.** The current MeshCore SDK does not provide delivery ACKs. The adapter reports `delivery_status="sent"` (the default adapter-level value) and carries `metadata["meshcore"]["local_acceptance"]=True` to indicate the message was accepted by the local SDK without network confirmation. Consumers MUST be tolerant of delivery uncertainty.

**Fake mode:** Returns `None` (no real delivery).

---

## Session Lifecycle

1. **Disconnected** — Initial state; `_meshcore=None`.
2. **Connecting** — `session.start()` calls `_connect_real()` which uses SDK factory
   methods (`MeshCore.create_tcp`, `MeshCore.create_serial`,
   `MeshCore.create_ble`) with SDK auto-reconnect disabled. Each pinned-SDK factory
   performs the transport connect and required `APP_START` handshake before
   returning. MEDRE then subscribes to `CONTACT_MSG_RECV`, `CHANNEL_MSG_RECV`, and
   `DISCONNECTED`, reads the SDK-populated `self_info` into diagnostics
   (`device_name`, `public_key_prefix`, `radio_freq`), and starts auto-message
   fetching best-effort. MEDRE reconnect creates a fresh SDK client through the same
   factory path, so each transport connection receives exactly one SDK-owned
   `APP_START`.
3. **Connected** — Client created, subscribed, appstart succeeded, auto-fetch attempted (best-effort); `_diag.connected=True`. Inbound events flow via `_on_sdk_event` → `_message_callback`.
4. **Reconnecting** — SDK `DISCONNECTED` event triggers bounded exponential backoff (1 s → 2 s → 4 s → … capped at 30 s, ±25 % jitter, max 10 attempts). On success, re-subscribes.
5. **Stopped** — `stop()` sets `_stop_requested`, stops auto-message-fetching (with bounded timeout), unsubscribes, disconnects SDK client, nulls references. Idempotent.

**Callback bridging:** SDK callbacks are async; the session invokes the adapter callback directly and awaits short callbacks inline. Long-running callbacks are scheduled as fire-and-forget tasks.

---

## Diagnostics Keys

`adapter.diagnostics()` returns (no secrets, no raw SDK objects):

| Key                                     | Type            | Description                                                        |
| --------------------------------------- | --------------- | ------------------------------------------------------------------ |
| `adapter_id`                            | `str`           | Adapter identifier                                                 |
| `started`                               | `bool`          | Adapter started flag                                               |
| `mode`                                  | `str`           | Config connection type                                             |
| `classifier_packets_seen`               | `int`           | Total classified                                                   |
| `classifier_packets_relayed`            | `int`           | Relay action count                                                 |
| `classifier_packets_ignored`            | `int`           | Ignore action count                                                |
| `classifier_packets_dropped`            | `int`           | Drop action count                                                  |
| `classifier_packets_deferred`           | `int`           | Deferred action count                                              |
| `classifier_packets_ack_ignored`        | `int`           | ACK sub-counter                                                    |
| `classifier_packets_empty_text_ignored` | `int`           | Empty text sub-counter                                             |
| `classifier_packets_unknown_deferred`   | `int`           | Unknown packet sub-counter                                         |
| `classifier_packets_dm_relayed`         | `int`           | DM relay sub-counter                                               |
| `classifier_packets_malformed`          | `int`           | Malformed sub-counter                                              |
| `inbound_published`                     | `int`           | Events published inbound                                           |
| `session.connected`                     | `bool`          | Session connected                                                  |
| `session.reconnecting`                  | `bool`          | Reconnect in progress                                              |
| `session.reconnect_attempts`            | `int`           | Consecutive reconnect attempts                                     |
| `session.last_error`                    | `str \| None`   | Last error description                                             |
| `session.last_message_time`             | `str \| None`   | ISO 8601 UTC timestamp of last inbound message (default `None`)    |
| `session.mode`                          | `str`           | Config connection type (`"fake"`, `"tcp"`, `"serial"`, or `"ble"`) |
| `session.transient_delivery_failures`   | `int`           | Transient send errors                                              |
| `session.permanent_delivery_failures`   | `int`           | Permanent send errors                                              |
| `session.device_name`                   | `str \| None`   | Device name from appstart (default `None`)                         |
| `session.public_key_prefix`             | `str \| None`   | Public key hex prefix (default `None`)                             |
| `session.radio_freq`                    | `float \| None` | Radio frequency in MHz (default `None`)                            |

---

## Relation Degradation Behavior

| Relation type | Capability level | Strategy        | Rendering path                                                                 |
| ------------- | ---------------- | --------------- | ------------------------------------------------------------------------------ |
| Replies       | `"unsupported"`  | `skip`          | No delivery. Reply-carrying events targeting this adapter are suppressed.      |
| Reactions     | `"unsupported"`  | `skip`          | No delivery. Reaction events targeting this adapter are suppressed.            |
| Edits         | `"unsupported"`  | `skip`          | No delivery. Edit events targeting this adapter are suppressed.                |
| Deletes       | `"unsupported"`  | `skip`          | No delivery. Delete events targeting this adapter are suppressed.              |
| Threads       | `"fallback"`     | `fallback_text` | Deterministic inline thread context; native thread emission is not advertised. |

MeshCore has no verified native relation support. Replies, reactions, edits, and
deletes remain unsupported planning-time skips, while `threads="fallback"`
selects deterministic inline thread context during normal live planning. Plain
message kinds continue to deliver directly.

When `fallback_text` is supplied for a relation, the MeshCore renderer produces its
native payload format with the relation context embedded as inline text. This is a
renderer contract, not a test-only quirk; any code path that populates
`fallback_text` on a routed relation triggers the same inline-text rendering path.

**Thread capability:** MeshCore has no native thread primitive; thread relations
are deterministically degraded to inline fallback text.

**Payload requirement:** The MeshCore renderer produces MeshCore-native payloads (text body, truncated to `max_text_bytes`, with `channel_index` or `contact_id`). The adapter dispatches these payloads via `session.send_text` without modification.

---

## Known Limitations

- **Pre-release maturity.** No end-to-end delivery confirmation; `delivery_status` is `"sent"` (the adapter-level default) while `metadata["meshcore"]["local_acceptance"]` is `True`.
- **No reply or reaction support.** Capabilities declare both as `"unsupported"`. MeshCore has no built-in threading/reply mechanism.
- **Sender identity is a pubkey prefix.** The 6-byte hex prefix is stable per sender but not human-readable. The adapter resolves a human-readable label from the local SDK contact cache when the sender is a known contact (see §Sender Identity Projection); otherwise the label fields are empty and only the opaque prefix is exposed via `{sender_id}`.
- **Duplicate-send risk from retry.** The session retries transient send failures up to 3 times. If the first attempt was received by the remote node but the ACK was lost, the message will be sent again.
- **No outbound DM initiation in capabilities.** `direct_messages=False` means MEDRE does not model outbound DM targeting. Inbound PRIV messages are still relayed.
- **BLE mode is validated against real hardware.** Tested June 2026 with a real MeshCore BLE node on Linux BlueZ. BLE requires pre-pairing and is subject to BlueZ stack limitations.
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
- Session tests cover lifecycle, reconnect, send retry, and SDK timeout/no-response classification.
- Deterministic local integration drives the pinned SDK over TCP for APPSTART,
  inbound parsing, malformed-frame resynchronization, outbound send/reconnect,
  cancellation, serialization, and repeated lifecycle.

---

## Reference Libraries

| Library    | Purpose                                                           | Optional                |
| ---------- | ----------------------------------------------------------------- | ----------------------- |
| `meshcore` | MeshCore Python SDK (`MeshCore` factory, `EventType`, `commands`) | Yes (`medre[meshcore]`) |
