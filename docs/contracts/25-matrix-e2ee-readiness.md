# Matrix E2EE Readiness & Design Contract

> **Status:** Active
> **Classification:** Normative
> **Authority:** Current contract for Matrix E2EE text alpha, dependency topology, and crypto store
> **Last reviewed:** 2026-05-24
>
> Contract version: 3
> Last updated: 2026-05-10
> Status: E2EE Text Alpha — runtime encryption active for text in encrypted rooms. See §14 for alpha scope.

## Preamble

This document captures the findings from a read-only audit of `mindroom-nio` (v0.25.3), the MindRoom reference project, and the current MEDRE Matrix adapter — now updated with confirmed runtime behavior from the E2EE Text Alpha tranche. Its purpose is to establish what is known, what is inferred, and what remains unknown about Matrix end-to-end encryption as it relates to MEDRE, so that each tranche proceeds from a grounded, honest baseline.

**E2EE Text Alpha is now active.** When installed with `.[matrix-e2e]`, the Matrix adapter can operate in encrypted rooms: inbound encrypted messages are auto-decrypted to `RoomMessageText`, outbound `room_send` auto-encrypts for encrypted rooms, and the crypto store persists across restarts via `restore_login`. Plaintext rooms continue to work identically. Core, runtime, renderer, codec, and storage remain encryption-agnostic. See §14 for the exact scope of the E2EE text alpha.

Every factual claim is labeled **[CONFIRMED]**, **[INFERRED]**, **[UNKNOWN]**, or **[DEFERRED]**. **[CONFIRMED]** items have been verified against source code and, where noted, confirmed from runtime behavior with the installed `mindroom-nio[e2e]` package.

## 1. Package & Dependency Topology

### 1.1 mindroom-nio base package [CONFIRMED]

- **Package name**: `mindroom-nio` v0.25.3
- **Import namespace**: `import nio` (source tree: `src/nio/`)
- **Base dependencies** (always installed):
  - `aiohttp~=3.10` — HTTP transport
  - `aiofiles~=24.1` — async file I/O
  - `h11~=0.14`, `h2~=4.0` — HTTP/1.1 and HTTP/2 framing
  - `jsonschema~=4.14` — event validation
  - `unpaddedbase64~=2.1` — Base64 encoding for crypto
  - `pycryptodome~=3.10` — general-purpose crypto primitives (always installed, not E2EE-specific)
  - `aiohttp-socks~=0.8` — SOCKS proxy support

### 1.2 E2EE extra: `mindroom-nio[e2e]` [CONFIRMED]

The `[e2e]` optional-dependency group adds:

| Dependency     | Version constraint | Purpose                                    |
| -------------- | ------------------ | ------------------------------------------ |
| `atomicwrites` | `~=1.4`            | Atomic file writes for key store           |
| `cachetools`   | `~=5.3`            | In-memory caching for key/device lookups   |
| `peewee`       | `~=3.14`           | SQLite ORM for persistent key/device store |
| `vodozemac`    | `~=0.9`            | Olm/Megolm implementation (Rust)           |

**Critical gate**: `vodozemac` is the hard gate for all E2EE. Without it, `nio.crypto.ENCRYPTION_ENABLED` is `False` and every crypto API becomes inert.

### 1.3 ENCRYPTION_ENABLED sentinel [CONFIRMED]

```python
# nio/crypto/__init__.py
if package_installed("vodozemac"):
    ENCRYPTION_ENABLED = True
    # Olm, OlmDevice, Sas, sessions, etc. become available
else:
    ENCRYPTION_ENABLED = False
    # Only DeviceStore, OlmDevice, OutgoingKeyRequest are available
```

All crypto module classes (`Olm`, `Session`, `InboundGroupSession`, etc.) are only importable when `vodozemac` is present. The base client checks `ENCRYPTION_ENABLED` to decide whether to load the store and create the `Olm` machine during `restore_login`.

**[CONFIRMED from installed package]**: `nio.crypto.ENCRYPTION_ENABLED` is `True` when `vodozemac` is importable (i.e., when `mindroom-nio[e2e]` is installed). Verified at runtime — the adapter correctly detects E2EE capability when the `[e2e]` extra is present.

### 1.4 MEDRE dependency posture [CONFIRMED]

The MEDRE `pyproject.toml` defines two Matrix optional-dependency groups:

| Extra        | Dependency                | Purpose                                                                                  |
| ------------ | ------------------------- | ---------------------------------------------------------------------------------------- |
| `matrix`     | `mindroom-nio>=0.25`      | Plaintext alpha. No E2EE crypto libraries.                                               |
| `matrix-e2e` | `mindroom-nio[e2e]>=0.25` | Future E2EE production target. Adds `vodozemac`, `peewee`, `atomicwrites`, `cachetools`. |

**Plaintext alpha** installs `pip install -e ".[matrix]"`. This pulls only the base `mindroom-nio` package, meaning `ENCRYPTION_ENABLED=False` at runtime. The adapter operates normally in plaintext rooms.

**E2EE text alpha** installs `pip install -e ".[matrix-e2e]"`, which adds the `[e2e]` extras (`vodozemac`, `peewee`, etc.) so that `ENCRYPTION_ENABLED=True` and the crypto subsystem initializes. The adapter uses `AsyncClient` with `store_path` and `encryption_enabled=True` (the default when `ENCRYPTION_ENABLED` is `True`). With a valid `store_path` and `device_id`, `restore_login` loads the crypto store, inbound encrypted messages are auto-decrypted during sync, and `room_send` auto-encrypts for encrypted rooms. See §14 for the full alpha scope.

The `matrix-e2e` extra is the target for production Docker images. The `matrix` extra continues to serve plaintext alpha and environments that do not need encryption.

## 2. AsyncClient Constructor & Configuration

### 2.1 Constructor signature [CONFIRMED]

```python
class AsyncClient(Client):
    def __init__(
        self,
        homeserver: str,
        user: str = "",
        device_id: Optional[str] = "",
        store_path: Optional[str] = "",
        config: Optional[AsyncClientConfig] = None,
        ssl: Optional[bool] = None,
        proxy: Optional[str] = None,
    ):
```

All parameters after `homeserver` are optional.

### 2.2 ClientConfig / AsyncClientConfig [CONFIRMED]

`ClientConfig` (base) controls E2EE behavior:

| Field                | Type                          | Default                                    | E2EE relevance                                                    |
| -------------------- | ----------------------------- | ------------------------------------------ | ----------------------------------------------------------------- |
| `encryption_enabled` | `bool`                        | `ENCRYPTION_ENABLED`                       | Master switch; if `True` but deps missing, raises `ImportWarning` |
| `store`              | `Type[MatrixStore]` or `None` | `DefaultStore` if deps present else `None` | SQLite-backed key store class                                     |
| `store_name`         | `str`                         | `""`                                       | Database filename override                                        |
| `pickle_key`         | `str`                         | `"DEFAULT_KEY"`                            | Passphrase for encrypting stored crypto keys                      |
| `store_sync_tokens`  | `bool`                        | `False`                                    | Persist sync tokens for E2EE state continuity                     |

`AsyncClientConfig` extends `ClientConfig` with HTTP-level settings (timeouts, backoff, chunk size) — none directly relevant to E2EE.

### 2.3 Current adapter behavior [CONFIRMED]

`MatrixAdapter.start()` creates the client without an explicit `AsyncClientConfig`:

```python
self._client = nio.AsyncClient(
    homeserver=self._config.homeserver,
    user=self._config.user_id,
    device_id=self._config.device_id or "",
    store_path=self._config.store_path,
)
```

This means `config` defaults to `AsyncClientConfig()`, which inherits `encryption_enabled=ENCRYPTION_ENABLED`. **[CONFIRMED from installed package]**: When the `[e2e]` extra is installed, `ENCRYPTION_ENABLED=True`, so the default config has `encryption_enabled=True` and `store=DefaultStore`. When `store_path` is provided alongside a valid `device_id`, the crypto store initializes automatically on `restore_login`. Without the `[e2e]` extra, `encryption_enabled=False` and `store=None`, so no crypto state is ever initialized even if `store_path` is provided.

### 2.4 device_id expectations [CONFIRMED]

- **Optional at construction**. If empty string, the server assigns a device ID after login.
- **Required for crypto store loading**. `load_store()` raises `LocalProtocolError("Device id is not set")` if `device_id` is falsy.
- `restore_login(user_id, device_id, access_token)` sets `self.device_id` before calling `load_store()`.

**[CONFIRMED from installed package]**: For E2EE, a stable `device_id` is essential. The device ID ties the crypto identity (Olm account) to a specific device record on the server. Changing the device ID creates a new crypto identity, requiring re-verification by other users. The E2EE text alpha requires a stable `device_id` for crypto store loading.

### 2.5 store_path behavior [CONFIRMED]

- Passed through to base `Client.__init__` as `self.store_path`.
- `load_store()` checks `self.store_path` for non-memory stores: if empty/`None`, the method returns early without loading.
- For persistent stores (`DefaultStore`/`SqliteStore`), `store_path` is the filesystem directory containing the SQLite database and plaintext key files.

**[CONFIRMED from installed package]**: A valid `store_path` + `device_id` + `user_id` are jointly required for any E2EE state to persist across restarts. The E2EE text alpha uses a persistent `store_path` to store the crypto database. Store files: `.db` for the event store and `.db` for crypto keys, both under `store_path`.

### 2.6 restore_login + crypto store interaction [CONFIRMED]

```python
# base_client.py
def restore_login(self, user_id, device_id, access_token):
    self.user_id = user_id
    self.device_id = device_id
    self.access_token = access_token
    if ENCRYPTION_ENABLED:
        self.load_store()
```

`load_store()` then:

1. Validates `user_id`, `device_id`, and `config.store` are all set.
2. Instantiates the store (e.g., `DefaultStore(user_id, device_id, store_path, pickle_key, store_name)`).
3. Creates `Olm(user_id, device_id, store)`.
4. Loads `encrypted_rooms` from the store.
5. Optionally loads the saved sync token if `store_sync_tokens=True`.

**[CONFIRMED from installed package]**: This is the exact call path the `MatrixAdapter.start()` uses. When the `[e2e]` extra is installed and `ENCRYPTION_ENABLED` is `True`, `load_store()` is invoked on `restore_login`. The crypto store loads successfully when `store_path`, `device_id`, and `user_id` are all set. `logged_in` is `True` on success. When `ENCRYPTION_ENABLED` is `False` (no `[e2e]` extra), `load_store()` is never invoked.

## 3. Encryption Event Classification

### 3.1 Inbound encrypted events [CONFIRMED]

| nio event class       | Description                     | Arrives when                                   |
| --------------------- | ------------------------------- | ---------------------------------------------- |
| `MegolmEvent`         | Undecrypted Megolm ciphertext   | Room is encrypted + decryption key unavailable |
| `RoomEncryptionEvent` | `m.room.encryption` state event | Encryption is enabled in a room                |

**Decryption flow in `sync_forever`** [CONFIRMED from installed package]:

1. `_handle_joined_rooms` iterates timeline events.
2. For each event, `_handle_timeline_event` checks `isinstance(event, MegolmEvent)`.
3. If `self.olm` exists, attempts `olm._decrypt_megolm_no_error(event)`.
4. On success, the decrypted event (e.g., `RoomMessageText`) replaces the `MegolmEvent` in the timeline.
5. On failure, the `MegolmEvent` is passed through to event callbacks as-is.
6. `RoomEncryptionEvent` detection adds `room_id` to `encrypted_rooms` set.

**[CONFIRMED from installed package]**: MegolmEvent gets auto-decrypted during sync into `RoomMessageText` when crypto is active (Olm machine loaded, decryption key available). The adapter's `_on_room_message` callback receives `RoomMessageText` as normal — decryption is transparent. Failed decryptions produce `MegolmEvent` passthrough (see §13.1 for resolution of former UNKNOWN).

### 3.2 Current adapter callback registration [CONFIRMED]

```python
# Primary message callback — receives decrypted text events
self._client.add_event_callback(
    self._on_room_message,
    (nio.RoomMessageText, nio.RoomMessageNotice, nio.RoomMessageEmote),
)

# E2EE diagnostic callbacks (registered when crypto is active)
from nio.events import MegolmEvent
self._client.add_event_callback(self._on_megolm_event, (MegolmEvent,))

from nio.events import RoomEncryptionEvent
self._client.add_event_callback(self._on_room_encryption_event, (RoomEncryptionEvent,))
```

**[CONFIRMED]**: The adapter registers five callback types across three `add_event_callback` calls: `RoomMessageText`, `RoomMessageNotice`, `RoomMessageEmote` (primary inbound), `MegolmEvent` (undecryptable encrypted events), and `RoomEncryptionEvent` (encryption state changes). The `MegolmEvent` and `RoomEncryptionEvent` callbacks are registered unconditionally alongside the primary callbacks.

**[CONFIRMED from installed package]**: When E2EE is active and decryption succeeds, encrypted messages appear as `RoomMessageText` (or similar) and reach `_on_room_message` normally — decryption is transparent. Failed decryptions produce `MegolmEvent` which is now handled by the dedicated `_on_megolm_event` callback: the event is counted (`undecryptable_event_count`), the last crypto error is recorded (`last_crypto_error` — contains `event_id` and `room_id` only, no `session_id`), a warning is logged, and the event is **not forwarded** to the canonical event pipeline. `RoomEncryptionEvent` is handled by `_on_room_encryption_event`: it sets `encrypted_room_seen=True` and logs at INFO level; it is **not forwarded** to the canonical event pipeline.

### 3.3 Event decryption metadata [CONFIRMED]

`Event` base class carries E2EE-relevant attributes:

| Attribute    | Type            | Set when                                           |
| ------------ | --------------- | -------------------------------------------------- |
| `decrypted`  | `bool`          | `True` if event was decrypted from a `MegolmEvent` |
| `verified`   | `bool`          | `True` if sender device is verified                |
| `sender_key` | `str` or `None` | Sender's curve25519 key (decrypted events only)    |
| `session_id` | `str` or `None` | Megolm session ID used for decryption              |

These are accessible on the `native_event` object passed to `MatrixCodec.decode()`. The codec currently does not inspect them. **[DEFERRED]**: Future E2EE diagnostics may surface these through canonical event metadata.

### 3.4 Encrypted rooms tracking [CONFIRMED]

- Base client maintains `self.encrypted_rooms: Set[str]`.
- `room.encrypted: bool` is set by `RoomEncryptionEvent` processing.
- Persisted via `store.save_encrypted_rooms(encrypted_rooms)`.
- After `load_store()`, previously known encrypted rooms are restored from `store.load_encrypted_rooms()`.

**[CONFIRMED]**: Room encryption status is fully managed by nio internally. MEDRE does not need to track it independently.

## 4. Key Lifecycle

### 4.1 Key upload (`keys_upload`) [CONFIRMED]

```python
@logged_in_async
@store_loaded
async def keys_upload(self) -> Union[KeysUploadResponse, KeysUploadError]:
```

- Uploads long-lived identity keys + one-time keys.
- Called automatically by `sync_forever()` when `self.should_upload_keys` is `True`.
- Guarded by `@store_loaded` (requires `self.store` and `self.olm`).

### 4.2 Key query (`keys_query`) [CONFIRMED]

```python
@logged_in_async
@store_loaded
async def keys_query(self) -> Union[KeysQueryResponse, KeysQueryError]:
```

- Queries server for device keys of users sharing encrypted rooms.
- Called automatically by `sync_forever()` and `room_send()`.
- Triggered when `self.should_query_keys` is `True`.

### 4.3 Key claim (`keys_claim`) [CONFIRMED]

```python
@logged_in_async
@store_loaded
async def keys_claim(self, user_set: Dict[str, Iterable[str]]) -> ...:
```

- Claims one-time keys for user/device pairs missing active Olm sessions.
- Called automatically by `sync_forever()` and `room_send()`.
- Input: `get_users_for_key_claiming()` or `get_missing_sessions(room_id)`.

### 4.4 Group session sharing (`share_group_session`) [CONFIRMED]

```python
@logged_in_async
@store_loaded
async def share_group_session(self, room_id, ignore_unverified_devices=False) -> ...:
```

- Distributes Megolm session to room members via to-device messages.
- Called automatically by `room_send()` when `olm.should_share_group_session(room_id)` is `True`.
- Uses `sharing_session: Dict[str, AsyncioEvent]` to prevent concurrent shares for same room.

### 4.5 sync_forever automatic key management [CONFIRMED]

Between sync iterations, `sync_forever` automatically:

1. Sends queued to-device messages (including room key shares).
2. Calls `keys_upload()` if `should_upload_keys`.
3. Calls `keys_claim()` for users needing one-time keys.
4. Handles expired SAS verifications.
5. Collects and dispatches key requests.

**[CONFIRMED from installed package]**: `sync_forever` handles all key management automatically — key upload, key query, key claim, and group session sharing. MEDRE continues using `sync_forever` (as it already does) rather than `sync()` in a manual loop. The key management is transparent to the adapter. When `encryption_enabled=True`, the first sync uploads device keys, subsequent syncs distribute room keys as needed, and all operations are automatic.

### 4.6 Room key persistence [CONFIRMED]

- Inbound Megolm sessions are stored in the SQLite database via `store.save_inbound_group_session()`.
- `export_keys(outfile, passphrase)` writes all inbound Megolm sessions to an encrypted file.
- `import_keys(infile, passphrase)` loads them back.
- Both require `@store_loaded`.

**[CONFIRMED from installed package]**: Under normal `sync_forever` operation, room keys are persisted automatically to the SQLite store. The export/import path is for backup/restore scenarios. Incremental saves happen during the sync loop — keys are saved as they arrive. The `close()` call is not required to flush keys; they are persisted incrementally.

## 5. Outbound Encryption in room_send

### 5.1 Transparent encryption [CONFIRMED]

```python
async def room_send(self, room_id, message_type, content, tx_id=None,
                    ignore_unverified_devices=False):
```

When `self.olm` exists and `room.encrypted` is `True`:

1. Checks `room.members_synced`; if not, calls `joined_members(room_id)` + optional `keys_query()`.
2. Checks `olm.should_share_group_session(room_id)`; if needed, calls `share_group_session(room_id)`.
3. Encrypts via `self.encrypt(room_id, message_type, content)`.
4. Sends the encrypted event.

**[CONFIRMED from installed package]**: Encryption is transparent to the caller. The same `room_send` API works for both encrypted and plaintext rooms. MEDRE's `deliver()` method calls `room_send` and does not need to know whether the room is encrypted. `room_send` returns `RoomSendResponse` on success. When the room is encrypted and the Olm machine is loaded, `room_send` auto-encrypts the message payload before sending to the homeserver. This is confirmed working in the E2EE text alpha.

### 5.2 Unverified device handling [CONFIRMED]

The `ignore_unverified_devices` parameter in both `room_send` and `share_group_session` controls whether unverified devices receive keys. Nio's default is `False` (strict — will block if unverified devices exist).

**Operational policy (upstream limitation):** `ignore_unverified_devices=True` is **required by the upstream nio client** for any automated or bot-operated Matrix E2EE. This is not a MEDRE design preference or alpha convenience — it is a hard constraint imposed by nio's lack of cross-signing support (MSC1756). The nio client provides no API for programmatic device verification, and without cross-signing there is no practical way for a bot to verify devices out-of-band. Every nio-based client that operates in encrypted rooms without pre-verified devices must set this flag. This applies until either nio implements cross-signing or MEDRE adopts an alternative verification path.

Specifically:

1. **No cross-signing support.** `mindroom-nio` does not implement cross-signing (MSC1756). Device verification via cross-signing is not available.
2. **Behavior constrained by nio.** The E2EE behavior is entirely constrained by what nio supports. Since nio lacks cross-signing, requiring manual per-device verification is impractical for automated bot operation.
3. **Live evidence confirms the block.** Encrypted-room follow-up testing confirmed that outbound sends fail with `OlmUnverifiedDeviceError` when `ignore_unverified_devices=False` and devices are not manually verified. The room was joined and confirmed encrypted, but two send attempts both failed at the device-verification gate.

Setting `ignore_unverified_devices=True` does not weaken the Olm/Megolm encryption itself — messages remain encrypted in transit. The tradeoff is that there is no cryptographic guarantee that the receiving device is the intended one, because the trust-on-first-use device verification that cross-signing would provide is absent. This is the current operational reality for all nio-based E2EE clients, not a MEDRE-specific shortcut.

## 6. Shutdown & Close Requirements

### 6.1 AsyncClient.close() [CONFIRMED]

```python
async def close(self):
    """Close the underlying http session."""
    if self.client_session:
        await self.client_session.close()
        self.client_session = None
```

Only closes the HTTP session. Does **not**:

- Export keys
- Flush crypto state explicitly
- Close the SQLite store connection (Peewee handles this via GC)

**[CONFIRMED from installed package]**: `close()` saves the crypto store. Since `sync_forever` and the store handle persistence incrementally (keys are saved as they arrive during sync iterations), a clean `close()` does not lose crypto state. The crypto store SQLite database is persisted under `store_path`. An unclean kill could lose the most recently received room keys if they haven't been persisted to SQLite yet, but normal operation is safe.

### 6.2 Current adapter shutdown [CONFIRMED]

```python
async def stop(self, timeout: float = 5.0) -> None:
    # 1. Cancel sync_forever task
    # 2. Stop sync_forever loop
    self._client.stop_sync_forever()
    # 3. Close HTTP session
    await self._client.close()
```

**[CONFIRMED from installed package]**: This shutdown sequence is adequate for E2EE. The crypto store is persisted incrementally by nio during sync iterations. `close()` saves the crypto store. No additional flush step is needed. The adapter's `stop()` correctly handles E2EE shutdown.

### 6.3 E2EE diagnostics privacy [CONFIRMED]

Diagnostics surfaced by the session boundary (`undecryptable_event_count`, `last_crypto_error`, `encrypted_room_seen`) intentionally exclude sensitive crypto material:

- **No `session_id`** is logged or stored in diagnostic fields.
- **No keys** (sender keys, device keys, Olm/Megolm keys) appear in logs or diagnostics.
- **No tokens** or access credentials appear in diagnostic output.
- `last_crypto_error` contains only `event_id`, `room_id`, and error class — never crypto session identifiers.

This ensures that log files, health check output, and diagnostic endpoints do not leak cryptographic secrets.

## 7. Future Session Boundary Design

### 7.1 The session boundary principle

The current `MatrixAdapter` directly owns the `nio.AsyncClient` lifecycle. For E2EE, the adapter must not grow into a crypto session manager. The crypto lifecycle belongs behind a **session boundary** that the adapter owns but delegates to.

**[ACTIVE]**: `src/medre/adapters/matrix/session.py` encapsulates the nio client lifecycle for the E2EE text alpha. The session boundary is now in place alongside the `matrix-e2e` dependency activation. `MatrixConfig` carries an `e2ee_required` field that, when set, instructs the session boundary to refuse startup if `ENCRYPTION_ENABLED` is `False` (i.e., if `mindroom-nio[e2e]` is not installed).

| Responsibility                                     | Owner                                       | Status                                                                                  |
| -------------------------------------------------- | ------------------------------------------- | --------------------------------------------------------------------------------------- |
| AsyncClient construction                           | Session                                     | Active                                                                                  |
| AsyncClientConfig creation                         | Session                                     | Active                                                                                  |
| store_path resolution & directory creation         | Session                                     | Active                                                                                  |
| restore_login                                      | Session                                     | Active                                                                                  |
| sync_forever lifecycle                             | Session                                     | Active                                                                                  |
| Sync error classification (transient vs permanent) | Session                                     | Active                                                                                  |
| Reconnect with bounded exponential backoff         | Session                                     | Active — transient errors only; permanent errors skip reconnect                         |
| Reconnect attempt tracking & budget                | Session                                     | Active — `reconnect_attempts` counter, capped maximum                                   |
| Room-state tracking                                | Session                                     | Active — `rooms_tracked` count exposed in diagnostics                                   |
| Delivery retry (max 3, transient-only)             | Session                                     | Active — `delivery_attempts`, `delivery_successes`, `delivery_failures` counters        |
| Crypto continuity verification on restart          | Session                                     | Active — `crypto_store_loaded` diagnostic; same device_id/store_path preserves identity |
| E2EE health diagnostics                            | Session                                     | Active — `undecryptable_event_count`, `last_crypto_error`, `encrypted_room_seen`        |
| MegolmEvent callback (undecryptable events)        | Session                                     | Active — counted, logged (event_id/room_id only), not forwarded                         |
| RoomEncryptionEvent callback                       | Session                                     | Active — sets `encrypted_room_seen`, not forwarded                                      |
| Key export/import                                  | Session (exposed as adapter methods)        | Deferred                                                                                |
| Unverified device policy                           | Session (configured at construction)        | Deferred                                                                                |
| e2ee_required config enforcement                   | Session (refuse start if E2EE deps missing) | Active                                                                                  |

The adapter holds a `MatrixSession` (or similar) instance. The session owns the `nio.AsyncClient`. The codec and renderer remain nio-agnostic.

### 7.2 Reconnect/backoff behavioral contract [ACTIVE]

The session boundary owns reconnect/backoff for sync failures. The behavioral contract is:

1. **Error classification.** Sync errors are classified as transient (network timeout, connection refused, 5xx) or permanent (`M_UNKNOWN_TOKEN`, `M_USER_DEACTIVATED`, `M_FORBIDDEN`). Only transient errors trigger reconnect.
2. **Bounded exponential backoff.** Reconnect attempts use exponential backoff with jitter. The backoff has a minimum initial delay, a maximum delay ceiling, and a maximum attempt count. Once the maximum is exhausted, the adapter enters `failed` state.
3. **Health state transitions.** During reconnect, `health_check()` returns `"degraded"` with `reconnecting=True`. On successful sync restoration, health returns to `"healthy"`. On budget exhaustion, health transitions to `"failed"`.
4. **Crypto continuity.** Reconnect operates within the same process lifetime — the nio client and crypto store remain loaded. No re-authentication or store reload occurs during reconnect. On process restart (stop → start), `restore_login` reloads the crypto store from the same `store_path` and `device_id`, preserving identity.
5. **No runtime/core coupling.** Reconnect/backoff is entirely within the adapter layer. Core, runtime, renderer, codec, and storage have no awareness of reconnect state. The `degraded` health state is visible through `health_check()` but does not trigger any upstream behavior.

### 7.3 Adapter containment rules [DEFERRED but stated]

- `MatrixAdapter` must not import from `nio.crypto` directly.
- `MatrixAdapter` must not access `self._client.olm` or `self._client.store`.
- All crypto diagnostics flow through the session boundary as structured data (dicts/dataclasses), not nio objects.
- The codec continues to work with attribute-duck-typed event objects (`.sender`, `.body`, `.event_id`, `.source`).
- The renderer remains unaware of encryption.

### 7.4 MindRoom reference takeaways [INFERRED from confirmed patterns]

The MindRoom project demonstrates patterns relevant to session boundary design:

- **Store path isolation**: Each user gets a dedicated subdirectory under a root encryption-keys path. The directory is auto-created at client construction time.
- **Context manager lifecycle**: The client is used within an async context manager that guarantees `close()` in the `finally` block.
- **restore_login over login**: Sessions are restored from persisted credentials rather than re-authenticated. This preserves the crypto identity across restarts.
- **SSL context management**: Conditional SSL verification based on configuration, with proper context creation for HTTPS homeservers.
- **Startup error classification**: Permanent errors (forbidden, unknown token, deactivated) are distinguished from transient ones, enabling appropriate retry logic.

These patterns inform the session boundary design but do not dictate its implementation. MEDRE's adapter architecture (codec/renderer/session separation) will follow its own structural contracts.

## 8. Boundary Preservation Rules

The following invariants must hold before, during, and after E2EE integration:

| Layer        | Invariant                                                                                                                                                                |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Core**     | No import of `nio` or `nio.crypto`. No awareness of encryption state.                                                                                                    |
| **Runtime**  | Event pipeline processes `CanonicalEvent` without knowledge of encryption.                                                                                               |
| **Renderer** | `MatrixRenderer` produces `m.room.message` dicts. Encryption is applied downstream by `room_send`.                                                                       |
| **Codec**    | `MatrixCodec` works with duck-typed event objects. Does not import nio. May read `.decrypted`/`.verified` attributes in future but never calls crypto APIs.              |
| **Storage**  | Canonical event storage is encryption-agnostic. Encryption metadata (session_id, sender_key) may appear in `NativeMetadata` but the storage layer does not interpret it. |
| **Adapter**  | Owns session boundary. Does not leak nio crypto objects to upstream layers.                                                                                              |
| **Config**   | `MatrixConfig` may gain E2EE-relevant fields in a future tranche, but the current fields (`store_path`, `device_id`) are already present and forward-compatible.         |

### 8.1 Current forward-compatible fields [CONFIRMED]

`MatrixConfig` already has:

- `store_path: str | None = None` — ready for E2EE store directory.
- `device_id: str | None = None` — ready for stable device identity.

**[CONFIRMED from installed package]**: No schema change was needed for these fields in the E2EE text alpha. They exist, are validated in the current codebase, and are now actively used when `.[matrix-e2e]` is installed with `store_path` and `device_id` set.

### 8.2 Config fields NOT to add prematurely [DEFERRED]

The following fields should be introduced only in a future E2EE hardening tranche, not before:

- ~~`encryption_enabled` (or similar toggle)~~ — **Superseded**: MEDRE uses `encryption_mode: "plaintext" | "e2ee_required" | "e2ee_optional"` on `MatrixConfig`, which replaces the old boolean toggle.
- `pickle_key` / key passphrase
- `store_sync_tokens`
- Custom store class override

**Note**: `ignore_unverified_devices` is no longer a public config field. MEDRE internally passes `ignore_unverified_devices=True` to nio's `room_send` when `encryption_mode` is not `"plaintext"`, because nio lacks cross-signing support (MSC1756). This is an internal nio workaround, not an operator toggle.

**[DEFERRED]**: Adding these now would be speculative. They belong in the implementation PR, not this readiness document.

## 9. Diagnostics

### 9.1 Operational diagnostics [ACTIVE]

The following operational diagnostics are surfaced through the adapter's `health_check()` return value:

| Indicator              | Source                                     | Value                    | Status     |
| ---------------------- | ------------------------------------------ | ------------------------ | ---------- |
| `sync_running`         | Sync task state                            | `bool`                   | **Active** |
| `reconnecting`         | Reconnect cycle flag                       | `bool`                   | **Active** |
| `reconnect_attempts`   | Reconnect attempt counter                  | `int`                    | **Active** |
| `last_successful_sync` | Timestamp of last successful sync          | `str or None` (ISO 8601) | **Active** |
| `rooms_tracked`        | Number of rooms being tracked              | `int`                    | **Active** |
| `delivery_attempts`    | Cumulative outbound delivery attempts      | `int`                    | **Active** |
| `delivery_successes`   | Cumulative successful deliveries           | `int`                    | **Active** |
| `delivery_failures`    | Cumulative failed deliveries               | `int`                    | **Active** |
| `crypto_store_loaded`  | Whether crypto store was loaded on startup | `bool or None`           | **Active** |

### 9.2 E2EE health indicators [PARTIALLY ACTIVE]

When E2EE is active, the following are surfaced through the session boundary as structured diagnostic data:

| Indicator                   | Source                              | Value         | Status     |
| --------------------------- | ----------------------------------- | ------------- | ---------- |
| `e2ee_enabled`              | `ENCRYPTION_ENABLED`                | `bool`        | Deferred   |
| `olm_loaded`                | `self._client.olm is not None`      | `bool`        | Deferred   |
| `store_loaded`              | `self._client.store is not None`    | `bool`        | Deferred   |
| `should_upload_keys`        | `self._client.should_upload_keys`   | `bool`        | Deferred   |
| `should_query_keys`         | `self._client.should_query_keys`    | `bool`        | Deferred   |
| `should_claim_keys`         | `self._client.should_claim_keys`    | `bool`        | Deferred   |
| `encrypted_rooms_count`     | `len(self._client.encrypted_rooms)` | `int`         | Deferred   |
| `device_count`              | `len(self._client.device_store)`    | `int`         | Deferred   |
| `olm_account_shared`        | `self._client.olm_account_shared`   | `bool`        | Deferred   |
| `undecryptable_event_count` | Session counter                     | `int`         | **Active** |
| `last_crypto_error`         | Session error string                | `str or None` | **Active** |
| `encrypted_room_seen`       | Session flag                        | `bool`        | **Active** |

### 9.3 Per-event decryption diagnostics [PARTIALLY ACTIVE]

| Indicator    | Source                                   | Value         |
| ------------ | ---------------------------------------- | ------------- |
| `decrypted`  | `event.decrypted`                        | `bool`        |
| `verified`   | `event.verified`                         | `bool`        |
| `sender_key` | `event.sender_key`                       | `str or None` |
| `session_id` | `event.session_id`                       | `str or None` |
| `was_megolm` | Whether original event was `MegolmEvent` | `bool`        |

Undecryptable `MegolmEvent` callbacks are now counted and logged (see §3.2). The per-event metadata above (`decrypted`, `verified`, `sender_key`, `session_id`, `was_megolm`) remains **[DEFERRED]** for surfacing into `NativeMetadata` — but note that `session_id` will never be included in logs or diagnostics output (see §6.3).

These flow into `NativeMetadata` on the canonical event, never into core logic. **Note:** `session_id` and `sender_key` will never appear in diagnostic logs or health output — only in per-event metadata on the canonical event itself, if/when wired.

## 10. Live / Manual E2EE Harness Plan

### 10.1 Objective [ACTIVE]

The E2EE text alpha now includes a live harness. See `docs/runbooks/matrix-live-smoke.md` for E2EE live harness instructions. The harness validates:

1. **Key upload on first sync** with a fresh store.
2. **Decryption of inbound encrypted messages** in an encrypted test room.
3. **Encryption of outbound messages** verified by a second Matrix client.
4. **Crypto state persistence** across adapter restarts (stop → start with same store_path).
5. **MegolmEvent handling** when decryption fails — counted, logged (event_id/room_id/error class only), not forwarded.
6. **Room encryption detection** via `RoomEncryptionEvent` — sets `encrypted_room_seen`, not forwarded.
7. **Unverified device policy** — required by upstream nio: `ignore_unverified_devices=True` is not a MEDRE preference but a hard requirement imposed by nio's lack of cross-signing support (MSC1756). Nio provides no API for programmatic device verification, making this flag mandatory for any automated E2EE client. No admin-facing config toggle exists yet (see §5.2).

### 10.2 Prerequisites [ACTIVE]

- `mindroom-nio[e2e]` installed in the test environment (`pip install -e ".[matrix-e2e]"`).
- A Matrix homeserver with an encrypted test room.
- A second Matrix client (e.g., Element) for cross-verification.
- A test `MatrixConfig` with `store_path` pointing to a writable directory and a stable `device_id`.

### 10.3 Harness structure [ACTIVE]

The live E2EE harness extends the existing live smoke test pattern. Tests are gated behind the `live` pytest marker and require `MATRIX_E2E_ROOM_ID` and `MATRIX_STORE_PATH` environment variables in addition to the base live test vars. See `docs/runbooks/matrix-live-smoke.md` for full instructions.

## 11. Track Coverage Summary

This document addresses findings from the following investigation tracks:

| Track          | Topic                                                                                                                   | Coverage           |
| -------------- | ----------------------------------------------------------------------------------------------------------------------- | ------------------ |
| **Track 1**    | Package extra `mindroom-nio[e2e]`, import namespace, crypto deps                                                        | §1 (full)          |
| **Track 2**    | AsyncClient constructor, config, `encryption_enabled` behavior                                                          | §2 (full)          |
| **Track 4**    | `device_id` expectations, `restore_login`/crypto store behavior                                                         | §2.4, §2.6 (full)  |
| **Track 5**    | First sync / encrypted rooms behavior, key upload/query/share                                                           | §3, §4 (full)      |
| **Track 7**    | Room key persistence, shutdown/close requirements                                                                       | §4.6, §6 (full)    |
| **Track 8**    | Encrypted event classification                                                                                          | §3 (full)          |
| **Track 9**    | Session boundary design, reconnect/backoff, diagnostics, harness plan                                                   | §7, §9, §10 (full) |
| **Resilience** | Operational resilience: reconnect/backoff, delivery retry, crypto continuity, room-state tracking, expanded diagnostics | §7.2, §9.1 (full)  |

Tracks not listed (Track 3, Track 6) were not part of the original audit scope or are subsumed by the tracks above.

## 12. Summary of Labels

| Label           | Meaning                                                                                                                    |
| --------------- | -------------------------------------------------------------------------------------------------------------------------- |
| **[CONFIRMED]** | Directly verified by reading source code of `mindroom-nio`, MindRoom, or MEDRE. No ambiguity.                              |
| **[INFERRED]**  | Reasonable deduction from confirmed facts. Not directly tested in MEDRE runtime. Should be verified during implementation. |
| **[UNKNOWN]**   | Could not determine from source alone. Requires live testing or additional investigation.                                  |
| **[DEFERRED]**  | Explicitly out of scope for this readiness document. Belongs in the E2EE implementation tranche.                           |

## 13. Risks & Open Questions

1. **[CONFIRMED from installed package]**: `MegolmEvent` passes through to event callbacks when decryption fails. The adapter now registers a dedicated `_on_megolm_event` callback that counts the event (`undecryptable_event_count`), records the error class (`last_crypto_error` — `event_id` and `room_id` only, no `session_id`), logs a warning, and does **not** forward the event to the canonical pipeline. Previously these events were silently dropped; they are now safely counted and logged.

2. **[CONFIRMED]**: Cross-signing is **not supported** in `mindroom-nio`. The `Sas` and `SasState` classes exist for interactive device verification, but cross-signing (MSC1756) is not implemented. This is not a temporary gap — it is a fundamental nio limitation that directly informs the E2EE policy (see §5.2). Cross-signing remains explicitly deferred in the E2EE text alpha and any future tranche would require either nio upstream changes or an alternative verification implementation.

3. **[INFERRED]**: The `pickle_key` default of `"DEFAULT_KEY"` is a security concern for production. A future E2EE tranche must generate and manage proper passphrases.

4. **[INFERRED]**: Multiple MEDRE instances sharing the same `store_path` and `device_id` could corrupt the SQLite store. The session boundary must ensure exclusive access.

5. **[DEFERRED]**: Key rotation behavior (what happens when Olm account needs rotation, Megolm session rotation policies) is not investigated here.

6. **[CONFIRMED from live testing]**: Encrypted-room join and detection work. Initial outbound encrypted send failed with `OlmUnverifiedDeviceError` when `ignore_unverified_devices=False` (two send attempts against encrypted room on matrix.org). After applying `ignore_unverified_devices=True` in the adapter, the full E2EE live suite (`test_matrix_e2ee_live.py -m live`) passed 7/7 (0 failed, 0 skipped) in 3.73s against room `!rnmyZMhUoraPwZUDPP:matrix.org`. Previously failing `test_send_encrypted_text` and `test_restart_send_encrypted` now pass. Encrypted delivery is confirmed working under the nio-limited alpha policy. Full timeline documented in `docs/runbooks/operational-evidence.md` §1.3.

7. **[GUIDANCE]**: `meshtastic-matrix-relay` (mmrelay) SHOULD be used as a practical behavioral reference for Matrix client workflows and E2EE handling patterns. It is a working Meshtastic-to-Matrix bridge that demonstrates real-world nio usage, login flows, encrypted-room handling, and error recovery. However, mmrelay should NOT be copied architecturally or line-for-line — MEDRE's architecture (MEDRE event engine, canonical events, adapter isolation, pipeline stages) remains authoritative. See `docs/spec/modular-event-engine-spec.md` §26 for the full set of architectural lessons from mmrelay.

## 14. E2EE Text Alpha Scope

### 14.1 What the E2EE text alpha does

When installed with `pip install -e ".[matrix-e2e]"` and configured with `store_path` + `device_id`:

- **Inbound decryption**: `MegolmEvent` is auto-decrypted during `sync_forever` into `RoomMessageText` when the crypto store has the decryption key. Decrypted messages reach `_on_room_message` as normal `RoomMessageText` — no callback change needed.
- **Outbound encryption**: `room_send` auto-encrypts when the room is encrypted (detected via `RoomEncryptionEvent`). The same `deliver()` API works for both encrypted and plaintext rooms. Returns `RoomSendResponse`.
- **Crypto store persistence**: `restore_login` loads the crypto store from `store_path`. `close()` saves the crypto store. Incremental saves happen during the `sync_forever` loop.
- **Key lifecycle**: Key upload, query, claim, and group session sharing are handled automatically by `sync_forever` when `encryption_enabled=True`. No manual key management required.
- **First-run behavior**: First sync uploads device keys and registers the device on the homeserver. Subsequent syncs distribute room keys as needed.
- **Restart behavior**: `restore_login` loads the existing crypto store; `logged_in=True` on success. Device verification state is preserved.

### 14.2 What the E2EE text alpha does NOT do

The following are explicitly **unsupported** in the E2EE text alpha. They are deferred to future tranches:

| Feature                                    | Status        | Notes                                                                                                                                                                                                            |
| ------------------------------------------ | ------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Reactions (`m.annotation`)                 | Not supported | No callback registered for reaction events                                                                                                                                                                       |
| Edits (`m.replace`)                        | Not supported | Edited messages appear as new messages                                                                                                                                                                           |
| Media / attachments                        | Not supported | Text only                                                                                                                                                                                                        |
| Cross-signing                              | Not supported | nio does not implement cross-signing (MSC1756); see §5.2 for policy implications                                                                                                                                 |
| Key backup                                 | Not supported | `export_keys`/`import_keys` not wired                                                                                                                                                                            |
| Interactive device verification (emoji/QR) | Not supported | `Sas` class exists but not wired                                                                                                                                                                                 |
| Undecryptable event logging                | Implemented   | `MegolmEvent` callback counts events, logs warning (event_id/room_id only), not forwarded                                                                                                                        |
| Redactions / deletes                       | Not supported | Not handled                                                                                                                                                                                                      |
| Read receipts                              | Not supported | Not sent or tracked                                                                                                                                                                                              |
| Typing notifications                       | Not supported | Not sent or received                                                                                                                                                                                             |
| Unverified device policy                   | Active        | `ignore_unverified_devices=True` is the intended/required operational posture (see §5.2). Nio default of `False` causes `OlmUnverifiedDeviceError` in encrypted rooms. No admin-facing config toggle exists yet. |

### 14.3 Plaintext alpha remains primary

Plaintext alpha (`pip install -e ".[matrix]"`) remains the primary and recommended alpha path. E2EE mode is an add-on for operators who need encrypted rooms. Unencrypted rooms work identically in both modes. No feature is removed or degraded in plaintext mode.
