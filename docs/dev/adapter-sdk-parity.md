# Adapter SDK Parity

This reference documents how MEDRE stays honest against the exact adapter
dependency pins. The dependency declarations in `pyproject.toml` and the
lockfile are the version authority; this document deliberately does not
repeat version tables or locked hashes. Dedicated SDK-contract test tiers
import the real pinned packages so fake adapters cannot mask incompatible
constructor signatures, enum values, protobuf fields, or lifecycle behavior.

Each transport has an exact-pin optional-dependency group (`matrix`, `lxmf`,
`meshtastic`, `meshcore`). The `lxmf` extra pins Reticulum explicitly in
addition to `lxmf` itself, keeping installed-SDK contract CI aligned with the
lock instead of resolving a newer transitive Reticulum release.

## Matrix

Matrix has a dedicated `matrix_sdk` contract tier that freezes the Classic
Sync recovery surfaces MEDRE owns. The ordinary fake-heavy test suite runs
without these optional SDKs; the SDK-contract jobs are separate opt-in tiers
and fail when their exact pinned package is missing or changes an interface
MEDRE consumes.

## LXMF / RNS

The `lxmf_sdk` tier executes the real pinned constructor with real
`RNS.Destination` instances and verifies that an arbitrary object is rejected.
This is deliberately an installed-SDK contract rather than another permissive
fake.

### Constructor and local source identity

The important outbound contract is strict: `LXMessage(destination, source, ...)`
accepts an `RNS.Destination` (or `None`) for both destination and source. An
`LXMRouter` is not a valid source. `LXMRouter.register_delivery_identity()`
creates and returns the local inbound `RNS.Destination` for the `lxmf.delivery`
aspect.

This is **not** a new 1.1.x incompatibility. The same strict source-type check
and the same returned delivery destination are present in the audited 0.9.6
source. The earlier MEDRE audit validated the surrounding identity/addressing
model but missed this constructor call-site mismatch because adapter tests used
permissive fakes. This audit corrects that audit gap and adds a real-SDK guard.

MEDRE previously retained only that destination's hash, then incorrectly passed
the router object as `LXMessage.source`. MEDRE now retains the returned local
delivery destination for the session lifetime and passes that exact object to
`LXMessage`. The hash remains separately retained for announces and diagnostics.
A missing delivery destination now fails real-session startup instead of
reporting a connected adapter that cannot receive, announce, or send.

The audit also found that MEDRE previously called
`set_inbound_stamp_cost(None, configured_cost)` before any destination was
registered. The router only applies that setter to a known destination hash.
MEDRE instead passes `stamp_cost` directly to
`register_delivery_identity(...)`, whose public API applies the cost to the
newly created local destination. Zero remains the unset `None` value, and
MEDRE now rejects configured costs above `254` instead of allowing the SDK to
ignore them.

The `lxmf_sdk` tier executes the real 1.1.1 constructor with real
`RNS.Destination` instances and verifies that an arbitrary object is rejected.
This is deliberately an installed-SDK contract rather than another permissive
fake.

### Identity and destination behavior

The currently pinned RNS release retains the expected
`RNS.Destination(identity, direction, type, app_name, *aspects)` contract.
Outbound `SINGLE` destinations require an
identity; the LXMF delivery destination returned by the router is an inbound
`SINGLE` destination for `lxmf.delivery`.

MEDRE's persisted identity ownership is unchanged. The adapter loads or creates
one `RNS.Identity`, constructs one `LXMRouter` with that identity, registers the
same identity for LXMF delivery, and uses the returned delivery destination as
the source of outbound messages.

### Delivery states and methods

The 0.9.6-to-1.1.x source comparison spans 47 upstream commits and substantial
router/stamper changes, but the MEDRE-consumed constructor source contract,
identity-registration return value, outbound propagation selector, and core
delivery-state constants remain stable across the inspected endpoints. The
installed contract freezes the delivery states MEDRE maps:

- `GENERATING = 0x00`
- `OUTBOUND = 0x01`
- `SENDING = 0x02`
- `SENT = 0x04`
- `DELIVERED = 0x08`
- `REJECTED = 0xFD`
- `CANCELLED = 0xFE`
- `FAILED = 0xFF`

The four delivery methods remain opportunistic, direct, propagated, and paper.
MEDRE continues to map terminal delivery callbacks asynchronously and uses a
bounded local retry loop for transient handoff failures; a successful local
`handle_outbound()` call is not an end-to-end delivery guarantee.

### Announces and propagation

`LXMRouter.announce(destination_hash)` resolves the hash through the router's
registered `delivery_destinations`; retaining the local destination therefore
also preserves the existing periodic announce path. The exact contract tier
pins `register_delivery_identity`, `announce`, `set_outbound_propagation_node`,
and `get_outbound_propagation_node` as required surfaces.

The audit found one operational gap here as well: `LXMRouter` initializes its
outbound propagation-node selection to `None`, while MEDRE already exposed
`"propagated"` as a delivery method without any way to select the required
node. The fix adds `outbound_propagation_node` as an optional 16-byte
destination hash, configures it through `set_outbound_propagation_node()`, and
rejects propagated sends when no node is selected. This is transport setup,
not a new MEDRE routing primitive.

### Shutdown ownership

The currently pinned RNS release remains a process singleton. Its
`Reticulum.exit_handler()` is a
global shutdown operation that detaches interfaces and shuts down shared
transport/identity state. There is no per-instance Reticulum `stop()` method,
so MEDRE must not invoke that global exit handler for one adapter session.

`LXMRouter` has separate per-router lifecycle state: construction registers an
`atexit` callback, replaces SIGINT/SIGTERM handlers, and starts a daemon job
loop. The fix therefore snapshots the process signal handlers before each router
constructor and immediately restores those handlers after construction, so an
embedded router cannot retain process-signal ownership. On stop/reconnect MEDRE
calls the owned router's idempotent `exit_handler()` and unregisters its `atexit`
callback. This quiesces
delivery callbacks, links, queue persistence, and router jobs without tearing
down shared RNS transport.

The upstream router job loop has no join/stop primitive and remains a dormant
daemon after `exit_handler_running` is set. MEDRE cannot join that thread using
a public LXMF API; repeated router recreation can therefore leave dormant
daemon threads until process exit. This residual SDK lifecycle limitation is
explicitly deferred rather than worked around with private thread mutation.

## Meshtastic / mtjk

The exact fork tag confirms that the private `_sendPacket` method remains a
stable alias for `_send_packet`, is synchronous, and accepts the `MeshPacket`,
`destinationId`, and `wantAck` surface MEDRE uses. `sendText()` and `close()` are
also synchronous, so MEDRE correctly executes blocking sends via
`asyncio.to_thread()` and calls close synchronously during shutdown.

MMRelay independently exercises the same mtjk callback/send API family and
remains a useful behavior reference. The contract tier still freezes the SDK
contract directly in MEDRE so MMRelay behavior is corroborating evidence rather
than a transitive dependency.

The executable `meshtastic_sdk` tier freezes:

- installed mtjk/PyPubSub distributions match MEDRE's current declared `meshtastic` extra;
- synchronous `sendText`, `_sendPacket`, `_generatePacketId`, and `close`;
- the `_sendPacket` call shape MEDRE uses, including explicit `wantAck=False`;
- real uint32 packet-ID generation;
- protobuf `Data.portnum`, `Data.payload`, `Data.reply_id`, and `Data.emoji`;
- `TEXT_MESSAGE_APP` availability;
- SDK decoded-data payload ceiling of 233 bytes;
- MEDRE's default final text budget of 227 UTF-8 bytes fitting below that cap;
- the `meshtastic.receive` and `meshtastic.connection.lost` pubsub topics;
- MEDRE unsubscribe-before-close shutdown ordering.

MEDRE's structured reply/reaction path remains intentionally lower-level than
`sendText`: it constructs protobuf `Data`, sets `reply_id` and optional
`emoji=1`, allocates a packet ID when the compatibility generator is available,
and hands the packet to `_sendPacket`. Plain text without a native relation
continues to use `sendText`.

Shutdown ownership is unchanged: MEDRE unsubscribes both pubsub callbacks before
closing the client, preventing callback retention across reconnect/session
lifetimes.

## MeshCore

The 2.3.8 source audit found one redundant lifecycle action rather than a
wire-format mismatch: all three SDK factories call `connect()`, and `connect()`
already sends the required `APP_START`. MEDRE had been sending a second
`APP_START` immediately after factory return. The fix removes that duplicate
and opportunistically reads the SDK's public `self_info` snapshot for safe
diagnostics. The executable `meshcore_sdk` tier freezes the surfaces MEDRE
relies on:

- the installed MeshCore distribution matches MEDRE's current declared `meshcore` extra;
- `create_tcp`, `create_serial`, and `create_ble` accept MEDRE's explicit
  `auto_reconnect=False` control without pinning unrelated SDK defaults;
- MEDRE remains the reconnect owner and now passes `auto_reconnect=False`
  explicitly instead of depending only on the SDK default;
- `CONTACT_MSG_RECV`, `CHANNEL_MSG_RECV`, `MSG_SENT`, `ACK`, `CONTACTS`,
  `SELF_INFO`, and `DISCONNECTED` retain their expected string values;
- every `create_*()` factory calls `connect()`, and `connect()` performs the
  required initial `send_appstart()`; MEDRE no longer sends a duplicate
  `APP_START` after factory return;
- a real `MSG_SENT` frame decodes a four-byte `expected_ack` and an integer
  `suggested_timeout` measured in milliseconds;
- subscription management remains synchronous while `disconnect()` is async,
  and MEDRE unsubscribes before stopping fetch/disconnecting.

MEDRE's direct-message retry path correctly treats the four-byte ACK as the
native correlation identifier and converts `suggested_timeout` from
milliseconds to seconds before caching/using it. Channel sends do not use that
direct-message ACK retry delay. On each MEDRE reconnect, a fresh SDK factory
call performs the single required `APP_START`; SDK auto-reconnect remains
disabled.

## CI ownership

### Contract-version authority

`pyproject.toml` is the version authority for installed adapter SDKs. Contract
tests read the exact pins from the selected optional-dependency group and verify
the installed distributions match them; they do not duplicate version literals.
This keeps Renovate pin bumps meaningful: CI fails for a consumed API/behavior
change or a dependency-resolution mismatch, not merely because the expected
version string changed. Historical pin tables in this audit remain point-in-time
evidence for the original parity review.

The `adapter-sdk-contract` job installs one optional adapter extra at a time and
runs only its contract marker across Python 3.11, 3.12, 3.13, and 3.14:

- `lxmf_sdk` with `medre[lxmf]`
- `meshtastic_sdk` with `medre[meshtastic]`
- `meshcore_sdk` with `medre[meshcore]`

The default suite explicitly excludes all three markers, just as it excludes
`matrix_sdk`, `live`, `docker`, and `hardware`. This keeps optional dependency
boundaries honest while making dependency upgrades executable rather than
comment-only audits.

## Open parity gaps

Reliability gaps between MEDRE and the reference implementations that are
still open. Each is characterized behaviorally by
`tests/test_sdk_parity_runtime_backlog.py` so improvements and regressions
are detectable; none is a normative spec obligation.

- **Meshtastic connection liveness.** No periodic TCP health verification
  exists; a silently dropped half-open TCP connection leaves the bridge deaf
  until an outbound send fails. Candidate: a configurable health-check
  interval issuing a bounded SDK call.
- **Meshtastic reconnect budget.** Reconnect backoff is capped at 30 seconds
  with a maximum of 10 attempts, after which the session gives up. A bridge
  that outlives radio downtime may want a longer cap and no attempt ceiling.
- **Meshtastic queue water-marks.** The outbound queue has no warning
  thresholds before capacity rejection; fill is only visible after
  `MeshtasticSendError`.
- **Matrix sync-token durability.** Runtime-managed Matrix adapters persist
  MEDRE-owned Classic Sync checkpoints (see
  [spec/durable-ingress.md](../spec/durable-ingress.md)); nio-internal
  `store_sync_tokens` remains disabled by design. Sessions constructed
  without checkpoint callbacks (test-only paths) still perform a full
  initial sync.
- **Matrix stale-sync watchdog.** Stale-sync detection exists in
  `health_check()` with a threshold; there is no proactive watchdog beyond
  it.
- **Matrix key-request rate limiting.** Undecryptable-event key requests are
  not rate-limited; the 60-second logging dedup does not gate the to-device
  send.
- **LXMF outbound-tracking eviction detail.** The bounded outbound tracking
  set logs only a count on eviction, not the state/age of the evicted
  entries.

## Remaining evidence gaps

This reference covers SDK parity, not hardware validation. Remaining gaps are
intentionally unchanged:

- LXMF multi-hop/propagation-node live behavior and real delivery-state timing;
- Meshtastic TCP/serial/BLE hardware callback timing and RF ACK behavior;
- MeshCore TCP/serial hardware validation and long-running ACK/reconnect behavior.

Those belong to the transport-realism work (see
[spec/appendices/transport-realism.md](../spec/appendices/transport-realism.md)).
The contract is that MEDRE's code and tests agree with the exact pinned SDK
interfaces before hardware validation is introduced.
