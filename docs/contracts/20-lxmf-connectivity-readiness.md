# LXMF/Reticulum Connectivity Readiness

> Contract version: 1
> Last updated: 2026-05-09
> Track: 1 (Contract)
> Supersedes: Nothing. Supplements contracts 13, 14, 16, 18.

This document consolidates all confirmed, inferred, and unknown findings
about the LXMF and Reticulum SDKs into a single connectivity readiness
assessment. It is the authoritative reference for what the SDK provides,
what the MEDRE adapter scaffold currently looks like, and what remains
genuinely unknown until someone runs code against a real Reticulum
network.

**No production LXMF/Reticulum connectivity is claimed.
The adapter supports `connection_type="reticulum"` for real Reticulum/LXMF operation via `LxmfSession` when optional dependencies are installed; fake mode remains the default.**


## 1. Package and Import

| Item | Value |
|------|-------|
| LXMF PyPI distribution | `lxmf` |
| LXMF import | `import LXMF` |
| LXMF version audited | 0.9.6 |
| LXMF source | `/home/jeremiah/dev/LXMF/LXMF/` |
| Reticulum PyPI distribution | `rns` |
| Reticulum import | `import RNS` |
| Reticulum version audited | 1.2.4 |
| Reticulum source | `/home/jeremiah/dev/Reticulum/RNS/` |
| Author | Mark Qvist |
| Wire format | msgpack (not protobuf) |
| Serialization | `RNS.vendor.umsgpack` bundled |


## 2. Confirmed SDK Findings

Everything in this section is verified by reading the SDK source code at
the paths listed above. Nothing here depends on a running network.

### 2.1 Reticulum Identity

```python
import RNS

# Create a new identity (generates X25519 + Ed25519 keypair)
identity = RNS.Identity()

# The identity hash is the first 16 bytes of SHA-256(public_key)
identity.hash       # bytes, 16 bytes
identity.hexhash    # str, 32 hex chars

# Persist to file (writes 64-byte private key)
identity.to_file("/path/to/identity")

# Load from file
identity = RNS.Identity.from_file("/path/to/identity")

# Save only public key
identity.pub_to_file("/path/to/pubkey")

# Load from raw bytes
identity = RNS.Identity.from_bytes(prv_bytes)
```

Key details:

- Private key is 64 bytes (32-byte X25519 + 32-byte Ed25519).
- Public key is 64 bytes (32-byte X25519 + 32-byte Ed25519).
- `to_file()` writes the raw private key bytes. Anyone with this file
  can decrypt all communication. This is a single file, not a
  directory.
- `from_file()` returns `None` on invalid data, not an exception.
- `RNS.Identity()` with default `create_keys=True` generates fresh
  keypairs immediately.
- `RNS.Identity(create_keys=False)` creates an empty identity shell
  for loading existing keys.

### 2.2 Reticulum Singleton

```python
import RNS

# Initialize Reticulum (must be done before anything else)
reticulum = RNS.Reticulum(configdir="/path/to/reticulum_config")
# Or use default config search:
#   1. /etc/reticulum/config
#   2. ~/.config/reticulum/config
#   3. ~/.reticulum/config

# It is a singleton. Second instantiation raises OSError.
# RNS.Reticulum.get_instance() retrieves the running instance.

# Shutdown
RNS.Reticulum.exit_handler()  # or RNS.exit(code)
```

Key details:

- `RNS.Reticulum()` is a singleton per process. Calling it twice raises
  `OSError("Attempt to reinitialise Reticulum...")`.
- The configdir contains the `config` file and `storage/`, `interfaces/`,
  and `storage/identities/` subdirectories.
- The first instance in a process becomes the "master" that owns
  hardware interfaces. Subsequent programs on the same system connect
  to it via local IPC (TCP port 37428 by default, or AF_UNIX socket).
- `configdir` defaults: `/etc/reticulum` > `~/.config/reticulum` >
  `~/.reticulum`, first one with an existing `config` file wins.

### 2.3 Destination

```python
import RNS

# Create an inbound LXMF delivery destination
destination = RNS.Destination(
    identity,              # RNS.Identity instance
    RNS.Destination.IN,    # direction: IN or OUT
    RNS.Destination.SINGLE,# type: SINGLE, GROUP, PLAIN, LINK
    "lxmf",                # app_name
    "delivery"             # *aspects (varargs)
)

destination.hash          # bytes, 16 bytes (destination hash)
destination.identity      # the Identity bound to this destination
destination.display_name  # optional display name attribute

# Announce presence on the network
destination.announce(app_data=bytes)

# Register callbacks
destination.set_packet_callback(callback)          # packet received
destination.set_link_established_callback(callback) # link established
```

Key details:

- Destination hash derivation:
  `SHA-256(SHA-256("lxmf.delivery")[:10] + identity_hash)[:16]`.
- LXMF uses two destination aspects: `"delivery"` for messages,
  `"propagation"` for propagation node sync.
- `SINGLE` type enables encryption. `GROUP` uses symmetric key.
  `PLAIN` is unencrypted.
- The destination hash is 16 bytes (TRUNCATED_HASHLENGTH = 128 bits).

### 2.4 Transport and Path

```python
import RNS

# Check if a path is known to a destination
RNS.Transport.has_path(destination_hash)  # returns bool

# Request a path to a destination (async, non-blocking)
RNS.Transport.request_path(destination_hash)

# Register an announce handler
RNS.Transport.register_announce_handler(handler)
```

### 2.5 Link

```python
import RNS

# Establish an encrypted link to a destination
link = RNS.Link(destination)
link.status          # RNS.Link.ACTIVE, RNS.Link.CLOSED, etc.
link.teardown()      # close the link
```

Links provide encrypted sessions with forward secrecy. Link MDU is 431
bytes by default. LXMF uses links for DIRECT delivery mode.

### 2.6 LXMRouter

```python
import LXMF

# Create an LXMF router (storagepath is required)
router = LXMF.LXMRouter(
    identity=None,              # auto-generated if None
    storagepath="/path/to/storage",  # REQUIRED, raises ValueError if None
    autopeer=True,
)

# Register a delivery identity
dest = router.register_delivery_identity(
    identity,          # RNS.Identity
    display_name="MEDRE Node",  # optional, shown in announces
    stamp_cost=8,      # optional PoW cost for inbound messages
)
# Returns an RNS.Destination for the "lxmf.delivery" aspect

# Register a callback for all received messages
router.register_delivery_callback(callback)
# callback signature: callback(lxmessage)

# Announce presence
router.announce(dest.hash)

# Set propagation node for outbound store-and-forward
router.set_outbound_propagation_node(node_hash)  # 16-byte bytes

# Enable local propagation node
router.enable_propagation()

# Request messages from propagation node
router.request_messages_from_propagation_node(identity, max_messages=0)

# Send a message
message = LXMF.LXMessage(destination, source, "content")
router.handle_outbound(message)

# Shutdown
router.exit_handler()   # persists state, tears down links
# Then:
RNS.exit(0)
```

Key details:

- `LXMRouter.__init__` requires `storagepath`. It raises `ValueError`
  if it is `None`. The actual storage goes to `storagepath + "/lxmf"`.
- Only one delivery identity per router instance is supported (as of
  0.9.6). `register_delivery_identity` logs an error and returns `None`
  if called a second time.
- The router starts a background daemon thread (`jobloop`) for
  processing outbound messages. This thread runs for the lifetime of
  the router.
- `handle_outbound()` sets the message state to `OUTBOUND`, packs it,
  checks path availability, and adds it to `pending_outbound`. The
  background thread then handles link establishment, retries, and
  delivery.
- `enable_propagation()` indexes the message store from disk and sets
  up propagation node callbacks. This is a blocking operation that
  scans all files in the message store directory.
- Signal handlers (SIGINT, SIGTERM) are registered automatically.

### 2.7 LXMessage

```python
import LXMF

# Create a message
message = LXMF.LXMessage(
    destination,       # RNS.Destination or None
    source,            # RNS.Destination or None
    content="Hello",   # str or bytes
    title="Subject",   # str or bytes, optional
    fields=None,       # dict, optional
    desired_method=LXMF.LXMessage.DIRECT,
)

# Message properties
message.hash                # bytes, 32 bytes, SHA-256 of wire data
message.state               # int enum
message.title_as_string()   # str
message.content_as_string() # str
message.get_fields()        # dict

# Callbacks
message.register_delivery_callback(cb)   # cb(msg) on SENT/DELIVERED
message.register_failed_callback(cb)     # cb(msg) on FAILED/REJECTED/CANCELLED

# Content setters
message.set_title_from_string("title")
message.set_content_from_string("body")
message.set_fields({"key": "value"})
```

Message state transitions:

```
GENERATING (0x00) -> OUTBOUND (0x01) -> SENDING (0x02) -> SENT (0x04) -> DELIVERED (0x08)
                                                            \
                                                             -> FAILED (0xFF)
                                                             -> REJECTED (0xFD)
                                                             -> CANCELLED (0xFE)
```

Delivery methods:

| Method | Code | Description |
|--------|------|-------------|
| `LXMessage.OPPORTUNISTIC` | `0x01` | Single packet, fire-and-forget. Max 295 bytes encrypted content. |
| `LXMessage.DIRECT` | `0x02` | Link-based, reliable. Up to 319 bytes per packet, larger via RNS Resource transfer. |
| `LXMessage.PROPAGATED` | `0x03` | Store-and-forward via propagation node. |
| `LXMessage.PAPER` | `0x05` | Offline transfer via QR code or `lxm://` URI. |

### 2.8 Message Hash as native_message_id

The LXMF message hash (`lxm.hash`) is a 32-byte SHA-256 digest computed
over `destination_hash + source_hash + msgpack_payload`. This is a strong
candidate for `native_message_id` in MEDRE's `NativeMessageRef`:

```python
# MEDRE already maps this in LxmfCodec.decode():
hex_message_id = lxm.hash.hex()  # 64 hex chars
```

This is already implemented in the fake adapter with deterministic
SHA-256 hashes. The real adapter would use the actual `lxm.hash`.

### 2.9 Fields Dict

LXMF defines field keys in `LXMF.LXMF.py`. MEDRE uses `FIELD_CUSTOM_META`
(0xFD) for its metadata envelope. Full field list from the source:

| Key | Constant | Description |
|-----|----------|-------------|
| `0x01` | `FIELD_EMBEDDED_LXMS` | Embedded LXMF messages |
| `0x02` | `FIELD_TELEMETRY` | Telemetry data |
| `0x03` | `FIELD_TELEMETRY_STREAM` | Streaming telemetry |
| `0x04` | `FIELD_ICON_APPEARANCE` | Icon/appearance metadata |
| `0x05` | `FIELD_FILE_ATTACHMENTS` | File attachments |
| `0x06` | `FIELD_IMAGE` | Image data |
| `0x07` | `FIELD_AUDIO` | Audio data (Codec2, Opus modes) |
| `0x08` | `FIELD_THREAD` | Conversation grouping |
| `0x09` | `FIELD_COMMANDS` | Command interface |
| `0x0A` | `FIELD_RESULTS` | Command results |
| `0x0B` | `FIELD_GROUP` | Group addressing |
| `0x0C` | `FIELD_TICKET` | Reply permission (PoW bypass) |
| `0x0D` | `FIELD_EVENT` | Event data |
| `0x0E` | `FIELD_RNR_REFS` | RNR references |
| `0x0F` | `FIELD_RENDERER` | Renderer hint (plain, micron, markdown, bbcode) |
| `0xFB` | `FIELD_CUSTOM_TYPE` | Custom type identifier |
| `0xFC` | `FIELD_CUSTOM_DATA` | Custom payload |
| `0xFD` | `FIELD_CUSTOM_META` | **MEDRE uses this** for metadata envelope |
| `0xFE` | `FIELD_NON_SPECIFIC` | Non-specific extension |
| `0xFF` | `FIELD_DEBUG` | Debug information |

### 2.10 Stamp Cost (Proof of Work)

LXMF implements a proof-of-work rate limiting mechanism via stamps:

- `stamp_cost` is configured per destination. A cost of 0 or None means
  no stamp required.
- Stamps are validated against `LXStamper.stamp_valid(stamp, cost,
  workblock)`.
- Higher stamp_cost means more CPU work before the message is accepted.
- `LXMRouter.set_inbound_stamp_cost(dest_hash, cost)` configures the
  cost for inbound messages to that destination.
- Tickets (FIELD_TICKET) allow bypassing stamp generation for authorized
  repliers.

### 2.11 Shutdown Sequence

Confirmed from source:

```python
# LXMF router shutdown
router.exit_handler()
# This:
#   1. Tears down delivery destination callbacks
#   2. Tears down propagation links (if propagation node)
#   3. Persists peer sync states to disk
#   4. Saves locally delivered/processed transient IDs
#   5. Saves node stats

# Reticulum shutdown
RNS.exit(0)
# Equivalent to RNS.Reticulum.exit_handler(), which:
#   1. Detaches interfaces
#   2. Runs Transport.exit_handler()
#   3. Runs Identity.exit_handler() (saves known destinations)
```

The router also registers its own `atexit` handler and SIGINT/SIGTERM
handlers during `__init__`.


## 3. Inferred Behaviors

These are plausible assumptions based on the source code, but have not
been verified with a running Reticulum instance.

### 3.1 Config Directory Interaction

The adapter's `identity_path` config field would point to a file
produced by `RNS.Identity.to_file(path)`. The Reticulum configdir
(`~/.reticulum` by default) is a separate concern: it controls transport
interfaces (TCP, UDP, serial, RNode, etc.). The adapter likely needs
both an identity file path and either a shared Reticulum instance or
its own configdir.

### 3.2 Resource Handling for Attachments

`RNS.Resource` handles arbitrary-size data transfer over links. LXMF
uses this for messages larger than the single-packet limit (319 bytes
over a link). The `FIELD_FILE_ATTACHMENTS` (0x05) field presumably
references resource data, but the exact mechanism for correlating
field entries with resource transfers is not documented in the source
and would need live testing.

### 3.3 Channel/Buffer Streaming

`RNS.Channel` and `RNS.Buffer` provide reliable sequential delivery
over links. These are higher-level abstractions that LXMF does not
currently use (LXMF has its own message-level sequencing via Resource).
They may be relevant for future streaming or bulk transfer features.

### 3.4 Single Delivery Identity

The router enforces one delivery identity per instance. This means
MEDRE would either need one LXMRouter per identity or accept the
limitation. Multiple adapters each wanting their own identity would
each need their own `LXMRouter` and potentially their own Reticulum
instance (or shared instance).

### 3.5 Threading Model

Both Reticulum and LXMF use background daemon threads (not asyncio).
The router's `jobloop` runs in a thread. Transport processing runs in
threads. This will need careful integration with MEDRE's asyncio event
loop, likely via `loop.run_in_executor()` or callback-based bridging
similar to how the Meshtastic adapter handles sync callbacks.

### 3.6 Auto-Discovery

Reticulum auto-discovers peers on local interfaces. The `AutoInterface`
detects other Reticulum nodes on the same network segment. No explicit
host/port configuration is needed for local mesh operation. The TCP and
UDP interfaces do require explicit configuration in the Reticulum
config file.


## 4. Unknowns

These questions cannot be answered from source code alone. They require
running Reticulum with real or simulated transport.

### 4.1 Transport Interface Configuration

What Reticulum config file entries are needed for a test environment?
The default config provides basic local connectivity, but the exact
interface definitions for a developer test setup (e.g., two LXMF
instances on the same machine via TCP) are not documented in the MEDRE
context.

### 4.2 Multiple RNS.Reticulum Instances

The singleton pattern means only one `RNS.Reticulum` per process. For
testing with sender and receiver in the same process, either:
- Two separate processes are needed, or
- The shared instance mechanism (local IPC on port 37428) is used,
  or
- One side uses `connection_type="fake"`.

### 4.3 Default Config Paths

The search order is `/etc/reticulum` > `~/.config/reticulum` >
`~/.reticulum`. On first run with no existing config, Reticulum creates
a default config at `~/.reticulum/config`. The exact content of this
default config (which interfaces are enabled, what parameters) affects
what works out of the box.

### 4.4 LXMF Message Object Shape for Codec

MEDRE's `LxmfCodec.decode()` currently expects a dict shaped like a
packet. When receiving real LXMF messages via the delivery callback,
the callback receives an `LXMessage` object. The adapter will need to
convert the `LXMessage` attributes into the dict shape the codec
expects, or the codec needs to accept `LXMessage` objects directly.

### 4.5 Async/Sync Boundary Behavior

Reticulum's threading model and LXMF's callback-based delivery interact
with Python's GIL and asyncio in ways that are hard to predict from
source alone. Deadlocks, event loop blocking, and callback timing under
load are all potential issues.

### 4.6 Error Recovery

What happens when a link drops mid-transfer? When path requests time
out? When propagation node sync fails? The retry logic exists in
`process_outbound`, but the actual behavior under network failure
conditions is untested in the MEDRE context.


## 5. Current Adapter Scaffold Status

The MEDRE LXMF adapter (`src/medre/adapters/lxmf/`) is in tranche 1.
Here is exactly what exists and what does not.

### 5.1 What Exists

| Component | File | Status |
|-----------|------|--------|
| `LxmfAdapter` | `adapter.py` | Scaffold. `start()` only supports `connection_type="fake"`. `deliver()` returns `None`. |
| `LxmfConfig` | `config.py` | Functional. `connection_type` accepts `"fake"` and `"reticulum"` as valid shapes. `reticulum` validates at config level (shape only); runtime `start()` raises `LxmfConnectionError` for non-fake modes. `identity_path` is a `str | None` placeholder. |
| `LxmfCodec` | `codec.py` | Functional against dict-shaped test data. Does not accept `LXMessage` objects. |
| `LxmfRenderer` | `renderer.py` | Functional. Produces content/title/fields dicts. |
| `LxmfFieldsHelper` | `fields.py` | Functional. Embeds/extracts MEDRE envelope under `FIELD_CUSTOM_META` (0xFD). |
| `LxmfPacketClassifier` | `packet_classifier.py` | Functional against dict-shaped test data. |
| Error hierarchy | `errors.py` | Complete. `LxmfError` base, `LxmfConnectionError`, `LxmfSendError`, `LxmfConfigError`, `LxmfCodecError`, `LxmfPacketError`. |

### 5.2 What Does NOT Exist

| Missing Piece | Impact |
|---------------|--------|
| No real `RNS.Identity` loading | `identity_path` in config is unwired. |
| No `RNS.Reticulum()` initialization | No Reticulum transport layer. |
| No `LXMF.LXMRouter()` creation | No LXMF message routing. |
| No `register_delivery_identity` call | No inbound message destination. |
| No `register_delivery_callback` | No message reception. |
| No `router.announce()` | No presence on the network. |
| No `router.handle_outbound()` | No real message sending. |
| No `router.exit_handler()` / `RNS.exit()` | No clean shutdown. |
| No `RNS.Transport` path management | No path discovery. |
| No `LXMessage` to dict conversion | Codec expects dicts, not LXMessage objects. |

### 5.3 Config Gaps

`LxmfConfig` has fields for `identity_path`, `default_delivery_method`,
`stamp_cost`, and `display_name`. None of these are wired to real
Reticulum/LXMF operations. The `connection_type` field accepts `"fake"`
and `"reticulum"` as valid shape values. `start()` raises
`LxmfConnectionError` for `"reticulum"` mode regardless of SDK
availability — production connectivity is not implemented.

Additional config fields that would be needed for real connectivity:

- `reticulum_configdir` for `RNS.Reticulum(configdir=...)`.
- `storage_path` for `LXMF.LXMRouter(storagepath=...)`.
- `propagation_node` (destination hash) for store-and-forward.
- `listen` (bool) for whether to announce and receive.


## 6. Delivery Method Semantics

| Method | Wire Behavior | Reliability | Size Limit | Use Case |
|--------|--------------|-------------|------------|----------|
| DIRECT | Establishes `RNS.Link`, sends via link packet or `RNS.Resource` | High. Retries up to `MAX_DELIVERY_ATTEMPTS` (5). Proof receipts confirm delivery. | Link packet: 319B. Resource: arbitrary. | Default for most messaging. |
| OPPORTUNISTIC | Single RNS packet, no link. Embedded in opportunistic route. | Best-effort. No ACK, no retry. Max 1 attempt (`MAX_PATHLESS_TRIES=1`). | 295 bytes encrypted content. | Quick, low-overhead messages to online peers. |
| PROPAGATED | Delivered to propagation node via link. Node stores for recipient. | Moderate. Delivery to node is reliable. Recipient must sync from node. | Limited by propagation node config (`PROPAGATION_LIMIT=256KB`). | Offline recipients, delayed delivery. |
| PAPER | Encoded as QR code or `lxm://` URI. No network transport. | None. Physical delivery only. | `PAPER_MDU` varies, roughly 2KB. | Air-gapped transfer, QR scanning. |

### 6.1 Method Selection Logic

When `desired_method` is set on an `LXMessage`, the router respects it
if feasible. If `desired_method=DIRECT` but no path exists, the router
requests a path and retries. If `desired_method=OPPORTUNISTIC` and no
path exists, the router requests a path and waits
(`PATH_REQUEST_WAIT=7s`), then fails if still no path.

If `desired_method` is `None`, the router selects the best available
method. The message is packed first, then the router decides based on
size and path availability.

### 6.2 Implications for MEDRE

The `LxmfConfig.default_delivery_method` field maps to these methods.
For a mesh messaging scenario:

- DIRECT is the best default for online peers.
- PROPAGATED is essential for offline delivery.
- OPPORTUNISTIC is useful for quick status messages.
- PAPER is unlikely to be needed in MEDRE's bridging use case.


## 7. Storage Paths

Reticulum and LXMF both use filesystem directories for persistent state.
These are separate from each other and from MEDRE's storage.

| System | Path | Contents |
|--------|------|----------|
| Reticulum config | `~/.reticulum/` (default) | `config` file, `storage/`, `interfaces/` |
| Reticulum storage | `{configdir}/storage/` | `known_destinations`, `cache/`, `resources/`, `identities/` |
| LXMF router storage | `{storagepath}/lxmf/` | `local_deliveries`, `locally_processed`, `outbound_stamp_costs`, `available_tickets`, `messagestore/`, `ratchets/` |
| MEDRE SQLite | Configured by MEDRE | Events, receipts, native refs, route configs |

The LXMF `storagepath` is passed to `LXMRouter(storagepath=...)`. It
must be a writable directory. The router creates `lxmf/` inside it.

For MEDRE, these would be configured via adapter config:
- `reticulum_configdir` -> `RNS.Reticulum(configdir=...)`
- `storage_path` -> `LXMRouter(storagepath=...)`
- `identity_path` -> `RNS.Identity.from_file(...)`


## 8. Message Lifecycle: Inbound

Confirmed sequence from source code:

1. `RNS.Reticulum()` initializes transport, loads interfaces.
2. `LXMRouter(identity, storagepath)` registers announce handlers.
3. `router.register_delivery_identity(identity, display_name)` creates
   an `RNS.Destination` with the `lxmf.delivery` aspect, enables
   ratchets, sets packet and link callbacks.
4. `router.register_delivery_callback(cb)` registers the application
   callback.
5. `router.announce(dest.hash)` broadcasts presence.
6. When a packet arrives at the delivery destination, the router calls
   `delivery_packet()` or `delivery_link_established()`.
7. The router unpacks the `LXMessage`, validates signature, checks
   stamps, deduplicates via transient ID cache.
8. The delivery callback fires: `cb(lxmessage)` with the validated
   `LXMessage` object.
9. The adapter would need to convert `LXMessage` to a dict for
   `LxmfCodec.decode()`, or the codec needs to accept `LXMessage`.

### 8.1 Conversion Gap

The current codec expects a dict like:

```python
{
    "source_hash": bytes,        # 16 bytes
    "destination_hash": bytes,   # 16 bytes
    "message_id": str,           # hex string
    "title": str,
    "content": str,
    "fields": dict,
    "timestamp": float,
    "is_direct_message": bool,
}
```

A real `LXMessage` has:
- `lxm.source_hash` (bytes, 16B)
- `lxm.destination_hash` (bytes, 16B)
- `lxm.hash` (bytes, 32B, the message ID)
- `lxm.title_as_string()` (str)
- `lxm.content_as_string()` (str)
- `lxm.get_fields()` (dict)
- `lxm.timestamp` (float)
- `lxm.method` (int: DIRECT, OPPORTUNISTIC, PROPAGATED, PAPER)
- `lxm.signature_validated` (bool)
- `lxm.transport_encrypted` (bool)

The adapter needs a thin conversion layer between `LXMessage` objects
and the codec's expected dict shape.


## 9. Message Lifecycle: Outbound

Confirmed sequence from source code:

1. Create `LXMessage(destination, source, content, title, fields,
   desired_method)`.
2. `router.handle_outbound(lxm)`:
   - Sets state to `OUTBOUND`.
   - Checks for outbound ticket (for stamp bypass).
   - Packs the message.
   - Requests path if needed.
   - Determines transport encryption.
   - Adds to `pending_outbound`.
   - Starts background `process_outbound` thread.
3. `process_outbound` handles delivery per method:
   - DIRECT: establishes link, sends as link packet or resource.
   - OPPORTUNISTIC: sends as single packet.
   - PROPAGATED: sends to propagation node via link.
4. Per-message callbacks fire: delivery callback on `SENT`/`DELIVERED`,
   failed callback on `FAILED`/`REJECTED`/`CANCELLED`.
5. `lxm.hash` is available after `pack()`. This is the message ID.

### 9.1 MEDRE Integration

For the real adapter, `deliver()` would:

1. Extract content, title, fields from `RenderingResult.payload`.
2. Resolve the destination hash from the target address.
3. Create `LXMessage(destination_dest, source_dest, content, title,
   fields, desired_method)`.
4. Register delivery and failed callbacks.
5. Call `router.handle_outbound(lxm)`.
6. Return `AdapterDeliveryResult(native_message_id=lxm.hash.hex(),
   native_channel_id="")`.

The tricky part is resolving the destination hash. MEDRE addresses are
identity hashes (32 hex chars). The destination hash must be computed
from the identity hash and the `lxmf.delivery` aspect. This requires
either having the remote identity loaded (from an announce) or computing
the destination hash from the known identity hash.


## 10. Cross-References

| Topic | Contract |
|-------|----------|
| LXMF source audit (identity, wire format, fields, delivery methods) | `13-lxmf-source-audit.md` |
| LXMF adapter tranche 1 scope, config, capabilities, pipeline | `14-lxmf-tranche-1.md` |
| Production connectivity readiness per adapter (LXMF section) | `16-production-connectivity-readiness.md` |
| Operational readiness gaps (LXMF section) | `18-operational-readiness-gaps.md` |

Key findings that this contract consolidates:

- Contract 13 confirmed the wire format, identity model, delivery
  methods, and field definitions. This contract adds the full API
  surface (constructor signatures, callback registration, shutdown
  sequence) confirmed from source.

- Contract 14 defined the adapter's fake-only scope, config shape,
  and capability declaration. This contract documents exactly what
  real SDK calls need to be wired for each scaffolded feature.

- Contract 16 assessed LXMF as the adapter needing the most work.
  This contract provides the implementation roadmap by documenting
  every API call needed.

- Contract 18 identified the async/sync boundary as a risk. This
  contract confirms the threading model (Reticulum uses daemon threads,
  not asyncio) and the need for callback bridging.


## 11. Implementation Readiness Summary

| Task | SDK API Available | Adapter Code Exists | Blocked By |
|------|------------------|--------------------|-------------| 
| Identity load/create | `RNS.Identity.from_file()`, `RNS.Identity()` | No | `identity_path` wiring |
| Reticulum init | `RNS.Reticulum(configdir)` | No | `reticulum_configdir` config field |
| Router creation | `LXMF.LXMRouter(storagepath)` | No | `storage_path` config field |
| Register delivery identity | `router.register_delivery_identity()` | No | Identity + router creation |
| Register delivery callback | `router.register_delivery_callback()` | No | Router creation |
| Announce presence | `router.announce()` | No | Delivery identity registration |
| Send message | `router.handle_outbound()` | No | Router + destination resolution |
| LXMessage to dict conversion | N/A (application code) | No | Need to decide: modify codec or add adapter layer |
| Clean shutdown | `router.exit_handler()`, `RNS.exit()` | No | Router creation |
| Propagation node sync | `router.request_messages_from_propagation_node()` | No | Propagation node config |


## 12. Explicit Non-Claims

- **No production LXMF/Reticulum connectivity exists.**
- **No live testing has been performed.** All findings are from source
  code analysis.
- **No compatibility with any specific Reticulum network is claimed.**
- **No real identity management is implemented.** The `identity_path`
  config field is a placeholder.
- **No reconnection, retry, or error recovery logic is implemented.**
- **No async/sync bridge between Reticulum threads and MEDRE's asyncio
  event loop is implemented.**
- **Field key 0xFD for MEDRE metadata has not been validated against
  real LXMF traffic.** It is a MEDRE convention that other clients may
  or may not respect.


---

*This document was produced by auditing the LXMF 0.9.6 and Reticulum
1.2.4 source code. It does not replace live transport testing. All
findings are based on source code analysis, not running Reticulum
network captures.*
