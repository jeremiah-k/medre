# Meshtastic Transport Profile

## Purpose and Role

The Meshtastic adapter is a **transport adapter** (`AdapterRole.TRANSPORT`) that connects to a Meshtastic radio node via TCP, serial, BLE, or a test fake. It bridges inbound radio packets into the MEDRE canonical event stream and enqueues outbound rendered payloads for paced delivery through an internal outbound queue.

The adapter delegates raw transport lifecycle to `MeshtasticSession`. The session owns the SDK client; the adapter owns semantic conversion (classification, codec decode, event publishing, queue management).

**Platform identifier:** `meshtastic`

---

## Configuration Fields

| Field                              | Type                                   | Default              | Description                                                       |
| ---------------------------------- | -------------------------------------- | -------------------- | ----------------------------------------------------------------- |
| `adapter_id`                       | `str`                                  | _(required)_         | Unique adapter instance identifier                                |
| `connection_type`                  | `Literal["fake","tcp","serial","ble"]` | `"fake"`             | Connection mode                                                   |
| `host`                             | `str \| None`                          | `None`               | Hostname/IP for TCP (required when `connection_type="tcp"`)       |
| `port`                             | `int \| None`                          | `None`               | Port for TCP (default 4403)                                       |
| `serial_port`                      | `str \| None`                          | `None`               | Serial device path (required when `connection_type="serial"`)     |
| `ble_address`                      | `str \| None`                          | `None`               | BLE MAC address (required when `connection_type="ble"`)           |
| `origin_label`                     | `str`                                  | `""`                 | Platform-neutral operator-defined source label for relay prefixes |
| `default_channel`                  | `int`                                  | `0`                  | Default radio channel index for outbound                          |
| `channel_mapping`                  | `dict[int, str]`                       | `{}`                 | Display-label map (NOT a relay allowlist)                         |
| `message_delay_seconds`            | `float`                                | `0.5`                | Minimum seconds between outbound messages                         |
| `startup_backlog_suppress_seconds` | `float`                                | `5.0`                | Window after start to suppress stale packets                      |
| `sync_timeout_ms`                  | `int`                                  | `30000`              | Sync operation timeout                                            |
| `radio_relay_prefix`               | `str`                                  | `"{sender_short}: "` | Prefix template for Matrix→Meshtastic direction                   |
| `mmrelay_compatibility`            | `bool`                                 | `False`              | Embed mmrelay-compatible mesh metadata in Matrix events           |
| `max_text_bytes`                   | `int`                                  | `227`                | UTF-8 byte budget for final radio text                            |
| `queue_send_max_attempts`          | `int`                                  | `3`                  | Max send attempts per queued item                                 |
| `outbound_mode`                    | `Literal["enabled","listen_only"]`     | `"enabled"`          | `"listen_only"` suppresses all radio sends                        |

---

## Capabilities

Machine-readable capability declaration: [`meshtastic-capabilities.json`](meshtastic-capabilities.json)

> Capability levels map to the CapabilityLevel enum (adapter-runtime.md §6.2): `"native"` = `TRUE`, `"unsupported"` = `FALSE`.

| Capability        | Value                      |
| ----------------- | -------------------------- |
| text              | `True`                     |
| replies           | `"native"`                 |
| reactions         | `"native"`                 |
| edits             | `"unsupported"`            |
| deletes           | `"unsupported"`            |
| attachments       | `False`                    |
| metadata_fields   | `True`                     |
| delivery_receipts | `False`                    |
| store_and_forward | `False`                    |
| direct_messages   | `False`                    |
| channels          | `True`                     |
| async_delivery    | `True`                     |
| mesh_routing      | `True`                     |
| max_text_bytes    | Configurable (default 227) |

---

## Relay Attribution Prefix

The Meshtastic renderer prepends a human-readable relay attribution prefix to
outbound radio text when `radio_relay_prefix` is non-empty on the target
adapter's `MeshtasticConfig`. The prefix is applied for plain text messages
and replies. It is **not** applied for structured native reactions
(`emoji=1`) or cross-platform descriptive reactions (which embed their own
compact prefix in the text body).

**Configuration:** `radio_relay_prefix` (string, default
`"{sender_short}: "`).

**Template syntax:** `{placeholder}` variables resolved by the shared core
formatter (`format_relay_prefix`) against `RelayAttribution` extracted from
the source event. `{origin_label}` is resolved through a precedence chain:
route-level `source_origin_label` (or `dest_origin_label` for reverse legs)
from the matched route's expansion context takes priority; when no route-level
label is set, the renderer falls back to the source adapter's `origin_label`
config via the runtime source-attribution registry; when neither source
provides a label, the variable resolves to empty string.

### Supported Template Variables

The shared prefix formatter (`format_relay_prefix` in
`src/medre/core/rendering/attribution.py`) defines all available template
variables. This is the authoritative list used by all four transport
renderers (Matrix, Meshtastic, MeshCore, LXMF).

**Canonical fields** (from `RelayAttribution`):

| Variable                      | Source                                                         |
| ----------------------------- | -------------------------------------------------------------- |
| `{source_adapter_id}`         | Adapter instance ID                                            |
| `{source_platform}`           | Platform name (`matrix`, `meshtastic`, etc.)                   |
| `{source_transport}`          | Transport identifier                                           |
| `{source_sender_id}`          | Native sender ID (MXID, node ID, pubkey, hash)                 |
| `{source_sender_label}`       | Primary human-readable sender label                            |
| `{source_sender_short_label}` | Abbreviated sender label                                       |
| `{source_sender_handle}`      | Sender handle or address                                       |
| `{source_room_or_channel}`    | Room or channel ID from source                                 |
| `{source_origin_label}`       | Source adapter origin label (from source-attribution registry) |
| `{source_native_message_id}`  | Native message ID from source                                  |
| `{source_native_channel_id}`  | Native channel ID from source                                  |
| `{route_id}`                  | Route identifier                                               |

**Preferred aliases** (short names for common use in templates):

| Alias             | Canonical field             |
| ----------------- | --------------------------- |
| `{sender}`        | `source_sender_label`       |
| `{sender_short}`  | `source_sender_short_label` |
| `{sender_id}`     | `source_sender_id`          |
| `{sender_handle}` | `source_sender_handle`      |
| `{platform}`      | `source_platform`           |
| `{route_id}`      | `route_id`                  |
| `{channel}`       | `source_room_or_channel`    |
| `{origin_label}`  | `source_origin_label`       |

### Formatting Rules

- **None/missing values** format as empty strings. The literal text
  `"None"` is never rendered.
- **Unknown placeholders** (not in the tables above) are left unchanged in
  the output (e.g. `{bogus}` stays `{bogus}`) and recorded in
  `unknown_variables` with `formatting_error` set.
- **Deterministic:** Same inputs always produce the same output.
- **Never raises:** All internal errors are captured in `formatting_error`.

### Truncation

The prefix is prepended **before** UTF-8 byte-budget truncation
(`max_text_bytes`, default 227). The rendered prefix counts toward the byte
budget. Multi-byte UTF-8 codepoints are never split. Operators should
consider prefix length when setting `max_text_bytes` — long prefixes reduce
space for message content.

### Metadata Keys

When a prefix is rendered, the following diagnostic keys are set on the
`RenderingResult.metadata` (normalized keys, consistent across all renderers):

| Key                              | Value                                                              |
| -------------------------------- | ------------------------------------------------------------------ |
| `relay_prefix_template`          | The raw template string from adapter config                        |
| `relay_prefix_rendered`          | The fully resolved prefix after variable substitution              |
| `relay_prefix_variables_used`    | Variables that were resolved from event context                    |
| `relay_prefix_missing_variables` | Variables whose source values were `None`, absent, or empty string |
| `relay_prefix_unknown_variables` | Placeholders not recognized by the formatter                       |
| `relay_prefix_formatting_error`  | Non-`None` when a formatting exception occurred                    |

### Matrix-Bound Prefix

When the Meshtastic adapter is the **source** of a relay to a Matrix target,
the Matrix renderer resolves the prefix template. The preferred path is
`MatrixConfig.relay_prefix` (target-local, default `""`). When that is empty,
the renderer falls back to an empty string (no prefix). Matrix prefix is now
target-local only — there is no `matrix_relay_prefix` on MeshtasticConfig.

For cross-platform prefix templates, operators SHOULD prefer `{origin_label}`
— the MEDRE-generic source label populated on all adapter configs. `{origin_label}` is resolved
through a precedence chain: route-level `source_origin_label` (or
`dest_origin_label` for reverse legs) takes priority over the source adapter's
config-level `origin_label`.

The same shared variable table applies. The Matrix renderer has no
constrained radio byte budget — prefix length is unconstrained in the
renderer, though Matrix homeservers impose their own event size limits.

### Attribution Caveat

The prefix is human-readable attribution only. It does not constitute
delivery evidence. The MEDRE metadata namespace remains the authoritative
source for machine-readable provenance. Local queue acceptance does not
confirm Meshtastic RF transmission.

---

The packet classifier (`MeshtasticPacketClassifier`) applies a 10-step conservative policy:

| Priority | Condition                             | Action       | Reason                                    |
| -------- | ------------------------------------- | ------------ | ----------------------------------------- |
| 1        | Encrypted packet                      | **drop**     | `"encrypted packet"`                      |
| 2        | Malformed / no decoded payload        | **drop**     | `"malformed or missing decoded payload"`  |
| 3        | Detection sensor portnum              | **deferred** | `"detection sensor packets are deferred"` |
| 4        | ACK / admin                           | **ignore**   | `"ack/admin/system message"`              |
| 5        | Unknown / custom portnum              | **deferred** | `"unknown or custom portnum"`             |
| 6        | Telemetry / position / nodeinfo       | **ignore**   | `"non-chat message type"`                 |
| 7        | Direct message (non-broadcast `toId`) | **ignore**   | `"direct message to specific node"`       |
| 8        | Plugin-only portnum                   | **deferred** | `"plugin_only packets are deferred"`      |
| 9        | Empty text body                       | **ignore**   | `"empty text"`                            |
| 10       | Valid text message                    | **relay**    | `"text message"`                          |

Relayed packets are decoded by `MeshtasticCodec` into:

- **`MESSAGE_CREATED`** — regular text messages and replies (`replyId` without `emoji`).
- **`MESSAGE_REACTED`** — reaction messages (`replyId` with `emoji == 1`).

---

## Supported Outbound Event Kinds

The Meshtastic renderer (`MeshtasticRenderer`) produces:

- **Plain text** — body text with configurable `radio_relay_prefix` (see §Relay Attribution Prefix), UTF-8 byte-budget truncation.
- **Native reply** — `reply_id` (int) set from relation's Meshtastic native ref; plain text body.
- **Native reaction** — `reply_id` + `emoji=1` for Meshtastic-originated tapbacks; text is the emoji key.
- **Cross-platform descriptive reaction** — MMRelay-style text `"reacted {emoji} to \"{text}\""` with `reply_id` but NO `emoji=1`.
- **UTF-8 truncation** — Applied after all rendering; multi-byte codepoints never split.

---

## Native Reference Format

- **Inbound native ref:** `NativeRef(adapter=<id>, native_channel_id=<str(channel)>, native_message_id=<str(packet_id)>)`
- **Outbound native ref (enqueued):** `native_message_id=None`, `delivery_status="enqueued"`, `native_channel_id=str(channel_index)`.
- **Outbound native ref (sent):** Delayed outbound ref recorded by `_process_queue` when `send_one()` returns a real packet ID from the SDK.

---

## Delivery Semantics

**Local acceptance:** `deliver()` enqueues the payload into `MeshtasticOutboundQueue` and returns `AdapterDeliveryResult(delivery_status="enqueued")`. This confirms local queue acceptance only — it does NOT imply RF transmission.

**Actual send:** A background `_process_queue` task drains the queue at `message_delay_seconds` pace via `session.send()`. The session sends via the SDK (`sendText` or structured `_sendPacket`) with bounded retry (3 attempts, linear backoff 0.1 s × attempt).

**Queue semantics:**

- Bounded queue (default 1024 items); rejects with `MeshtasticSendError(transient=True)` when full.
- Transient send failures: item is **front-requeued** up to `queue_send_max_attempts`; then reported as terminal (`exhausted`) via `record_outbound_terminal`.
- Permanent failures: reported as terminal (`permanent_failed`) immediately via `record_outbound_terminal`.
- `asyncio.CancelledError` during send: in-flight item stored for cancellation reporting; remaining queue items reported as `abandoned`.
- Adapter stop: when an in-flight cancelled item exists (evidence the drain task was actively processing), remaining queued items are drained and reported as `abandoned`. When no in-flight item exists, remaining items are left in the in-memory queue to survive across the stop boundary for the next `start()` cycle.
- Terminal outcomes produce durable receipts and outbox transitions; there is no silent drop.
- `listen_only` mode: `deliver()` raises `AdapterPermanentError` before enqueue.

**Correlation:** Each queued item carries an internal `outbox_id` and `attempt_number` from the pipeline. These are stored in queue item metadata (never in the wire payload sent to the radio). When the delayed callback arrives, these keys enable exact outbox-level correlation with stale-callback protection.

**Startup backlog suppression:** Packets with `rxTime` before `adapter_start_epoch - startup_backlog_suppress_seconds` are silently dropped.

---

## Session Lifecycle

1. **Disconnected** — Initial state; `_client=None`.
2. **Connecting** — `session.start()` creates SDK client (TCP/Serial/BLE interface), subscribes to `meshtastic.receive` pubsub callback.
3. **Connected** — Client created and subscribed; inbound packets flow via `_on_receive` → `_on_packet`.
4. **Reconnecting** — `notify_connection_lost()` triggers bounded exponential backoff (1 s → 2 s → 4 s → … capped at 30 s, ±25 % jitter, max 10 attempts). On success, counters reset.
5. **Stopped** — `stop()` sets `_stop_requested`, cancels reconnect task, unsubscribes pubsub, closes client. Idempotent.

**Thread bridging:** `_on_packet` is called from the Meshtastic SDK reader thread. The adapter uses `asyncio.run_coroutine_threadsafe` to bridge onto the event loop; futures are tracked and cancelled on stop.

---

## Diagnostics Keys

`adapter.diagnostics()` returns (no secrets, no raw protobuf):

| Key                                     | Type    | Description                        |
| --------------------------------------- | ------- | ---------------------------------- |
| `adapter_id`                            | `str`   | Adapter identifier                 |
| `started`                               | `bool`  | Adapter started flag               |
| `connection_type`                       | `str`   | Config connection mode             |
| `queue_pending`                         | `int`   | Items in outbound queue            |
| `queue_total_sent`                      | `int`   | Successfully sent items            |
| `queue_total_failed`                    | `int`   | Terminal failures                  |
| `queue_total_enqueued`                  | `int`   | Total enqueue successes            |
| `queue_total_dequeued`                  | `int`   | Total dequeue operations           |
| `queue_total_rejected`                  | `int`   | Enqueue rejections (full queue)    |
| `queue_total_requeued`                  | `int`   | Transient-failure front-requeues   |
| `queue_total_exhausted`                 | `int`   | Items dropped after max attempts   |
| `queue_total_permanent_failed`          | `int`   | Items dropped for permanent errors |
| `queue_utilization_pct`                 | `float` | Queue fullness percentage          |
| `drain_task_running`                    | `bool`  | Background drain task alive        |
| `classifier_packets_seen`               | `int`   | Total classified                   |
| `classifier_packets_relayed`            | `int`   | Relay action count                 |
| `classifier_packets_ignored`            | `int`   | Ignore action count                |
| `classifier_packets_dropped`            | `int`   | Drop action count                  |
| `classifier_packets_deferred`           | `int`   | Deferred action count              |
| `classifier_packets_encrypted_dropped`  | `int`   | Encrypted drop sub-counter         |
| `classifier_packets_dm_ignored`         | `int`   | DM ignore sub-counter              |
| `classifier_packets_empty_text_ignored` | `int`   | Empty text sub-counter             |
| `inbound_published`                     | `int`   | Events published inbound           |
| `startup_backlog_packets_suppressed`    | `int`   | Stale backlog suppressions         |
| `outbound_mode`                         | `str`   | Current outbound mode              |
| `outbound_gate_suppressed`              | `int`   | Listen-only suppressions           |
| `session.connected`                     | `bool`  | Session connected                  |
| `session.reconnecting`                  | `bool`  | Reconnect in progress              |
| `session.reconnect_attempts`            | `int`   | Consecutive reconnect attempts     |
| `session.transient_delivery_failures`   | `int`   | Transient send errors              |
| `session.permanent_delivery_failures`   | `int`   | Permanent send errors              |

---

## Relation Degradation Behavior

Meshtastic is a transport adapter with selective native relation support. The Meshtastic renderer handles all rendering within its native format.

| Relation type | Capability level | Strategy | Rendering path                                                                                 |
| ------------- | ---------------- | -------- | ---------------------------------------------------------------------------------------------- |
| Replies       | `"native"`       | `direct` | `reply_id` (int) set from relation's Meshtastic native ref; plain text body                    |
| Reactions     | `"native"`       | `direct` | `reply_id` + `emoji=1` for Meshtastic-originated tapbacks; descriptive text for cross-platform |
| Edits         | `"unsupported"`  | `skip`   | No delivery. Edit events targeting this adapter are suppressed.                                |
| Deletes       | `"unsupported"`  | `skip`   | No delivery. Delete events targeting this adapter are suppressed.                              |
| Threads       | _deferred_       | —        | Reserved. Meshtastic has no thread concept.                                                    |

Meshtastic does not currently declare the `"fallback"` capability level for any relation type in its capability JSON. All relations are either native or unsupported. When a relation type is unsupported, the delivery is skipped entirely at the planning stage. Because the capability profile does not advertise fallback, the live planner will not normally select `fallback_text` for this adapter.

If a future profile revision or a directly constructed `RenderingContext` supplies `fallback_text` for a relation, the Meshtastic renderer would produce its native payload format with the relation context embedded as inline text. This is a renderer contract, not a test-only quirk; any code path that populates `fallback_text` on a routed relation triggers the same inline-text rendering path.

**Thread deferral:** The `"thread"` relation type is defined in the canonical event model (`VALID_RELATION_TYPES`), but no adapter currently renders thread relations natively. However, fallback-text rendering for threads is implemented: when `delivery_strategy == "fallback_text"`, thread relations are degraded into inline text (e.g. `[thread: {target}] {payload_text}`). Thread capability requires a future `AdapterCapabilities.threads` field and planner-level thread routing before any adapter can advertise or render threads natively.

**Cross-platform reaction note:** When a reaction originates from a non-Meshtastic source, the Meshtastic renderer produces a descriptive text reaction (`"reacted {emoji} to \"{text}\""`) with `reply_id` but without `emoji=1`. This is still a native Meshtastic payload, not a fallback text payload. The renderer operates within its native format.

**Payload requirement:** The Meshtastic renderer produces Meshtastic-native payloads (text body with optional `reply_id`/`emoji` fields, truncated to `max_text_bytes`). The adapter enqueues these payloads via `MeshtasticOutboundQueue` without modification.

---

## Known Limitations

- **No delivery confirmation.** Meshtastic `sendText` returns a packet object but does not guarantee the recipient received it. There is no ACK-based confirmation in the current implementation.
- **Duplicate-send risk from queue retry.** Transient failures are retried; if the first attempt actually transmitted but the ACK was lost, the message will be sent again.
- **No DM support.** `direct_messages=False`; inbound DMs are ignored by the classifier.
- **No edits, deletes, or attachments.** Declared unsupported in capabilities.
- **Structured send uses internal `_sendPacket`.** Relies on protobuf `MeshPacket` construction and the `_sendPacket` SDK method, which is not part of the public API and may break across SDK versions.
- **Channel mapping is display-only.** `channel_mapping` is NOT a relay allowlist; the classifier does not gate on channel membership.
- **Node info enrichment is best-effort.** Sender labels (`source_sender_label`, `source_sender_short_label`) are populated from the SDK `nodes` dict; missing node info results in empty strings.

---

## Duplicate-Send Risk Level

**High.** Queue-based retry with front-requeue can produce duplicates when the first attempt succeeded at the radio level but the response was lost. The SDK does not provide idempotent send IDs. The paced queue (`message_delay_seconds`) does not deduplicate. Consumers must be tolerant of duplicates.

---

## Validation Status

- Config validation enforces: non-empty `adapter_id`, valid `connection_type`, connection-type-specific required fields, non-negative `max_text_bytes` (int, not bool), positive `queue_send_max_attempts`, valid `outbound_mode`.
- Classifier tests cover all 10 policy branches.
- Codec tests cover text, reply, and reaction decode.
- Renderer tests cover prefix, reply, native/cross-platform reaction, UTF-8 truncation.
- Queue tests cover enqueue/dequeue, front-requeue, bounded capacity rejection, exhaustion.

---

## Reference Libraries

| Library               | Purpose                                             | Optional                          |
| --------------------- | --------------------------------------------------- | --------------------------------- |
| `meshtastic` / `mtjk` | Meshtastic Python SDK (TCP, serial, BLE interfaces) | Yes (`medre[meshtastic]`)         |
| `pubsub` (pypubsub)   | Meshtastic SDK callback subscription                | Yes (via `meshtastic` dependency) |
| `meshtastic.protobuf` | Protobuf `MeshPacket` / `Data` for structured send  | Yes (via `meshtastic` dependency) |
