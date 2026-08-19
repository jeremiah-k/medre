# Adapter SDK Parity Audit

This audit verifies MEDRE against the exact adapter dependency pins. It supplements the historical snapshots in `source-audits.md` and
`adapter-reality-audit.md`; it does not rewrite those older observations.

The dependency declarations and lockfile are the version authority. Dedicated
SDK-contract test tiers import the real pinned packages so fake adapters cannot
mask incompatible constructor signatures, enum values, protobuf fields, or
lifecycle behavior.

## Audited pins

| Adapter | Distribution pin | Source/runtime basis |
| --- | --- | --- |
| Matrix | `mindroom-nio==0.40.0` | existing exact installed `matrix_sdk` CI contract from the earlier parity round; no runtime delta in this audit |
| LXMF | `lxmf==1.1.1` | lockfile artifact + exact installed distribution in `lxmf_sdk` CI; upstream 1.1.0 mirror and 0.9.6 tag corroborate the consumed API where the public Git mirror does not expose 1.1.1 |
| Reticulum | `rns==1.4.2` in the LXMF extra | exact upstream `1.4.2` source and exact installed distribution in `lxmf_sdk` CI |
| Meshtastic | `mtjk==2.7.11.post5` | exact upstream fork tag plus exact installed distribution in `meshtastic_sdk` CI |
| MeshCore | `meshcore==2.3.8` | exact upstream `v2.3.8` source plus exact installed distribution in `meshcore_sdk` CI |

The lockfile records the exact source artifacts used when the pins were resolved:

| Distribution | Locked sdist SHA-256 |
| --- | --- |
| `lxmf==1.1.1` | `f2f7ea17d793fcc32cab826e81e8e9824404d025d1fc71b143be3242d45e6a5e` |
| `rns==1.4.2` | `275e4369819c99fbbdb8b70a0d4eb3fc9767716fca639fe7206856839fb3867a` |
| `mtjk==2.7.11.post5` | `3bd50eb5bd4db2daf8a2cbc0aa4e57322a809ac8a48ad5cab47c50b57fb7e27e` |
| `meshcore==2.3.8` | `22d57dbb59186af6ed2303fd149635022989a4f8dc867c729a6f0abc34ad3aab` |

The LXMF extra pins `rns==1.4.2` explicitly in addition to `lxmf==1.1.1`;
this keeps installed-SDK contract CI aligned with the lock instead of resolving
a newer transitive Reticulum release.

## Matrix 0.40.0 status

Matrix has a dedicated `matrix_sdk` contract tier that freezes the Classic Sync
recovery surfaces MEDRE owns. This audit rechecks the exact pin but makes no
Matrix runtime change; the adapter changes here concern LXMF, Meshtastic, and
MeshCore.

The ordinary fake-heavy test suite still runs without these optional SDKs. The
SDK-contract jobs are separate opt-in tiers and fail when their exact pinned
package is missing or changes an interface MEDRE consumes.

## LXMF 1.1.1 / RNS 1.4.2

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

RNS 1.4.2 retains the expected `RNS.Destination(identity, direction, type,
app_name, *aspects)` contract. Outbound `SINGLE` destinations require an
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

RNS 1.4.2 remains a process singleton. Its `Reticulum.exit_handler()` is a
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

## Meshtastic / mtjk 2.7.11.post5

The exact fork tag confirms that the private `_sendPacket` method remains a
stable alias for `_send_packet`, is synchronous, and accepts the `MeshPacket`,
`destinationId`, and `wantAck` surface MEDRE uses. `sendText()` and `close()` are
also synchronous, so MEDRE correctly executes blocking sends via
`asyncio.to_thread()` and calls close synchronously during shutdown.

MMRelay independently exercises the same `mtjk==2.7.11.post5` pin and remains
a useful behavior reference for callback/send semantics. The contract tier still freezes
the SDK contract directly in MEDRE so MMRelay behavior is corroborating
evidence rather than a transitive dependency.

The executable `meshtastic_sdk` tier freezes:

- exact distribution version `2.7.11.post5`;
- synchronous `sendText`, `_sendPacket`, `_generatePacketId`, and `close`;
- `_sendPacket` argument shape and `wantAck=False` default;
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

## MeshCore 2.3.8

The 2.3.8 source audit found one redundant lifecycle action rather than a
wire-format mismatch: all three SDK factories call `connect()`, and `connect()`
already sends the required `APP_START`. MEDRE had been sending a second
`APP_START` immediately after factory return. The fix removes that duplicate
and opportunistically reads the SDK's public `self_info` snapshot for safe
diagnostics. The executable `meshcore_sdk` tier freezes the surfaces MEDRE
relies on:

- exact distribution version `2.3.8`;
- `create_tcp`, `create_serial`, and `create_ble` all expose
  `auto_reconnect=False` by default and `max_reconnect_attempts=3`;
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

The `adapter-sdk-contract` job installs one optional adapter extra at a time and
runs only its contract marker across Python 3.11, 3.12, 3.13, and 3.14:

- `lxmf_sdk` with `medre[lxmf]`
- `meshtastic_sdk` with `medre[meshtastic]`
- `meshcore_sdk` with `medre[meshcore]`

The default suite explicitly excludes all three markers, just as it excludes
`matrix_sdk`, `live`, `docker`, and `hardware`. This keeps optional dependency
boundaries honest while making dependency upgrades executable rather than
comment-only audits.

## Remaining evidence gaps

This audit covers SDK parity, not hardware validation. Remaining gaps are
therefore intentionally unchanged:

- LXMF multi-hop/propagation-node live behavior and real delivery-state timing;
- Meshtastic TCP/serial/BLE hardware callback timing and RF ACK behavior;
- MeshCore TCP/serial hardware validation and long-running ACK/reconnect behavior.

Those belong to the transport-realism work. The contract is that MEDRE's code and tests agree with the exact pinned SDK
interfaces before hardware validation is introduced.
