# Matrix Transport Profile

## Purpose and Role

The Matrix adapter is a **presentation adapter** (`AdapterRole.PRESENTATION`) that connects to a Matrix homeserver via the `mindroom-nio` async client library. It bridges inbound Matrix room messages into the MEDRE canonical event stream and delivers outbound rendered payloads back to Matrix rooms.

The adapter delegates all client lifecycle (creation, login, sync, teardown) to `MatrixSession`. Semantic conversion (codec decode/encode, event classification) is owned by the adapter itself.

**Platform identifier:** `matrix`

---

## Configuration Fields

| Field                                      | Type                                                   | Default       | Description                                                                     |
| ------------------------------------------ | ------------------------------------------------------ | ------------- | ------------------------------------------------------------------------------- |
| `adapter_id`                               | `str`                                                  | _(required)_  | Unique adapter instance identifier                                              |
| `homeserver`                               | `str`                                                  | _(required)_  | Matrix homeserver URL (`https://…` or `http://…`)                               |
| `user_id`                                  | `str`                                                  | _(required)_  | Fully-qualified Matrix user ID (must start with `@`)                            |
| `device_id`                                | `str \| None`                                          | `None`        | Internal — discovered via `whoami()` when needed                                |
| `access_token`                             | `str`                                                  | `""`          | Access token for authentication; sidecar fallback supported                     |
| `room_allowlist`                           | `set[str] \| None`                                     | `None`        | Optional set of room IDs to accept; `None` = all rooms                          |
| `metadata_embedding_mode`                  | `str`                                                  | `"safe"`      | How metadata is embedded in messages                                            |
| `store_path`                               | `str \| None`                                          | `None`        | Internal — derived under `{state}/adapters/{id}/matrix/store`                   |
| `sync_timeout_ms`                          | `int`                                                  | `30000`       | Long-polling sync timeout in milliseconds                                       |
| `sync_stale_timeout_seconds`               | `float`                                                | `300.0`       | Max durable-sync silence before active loop recycle; `0` disables watchdog      |
| `megolm_key_request_rate_limit_per_minute` | `int`                                                  | `30`          | Max missing-room-key to-device attempts per rolling minute                      |
| `megolm_key_request_max_inflight`          | `int`                                                  | `4`           | Max concurrent detached missing-room-key recovery tasks                         |
| `encryption_mode`                          | `Literal["plaintext","e2ee_required","e2ee_optional"]` | `"plaintext"` | E2EE policy                                                                     |
| `require_encrypted_rooms`                  | `bool`                                                 | `False`       | If `True`, reject plaintext rooms; invalid with `encryption_mode="plaintext"`   |
| `auto_join_rooms`                          | `tuple[str, ...]`                                      | `()`          | Canonical room IDs (`!localpart:server`) to auto-join on startup and via invite |
| `origin_label`                             | `str`                                                  | `""`          | Platform-neutral operator-defined source label for relay prefixes               |
| `relay_prefix`                             | `str`                                                  | `""`          | Target-local prefix template for Matrix outbound body text (empty = no prefix)  |

---

## Capabilities

Machine-readable capability declaration: [`matrix-capabilities.json`](matrix-capabilities.json)

> Capability levels map to the CapabilityLevel type alias (adapter-runtime.md §6.2):
> `"native"` = `TRUE`, `"fallback"` = degraded inline text, and
> `"unsupported"` = `FALSE`.

| Capability        | Value      |
| ----------------- | ---------- |
| text              | `True`     |
| threads           | `"native"` |
| replies           | `"native"` |
| reactions         | `"native"` |
| edits             | `"native"` |
| deletes           | `"native"` |
| attachments       | `False`    |
| store_and_forward | `False`    |
| direct_messages   | `True`     |
| channels          | `True`     |
| topic_rooms       | `True`     |

### Mutation eligibility (native edits and deletes)

Native `edits` and `deletes` are **mutation** operations: they act on a
previously delivered native event, so delivery requires a destination-scoped
binding proof, not merely the capability claim. Core relation binding computes
a `RelationTargetFact` per delivery target, and the renderer/adapter treat it
as the sole mutation authority:

| Fact status           | Edit/delete behavior                                                               |
| --------------------- | ---------------------------------------------------------------------------------- |
| `bound_owned`         | Delivered natively (`m.replace` edit / `redact_event` redaction).                  |
| `bound`               | Suppressed for mutations (referential relations still render); reason carries why. |
| `unresolved_target`   | Suppressed — the target canonical event is unknown or not stored.                  |
| `out_of_scope`        | Suppressed — target stored but zero native copies in this destination.             |
| `ambiguous`           | Suppressed — multiple distinct native targets match; MEDRE never guesses.          |
| `not_authorized`      | Suppressed — authorship/ownership proof failed (e.g. inbound-only copy).           |
| `binding_unavailable` | Suppressed — storage read failure; always fail closed.                             |

Suppressed mutation deliveries produce lifecycle receipts with
`status="suppressed"` / `failure_kind=CAPABILITY_SUPPRESSED` and a stable
`relation_target_not_bindable:<reason>` error string. No adapter call, no
fallback ordinary message, and no sent receipt/native ref is produced. Binding
is recomputed from current stored facts on every attempt (including replays),
so a previously-owned target that became ambiguous fails closed on retry.

Unresolved targets therefore degrade **silently-but-evidently** (suppression
with a machine-readable reason), never as fabricated messages or guessed
targets. The renderer additionally fails closed (raises) if a mutation ever
reaches native rendering without a `bound_owned` fact — defense-in-depth
against a missing core gate.

---

## Relay Attribution Prefix

The Matrix renderer prepends a human-readable relay attribution prefix to the
message body when a prefix template is available. The prefix template is
resolved from the target-local configuration:

1. **Target-local:** `MatrixConfig.relay_prefix` (string, default `""`). When
   non-empty, this template is used for all Matrix outbound renders. This is
   the target-local prefix — it lives on the adapter that owns the rendering,
   not on the source adapter.

**Template syntax:** `{placeholder}` variables resolved by the shared core
formatter (`format_relay_prefix`) against `RelayAttribution` extracted from
the source event. Operators SHOULD prefer `{origin_label}` in cross-platform
prefix templates — `origin_label` is the MEDRE-generic source label available
on all adapter configs. See the Meshtastic Transport Profile
§Relay Attribution Prefix for the authoritative list of supported template
variables and formatting rules.

**Renderer lookup:** `{origin_label}` is resolved through a precedence chain:
route-level `source_origin_label` (or `dest_origin_label` for reverse legs)
from the matched route's expansion context takes priority; when no route-level
label is set, the renderer falls back to the source adapter's `origin_label`
config via the runtime source-attribution registry; when neither source
provides a label, the variable resolves to empty string.

**Application points:**

1. Direct mode body (prefix only, no truncation).
2. Fallback-text mode body (before truncation).
3. Reaction emote body (via `_format_reaction_prefix`).

**When no prefix is found** (`MatrixConfig.relay_prefix` is empty), no prefix
is prepended.

**Truncation:** The Matrix renderer has no constrained radio byte budget.
Prefix length is unconstrained in the renderer, though Matrix homeservers
impose their own event size limits. When a `RenderingContext` text budget is
active (`max_text_chars` or `max_text_bytes`), the prefix counts toward that
budget **in the fallback-text path**.

**Metadata keys** (when prefix is configured):

| Key                              | Value                                  |
| -------------------------------- | -------------------------------------- |
| `relay_prefix_template`          | Original template string               |
| `relay_prefix_rendered`          | Rendered prefix string                 |
| `relay_prefix_variables_used`    | Tuple of template variables resolved   |
| `relay_prefix_missing_variables` | Tuple of variables that resolved empty |
| `relay_prefix_unknown_variables` | Tuple of unknown placeholder names     |
| `relay_prefix_formatting_error`  | Error string or `None`                 |

**Attribution caveat:** The prefix is human-readable attribution only. It
does not constitute delivery evidence. The MEDRE metadata envelope
(`medre.envelope`) remains the authoritative source for machine-readable
provenance. Matrix `room_send` success confirms local homeserver acceptance
only — it does not confirm remote delivery or federation fan-out.

---

## Sender Identity Projection

The Matrix adapter projects Matrix-native sender identity into the
generic `RelayAttribution` sender fields (see
[Routing and Delivery §17.5.9](../routing-delivery.md#1759-generic-sender-identity-semantics)).
Projection is owned by `project_matrix_attribution` in the Matrix
adapter package; core rendering consumes only the generic fields.

At ingress, the codec records Matrix-native identity under the versioned
`metadata.native.data["matrix"]` object. The sender MXID is stored as `sender`.
After codec decode, the adapter enriches `sender_display_name` from already-synced
homeserver member state. No extra network call is issued during enrichment. When
no member display name is available, the adapter records the MXID as the live
`sender_display_name` fallback.

| Generic field               | Source                                      |
| --------------------------- | ------------------------------------------- |
| `source_sender_id`          | `native.matrix.sender` (full MXID)          |
| `source_sender_handle`      | `native.matrix.sender` (full MXID)          |
| `source_sender_label`       | `native.matrix.sender_display_name` only    |
| `source_sender_short_label` | MXID localpart via `extract_mxid_localpart` |

The dispatch projection reads only the current Matrix native schema version. An
empty or absent display name leaves `source_sender_label` as `None`; the MXID
localpart is never substituted into that label by the projection itself.

`extract_mxid_localpart` is deterministic for malformed MXIDs. An empty localpart
after `@` (for example `@:domain`) returns `""` rather than including the colon
and domain. Empty or `None` display names stay `None`; the literal text `"None"`
is never rendered.

### mmrelay Wire-Key Boundary

The Matrix display name is never converted to MMRelay wire keys
(`KEY_LONGNAME` / `KEY_SHORTNAME`). Captured MMRelay scalar fields live under
`metadata.native.data["interop"]["mmrelay"]`; they are external wire
interoperability, not Matrix-native identity fields.

### mmrelay Packet-ID Resolution

`MatrixRenderer._resolve_mmrelay_packet_id` reads the current Meshtastic native
shape at `metadata.native.data["meshtastic"]["packet_id"]`. It does not read an
older flat or dotted MEDRE metadata representation.

### Display-Name Staleness

Matrix display names come from homeserver sync state held by the
`mindroom-nio` client. They converge with homeserver member state and
may lag profile changes on the homeserver. Display-name staleness is an
inherent property of homeserver member-state sync, not a MEDRE-managed
concern.

Identity labels may appear in rendered messages and renderer-local
metadata; enrichment is observational and is not delivery evidence.
Diagnostics expose no secrets (access tokens, credentials, session
material) and no SDK objects. See
[Routing and Delivery §17.5.10](../routing-delivery.md#17510-identity-enrichment-diagnostics-and-privacy)
for the cross-transport policy.

---

The Matrix codec (`MatrixCodec`) decodes three inbound categories:

1. **True Matrix reactions** (`m.annotation` in `m.relates_to`) → `MESSAGE_REACTED` with a `reaction` relation targeting the annotated event.
2. **MMRelay emote reactions** (`m.emote` with `meshtastic_replyId` and `meshtastic_emoji == 1`) → `MESSAGE_REACTED` with a canonical reaction relation carrying MMRelay metadata.
3. **Regular messages** (including replies) → `MESSAGE_CREATED`. Reply fallback body is stripped.

---

## Supported Outbound Event Kinds

The Matrix renderer (`MatrixRenderer`) produces:

- **Plain text messages** — `m.room.message` with `m.text` msgtype, optional relay prefix, and MEDRE metadata envelope.
- **Native replies** — `m.relates_to.m.in_reply_to` with `event_id`, plus `KEY_REPLY_ID` when MMRelay metadata is available.
- **Native reactions** — `m.reaction` event type with `m.annotation` (a deliberately plaintext event type; see E2EE notes below).
- **Native edits** — `m.replace` / `m.new_content` room messages with exactly one relay attribution and a `"* "` fallback body, targeted at the bound original copy.
- **Native deletes** — `redact_event` operations through the dedicated redaction endpoint, guarded by the mutation-eligibility rules above.
- **MMRelay emote reaction fallback** — `m.emote` with `KEY_EMOJI=1`, `KEY_REPLY_ID`, and full mesh metadata (used when `mmrelay_compatibility=True` or no Matrix-native target exists).

---

## Native Reference Format

- **Inbound native ref:** `NativeRef(adapter=<id>, native_channel_id=<room_id>, native_message_id=<event_id>)`
- **Outbound native ref:** Returned from `deliver()` as `AdapterHandoffResult.native_message_id` (the Matrix `event_id` from `RoomSendResponse`).
- **Deterministic transaction ID:** `medre_<sha256[:32]>` computed from `result.event_id + target_adapter + target_channel + room_id`. The homeserver deduplicates within its transaction-ID window.

---

## Delivery Semantics

**Local acceptance:** `deliver()` performs a synchronous `room_send` with bounded retry (up to 3 attempts, exponential backoff 500 ms → 1 s → 2 s with ±25 % jitter). Success means the homeserver accepted the event and returned an `event_id`.

**Remote delivery:** The homeserver is responsible for fan-out. MEDRE treats the returned `event_id` as confirmation of _local acceptance only_ — it does not track whether other federation servers or clients received the event.

**Rate-limit handling:** `M_LIMIT_EXCEEDED` / HTTP 429 raises `AdapterSendError(transient=True)` immediately so the pipeline retry worker can honour `retry_after_ms`.

**Permanent errors** (`M_FORBIDDEN`, `M_NOT_FOUND`, encrypted-room without crypto, etc.) raise `AdapterPermanentError` without retry.

---

## Session Lifecycle

1. **Disconnected** — Initial state; `session=None`.
2. **Connecting** — `start()` creates `MatrixSession`, restores login, registers
   mindroom-nio admission/response callbacks, restores MEDRE's committed Classic
   Sync cursor, and starts the supervised sync task.
3. **Syncing** — mindroom-nio owns Classic Sync iteration, request execution,
   key sequencing, decryption, limited-timeline recovery, event provenance, and
   homeserver-directed 429 handling. Provider timeout retries are disabled so a
   timeout reaches MEDRE's owning boundary promptly; sync timeouts are handled by
   the outer session supervisor.
4. **Durable admission** — `LIVE` and `RECOVERED` timeline events are atomically
   admitted for routing; `HISTORY` is durably recorded with routing suppressed. An
   admission failure rejects the nio callback so the event remains replayable.
5. **Checkpoint commit** — after a successful response has no unaccepted relevant
   events, MEDRE persists `next_batch` and recovery-abandonment metadata, then calls
   `acknowledge_classic_sync()`. nio does not persist the Classic cursor.
6. **Supervising / reconnecting** — adapter health remains `degraded` until
   the first successful sync response; authentication alone is not Matrix
   readiness. `MatrixSession` then watches durable Classic Sync progress. If the
   configured stale-progress deadline expires, MEDRE asks nio to stop the current
   `sync_forever()` owner, cancels it, and verifies it terminated before the outer
   recovery path may start another loop. Retryable sync-loop failures use the same
   reset-to-committed-cursor path and continuous outer backoff (1 s → 2 s → 4 s →
   … with ±25 % jitter and every delay clamped to 60 s) until recovery or adapter
   shutdown. There is no finite transient-failure attempt ceiling. Unexpected
   non-operational exception classes fail closed rather than being retried forever.
   A stale loop that ignores cancellation, or a durable-state reset that cannot be
   completed safely, also fails closed; MEDRE never overlaps two sync owners on one
   client.
7. **Stopped** — `stop(timeout)` asks nio to stop `sync_forever()`, cancels
   MEDRE-owned Megolm recovery and room-join tasks, drains nio client-bound request
   tasks, and closes the client under one shared absolute timeout budget. Tasks that
   ignore cancellation past that budget are detached with terminal-result ownership
   so shutdown remains bounded without producing unobserved-task warnings. The
   session is then nulled. Idempotent.

### Classic Sync ownership and recovery

MEDRE uses application-owned Classic Sync checkpointing when the adapter is created
by the runtime with storage available. The pinned mindroom-nio client is configured with
`backfill_limited_timelines=True`, `store_sync_tokens=False`,
`backfill_persist_recovery=False`, and `max_timeouts=0`. This establishes one owner
per concern:

- mindroom-nio owns Matrix request execution, server-directed 429 handling,
  parsing/decryption, event ordering, limited-timeline walking, and
  `LIVE`/`RECOVERED`/`HISTORY` provenance;
- `MatrixSession` owns timeout-level retry, loop supervision, continuous capped
  restart backoff, and health;
- MEDRE storage owns canonical admission, deduplication, pending ingress work, and
  the committed Classic cursor; and
- MEDRE's core pipeline owns routing, outbox creation, and delivery retries.

When storage is unavailable, the runtime MUST omit all three durable callbacks
(admission, checkpoint load, and checkpoint commit). In that non-durable mode,
limited-timeline recovery is disabled and mindroom-nio retains ordinary Classic
cursor ownership with `store_sync_tokens=True`; MEDRE MUST NOT advertise a partial
durability mode that cannot commit its cursor.

Protocol provenance is authoritative. The generic adapter-start timestamp filter does
not override Matrix recovery evidence. `LIVE` and `RECOVERED` events are routed;
`HISTORY` events are persisted as `suppressed_history` and are not routed. Recovery
abandonment is persisted with the checkpoint and exposed through diagnostics before
the cursor advances. After MEDRE commits a Classic cursor, it asks the pinned SDK to
acknowledge that cursor. If the SDK reports its specific staged-token mismatch while
recovery work is still active, MEDRE defers that acknowledgement instead of killing
the sync loop; unrelated protocol errors still propagate. The consecutive deferral
count is exposed as `classic_ack_deferrals` and resets after a successful ack.

The durability guarantee is intentionally narrower than exactly-once delivery: once
Matrix ingress is accepted, MEDRE retains the canonical event and durable work state
needed to resume processing after a crash. External delivery can still be at-least-once
where a target transport cannot make send plus acknowledgement atomic.

**E2EE modes and identity policy:**

- `plaintext` — No crypto; `ignore_unverified_devices=False`; no cross-signing
  reconciliation.
- `e2ee_required` — Fails if `mindroom-nio[e2e]` is not installed or the Olm/store
  subsystem is broken. Once crypto state loads, runtime performs non-destructive
  own-device cross-signing reconciliation.
- `e2ee_optional` — Attempts the same crypto + identity path; falls back to plaintext
  with `crypto_enabled=False` only when crypto startup itself fails. A cross-signing
  mismatch is diagnostic state and does not downgrade transport encryption.

Cross-signing and peer-device trust are separate policies. MEDRE cross-signs its own
current device so other Matrix clients can authenticate the bot device. Encrypted
outbound sends still use `ignore_unverified_devices=True` as an intentional permissive
peer-device policy. When the pinned provider exposes `replace_rotated_device_keys`,
MEDRE enables it for encrypted sessions so a peer device-key rotation does not wedge
the bot's permissive delivery policy. Neither setting verifies peer devices. Runtime
reconciliation has no password and cannot bootstrap or rotate master/self-signing
identity material.

When device discovery requires `whoami()`, MEDRE also checks a returned `user_id`
against the configured account and fails startup on mismatch instead of restoring an
access token under the wrong account identity.

A live undecryptable `MegolmEvent` is recorded but never forwarded into the canonical
event pipeline. If crypto, user ID, and device ID are available, MEDRE builds the
provider's missing-room-key request in a tracked background task with at most three
attempts and a ten-second per-attempt timeout. The nio sync callback MUST return without
waiting for this recovery task. Retryable transport or explicit to-device failures use
2 s then 4 s backoff; cancellation propagates, and permanent Matrix errcodes terminate
recovery immediately. Startup-history events do not trigger recovery requests.

Recovery admission has two independent bounds: `megolm_key_request_max_inflight` caps
concurrent detached recovery tasks, while
`megolm_key_request_rate_limit_per_minute` caps actual outbound to-device request
attempts in a rolling minute, including retries. These are network-admission controls,
not logging controls. Live undecryptable warnings are separately deduplicated for 60
seconds by `(room_id, session_id)`; changing that warning window does not change the
request limiter. Raw Megolm session IDs MUST NOT appear in logs or diagnostics.

---

## Diagnostics Keys

`adapter.diagnostics()` returns a dict (no secrets):

| Key                                        | Type            | Description                                            |
| ------------------------------------------ | --------------- | ------------------------------------------------------ |
| `connected`                                | `bool`          | Session has active client                              |
| `logged_in`                                | `bool`          | Client reports authenticated                           |
| `sync_task_running`                        | `bool`          | Sync asyncio task alive                                |
| `last_sync_error`                          | `str \| None`   | Last sync failure message                              |
| `store_path_configured`                    | `bool`          | E2EE store path set                                    |
| `device_id_configured`                     | `bool`          | Device ID known                                        |
| `encryption_mode`                          | `str`           | Current E2EE mode                                      |
| `crypto_enabled`                           | `bool`          | Crypto subsystem active                                |
| `last_crypto_error`                        | `str \| None`   | Last crypto error                                      |
| `encrypted_room_seen`                      | `bool`          | At least one encrypted room detected                   |
| `undecryptable_event_count`                | `int`           | MegolmEvents that could not be decrypted               |
| `megolm_recovery_attempts`                 | `int`           | Missing-room-key to-device send attempts               |
| `megolm_recovery_successes`                | `int`           | Missing-room-key requests accepted by the provider     |
| `megolm_recovery_failures`                 | `int`           | Terminal missing-room-key request failures             |
| `megolm_recovery_rate_limited`             | `int`           | Outbound key-request attempts refused by rolling limit |
| `megolm_recovery_inflight_rejected`        | `int`           | Recovery campaigns refused by max-in-flight cap        |
| `megolm_recovery_inflight`                 | `int`           | Recovery tasks currently in flight                     |
| `sync_running`                             | `bool`          | Sync loop active                                       |
| `reconnecting`                             | `bool`          | Reconnect backoff in progress                          |
| `reconnect_attempts`                       | `int`           | Consecutive reconnect attempts                         |
| `stale_sync_recoveries`                    | `int`           | Sync loops successfully recycled after stale detection |
| `last_stale_sync_at`                       | `float \| None` | Monotonic time of last stale-progress detection        |
| `classic_ack_deferrals`                    | `int`           | Consecutive deferred Classic acknowledgements          |
| `last_successful_sync`                     | `float \| None` | Monotonic time of last good sync                       |
| `checkpoint_owned_by_medre`                | `bool`          | MEDRE owns the Classic Sync checkpoint                 |
| `committed_checkpoint_present`             | `bool`          | A committed Classic cursor has been restored/stored    |
| `recovered_event_count`                    | `int`           | Recovered timeline events seen at admission            |
| `history_event_count`                      | `int`           | Cold-history timeline events seen at admission         |
| `recovery_abandoned_room_count`            | `int`           | Rooms with recorded unrecoverable history              |
| `recovery_last_abandonment`                | `str \| None`   | Identifier-free room/cause-count abandonment summary   |
| `crypto_store_loaded`                      | `bool`          | Olm/store initialised                                  |
| `olm_loaded`                               | `bool`          | Olm subsystem loaded                                   |
| `encrypted_room_count`                     | `int`           | Rooms tracked as encrypted                             |
| `plaintext_room_count`                     | `int`           | Rooms tracked as plaintext                             |
| `cross_signing_provider_supported`         | `bool`          | SDK exposes MEDRE's required cross-signing API         |
| `cross_signing_local_identity_present`     | `bool`          | Persisted local own-device identity is available       |
| `cross_signing_server_identity_present`    | `bool \| None`  | Homeserver exposes an own-account master identity      |
| `cross_signing_current_device_self_signed` | `bool \| None`  | Current device has expected self-signing signature     |
| `cross_signing_chain_status`               | `str`           | Secret-free server-visible identity-chain state        |
| `cross_signing_repair_required`            | `bool`          | Safe bootstrap/repair remains                          |
| `cross_signing_reset_required`             | `bool`          | Explicit authenticated identity recovery is required   |
| `cross_signing_last_failure_category`      | `str \| None`   | Secret-free reconciliation failure category            |
| `transient_delivery_failures`              | `int`           | Transient outbound errors                              |
| `permanent_delivery_failures`              | `int`           | Permanent outbound errors                              |
| `outbound_rate_limit_events`               | `int`           | Homeserver rate-limit responses observed               |
| `outbound_cooldown_deferrals`              | `int`           | Sends deferred locally during a shared cooldown        |
| `outbound_cooldown_remaining_seconds`      | `float`         | Remaining server-directed outbound cooldown            |
| `inbound_published`                        | `int`           | Events published inbound                               |
| `inbound_duplicate_admissions`             | `int`           | Duplicate durable admissions                           |
| `inbound_suppressed_self`                  | `int`           | Self-message suppressions                              |
| `inbound_suppressed_envelope`              | `int`           | MEDRE-origin loop hint suppressions                    |
| `inbound_filtered_allowlist`               | `int`           | Room allowlist rejections                              |
| `inbound_filtered_encryption_policy`       | `int`           | Events dropped by `require_encrypted_rooms` policy     |
| `inbound_suppressed_startup`               | `int`           | Backlog events before first live sync                  |

The delivery-failure and inbound counters reset to zero each time the adapter
starts.

---

## Canonical Inbound Event Shape

Matrix ingress normalizes SDK events into the transport-neutral canonical model
before durable admission. Matrix-specific richness is retained under the
versioned `metadata.native.data["matrix"]` namespace rather than adding Matrix
fields to `CanonicalEvent`.

| Matrix fact                  | Canonical representation                                                       |
| ---------------------------- | ------------------------------------------------------------------------------ |
| Sender MXID                  | `source_transport_id`; repeated in `native.matrix.sender`                      |
| Room ID                      | `source_channel_id`; repeated in `native.matrix.room_id`                       |
| Matrix event ID              | `source_native_ref.native_message_id`; repeated in `native.matrix.event_id`    |
| Origin timestamp             | `timestamp`; original milliseconds in `native.matrix.origin_server_ts_ms`      |
| Reply                        | Generic `EventRelation(relation_type="reply")`                                 |
| Edit (`m.replace`)           | `message.edited` plus generic `edit` relation                                  |
| Reaction (`m.annotation`)    | `message.reacted` plus generic `reaction` relation                             |
| Thread (`m.thread`)          | Generic `thread` relation; thread root in `source_native_ref.native_thread_id` |
| Redaction                    | `message.deleted` plus generic `delete` relation                               |
| Image/audio/video/file       | `message.file`; descriptor in `native.matrix.media`                            |
| MEDRE relay envelope         | `native.matrix.relay.medre_envelope`                                           |
| MMRelay compatibility fields | `native.interop.mmrelay`                                                       |
| E2EE provenance              | Safe booleans in `native.matrix.encryption`                                    |

The normative projection is defined by
[matrix-event-shape.md](../matrix-event-shape.md). The native namespace has
`schema_version = 1` and a standalone machine-readable JSON Schema.
Raw Matrix content is not persisted in this namespace. Crypto sender keys,
Megolm session IDs, encrypted-media keys, IVs, and hashes are deliberately
excluded. The generic `metadata.transport.transport_encrypted` field records
whether the normalized event arrived through Matrix encryption; Matrix-specific
verification detail remains native.

Inbound edit, redaction, thread, and attachment normalization is the ingress
contract; the outbound capability profile below is authoritative for what
MEDRE renders back to Matrix. Native edits/deletes additionally require the
mutation-eligibility rules in [Capabilities](#capabilities).

---

## Relation Degradation Behavior

Matrix is a presentation adapter with rich native relation support. The Matrix renderer handles all rendering within its native format.

| Relation type | Capability level | Strategy | Rendering path                                                                                   |
| ------------- | ---------------- | -------- | ------------------------------------------------------------------------------------------------ |
| Replies       | `"native"`       | `direct` | `m.in_reply_to` with `event_id` in `m.relates_to`                                                |
| Reactions     | `"native"`       | `direct` | `m.reaction` event type with `m.annotation`                                                      |
| Edits         | `"native"`       | `direct` | `m.replace` / `m.new_content` — only for `bound_owned` targets; otherwise suppressed (see above) |
| Deletes       | `"native"`       | `direct` | `redact_event` — only for `bound_owned` targets; otherwise suppressed (see above)                |
| Threads       | `"native"`       | `direct` | `m.thread` rooted at the bound thread root, with spec reply-fallback parent semantics            |

Every outbound render wraps its wire content in a closed
`_matrix_operation` envelope (`send_event` / `redact_event`); the adapter
validates the envelope strictly, pops it before transport, and dispatches on
its `kind`. The old `_matrix_event_type` magic key no longer exists.
Mutation deliveries additionally require a `bound_owned` target fact —
see [Mutation eligibility](#mutation-eligibility-native-edits-and-deletes).

When `fallback_text` is supplied for a relation, the Matrix renderer produces its
native message format with the relation context embedded as inline text. This is a
renderer contract, not a test-only quirk; any code path that populates
`fallback_text` on a routed relation triggers the same inline-text rendering path.

**Thread capability:** threads are native. The `m.relates_to` root is the bound
destination thread root; an explicit bound reply relation on the same event
becomes the parent with `is_falling_back=false`, otherwise the root itself is
the fallback parent with `is_falling_back=true`. An unbound root degrades to a
plain message without `m.relates_to` (honest degradation — never a fabricated
or source-platform ID). An explicit reply-in-thread (thread + reply relations
inbound) inherits plain-reply capability semantics downstream: a destination
with `replies="unsupported"` skips the delivery even though a thread-only
event would degrade to inline text there.

**Payload requirement:** The Matrix renderer produces closed outbound operation payloads. The adapter transports `send_event` wire content via `room_send` and `redact_event` operations via `room_redact` — nothing under `_matrix_operation` reaches the homeserver.

---

## Outbound Rate-Limit Coordination

Matrix delivery keeps durable retry scheduling in the core lifecycle. When the
homeserver returns a recognizable room-send `M_LIMIT_EXCEEDED` / HTTP 429, the
session intercepts that `RoomSendError` through mindroom-nio's filtered
response-callback boundary before the SDK sleeps/retries it. The response is then
classified by the adapter as a transient delivery failure.

If the response carries a valid `retry_after_ms`, the adapter converts it into the
generic `AdapterSendError.retry_after_seconds` hint. A positive value also extends
a shared monotonic cooldown for the adapter instance. While that window is active,
sibling deliveries fail fast without calling `room_send`; their retry hint is the
remaining cooldown.
The durable receipt/outbox scheduler uses
`max(policy_backoff, retry_after_seconds)` for `next_retry_at`. Missing or invalid
`retry_after_ms` values remain ordinary transient failures and do not create a
shared cooldown.

This is server-directed backpressure, not a second retry engine. MEDRE does not
change mindroom-nio's client-global 429 policy because that policy also covers sync,
join, and key-management requests; the interception is filtered to room-send and
room-redact error responses (`RoomSendError` and `RoomRedactError` are filtered
separately — redaction 429s reach MEDRE's retry owner exactly like sends, before
the SDK sleeps or retries). The adapter does not sleep through a MEDRE-owned cooldown, does not
consume additional Matrix transaction IDs, and does not override route retry limits.
Both the in-memory shared cooldown and durable hint scheduling are bounded to 30 days
so a hostile or broken value cannot park delivery indefinitely.

## Known Limitations

- **Mutations require proof, not just capability.** `edits="native"` and
  `deletes="native"` are gated by the destination-scoped `bound_owned`
  binding proof (see [Mutation eligibility](#mutation-eligibility-native-edits-and-deletes));
  unresolvable or unauthorized targets are suppressed with a stable reason,
  never guessed.
- **Native edits are text-only.** Binary attachments are unsupported outbound
  (`attachments=False`), so edits of media events are out of scope; text edits
  render `m.replace`/`m.new_content`.
- **Duplicate-send risk.** The deterministic `tx_id` reduces duplicates within the
  homeserver's dedup window, but duplicates are still possible across restarts, replay,
  or changed delivery identity. Redactions use their own deterministic
  transaction ids (operation kind + target folded in), so a redaction never
  deduplicates against a send or a different redaction.
- **Peer-device trust is permissive.** Own-device cross-signing is implemented
  with the currently pinned `mindroom-nio` release, but MEDRE does not yet expose
  an operator-configurable policy for verifying peer devices. `ignore_unverified_devices=True` remains intentional for
  E2EE sends.
- **No room-key backup workflow.** MEDRE does not manage Matrix room-key
  backup/import/export or interactive verification ceremonies.
- **No outbound attachment rendering.** Inbound image/audio/video/file events
  are normalized as `message.file` with safe native media descriptors, while
  `attachments=False` remains the outbound capability.
- **Room-state tracking cap.** Maximum 10 000 rooms tracked in session `_room_states`; oldest evicted on overflow.
- **Self-message suppression** only matches `config.user_id`; bot-to-bot echoes from other Matrix users are not suppressed.

---

## Plaintext Event Types and E2EE

- **Edits encrypt like normal sends.** `m.replace` edits are `m.room.message`
  events and go through the same room-message encryption path as ordinary
  sends: in encrypted rooms `m.new_content` travels inside the encrypted
  payload and only `m.relates_to` stays cleartext (spec-mandated). No message
  text leaks in plaintext.
- **Redactions are protocol-plaintext by design.** `m.room.redaction` is sent
  through the dedicated redaction endpoint (never the encrypted room-message
  path) and carries no message content — only the `redacts` target and an
  optional neutral reason. There is nothing to encrypt and nothing to leak;
  MEDRE does not attempt to encrypt it.
- **Reactions are plaintext event types.** `m.reaction` annotation events are
  intentionally not encrypted (per the pinned SDK: "Reactions do not support
  encryption yet"); their content is only the annotation relationship and key.
  MEDRE surfaces no message content through them.
- Crypto secrets are never persisted: native metadata keeps only safe
  provenance booleans, and session diagnostics expose no tokens or keys.

---

## Duplicate-Send Risk Level

**Low–Medium.** Deterministic transaction IDs provide within-window dedup on the homeserver. Cross-restart replay or delivery identity changes can still produce duplicates. The adapter does not implement application-level dedup beyond `tx_id`.

---

## Validation Status

- Config validation enforces: non-empty `homeserver` (http/https), `user_id` starting
  with `@`, non-empty `access_token`, valid `encryption_mode`, valid `auto_join_rooms`
  entries (canonical `!localpart:server` form).
- Sidecar credential fallback from `~/.config/medre/credentials/matrix.json` when config
  fields are empty.
- Adapter unit tests cover messages, replies, reactions, edits, threads,
  redactions, media descriptors, renderer output, session lifecycle, delivery retry,
  E2EE mode guards, cross-signing policy/recovery, and auth bootstrap behavior.
- An SDK-contract test checks the currently pinned `mindroom-nio` cross-signing
  surface when the E2EE dependency is installed.

---

## Reference Libraries

| Library             | Purpose                                | Optional                  |
| ------------------- | -------------------------------------- | ------------------------- |
| `mindroom-nio`      | Async Matrix client (sync, send, E2EE) | Yes (`medre[matrix]`)     |
| `mindroom-nio[e2e]` | E2EE crypto (vodozemac + Olm)          | Yes (`medre[matrix-e2e]`) |
