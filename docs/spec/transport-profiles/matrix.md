# Matrix Transport Profile

## Purpose and Role

The Matrix adapter is a **presentation adapter** (`AdapterRole.PRESENTATION`) that connects to a Matrix homeserver via the `mindroom-nio` async client library. It bridges inbound Matrix room messages into the MEDRE canonical event stream and delivers outbound rendered payloads back to Matrix rooms.

The adapter delegates all client lifecycle (creation, login, sync, teardown) to `MatrixSession`. Semantic conversion (codec decode/encode, event classification) is owned by the adapter itself.

**Platform identifier:** `matrix`

---

## Configuration Fields

| Field                     | Type                                                   | Default       | Description                                                                     |
| ------------------------- | ------------------------------------------------------ | ------------- | ------------------------------------------------------------------------------- |
| `adapter_id`              | `str`                                                  | _(required)_  | Unique adapter instance identifier                                              |
| `homeserver`              | `str`                                                  | _(required)_  | Matrix homeserver URL (`https://…` or `http://…`)                               |
| `user_id`                 | `str`                                                  | _(required)_  | Fully-qualified Matrix user ID (must start with `@`)                            |
| `device_id`               | `str \| None`                                          | `None`        | Internal — discovered via `whoami()` when needed                                |
| `access_token`            | `str`                                                  | `""`          | Access token for authentication; sidecar fallback supported                     |
| `room_allowlist`          | `set[str] \| None`                                     | `None`        | Optional set of room IDs to accept; `None` = all rooms                          |
| `metadata_embedding_mode` | `str`                                                  | `"safe"`      | How metadata is embedded in messages                                            |
| `store_path`              | `str \| None`                                          | `None`        | Internal — derived under `{state}/adapters/{id}/matrix/store`                   |
| `sync_timeout_ms`         | `int`                                                  | `30000`       | Long-polling sync timeout in milliseconds                                       |
| `encryption_mode`         | `Literal["plaintext","e2ee_required","e2ee_optional"]` | `"plaintext"` | E2EE policy                                                                     |
| `require_encrypted_rooms` | `bool`                                                 | `False`       | If `True`, reject plaintext rooms; invalid with `encryption_mode="plaintext"`   |
| `auto_join_rooms`         | `tuple[str, ...]`                                      | `()`          | Canonical room IDs (`!localpart:server`) to auto-join on startup and via invite |

---

## Capabilities

Machine-readable capability declaration: [`matrix-capabilities.json`](matrix-capabilities.json)

> Capability levels map to the CapabilityLevel enum (adapter-runtime.md §6.2): `"native"` = `TRUE`, `"unsupported"` = `FALSE`.

| Capability        | Value           |
| ----------------- | --------------- |
| text              | `True`          |
| replies           | `"native"`      |
| reactions         | `"native"`      |
| edits             | `"unsupported"` |
| deletes           | `"unsupported"` |
| attachments       | `False`         |
| delivery_receipts | `True`          |
| store_and_forward | `False`         |
| direct_messages   | `True`          |
| channels          | `True`          |
| async_delivery    | `True`          |
| topic_rooms       | `True`          |

---

## Supported Inbound Event Kinds

The Matrix codec (`MatrixCodec`) decodes three inbound categories:

1. **True Matrix reactions** (`m.annotation` in `m.relates_to`) → `MESSAGE_REACTED` with a `reaction` relation targeting the annotated event.
2. **MMRelay emote reactions** (`m.emote` with `meshtastic_replyId` and `meshtastic_emoji == 1`) → `MESSAGE_REACTED` with a canonical reaction relation carrying MMRelay metadata.
3. **Regular messages** (including replies) → `MESSAGE_CREATED`. Reply fallback body is stripped.

---

## Supported Outbound Event Kinds

The Matrix renderer (`MatrixRenderer`) produces:

- **Plain text messages** — `m.room.message` with `m.text` msgtype, optional relay prefix, and MEDRE metadata envelope.
- **Native replies** — `m.relates_to.m.in_reply_to` with `event_id`, plus `KEY_REPLY_ID` when MMRelay metadata is available.
- **Native reactions** — `m.reaction` event type (via internal `_matrix_event_type` key) with `m.annotation`.
- **MMRelay emote reaction fallback** — `m.emote` with `KEY_EMOJI=1`, `KEY_REPLY_ID`, and full mesh metadata (used when `mmrelay_compatibility=True` or no Matrix-native target exists).

---

## Native Reference Format

- **Inbound native ref:** `NativeRef(adapter=<id>, native_channel_id=<room_id>, native_message_id=<event_id>)`
- **Outbound native ref:** Returned from `deliver()` as `AdapterDeliveryResult.native_message_id` (the Matrix `event_id` from `RoomSendResponse`).
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
2. **Connecting** — `start()` creates `MatrixSession`, calls `session.start()`. Session creates nio `AsyncClient`, restores login via `restore_login()`, registers callbacks, starts sync task.
3. **Live (pre-sync)** — After `start()` returns, the first sync is in progress. `session.is_live` is `False`; inbound events are suppressed as backlog.
4. **Live (syncing)** — First successful sync with `next_batch` token sets `is_live = True`. Subsequent inbound events are processed normally.
5. **Reconnecting** — Sync failure triggers bounded exponential backoff (1 s → 2 s → 4 s → … capped at 60 s, ±25 % jitter, max 10 consecutive attempts).
6. **Stopped** — `stop()` cancels sync task, disconnects client, nulls session. Idempotent.

**E2EE modes:**

- `plaintext` — No crypto; `ignore_unverified_devices=False`.
- `e2ee_required` — Fails if `mindroom-nio[e2e]` not installed or crypto subsystem broken.
- `e2ee_optional` — Attempts crypto; falls back to plaintext with `crypto_enabled=False` on failure.

---

## Diagnostics Keys

`adapter.diagnostics()` returns a dict (no secrets):

| Key                           | Type            | Description                              |
| ----------------------------- | --------------- | ---------------------------------------- |
| `connected`                   | `bool`          | Session has active client                |
| `logged_in`                   | `bool`          | Client reports authenticated             |
| `sync_task_running`           | `bool`          | Sync asyncio task alive                  |
| `last_sync_error`             | `str \| None`   | Last sync failure message                |
| `store_path_configured`       | `bool`          | E2EE store path set                      |
| `device_id_configured`        | `bool`          | Device ID known                          |
| `encryption_mode`             | `str`           | Current E2EE mode                        |
| `crypto_enabled`              | `bool`          | Crypto subsystem active                  |
| `last_crypto_error`           | `str \| None`   | Last crypto error                        |
| `encrypted_room_seen`         | `bool`          | At least one encrypted room detected     |
| `undecryptable_event_count`   | `int`           | MegolmEvents that could not be decrypted |
| `sync_running`                | `bool`          | Sync loop active                         |
| `reconnecting`                | `bool`          | Reconnect backoff in progress            |
| `reconnect_attempts`          | `int`           | Consecutive reconnect attempts           |
| `last_successful_sync`        | `float \| None` | Monotonic time of last good sync         |
| `crypto_store_loaded`         | `bool`          | Olm/store initialised                    |
| `olm_loaded`                  | `bool`          | Olm subsystem loaded                     |
| `encrypted_room_count`        | `int`           | Rooms tracked as encrypted               |
| `plaintext_room_count`        | `int`           | Rooms tracked as plaintext               |
| `transient_delivery_failures` | `int`           | Transient outbound errors                |
| `permanent_delivery_failures` | `int`           | Permanent outbound errors                |
| `inbound_published`           | `int`           | Events published inbound                 |
| `inbound_suppressed_self`     | `int`           | Self-message suppressions                |
| `inbound_suppressed_envelope` | `int`           | MEDRE-origin loop hint suppressions      |
| `inbound_filtered_allowlist`  | `int`           | Room allowlist rejections                |
| `inbound_suppressed_startup`  | `int`           | Backlog events before first live sync    |

---

## Relation Degradation Behavior

Matrix is a presentation adapter with rich native relation support. The Matrix renderer handles all rendering within its native format.

| Relation type | Capability level | Strategy      | Rendering path                                                    |
| ------------- | ---------------- | ------------- | ----------------------------------------------------------------- |
| Replies       | `"native"`       | `direct`      | `m.in_reply_to` with `event_id` in `m.relates_to`                |
| Reactions     | `"native"`       | `direct`      | `m.reaction` event type with `m.annotation`                      |
| Edits         | `"unsupported"`  | `skip`        | No delivery. Edit events targeting this adapter are suppressed.   |
| Deletes       | `"unsupported"`  | `skip`        | No delivery. Delete events targeting this adapter are suppressed. |
| Threads       | `"native"`       | `direct`      | Thread relation via `m.relates_to` with `rel_type: m.thread`     |

Matrix does not use the `"fallback"` capability level for any relation type. All relations are either native or unsupported. When a relation type is unsupported, the delivery is skipped entirely at the planning stage. No fallback text rendering occurs.

**Payload requirement:** The Matrix renderer produces Matrix-native payloads (`m.room.message` with msgtype/body/`m.relates_to`). The adapter transports these payloads via `room_send` without modification.

---

## Known Limitations

- **No edits or deletes.** The capabilities declare `edits="unsupported"` and `deletes="unsupported"`.
- **Duplicate-send risk.** The deterministic `tx_id` reduces duplicates within the homeserver's dedup window, but duplicates are still possible across restarts, replay, or changed delivery identity.
- **nio cross-signing.** `mindroom-nio` lacks MSC1756 cross-signing support; `ignore_unverified_devices=True` is set internally when E2EE is active.
- **No attachment support.** `attachments=False` in capabilities.
- **Room-state tracking cap.** Maximum 10 000 rooms tracked in session `_room_states`; oldest evicted on overflow.
- **Self-message suppression** only matches `config.user_id`; bot-to-bot echoes from other Matrix users are not suppressed.

---

## Duplicate-Send Risk Level

**Low–Medium.** Deterministic transaction IDs provide within-window dedup on the homeserver. Cross-restart replay or delivery identity changes can still produce duplicates. The adapter does not implement application-level dedup beyond `tx_id`.

---

## Validation Status

- Config validation enforces: non-empty `homeserver` (http/https), `user_id` starting with `@`, non-empty `access_token`, valid `encryption_mode`, valid `auto_join_rooms` entries (canonical `!localpart:server` form).
- Sidecar credential fallback from `~/.config/medre/credentials/matrix.json` when config fields are empty.
- Adapter unit tests cover codec decode for all three event categories, renderer output, session lifecycle, delivery retry, and E2EE mode guards.

---

## Reference Libraries

| Library             | Purpose                                | Optional                  |
| ------------------- | -------------------------------------- | ------------------------- |
| `mindroom-nio`      | Async Matrix client (sync, send, E2EE) | Yes (`medre[matrix]`)     |
| `mindroom-nio[e2e]` | E2EE crypto (vodozemac + Olm)          | Yes (`medre[matrix-e2e]`) |
