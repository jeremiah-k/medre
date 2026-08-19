# MMRelay Behavioral Reference

MEDRE uses MMRelay as a **behavioral reference**, not as a runtime dependency,
shared-library source, or architectural template. MMRelay has accumulated substantial
operational experience with Matrix, `mindroom-nio`, Meshtastic, and the `mtjk` fork.
Where that experience identifies transport behavior that also applies to MEDRE, MEDRE
turns it into an explicit requirement at its own adapter boundaries.

The reference snapshot audited for the requirements below is MMRelay commit
`922de25b26a0e3e769b237f6ee9debe252cb0c91`. MEDRE remains responsible for its own
canonical event model, durable ingress and delivery evidence, supervision, routing,
and adapter lifecycle.

## Interpretation Rules

1. A behavior is adopted only when its transport or SDK semantics also apply to MEDRE.
2. MEDRE does not import MMRelay modules in production or tests.
3. MMRelay global-state patterns are not copied into MEDRE adapter or core code.
4. Existing MEDRE guarantees are preserved when they are stronger or have different
   ownership boundaries.
5. Each adopted behavior has an executable MEDRE test. The source locations below are
   provenance for why the requirement exists, not a second implementation.

## Reference Corpus

The source audit used MMRelay's implementation and its focused tests together. The
most relevant Matrix tests are `tests/test_matrix_auth_discovery.py`,
`tests/test_matrix_client_config.py`, `tests/test_matrix_cross_signing.py`,
`tests/test_matrix_utils_auth_e2ee.py`, `tests/test_matrix_utils_connect_sync.py`,
`tests/test_matrix_utils_relay.py`, and `tests/test_matrix_utils_replies.py`. The most
relevant Meshtastic tests are `tests/test_meshtastic_events.py`,
`tests/test_meshtastic_health.py`, `tests/test_meshtastic_subscriptions.py`,
`tests/test_meshtastic_utils_connect_paths.py`, and
`tests/test_meshtastic_utils_disconnect.py`.

The MEDRE reference tests deliberately do not execute MMRelay code. Their purpose is to
keep the behavioral requirement stable even if either repository is later refactored.

## Matrix Behavior Crosswalk

| Behavior                       | MMRelay evidence                                                                                                      | MEDRE requirement                                                                                                                                               | Disposition                                          |
| ------------------------------ | --------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------- |
| Authenticated device discovery | `src/mmrelay/matrix/auth.py` uses `whoami()` when a device ID is unavailable and reconciles authenticated identity    | Device discovery uses `whoami()` before login restoration when required; a returned user ID must match the configured account                                   | Adopted, with fail-closed identity mismatch handling |
| E2EE client initialization     | `src/mmrelay/matrix/client_config.py` enables E2EE-specific client options                                            | E2EE startup remains fail-closed in required mode and uses MEDRE's configured crypto store/device lifecycle                                                     | Adopted at MEDRE's session boundary                  |
| Rotated peer device keys       | `build_matrix_client_config()` enables `replace_rotated_device_keys` for MMRelay's permissive bot policy              | When the pinned provider exposes the option, encrypted MEDRE sessions enable rotated peer-device-key replacement                                                | Adopted; separate from own-device cross-signing      |
| Own-device cross-signing       | MMRelay reconciles persisted/server-visible cross-signing state                                                       | Runtime reconciliation must never create or rotate master/self-signing identity without the explicit authenticated setup/reset path                             | MEDRE deliberately stricter                          |
| Encrypted-room send            | MMRelay permits bot delivery to unverified peer devices                                                               | MEDRE keeps `ignore_unverified_devices=True` for encrypted sends while preserving own-device cross-signing                                                      | Adopted peer-device compatibility policy             |
| Undecryptable events           | `src/mmrelay/matrix/events.py::on_decryption_failure` treats failed Megolm decrypts as recoverable operational events | Live undecryptable events are counted and never forwarded as canonical messages; startup history and duplicate warnings remain suppressed                       | Adopted at session boundary                          |
| Missing-key requests           | MMRelay creates `MegolmEvent.as_key_request()` and retries bounded to-device delivery with timeout/backoff            | MEDRE requests missing keys at most three times, uses a ten-second per-attempt timeout, caps backoff, propagates cancellation, and exposes secret-free counters | Adopted                                              |
| Sync/reconnect                 | MMRelay has extensive initial-sync and reconnect handling                                                             | `mindroom-nio` owns request/sync parsing and recovery; MEDRE owns the committed Classic Sync cursor and durable ingress acknowledgement                         | MEDRE stronger/different ownership                   |
| Self-message suppression       | `on_room_message()` drops events from the relay account                                                               | Matrix events from MEDRE's configured user ID are dropped before canonical publication                                                                          | Adopted                                              |
| Native Matrix replies          | MMRelay preserves `m.in_reply_to` and reconstructs Matrix reply relations                                             | Matrix native IDs are represented by `NativeRef`; codec and renderer preserve `m.in_reply_to` without leaking Matrix semantics into core                        | Adopted in MEDRE shape                               |

Executable requirements live in `tests/test_matrix_behavior_reference.py`, alongside
specialized Matrix contract, recovery, codec, renderer, and integration tests.

## Meshtastic Behavior Crosswalk

| Behavior                     | MMRelay evidence                                                                                     | MEDRE requirement                                                                                                                                  | Disposition                                                        |
| ---------------------------- | ---------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------ |
| SDK callback thread boundary | MMRelay separates synchronous SDK callbacks from async relay work                                    | SDK reader-thread ingress must cross onto MEDRE's event loop with thread-safe scheduling                                                           | Adopted                                                            |
| Stale packet callbacks       | `src/mmrelay/meshtastic/events.py` rejects packets whose callback interface is not the active client | A callback from a replaced interface must not update session state or publish ingress                                                              | Adopted                                                            |
| Stale disconnect callbacks   | MMRelay ignores connection-loss events from an old interface after reconnect                         | Only the active client may trigger MEDRE reconnect; stale disconnects are counted and ignored                                                      | Adopted                                                            |
| Radio health                 | MMRelay uses transport-specific liveness and active metadata probing                                 | If the pinned SDK exposes `isConnected`, MEDRE treats it as authoritative backup liveness and re-enters its existing reconnect boundary when false | Partially adopted; MMRelay's metadata-probe executor is not copied |
| Reconnect ownership          | MMRelay serializes reconnect and suppresses duplicate triggers                                       | MEDRE retains its bounded session-owned reconnect loop and thread-safe scheduling                                                                  | Equivalent behavior, MEDRE ownership retained                      |
| Shutdown                     | MMRelay tears down subscriptions/client work so late callbacks cannot re-enter the relay             | MEDRE unsubscribes before client close, rejects post-stop callbacks, cancels reconnect work, and drains adapter-owned inbound/background work      | Adopted                                                            |
| Native Meshtastic replies    | MMRelay sends structured replies using the radio packet ID                                           | MEDRE renders a Meshtastic `NativeRef.native_message_id` as `reply_id` while keeping the canonical relation transport-neutral                      | Adopted in MEDRE shape                                             |

Executable requirements live in `tests/test_meshtastic_behavior_reference.py`, alongside
SDK-contract, session, queue, renderer, relation, and recovery tests.

## Intentionally Not Copied

Several MMRelay mechanisms are useful evidence but are not appropriate MEDRE designs:

- MMRelay's module-level Matrix/Meshtastic client state is not imported into MEDRE.
- MMRelay's Matrix sync-token ownership does not replace MEDRE's durable Classic Sync
  checkpoint and ingress acknowledgement contract.
- MMRelay's active Meshtastic metadata-probe executor is not duplicated. MEDRE uses the
  SDK connection event as a backup liveness signal and delegates recovery to its
  existing session supervisor. An active application-level probe would require a
  separate design
  if passive SDK state proves insufficient in production.
- MMRelay's storage/mapping implementation is not shared. MEDRE uses canonical
  relations, `NativeRef`, native-message mappings, outbox state, and delivery evidence.

This boundary lets MEDRE benefit from mature transport experience while keeping one
clear owner for MEDRE semantics and persistence.
