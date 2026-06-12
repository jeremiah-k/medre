# LXMF Transport Profile

## Purpose and Role

The LXMF adapter is a **transport adapter** (`AdapterRole.TRANSPORT`) that connects to a locally-running Reticulum instance via the `RNS` and `lxmf` packages, or operates in a test fake mode. It bridges inbound LXMF messages into the MEDRE canonical event stream and delivers outbound rendered payloads to the LXMRouter for asynchronous mesh delivery.

The adapter delegates all SDK interaction to `LxmfSession`. The session is the **sole owner** of `RNS.Reticulum`, `RNS.Identity`, and `LXMF.LXMRouter` instances. The adapter owns semantic conversion (classification, codec decode, event publishing).

**Platform identifier:** `lxmf`

---

## Configuration Fields

| Field                       | Type                                                     | Default      | Description                                                                                                                    |
| --------------------------- | -------------------------------------------------------- | ------------ | ------------------------------------------------------------------------------------------------------------------------------ |
| `adapter_id`                | `str`                                                    | _(required)_ | Unique adapter instance identifier                                                                                             |
| `connection_type`           | `Literal["fake","reticulum"]`                            | `"fake"`     | Connection mode                                                                                                                |
| `display_name`              | `str`                                                    | `""`         | Display name for LXMF announces                                                                                                |
| `stamp_cost`                | `int`                                                    | `8`          | Default stamp cost (0 = no stamp; non-zero must be positive int)                                                               |
| `default_delivery_method`   | `Literal["direct","opportunistic","propagated","paper"]` | `"direct"`   | Default LXMF delivery method                                                                                                   |
| `meshnet_name`              | `str`                                                    | `""`         | Human-readable meshnet name (informational)                                                                                    |
| `default_channel`           | `int`                                                    | `0`          | Default channel index (informational; LXMF has no channel concept)                                                             |
| `message_delay_seconds`     | `float`                                                  | `0.5`        | Minimum delay between outbound messages (pacing)                                                                               |
| `metadata_embedding`        | `bool`                                                   | `True`       | Embed MEDRE metadata envelopes in LXMF fields                                                                                  |
| `identity_path`             | `str \| None`                                            | `None`       | Path to Reticulum identity file; auto-generated if `None`                                                                      |
| `storage_path`              | `str \| None`                                            | `None`       | **Required** when `connection_type="reticulum"` — LXMF `LXMRouter` raises `ValueError` without it in the validated SDK version |
| `announce_interval_seconds` | `float`                                                  | `600.0`      | Interval in seconds between periodic LXMF announces; `0` disables                                                              |
| `lxmf_relay_prefix`         | `str`                                                    | `""`         | Relay prefix template for outbound body text (empty = no prefix; see §Relay Attribution Prefix)                                |

---

## Capabilities

Machine-readable capability declaration: [`lxmf-capabilities.json`](lxmf-capabilities.json)

> Capability levels map to the CapabilityLevel enum (adapter-runtime.md §6.2): `"unsupported"` = `FALSE`.

| Capability          | Value                               |
| ------------------- | ----------------------------------- |
| text                | `True`                              |
| title               | `True`                              |
| replies             | `"unsupported"`                     |
| reactions           | `"unsupported"`                     |
| edits               | `"unsupported"`                     |
| deletes             | `"unsupported"`                     |
| attachments         | `False`                             |
| metadata_fields     | `True`                              |
| delivery_receipts   | `False`                             |
| store_and_forward   | `True`                              |
| direct_messages     | `True`                              |
| channels            | `False`                             |
| async_delivery      | `True`                              |
| identity_encryption | `True`                              |
| mesh_routing        | `True`                              |
| max_text_bytes      | `None` (unbounded at adapter level) |
| max_text_chars      | `16384`                             |

---

## Supported Inbound Event Kinds

The packet classifier (`LxmfPacketClassifier`) applies a content-based policy:

| Condition                                                     | Category        | Notes                        |
| ------------------------------------------------------------- | --------------- | ---------------------------- |
| `content` field present (str, bytes, or bytearray, non-empty) | `"text"`        | Relay candidate              |
| No `content` but `fields` dict present and non-empty          | `"unsupported"` | Attachment-only; not relayed |
| Neither content nor recognisable structure                    | `"unknown"`     | Not relayed                  |

The adapter further gates on `is_ack` (always `False` from the classifier) and `category == "text"` before passing to the codec.

Relayed packets are decoded by `LxmfCodec` into:

- **`MESSAGE_CREATED`** — all text-shaped packets.

No reply or reaction event kinds are produced (capabilities declare both `"unsupported"`).

---

## Supported Outbound Event Kinds

The LXMF renderer (`LxmfRenderer`) produces:

- **Plain text with optional title** — `content` (body) and `title` extracted from the canonical event payload.
- **MEDRE metadata envelope** — when `metadata_embedding=True`, a provenance envelope is embedded in the LXMF `fields` dict under key `0xFD` (`FIELD_MEDRE_ENVELOPE`). The envelope contains: `schema_version`, `event_id`, `source_adapter`, `source_transport_id`, `source_channel_id`, `lineage`, `relations`, and `metadata_keys`. No secrets or private keys are ever embedded.
- **fallback_text envelope semantics** — under `delivery_strategy="fallback_text"`, the envelope's `relations` field is always an empty list (`[]`). Relations are represented **exclusively** as inline text in the content field (via `_degrade_relations_inline`). This prevents duplicate representation of relation data as both structured envelope fields and inline text, maintaining strict fallback semantics where the degraded text is the sole relation carrier.
- **Destination hash** — empty string placeholder in current release scope; populated by the routing layer before delivery.

No reply or reaction rendering — capabilities declare both as `"unsupported"`.

---

## Relay Attribution Prefix

The LXMF renderer prepends a human-readable relay attribution prefix to
outbound message body text when `lxmf_relay_prefix` is non-empty on the
`LxmfConfig`.

**Configuration:** `lxmf_relay_prefix` (string, default `""`). When empty,
no prefix is prepended.

**Template syntax:** `{placeholder}` variables resolved by the shared core
formatter (`format_relay_prefix`) against `RelayAttribution` extracted from
the source event. See the Meshtastic Transport Profile §Relay Attribution
Prefix for the authoritative list of supported template variables.

**Default:** `""` (no prefix). LXMF sender identity is a hex Reticulum
identity hash — templates referencing `{longname}` or `{shortname}` resolve
to empty strings for LXMF-origin events.

**Truncation:** The prefix is prepended to the content body before
character-budget truncation (`max_text_chars`, default 16384) and before
envelope handling. The rendered prefix counts toward the character budget.

**Metadata keys** (conditional, only when prefix is configured):

| Key                              | Value                                                      |
| -------------------------------- | ---------------------------------------------------------- |
| `relay_prefix_template`          | The original template string                               |
| `relay_prefix_rendered`          | The rendered prefix string                                 |
| `relay_prefix_variables_used`    | Variables resolved (value found, even if empty)            |
| `relay_prefix_missing_variables` | Variables in template whose value was `None` or empty      |
| `relay_prefix_unknown_variables` | Unknown placeholders left unchanged in the rendered prefix |
| `relay_prefix_formatting_error`  | Error description when unknown placeholders encountered    |

**Attribution caveat:** The prefix is human-readable attribution only. It
does not constitute delivery evidence. The MEDRE metadata namespace
(embedded in the LXMF `fields` envelope) remains the authoritative source
for machine-readable provenance. Local LXMRouter acceptance does not
confirm remote delivery.

---

## Native Reference Format

- **Inbound native ref:** `NativeRef(adapter=<id>, native_channel_id=None, native_message_id=<str(message_hash_hex)>)`
  - `message_id` is the hex-encoded `hash` attribute of the `LXMF.LXMessage` (bytes → hex string).
  - `source_hash` is the 16-byte sender identity hash (hex-encoded, 32 chars).
  - `destination_hash` is the 16-byte recipient identity hash (hex-encoded, 32 chars), if available.
  - `native_channel_id` is always `None` — LXMF has no channel concept.

- **Outbound native ref:** `native_message_id` extracted from the `LXMessage.hash` before and/or after `router.handle_outbound()`. `AdapterDeliveryResult.delivery_status` is always `"sent"` (meaning the message was handed to the local LXMRouter). The initial `LxmfDeliveryState` (typically `OUTBOUND` or `GENERATING`) is reported in `metadata["lxmf"]["delivery_state"]`, not in `delivery_status`.

---

## Delivery Semantics

**Honest asynchronous delivery.** LXMF delivery is inherently multi-hop and asynchronous. The adapter does **not** pretend real-time delivery success.

**Outbound flow:**

1. `deliver()` extracts `content`, `title`, `destination_hash`, `delivery_method`, and `fields` from the rendered payload.
2. `session.send_text()` constructs an `LXMF.LXMessage`, registers a delivery state callback, and calls `router.handle_outbound(lxm)`.
3. Returns `(native_message_id, initial_state)` where `initial_state` is typically `OUTBOUND` or `GENERATING`.
4. The `AdapterDeliveryResult.delivery_note` is `"accepted by LXMRouter — async delivery pending"`.

**Delivery state model (tracked per outbound message):**

`AdapterDeliveryResult.delivery_status` is `"sent"` for all LXMF deliveries, meaning the message was handed to the local LXMRouter. This does **not** mean confirmed delivery to the recipient. The actual LXMF delivery state is reported in `metadata["lxmf"]["delivery_state"]`.

| State        | Meaning                             |
| ------------ | ----------------------------------- |
| `generating` | Message being constructed           |
| `outbound`   | Queued for delivery                 |
| `sending`    | Actively transmitting               |
| `sent`       | Sent to network (not yet confirmed) |
| `delivered`  | Confirmed delivered to recipient    |
| `failed`     | Permanent delivery failure          |
| `rejected`   | Rejected by recipient               |
| `cancelled`  | Cancelled by sender                 |
| `unmapped`   | Unrecognised state from SDK         |

State transitions are tracked via `_on_delivery_state_update` callbacks from `LXMRouter`. Terminal states (`delivered`, `failed`, `rejected`, `cancelled`) remove the message from tracking.

**Delivery state is session-local observability only.** The LXMF adapter tracks SDK delivery state transitions for diagnostics and logging, but does not append durable MEDRE delivery receipts or update outbox lifecycle state from terminal states. `delivery_receipts` is `False` because confirmed recipient delivery is not yet persisted into the MEDRE receipt/outbox lifecycle. The `AdapterDeliveryResult.delivery_status` of `"sent"` means local handoff to `LXMRouter` only — not confirmed recipient delivery.

**Outbound delivery tracking is bounded** — capped at 1000 entries with FIFO eviction to prevent unbounded growth.

**Retry:** `send_text()` retries transient failures up to 3 attempts with linear backoff (0.1 s × attempt). Permanent failures (`ValueError`, `TypeError`) raise immediately.

**Fake mode:** Returns deterministic `fake-<id>-<monotonic_ns>` ID with `OUTBOUND` state.

---

## Session Lifecycle

1. **Disconnected** — Initial state; `_reticulum=None`, `_identity=None`, `_router=None`.
2. **Connecting** — `session.start()`:
   - Captures the asyncio event loop for thread bridging.
   - Fake mode: sets `connected=True`, `router_running=True`.
   - Real mode: `_connect_real()` — initialises `RNS.Reticulum` (reuses singleton if available), loads or auto-generates `RNS.Identity`, creates `LXMF.LXMRouter(identity=..., storagepath=...)`, registers delivery callback.
3. **Connected** — Router operational; inbound messages flow via `_on_lxmf_delivery` → normalise → `call_soon_threadsafe` → `_invoke_inbound_callback` → adapter `_on_packet`.
4. **Reconnecting** — On unexpected disconnect, bounded exponential backoff (1 s → 2 s → 4 s → … capped at 30 s, ±25 % jitter, max 10 attempts).
5. **Stopped** — `stop(timeout=5.0)`:
   - Sets `_stop_requested` (prevents reconnect loops).
   - Cancels announce and reconnect tasks.
   - Unsubscribes callbacks.
   - Tears down SDK objects in reverse order (router → identity → reticulum reference).
   - Clears outbound tracking and nulls loop/callback references so late SDK callbacks are dropped.
   - Idempotent.

**Thread bridging:** `LXMRouter` invokes delivery callbacks on Reticulum I/O threads. The session normalises the message (pure CPU, thread-safe) then schedules the adapter callback on the captured asyncio loop via `call_soon_threadsafe()`. The adapter's `_on_packet` then uses `asyncio.create_task` safely on the correct loop.

---

## Diagnostics Keys

`adapter.diagnostics()` returns (no secrets, no identity material, no raw RNS/LXMF objects):

| Key                                   | Type          | Description                       |
| ------------------------------------- | ------------- | --------------------------------- |
| `adapter_id`                          | `str`         | Adapter identifier                |
| `platform`                            | `str`         | `"lxmf"`                          |
| `started`                             | `bool`        | Adapter started flag              |
| `mode`                                | `str`         | Config connection type            |
| `session.connected`                   | `bool`        | Session connected                 |
| `session.router_running`              | `bool`        | LXMRouter operational             |
| `session.reconnecting`                | `bool`        | Reconnect in progress             |
| `session.reconnect_attempts`          | `int`         | Consecutive reconnect attempts    |
| `session.transient_delivery_failures` | `int`         | Transient send errors             |
| `session.permanent_delivery_failures` | `int`         | Permanent send errors             |
| `session.last_error`                  | `str \| None` | Last error description            |
| `session.mode`                        | `str`         | Config connection type (mirrored) |

Session also exposes `diagnostics()` and `delivery_state_counts()` with additional fields: `last_message_time`, `known_path_count`, `propagation_enabled`, `pending_delivery_count`.

---

## Relation Degradation Behavior

LXMF is a transport adapter with no native relation support beyond basic text delivery. All relation types are unsupported.

| Relation type | Capability level | Strategy | Rendering path                                                            |
| ------------- | ---------------- | -------- | ------------------------------------------------------------------------- |
| Replies       | `"unsupported"`  | `skip`   | No delivery. Reply-carrying events targeting this adapter are suppressed. |
| Reactions     | `"unsupported"`  | `skip`   | No delivery. Reaction events targeting this adapter are suppressed.       |
| Edits         | `"unsupported"`  | `skip`   | No delivery. Edit events targeting this adapter are suppressed.           |
| Deletes       | `"unsupported"`  | `skip`   | No delivery. Delete events targeting this adapter are suppressed.         |
| Threads       | _deferred_       | —        | Reserved. LXMF has no thread concept.                                     |

LXMF does not currently declare the `"fallback"` capability level for any relation type in its capability JSON. All relations are unsupported. Events carrying relation context (replies, reactions, edits, deletes) are skipped at the planning stage when the target is an LXMF adapter. Because the capability profile does not advertise fallback, the live planner will not normally select `fallback_text` for this adapter. The `message.created` and `message.text` kinds are delivered normally as they do not require relation support.

If a future profile revision or a directly constructed `RenderingContext` supplies `fallback_text` for a relation, the LXMF renderer produces its native payload format with the relation context embedded as inline text. Under `fallback_text`, the MEDRE fields envelope (`fields[0xFD]`) omits structured relations — its `relations` key is an empty list (`[]`). The only relation representation is the inline text appended to the content body. This is a deliberate design decision: fallback_text is a degradation path, and the envelope retains provenance metadata (event_id, source_adapter, lineage) but not relation data, preventing duplicate representation as both structured fields and inline text. This is a renderer contract, not a test-only quirk; any code path that populates `fallback_text` on a routed relation triggers the same inline-text rendering path with an empty envelope relations list.

**Thread deferral:** The `"thread"` relation type is defined in the canonical event model (`VALID_RELATION_TYPES`), but no adapter currently renders thread relations natively. However, fallback-text rendering for threads is implemented: when `delivery_strategy == "fallback_text"`, thread relations are degraded into inline text (e.g. `[thread: {target}] {payload_text}`). Thread capability requires a future `AdapterCapabilities.threads` field and planner-level thread routing before any adapter can advertise or render threads natively.

**Payload requirement:** The LXMF renderer produces LXMF-native payloads (`content` body, optional `title`, optional MEDRE metadata envelope in `fields[0xFD]`). The adapter dispatches these payloads to the LXMRouter via `handle_outbound` without modification.

---

## Known Limitations

- **No reply or reaction support.** Capabilities declare both as `"unsupported"`. LXMF has no built-in threading mechanism; however, relation reconstruction from the MEDRE fields envelope (`0xFD`) is implemented via `_reconstruct_relations` in `codec.py`. The codec reconstructs `EventRelation` objects from the envelope's `relations` list at decode time. FIELD_THREAD (`0x08`) is explicitly excluded — MEDRE does not read or write the LXMF native thread field.
- **Destination routing is a placeholder.** The renderer sets `destination_hash=""` — the actual routing/destination resolution must be handled upstream before delivery.
- **Attachment-only messages are classified but not relayed.** `has_fields` without `content` yields `"unsupported"` category.
- **No channel concept.** LXMF uses point-to-point identity hashes; `channels=False` and `channel_index` is always `None`.
- **Reticulum singleton constraint.** `RNS.Reticulum()` raises `OSError` if already running; the session uses `get_instance()` to reuse existing instances. Multiple sessions share the same Reticulum transport.
- **No LXMRouter callback deregistration API.** Callbacks are silenced by `_stop_requested` guard and `_teardown_sdk()` nulling the router reference rather than explicit deregistration.
- **stamp_cost validation is minimal.** Non-zero values must be positive integers, but no upper bound is enforced.
- **16-byte identity hashes are not human-readable.** Downstream consumers must map hex hashes to display names externally.

---

## Duplicate-Send Risk Level

**Low–Medium.** Session-level retry (3 attempts) on transient failures can produce duplicates if the first attempt succeeded at the router level but the response was lost. However, LXMF messages carry unique hashes (`LXMessage.hash`), and the LXMRouter's own dedup mechanisms provide some protection. The adapter does not add application-level dedup.

---

## Validation Status

- Config validation enforces: non-empty `adapter_id`, valid `connection_type` (`fake`/`reticulum`), valid `default_delivery_method`, non-negative numerics, `stamp_cost` integer check, `identity_path` string-or-None, `storage_path` required for `reticulum` mode.
- Classifier tests cover text, unsupported (attachment-only), and unknown categories; bytes/str/bytearray content normalisation; hex string conversion for source_hash and message_id.
- Codec tests cover text decode, title extraction, metadata construction, MEDRE envelope extraction from fields.
- Renderer tests cover text/title rendering, metadata embedding toggle, envelope structure.
- Fields helper tests cover embed/extract round-trip, corrupt/missing envelope handling, attachment detection, envelope relations check.
- Session tests cover lifecycle (start/stop idempotency), fake mode, real mode (mocked SDK), reconnect backoff, outbound send with retry, delivery state tracking, thread bridging.

---

## Reference Libraries

| Library | Purpose                                                            | Optional                    |
| ------- | ------------------------------------------------------------------ | --------------------------- |
| `lxmf`  | LXMF Python package (`LXMRouter`, `LXMessage`, delivery constants) | Yes (`medre[lxmf]`)         |
| `RNS`   | Reticulum network stack (`Reticulum`, `Identity`, `Destination`)   | Yes (via `lxmf` dependency) |
