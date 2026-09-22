# LXMF Transport Profile

## Purpose and Role

The LXMF adapter is a **transport adapter** (`AdapterRole.TRANSPORT`) that connects to a locally-running Reticulum instance via the `RNS` and `lxmf` packages, or operates in a test fake mode. It bridges inbound LXMF messages into the MEDRE canonical event stream and delivers outbound rendered payloads to the LXMRouter for asynchronous mesh delivery.

The adapter delegates all SDK interaction to `LxmfSession`. The session is the **sole owner** of `RNS.Reticulum`, `RNS.Identity`, and `LXMF.LXMRouter` instances. The adapter owns semantic conversion (classification, codec decode, event publishing).

**Platform identifier:** `lxmf`

---

## Configuration Fields

| Field                       | Type                                                     | Default      | Description                                                                                                                                         |
| --------------------------- | -------------------------------------------------------- | ------------ | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `adapter_id`                | `str`                                                    | _(required)_ | Unique adapter instance identifier                                                                                                                  |
| `connection_type`           | `Literal["fake","reticulum"]`                            | `"fake"`     | Connection mode                                                                                                                                     |
| `display_name`              | `str`                                                    | `""`         | Display name for LXMF announces                                                                                                                     |
| `stamp_cost`                | `int`                                                    | `8`          | Inbound stamp cost (0 = no stamp; valid range 0..254)                                                                                               |
| `default_delivery_method`   | `Literal["direct","opportunistic","propagated","paper"]` | `"direct"`   | Default LXMF delivery method                                                                                                                        |
| `outbound_propagation_node` | `str \| None`                                            | `None`       | 16-byte outbound propagation-node destination hash encoded as 32 hex characters                                                                     |
| `origin_label`              | `str`                                                    | `""`         | Platform-neutral operator-defined source label for relay prefixes                                                                                   |
| `default_channel`           | `int`                                                    | `0`          | Default channel index (informational; LXMF has no channel concept)                                                                                  |
| `message_delay_seconds`     | `float`                                                  | `0.5`        | Minimum delay between outbound messages (pacing)                                                                                                    |
| `metadata_embedding`        | `bool`                                                   | `True`       | Embed MEDRE metadata envelopes in LXMF fields                                                                                                       |
| `identity_path`             | `str \| None`                                            | `None`       | Path to Reticulum identity file; auto-generated if `None`                                                                                           |
| `reticulum_config_dir`      | `str \| None`                                            | `None`       | Explicit Reticulum configuration directory for embedded/local isolation (e.g. one RNodeInterface, `share_instance = No`); `None` = SDK discovery    |
| `storage_path`              | `str \| None`                                            | `None`       | **Required** when `connection_type="reticulum"` — config validation raises `LxmfConfigError`; session start raises `LxmfConnectionError` without it |
| `announce_interval_seconds` | `float`                                                  | `600.0`      | Interval in seconds between periodic LXMF announces; `0` disables                                                                                   |
| `lxmf_relay_prefix`         | `str`                                                    | `""`         | Relay prefix template for outbound body text (empty = no prefix; see §Relay Attribution Prefix)                                                     |

---

## Capabilities

Machine-readable capability declaration: [`lxmf-capabilities.json`](lxmf-capabilities.json)

> Capability levels map to the CapabilityLevel type alias (adapter-runtime.md §6.2):
> `"native"` = `TRUE`, `"fallback"` = degraded inline text, and
> `"unsupported"` = `FALSE`.

| Capability          | Value                               |
| ------------------- | ----------------------------------- |
| text                | `True`                              |
| title               | `True`                              |
| threads             | `"fallback"`                        |
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
outbound message body text when a relay prefix is configured.

**Configuration:** `lxmf_relay_prefix` (string, default `""`) on `LxmfConfig`.
When empty, no prefix is prepended. The runtime resolves the prefix per target
adapter — the LXMF renderer is **target-aware**: the prefix template comes from
the target LXMF adapter's config, not from a collapsed single-prefix model.
`{origin_label}` within the template is resolved from the source adapter's
config via the runtime source-attribution registry.

**Template syntax:** `{placeholder}` variables resolved by the shared core
formatter (`format_relay_prefix`) against `RelayAttribution` extracted from
the source event. Operators SHOULD prefer `{origin_label}` for
cross-platform prefix templates — `origin_label` is the MEDRE-generic source
label. `{origin_label}` is resolved through a precedence chain: route-level
`source_origin_label` (or `dest_origin_label` for reverse legs) takes
priority over the source adapter's config-level `origin_label`. See the
Meshtastic Transport Profile §Relay Attribution Prefix for the authoritative
list of supported template variables.

**Default:** `""` (no prefix). LXMF sender identity is a hex Reticulum
identity hash — templates referencing `{sender}` or `{sender_short}` resolve
to empty strings for LXMF-origin events. Operators SHOULD prefer
`{origin_label}` or `{sender_id}` for LXMF-bound prefixes.

**Order of operations:** The MEDRE metadata envelope is embedded in the
LXMF `fields` dict first; the relay prefix is then prepended to the
content body text; character-budget truncation (`max_text_chars`,
default 16384) is applied last. The rendered prefix counts toward the
character budget.

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

## Sender Identity Projection

The LXMF adapter projects LXMF-native sender identity into the generic
`RelayAttribution` sender fields (see
[Routing and Delivery §17.5.9](../routing-delivery.md#1759-generic-sender-identity-semantics)).
Projection is owned by `project_lxmf_attribution`; core rendering
consumes only the generic fields.

At ingress, each message carries a 16-byte `source_hash` (a truncated
SHA-256 of the sender Reticulum public key, hex-encoded). The message
hash is content-addressed. The attribution module returns
`dict[str, str | None]`, matching the Meshtastic and MeshCore pattern.

### Display-Name Capture

At ingress the adapter resolves the sender display name before codec
decode. Two sources are consulted, in precedence order:

1. **Message-carried `source_name`** — `_normalise_inbound_message`
   reads `getattr(message, "source_name", None)` without issuing a
   network call. The current LXMF library does not populate
   `source_name` on `LXMessage`, so this source is empty in practice.
   When a value is present, the codec maps it to `display_name` under `native.lxmf`.
2. **Announce-cache resolution** — when `source_name` is empty, the
   adapter calls `session.resolve_display_name(source_hash)`, which
   performs a synchronous local read of
   `RNS.Identity.known_destinations` via
   `RNS.Identity.recall_app_data(dest_hash_bytes)` and
   `LXMF.display_name_from_app_data(app_data)`. No network call is
   issued. The resolved value is injected into the packet's
   `source_name` so the codec projects it into `native.lxmf.display_name`.

The adapter enriches the packet at ingress only when the message does
not already carry a display name. Fake mode (no real SDK) yields no
display name: `resolve_display_name` returns `None` because the SDK
objects are absent.

### Projection Rules

| Generic field               | Source                                                              |
| --------------------------- | ------------------------------------------------------------------- |
| `source_sender_id`          | `normalize_source_hash(source_hash)` (bytes/str → canonical hex)    |
| `source_sender_label`       | `native.lxmf.display_name` only (opaque hash never becomes label)   |
| `source_sender_short_label` | `native.lxmf.short_name`, else compact(`native.lxmf.display_name`)  |
| `source_sender_handle`      | Not produced (the Reticulum hash is exposed via `source_sender_id`) |

When no display name is present, both label fields are `None`. The
opaque `source_hash` never populates `source_sender_label`, so `{sender}`
renders empty rather than a truncated hash. Operators who want the hash
in a prefix use `{sender_id}`. The default `lxmf_relay_prefix` is `""`;
templates referencing `{sender}` or `{sender_short}` resolve to empty
strings when no display name is captured (neither message-carried nor
announce-resolved).

Per the opacity rule ([§17.5.9](../routing-delivery.md#1759-generic-sender-identity-semantics)),
a Reticulum hash is not a label.

### Announce-Based Enrichment

LXMF display names live in the sender Identity `announce` `app_data`,
not in messages. The session resolves the sender display name from the
local RNS announce cache via `resolve_display_name(source_hash)`. This
performs a synchronous local read of
`RNS.Identity.known_destinations` — no network call. The session reads
`app_data` via `RNS.Identity.recall_app_data(dest_hash_bytes)` and
parses it via `LXMF.display_name_from_app_data(app_data)`, returning the
stripped display name or `None`. The method never raises.

**Precedence:** message-carried `source_name` (if non-empty) >
announce-cache resolved display name > `None`. The adapter enriches the
packet at ingress only when the message does not already carry a
display name.

The resolution MAY return a stale name if the peer has renamed since
the last heard announce. Enrichment is observational and MUST NOT be
treated as delivery or receipt evidence.

The opaque `source_hash` MUST NOT be promoted to `source_sender_label`.
It remains available as `source_sender_id` (`{sender_id}`). Operators
who want the hash in a prefix use `{sender_id}`.

The announce loop diagnostics are preserved and unaffected.

Identity labels may appear in rendered messages and renderer-local
metadata; enrichment is observational and is not delivery evidence.
Diagnostics expose no secrets (no identity material, no private keys, no
raw RNS or LXMF objects). See
[Routing and Delivery §17.5.10](../routing-delivery.md#17510-identity-enrichment-diagnostics-and-privacy)
for the cross-transport policy.

---

## Canonical Native Metadata

Inbound LXMF events persist one versioned object at
`metadata.native.data["lxmf"]`. The object captures source/destination hashes,
message identity, timestamp/title descriptors, delivery method, field presence,
and announce-derived display labels when available. The normative machine
contract is
[`lxmf-native-metadata.schema.json`](../../schemas/lxmf-native-metadata.schema.json),
and the representative payload is
[`lxmf-native-metadata-example.json`](../../schemas/examples/lxmf-native-metadata-example.json).
The codec builder in `medre.adapters.lxmf.event_shape` is the source
implementation authority. Flat LXMF event metadata is not an alternate shape.

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

`AdapterDeliveryResult.delivery_status` is `"sent"` for all LXMF deliveries,
meaning the message was handed to the local LXMRouter. This does **not** mean
confirmed delivery to the recipient. `metadata["lxmf"]["delivery_state"]` is
the **initial** state observed at local handoff (typically `outbound` or
`generating`), not the final provider state. Terminal states (`delivered`,
`failed`, `rejected`, `cancelled`) arrive later through SDK callbacks and are
persisted as post-handoff evidence in `delivery_observations`; operators
should not search receipt metadata for the final transport state.

| State        | Meaning                             |
| ------------ | ----------------------------------- |
| `generating` | Message being constructed           |
| `outbound`   | Queued for delivery                 |
| `sending`    | Actively transmitting               |
| `sent`       | Sent to network (not yet confirmed) |
| `delivered`  | LXMF reports delivery completion    |
| `failed`     | Permanent delivery failure          |
| `rejected`   | Rejected by recipient               |
| `cancelled`  | Cancelled by sender                 |
| `unmapped`   | Unrecognised state from SDK         |

Callback-emitted delivery updates are tracked via `_on_delivery_state_update`.
The pinned LXMF SDK exposes successful delivery/progression through the message
delivery callback and exposes some failures through a separate failed callback;
MEDRE registers both. A terminal state is reported to core only when the SDK
actually emits a callback carrying that state. MEDRE does not poll private SDK
state or infer unreported `rejected`/`cancelled` transitions.

**Delivery state is durable evidence, not lifecycle authority.** The LXMF
adapter still returns `delivery_status="sent"` with `confirmation_level` of
`local_queue` when the local `LXMRouter` accepts the message. A later terminal
SDK callback is appended to `delivery_observations` for the exact outbox
attempt; it does not rewrite the receipt or terminal outbox state. The
`delivery_receipts` capability remains `False` because MEDRE does not model
these provider callbacks as delivery receipts.
The adapter captures the message hash and state at callback time before
crossing from the SDK thread to the event loop; subsequent changes to the SDK
message object do not change the observation.

**Crash window.** Correlation between an in-flight LXMF message and its exact
outbox attempt is process-local until a terminal callback is received. Once an
observation is appended it is durable, but a hard process crash after local
handoff and before the terminal callback can lose that later provider fact.
MEDRE does not reconstruct or invent a terminal state after restart when LXMF
does not re-emit one. The original `sent/local_queue` receipt remains truthful.
During a retry, a terminal callback for the new attempt can also be lost if it
arrives before the outbox row advances its attempt number at finalization.

MEDRE persists LXMF `delivered` as the provider state but keeps the observation
`confirmation_level="unknown"`. It does not independently upgrade that state
to `end_to_end`; provider terminology and MEDRE evidence strength are separate
claims.

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
   - Quiesces the owned router, deregisters its Reticulum destinations and
     announce handlers, then releases identity/router/Reticulum references.
   - Clears outbound tracking and nulls loop/callback references so late SDK callbacks are dropped.
   - Idempotent.

**Thread bridging:** `LXMRouter` invokes delivery callbacks on Reticulum I/O threads. The session normalises the message (pure CPU, thread-safe) then schedules the adapter callback on the captured asyncio loop via `call_soon_threadsafe()`. The adapter's `_on_packet` then uses `asyncio.create_task` safely on the correct loop.

---

## Health Contract

`health_check()` reports **local** liveness only — the MEDRE-owned
session and its `LXMRouter` lifecycle flags:

| State     | Condition                                                                 |
| --------- | ------------------------------------------------------------------------- |
| `healthy` | Adapter started and the session reports `router_running` and `connected`. |
| `failed`  | Adapter started but the local session/router is missing or torn down.     |
| `unknown` | Adapter not started; no claim is made.                                    |

A started MEDRE flag alone is never presented as transport health: if
the owned session is stopped underneath a started adapter (observable
through the public session lifecycle), health reports `failed`.

Peer reachability is **not** part of the health string. The declared pinned
LXMF/RNS SDKs expose no supported
peer-liveness API — `LXMRouter.compile_stats()` returns `None` unless
propagation-node mode is enabled, and per-peer link state
(`delivery_link_available`) is per-destination link bookkeeping, not a
transport-health signal. MEDRE never sends probes merely to answer
health, so `diagnostics()` carries an explicit, always-`"unknown"`
`peer_reachability` marker alongside
`health_scope="local_session_and_router"`. A missing or broken local
router reports `failed`; the absence of a public peer-liveness API is
reported as `unknown`, not manufactured into a failure or a success.

Reticulum exposes no transport-down event (see
[`transport-limitations.md`](../appendices/transport-limitations.md)),
so a silently lost transport is first noticed when an outbound send
fails; recovery relies on that send's bounded local retry. This is a
documented observability boundary, not an invented health state.

---

## Diagnostics Keys

`adapter.diagnostics()` returns (no secrets, no identity material, no raw RNS/LXMF objects):

| Key                                   | Type          | Description                                                                                     |
| ------------------------------------- | ------------- | ----------------------------------------------------------------------------------------------- |
| `adapter_id`                          | `str`         | Adapter identifier                                                                              |
| `platform`                            | `str`         | `"lxmf"`                                                                                        |
| `started`                             | `bool`        | Adapter started flag                                                                            |
| `mode`                                | `str`         | Config connection type                                                                          |
| `health_scope`                        | `str`         | Always `"local_session_and_router"` — the health string describes local lifecycle only          |
| `peer_reachability`                   | `str`         | Always `"unknown"` — no supported peer-liveness API; MEDRE never probes merely to answer health |
| `session.connected`                   | `bool`        | Session connected                                                                               |
| `session.router_running`              | `bool`        | LXMRouter operational                                                                           |
| `session.reconnecting`                | `bool`        | Reconnect in progress                                                                           |
| `session.reconnect_attempts`          | `int`         | Consecutive reconnect attempts                                                                  |
| `session.transient_delivery_failures` | `int`         | Transient send errors                                                                           |
| `session.permanent_delivery_failures` | `int`         | Permanent send errors                                                                           |
| `session.last_error`                  | `str \| None` | Last error description                                                                          |
| `session.mode`                        | `str`         | Config connection type (mirrored)                                                               |

Session also exposes `diagnostics()` and `delivery_state_counts()` with additional fields: `last_message_time`, `known_path_count`, `propagation_enabled`, `pending_delivery_count`.

---

## Relation Degradation Behavior

| Relation type | Capability level | Strategy        | Rendering path                                                                 |
| ------------- | ---------------- | --------------- | ------------------------------------------------------------------------------ |
| Replies       | `"unsupported"`  | `skip`          | No delivery. Reply-carrying events targeting this adapter are suppressed.      |
| Reactions     | `"unsupported"`  | `skip`          | No delivery. Reaction events targeting this adapter are suppressed.            |
| Edits         | `"unsupported"`  | `skip`          | No delivery. Edit events targeting this adapter are suppressed.                |
| Deletes       | `"unsupported"`  | `skip`          | No delivery. Delete events targeting this adapter are suppressed.              |
| Threads       | `"fallback"`     | `fallback_text` | Deterministic inline thread context; native thread emission is not advertised. |

LXMF has no verified native relation support beyond basic text delivery. Replies,
reactions, edits, and deletes remain unsupported planning-time skips, while
`threads="fallback"` selects deterministic inline thread context during normal
live planning. Plain message kinds continue to deliver directly.

When `fallback_text` is selected for a relation, the LXMF renderer produces its
native payload format with the relation context embedded as inline text. Under
`fallback_text`, the MEDRE fields envelope (`fields[0xFD]`) omits structured
relations — its `relations` key is an empty list (`[]`). The only relation
representation is the inline text appended to the content body. This is a
deliberate degradation rule: the envelope retains provenance metadata
(`event_id`, `source_adapter`, lineage) but not relation data, preventing duplicate
representation as both structured fields and inline text.

**Thread capability:** LXMF has no native thread primitive; thread relations are
deterministically degraded to inline fallback text.

**Payload requirement:** The LXMF renderer produces LXMF-native payloads (`content` body, optional `title`, optional MEDRE metadata envelope in `fields[0xFD]`). The adapter dispatches these payloads to the LXMRouter via `handle_outbound` without modification.

---

## Known Limitations

- **No reply or reaction support.** Capabilities declare both as `"unsupported"`. LXMF has no built-in threading mechanism; however, relation reconstruction from the MEDRE fields envelope (`0xFD`) is implemented via `_reconstruct_relations` in `codec.py`. The codec reconstructs `EventRelation` objects from the envelope's `relations` list at decode time. FIELD_THREAD (`0x08`) is explicitly excluded — MEDRE does not read or write the LXMF native thread field.
- **Destination routing follows one documented precedence.** The payload's
  `destination_hash` is the recipient's 16-byte LXMF delivery destination
  hash (32 hex chars), resolved from the route target in this order:
  1. A structured `dest_destination` (`kind: "lxmf_destination"` with
     `destination_hash`) on the route — the normative identity/hash
     addressing form (routing-delivery §2.3/§2.4), threaded through
     `RouteTarget.destination` into the rendering context.
  2. Otherwise the route's `dest_channel`, which for LXMF routes acts as
     the transport-defined address selector carrying the same hash.
     A route configures exactly one of the two (`dest_destination` and
     `dest_channel`/`dest_room` are mutually exclusive at load time). An
     empty result (route with neither) keeps the historical empty destination
     and delivery fails permanently with "cannot recall identity" rather than
     guessing a recipient. The structured destination is durable: it is
     persisted in outbox metadata and reconstructed by retry.
- **Attachment-only messages are classified but not relayed.** `has_fields` without `content` yields `"unsupported"` category.
- **No channel concept.** LXMF uses point-to-point identity hashes; `channels=False` and `channel_index` is always `None`.
- **Reticulum singleton constraint.** `RNS.Reticulum()` raises `OSError` if already running; the session uses `get_instance()` to reuse existing instances. Multiple sessions share the same Reticulum transport.
- **No LXMRouter delivery-callback deregistration API.** `_stop_requested`
  silences late callbacks and `_teardown_sdk()` runs the owned router
  `exit_handler()` to detach delivery-destination callbacks/links. MEDRE then
  deregisters only that router's Reticulum destinations and announce handlers
  so a persistent identity can restart in the same process. The router daemon
  job loop has no public join/stop primitive and remains dormant until process
  exit.
- **stamp_cost follows the LXMF SDK range.** `0` disables stamps; positive
  values are limited to `1..254`.
- **Propagated delivery requires an outbound node.** `LXMRouter` starts with
  no outbound propagation node selected. MEDRE configures
  `outbound_propagation_node` through `set_outbound_propagation_node()` and
  rejects a propagated send when no node is selected.
- **16-byte identity hashes are not human-readable.** The announce-cache lookup resolves display names for locally-known senders; when the sender is unknown to the announce cache, the hash remains the only identifier and is exposed via `source_sender_id` (`{sender_id}`).

---

## Duplicate-Send Risk Level

**Low–Medium.** Session-level retry (3 attempts) on transient failures can produce duplicates if the first attempt succeeded at the router level but the response was lost. However, LXMF messages carry unique hashes (`LXMessage.hash`), and the LXMRouter's own dedup mechanisms provide some protection. The adapter does not add application-level dedup.

---

## Local Two-Instance Relation Roundtrip

Cross-instance relation preservation is exercised by a reproducible,
fully local regression harness (no external network, hardware, or
retained state):

```bash
uv sync --extra lxmf
uv run pytest "tests/integration/test_lxmf_local_integration.py::test_relation_preserved_across_two_local_instances" -m "local_integration or lxmf_sdk"
```

The harness runs two distinct OS processes: instance A renders a
relation-bearing canonical event via `LxmfRenderer` (MEDRE envelope
under `FIELD_CUSTOM_META`/`0xFD`) and delivers through its real
`LxmfAdapter`; instance B decodes through the real inbound
adapter/codec path. The instances are joined only by a loopback
Reticulum `UDPInterface` pair (127.0.0.1, per-run ephemeral ports,
per-process temp `HOME` and router storage, ephemeral identities).
Assertions cover decoded semantic equality (relation kind, referenced
canonical event id, referenced native identity, body/title) separately
from receipt, plus cross-instance identity: B's decoded
`source_transport_id` equals A's registered delivery destination hash,
and A's returned LXMF message hash equals B's decoded message id. The
runtime-resolved SDK versions are recorded in the result and asserted
against the pins.

**Evidence status: passed on the declared pinned SDKs** (see Validation
Status below). Relation fidelity remains "implemented but not interop-proven"
for anything beyond this local loopback tier — see
[`known-limitations.md`](../appendices/known-limitations.md). This is local-SDK
evidence only; it is not live-network, hardware, or external interoperability
proof.

---

## Validation Status

- Config validation enforces: non-empty `adapter_id`, valid `connection_type`
  (`fake`/`reticulum`), valid `default_delivery_method`, non-negative numerics,
  `stamp_cost` range `0..254`, optional 32-hex-character
  `outbound_propagation_node`, a required outbound node for real-mode propagated
  defaults, `identity_path` string-or-None, and `storage_path` required for
  `reticulum` mode.
- Classifier tests cover text, unsupported (attachment-only), and unknown categories; bytes/str/bytearray content normalisation; hex string conversion for source_hash and message_id.
- Codec tests cover text decode, title extraction, metadata construction, MEDRE envelope extraction from fields.
- Renderer tests cover text/title rendering, metadata embedding toggle, envelope structure.
- Fields helper tests cover embed/extract round-trip, corrupt/missing envelope handling, attachment detection, envelope relations check.
- Session tests cover lifecycle (start/stop idempotency), fake mode, real mode (mocked SDK), reconnect backoff, outbound send with retry, delivery state tracking, thread bridging, and `resolve_display_name` announce-cache lookup.
- Health lifecycle tests cover the local-scope contract: `healthy` only while the owned session reports its router running and connected, `failed` when the session is torn down under a started adapter, `unknown` when not started, and `peer_reachability`/`health_scope` diagnostics at every phase.
- The two-instance relation roundtrip
  (`tests/integration/test_lxmf_local_integration.py::test_relation_preserved_across_two_local_instances`)
  passed on the declared pinned SDKs in a locked disposable venv at the
  2026-09-18 gate: 3/3 local-integration tests were green and the standalone
  probe `PYTHONPATH=src python -m tests.helpers.lxmf_local_probe relation
<tmpdir>` exited 0 with every verdict true (relation kind/target/native-ref,
  title and body equality, envelope event-id equality, B source identity, and
  A-native-id↔B-message-id correlation). This is local loopback evidence only —
  no external mesh, hardware, or live-network claim.

---

## Reference Libraries

| Library | Purpose                                                            | Optional                    |
| ------- | ------------------------------------------------------------------ | --------------------------- |
| `lxmf`  | LXMF Python package (`LXMRouter`, `LXMessage`, delivery constants) | Yes (`medre[lxmf]`)         |
| `RNS`   | Reticulum network stack (`Reticulum`, `Identity`, `Destination`)   | Yes (via `lxmf` dependency) |

**Dependency-version authority:** `pyproject.toml` declares the exact LXMF/RNS
pins; `uv.lock` records the resolved artifact graph. The local integration tier
records runtime-resolved versions in its result payload and checks them against
the declarations, so environment drift surfaces as a named failure instead of
silent evidence skew.
