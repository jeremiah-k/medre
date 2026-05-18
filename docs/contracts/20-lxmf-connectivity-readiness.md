# LXMF/Reticulum Connectivity Readiness

> Contract version: 2
> Last updated: 2026-05-12
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

## 1. Package and Import [CONFIRMED]

| Item                        | Value                                                                | Label     |
| --------------------------- | -------------------------------------------------------------------- | --------- |
| LXMF PyPI distribution      | `lxmf`                                                               | CONFIRMED |
| LXMF import                 | `import LXMF` (uppercase)                                            | CONFIRMED |
| LXMF version installed      | 0.9.7                                                                | CONFIRMED |
| LXMF install path           | `/home/jeremiah/.platformio/penv/lib/python3.12/site-packages/LXMF/` | CONFIRMED |
| Reticulum PyPI distribution | `rns`                                                                | CONFIRMED |
| Reticulum import            | `import RNS`                                                         | CONFIRMED |
| Reticulum version installed | 1.2.5                                                                | CONFIRMED |
| Reticulum install path      | `/home/jeremiah/.platformio/penv/lib/python3.12/site-packages/RNS/`  | CONFIRMED |
| Author                      | Mark Qvist                                                           | CONFIRMED |
| Wire format                 | msgpack (not protobuf)                                               | CONFIRMED |
| Serialization               | `RNS.vendor.umsgpack` bundled                                        | CONFIRMED |
| pyserial version            | 3.5                                                                  | CONFIRMED |

Inspection commands used:

```bash
pip show lxmf rns 2>&1
python3 -c "import RNS; print(RNS.__version__, RNS.__file__)"
python3 -c "import LXMF; print(LXMF.__version__, LXMF.__file__)"
python3 -c "import serial; print(serial.__version__)"
```

## 2. Confirmed SDK Findings [CONFIRMED]

Everything in this section is verified by import inspection and source
reading on the installed packages at the paths listed above. Nothing here
depends on a running network.

### 2.1 Reticulum Identity [CONFIRMED]

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

### 2.2 Reticulum Singleton [CONFIRMED]

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

### 2.3 Destination [CONFIRMED]

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

### 2.4 Transport and Path [CONFIRMED]

```python
import RNS

# Check if a path is known to a destination
RNS.Transport.has_path(destination_hash)  # returns bool

# Request a path to a destination (async, non-blocking)
RNS.Transport.request_path(destination_hash)

# Register an announce handler
RNS.Transport.register_announce_handler(handler)
```

### 2.5 Link [CONFIRMED]

```python
import RNS

# Establish an encrypted link to a destination
link = RNS.Link(destination)
link.status          # RNS.Link.ACTIVE, RNS.Link.CLOSED, etc.
link.teardown()      # close the link
```

Links provide encrypted sessions with forward secrecy. Link MDU is 431
bytes by default. LXMF uses links for DIRECT delivery mode.

### 2.6 LXMRouter [CONFIRMED]

```python
import LXMF

# Create an LXMF router (storagepath is required)
router = LXMF.LXMRouter(
    identity=None,                       # auto-generated if None
    storagepath="/path/to/storage",      # REQUIRED, raises ValueError if None
    autopeer=True,                       # auto-peer with propagation nodes
    autopeer_maxdepth=None,              # max auto-peer depth (default 4)
    propagation_limit=256,               # KB limit per propagation transfer
    delivery_limit=1000,                 # KB limit per direct delivery
    sync_limit=10240,                    # KB limit per sync operation
    enforce_ratchets=False,              # reject non-ratcheted messages
    enforce_stamps=False,                # reject unstamped messages
    static_peers=[],                     # preconfigured peer hashes
    max_peers=None,                      # max propagation peers (default 20)
    from_static_only=False,              # only accept from static peers
    sync_strategy=LXMF.LXMPeer.STRATEGY_PERSISTENT,  # peer sync mode
    propagation_cost=16,                 # PoW cost for propagation messages
    propagation_cost_flexibility=3,      # cost flexibility range
    peering_cost=18,                     # PoW cost for peering
    max_peering_cost=26,                 # max accepted peering cost
    name=None,                           # human-readable router name
)

# Register a delivery identity
dest = router.register_delivery_identity(
    identity,                       # RNS.Identity
    display_name="MEDRE Node",      # optional, shown in announces
    stamp_cost=None,                # optional PoW cost for inbound messages
)
# Returns an RNS.Destination for the "lxmf.delivery" aspect
# CONFIRMED: Only ONE delivery identity per router. Second call logs error, returns None.

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
  CONFIRMED via `inspect.getsource(LXMF.LXMRouter.__init__)`.
- Only one delivery identity per router instance is supported (as of
  0.9.7). `register_delivery_identity` logs an error and returns `None`
  if called a second time. CONFIRMED.
- `register_delivery_identity(identity, display_name=None, stamp_cost=None)`
  signature CONFIRMED via `inspect.signature()`.
- `register_delivery_callback(callback)` sets `self.__delivery_callback`.
  The callback fires as `callback(lxmessage)` in a Reticulum thread.
  Exceptions in the callback are caught and logged, not propagated.
  CONFIRMED via source at line 1779.
- The router starts background daemon threads for processing outbound
  messages and periodic jobs. These threads run for the lifetime of
  the router.
- `handle_outbound()` sets the message state to `OUTBOUND`, packs it,
  checks path availability, and adds it to `pending_outbound`. The
  background thread then handles link establishment, retries, and
  delivery. CONFIRMED.
- `enable_propagation()` indexes the message store from disk and sets
  up propagation node callbacks. This is a blocking operation that
  scans all files in the message store directory. CONFIRMED.
- `disable_propagation()` simply sets `self.propagation_node = False`.
  CONFIRMED.
- Signal handlers (SIGINT, SIGTERM) are registered automatically.
  CONFIRMED.
- `announce(destination_hash, attached_interface=None)` delegates to
  the destination's announce method with app_data from
  `get_announce_app_data()`. CONFIRMED.
- `fail_message(lxmessage)` sets state to FAILED (or REJECTED if
  already rejected), removes from pending_outbound, and calls the
  per-message `failed_callback` if registered. CONFIRMED.
- `exit_handler()` tears down delivery destinations (clears callbacks,
  tears down links), tears down propagation node if active, persists
  peer sync states, saves locally delivered/processed transient IDs,
  and saves node stats. It is guarded against double-entry via
  `exit_handler_running` flag. CONFIRMED.

### 2.7 LXMessage [CONFIRMED]

```python
import LXMF

# Create a message
message = LXMF.LXMessage(
    destination,       # RNS.Destination or None (use destination_hash if None)
    source,            # RNS.Destination or None (use source_hash if None)
    content="Hello",   # str or bytes
    title="Subject",   # str or bytes, optional (default "")
    fields=None,       # dict, optional
    desired_method=LXMF.LXMessage.DIRECT,
    destination_hash=None,  # 16-byte bytes, used when destination=None
    source_hash=None,       # 16-byte bytes, used when source=None
    stamp_cost=None,        # PoW cost for this message
    include_ticket=False,   # include reply ticket for recipient
)

# Message properties
message.hash                # bytes, 32 bytes, SHA-256 of wire data
message.state               # int enum
message.title_as_string()   # str
message.content_as_string() # str
message.get_fields()        # dict

# Callbacks (per-message, not per-router)
message.register_delivery_callback(cb)   # cb(msg) on SENT/DELIVERED
message.register_failed_callback(cb)     # cb(msg) on FAILED/REJECTED/CANCELLED

# Content setters
message.set_title_from_string("title")
message.set_content_from_string("body")
message.set_fields({"key": "value"})
```

CONFIRMED: LXMessage constructor accepts `(destination, source, content,
title, fields, desired_method, destination_hash, source_hash, stamp_cost,
include_ticket)`. Confirmed via `inspect.getsource(LXMF.LXMessage.__init__)`.

Message state transitions:

```text
GENERATING (0x00) -> OUTBOUND (0x01) -> SENDING (0x02) -> SENT (0x04) -> DELIVERED (0x08)
                                                            \
                                                             -> FAILED (0xFF)
                                                             -> REJECTED (0xFD)
                                                             -> CANCELLED (0xFE)
```

Delivery methods:

| Method                    | Code   | Description                                                                         |
| ------------------------- | ------ | ----------------------------------------------------------------------------------- |
| `LXMessage.OPPORTUNISTIC` | `0x01` | Single packet, fire-and-forget. Max 295 bytes encrypted content.                    |
| `LXMessage.DIRECT`        | `0x02` | Link-based, reliable. Up to 319 bytes per packet, larger via RNS Resource transfer. |
| `LXMessage.PROPAGATED`    | `0x03` | Store-and-forward via propagation node.                                             |
| `LXMessage.PAPER`         | `0x05` | Offline transfer via QR code or `lxm://` URI.                                       |

### 2.8 Message Hash as native_message_id [CONFIRMED]

The LXMF message hash (`lxm.hash`) is a 32-byte SHA-256 digest computed
over `destination_hash + source_hash + msgpack_payload`. This is a strong
candidate for `native_message_id` in MEDRE's `NativeMessageRef`:

```python
# MEDRE already maps this in LxmfCodec.decode():
hex_message_id = lxm.hash.hex()  # 64 hex chars
```

This is already implemented in the fake adapter with deterministic
SHA-256 hashes. The real adapter would use the actual `lxm.hash`.

### 2.9 Fields Dict [CONFIRMED]

LXMF defines field keys in `LXMF.LXMF.py`. CONFIRMED via `dir(LXMF)` inspection.
MEDRE uses `FIELD_CUSTOM_META` (0xFD) for its metadata envelope. Full field list:

| Key    | Constant                 | Description                                     |
| ------ | ------------------------ | ----------------------------------------------- |
| `0x01` | `FIELD_EMBEDDED_LXMS`    | Embedded LXMF messages                          |
| `0x02` | `FIELD_TELEMETRY`        | Telemetry data                                  |
| `0x03` | `FIELD_TELEMETRY_STREAM` | Streaming telemetry                             |
| `0x04` | `FIELD_ICON_APPEARANCE`  | Icon/appearance metadata                        |
| `0x05` | `FIELD_FILE_ATTACHMENTS` | File attachments                                |
| `0x06` | `FIELD_IMAGE`            | Image data                                      |
| `0x07` | `FIELD_AUDIO`            | Audio data (Codec2, Opus modes)                 |
| `0x08` | `FIELD_THREAD`           | Conversation grouping                           |
| `0x09` | `FIELD_COMMANDS`         | Command interface                               |
| `0x0A` | `FIELD_RESULTS`          | Command results                                 |
| `0x0B` | `FIELD_GROUP`            | Group addressing                                |
| `0x0C` | `FIELD_TICKET`           | Reply permission (PoW bypass)                   |
| `0x0D` | `FIELD_EVENT`            | Event data                                      |
| `0x0E` | `FIELD_RNR_REFS`         | RNR references                                  |
| `0x0F` | `FIELD_RENDERER`         | Renderer hint (plain, micron, markdown, bbcode) |
| `0xFB` | `FIELD_CUSTOM_TYPE`      | Custom type identifier                          |
| `0xFC` | `FIELD_CUSTOM_DATA`      | Custom payload                                  |
| `0xFD` | `FIELD_CUSTOM_META`      | **MEDRE uses this** for metadata envelope       |
| `0xFE` | `FIELD_NON_SPECIFIC`     | Non-specific extension                          |
| `0xFF` | `FIELD_DEBUG`            | Debug information                               |

### 2.10 Stamp Cost (Proof of Work) [CONFIRMED]

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

### 2.11 Shutdown Sequence [CONFIRMED]

Confirmed from source via `inspect.getsource()`:

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

### 2.12 RNode Interface [CONFIRMED]

Confirmed from installed `RNS.Interfaces.RNodeInterface`:

```python
from RNS.Interfaces.RNodeInterface import RNodeInterface
# __init__(self, owner, configuration)
# Reads config from interface dict with keys:
#   port (REQUIRED, e.g. "/dev/ttyUSB0" or "tcp://host:port")
#   frequency, bandwidth, txpower, spreadingfactor, codingrate
#   flow_control, id_interval, id_callsign
#   airtime_limit_short, airtime_limit_long
```

Key details:

- RNodeInterface requires `pyserial` (CONFIRMED: `pyserial` 3.5 installed).
  If `serial` module is not found, Reticulum calls `RNS.panic()`.
- HW_MTU is 508 bytes. Serial speed defaults to 115200 baud.
- Port can be a serial device path, `tcp://host:port` for TCP-connected
  RNodes, or `ble://` for BLE-connected RNodes.
- CONFIRMED: RNodeInterface is NOT needed for basic LAN testing.
  `AutoInterface` (IPv6 link-local) requires no hardware.

### 2.13 RNS.exit() Behavior [CONFIRMED]

```python
RNS.exit(code=0)
# Calls Reticulum.exit_handler() then os._exit(code).
# This is a hard process exit. Do not call from library code.
# For graceful library shutdown, use:
RNS.Reticulum.exit_handler()  # @staticmethod, cleans up without exiting process
```

## 3. Inferred Behaviors [INFERRED]

These are plausible assumptions based on the installed source code, but
have not been verified with a running Reticulum instance.

### 3.1 Config Directory Interaction [INFERRED]

The adapter's `identity_path` config field would point to a file
produced by `RNS.Identity.to_file(path)`. The Reticulum configdir
(`~/.reticulum` by default) is a separate concern: it controls transport
interfaces (TCP, UDP, serial, RNode, etc.). The adapter likely needs
both an identity file path and either a shared Reticulum instance or
its own configdir.

### 3.2 Resource Handling for Attachments [INFERRED]

`RNS.Resource` handles arbitrary-size data transfer over links. LXMF
uses this for messages larger than the single-packet limit (319 bytes
over a link). The `FIELD_FILE_ATTACHMENTS` (0x05) field presumably
references resource data, but the exact mechanism for correlating
field entries with resource transfers is not documented in the source
and would need live testing.

### 3.3 Channel/Buffer Streaming [INFERRED]

`RNS.Channel` and `RNS.Buffer` provide reliable sequential delivery
over links. These are higher-level abstractions that LXMF does not
currently use (LXMF has its own message-level sequencing via Resource).
They may be relevant for future streaming or bulk transfer features.

### 3.4 Single Delivery Identity [CONFIRMED — promoted from inferred]

This was previously inferred. Now CONFIRMED via source inspection:
the router enforces one delivery identity per instance (`register_delivery_identity`
checks `if len(self.delivery_destinations) != 0`). MEDRE needs one LXMRouter per
identity or must accept the limitation.

### 3.5 Threading Model [CONFIRMED — promoted from inferred]

CONFIRMED: Both Reticulum and LXMF use background daemon threads (not asyncio).
The router's jobloop and transport processing run in threads. LXMF callback
exceptions are caught and logged (not propagated). Integration with MEDRE's
asyncio event loop requires bridging via `loop.create_task()` or similar.

### 3.6 Auto-Discovery [INFERRED]

Reticulum auto-discovers peers on local interfaces. The `AutoInterface`
detects other Reticulum nodes on the same network segment. No explicit
host/port configuration is needed for local mesh operation. The TCP and
UDP interfaces do require explicit configuration in the Reticulum
config file.

## 4. Unknowns [UNKNOWN]

These questions cannot be answered from source code alone. They require
running Reticulum with real or simulated transport.

### 4.1 Transport Interface Configuration [UNKNOWN]

What Reticulum config file entries are needed for a test environment?
The default config provides basic local connectivity, but the exact
interface definitions for a developer test setup (e.g., two LXMF
instances on the same machine via TCP) are not documented in the MEDRE
context.

### 4.2 Multiple RNS.Reticulum Instances [UNKNOWN]

The singleton pattern means only one `RNS.Reticulum` per process. For
testing with sender and receiver in the same process, either:

- Two separate processes are needed, or
- The shared instance mechanism (local IPC on port 37428) is used,
  or
- One side uses `connection_type="fake"`.

### 4.3 Default Config Paths [CONFIRMED — promoted from unknown]

CONFIRMED: The search order is `/etc/reticulum` (with `config` file) >
`~/.config/reticulum` (with `config` file) > `~/.reticulum` (fallback).
Source: `RNS.Reticulum.__init__` lines 15–22. If none have a `config` file,
Reticulum uses `~/.reticulum` and creates a default config on first run.

### 4.4 LXMF Message Object Shape for Codec [UNKNOWN]

MEDRE's `LxmfCodec.decode()` currently expects a dict shaped like a
packet. When receiving real LXMF messages via the delivery callback,
the callback receives an `LXMessage` object. The adapter will need to
convert the `LXMessage` attributes into the dict shape the codec
expects, or the codec needs to accept `LXMessage` objects directly.

### 4.5 Async/Sync Boundary Behavior [UNKNOWN]

Reticulum's threading model and LXMF's callback-based delivery interact
with Python's GIL and asyncio in ways that are hard to predict from
source alone. Deadlocks, event loop blocking, and callback timing under
load are all potential issues.

### 4.6 Error Recovery [UNKNOWN]

What happens when a link drops mid-transfer? When path requests time
out? When propagation node sync fails? The retry logic exists in
`process_outbound`, but the actual behavior under network failure
conditions is untested in the MEDRE context.

## 5. API/Runtime Findings Table

All findings from this reality pass. Inspection performed 2026-05-12
against installed packages in `~/.platformio/penv/` (Python 3.12).

| #   | Finding                                                                                                                      | Source                                                  | Label     |
| --- | ---------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------- | --------- |
| 1   | `import LXMF` succeeds (uppercase module name)                                                                               | `python3 -c "import LXMF"`                              | CONFIRMED |
| 2   | `import RNS` succeeds                                                                                                        | `python3 -c "import RNS"`                               | CONFIRMED |
| 3   | `RNS.__version__` = `"1.2.5"`                                                                                                | `python3 -c "import RNS; print(RNS.__version__)"`       | CONFIRMED |
| 4   | `LXMF.__version__` = `"0.9.7"`                                                                                               | `python3 -c "import LXMF; print(LXMF.__version__)"`     | CONFIRMED |
| 5   | PyPI package `lxmf` (lowercase), import `LXMF` (uppercase)                                                                   | `pip show lxmf` + `import LXMF`                         | CONFIRMED |
| 6   | PyPI package `rns`, import `RNS`                                                                                             | `pip show rns` + `import RNS`                           | CONFIRMED |
| 7   | `RNS.Reticulum()` singleton; second call raises `OSError`                                                                    | `inspect.getsource(RNS.Reticulum.__init__)` line 11     | CONFIRMED |
| 8   | `RNS.Reticulum.get_instance()` returns singleton or None                                                                     | `inspect.getsource()`                                   | CONFIRMED |
| 9   | Config dir search: `/etc/reticulum` > `~/.config/reticulum` > `~/.reticulum`                                                 | Source lines 17–22                                      | CONFIRMED |
| 10  | `RNS.Identity(create_keys=True)` default generates X25519+Ed25519                                                            | `inspect.signature(RNS.Identity.__init__)`              | CONFIRMED |
| 11  | `RNS.Identity.to_file(path)` writes 64-byte raw private key                                                                  | Source inspection                                       | CONFIRMED |
| 12  | `RNS.Identity.from_file(path)` returns None on failure                                                                       | Source inspection                                       | CONFIRMED |
| 13  | `LXMRouter(identity, storagepath, ...)` - storagepath REQUIRED                                                               | Source: raises ValueError if None                       | CONFIRMED |
| 14  | `register_delivery_identity(identity, display_name=None, stamp_cost=None)`                                                   | `inspect.signature()`                                   | CONFIRMED |
| 15  | Only ONE delivery identity per LXMRouter (second call returns None)                                                          | Source: `if len(self.delivery_destinations) != 0`       | CONFIRMED |
| 16  | `register_delivery_callback(callback)` sets private `__delivery_callback`                                                    | Source line 329                                         | CONFIRMED |
| 17  | Delivery callback invocation catches exceptions, logs, does NOT propagate                                                    | Source lines 1779–1784                                  | CONFIRMED |
| 18  | `announce(destination_hash, attached_interface=None)`                                                                        | `inspect.signature()`                                   | CONFIRMED |
| 19  | `handle_outbound(lxmessage)` sets OUTBOUND state, adds to pending_outbound                                                   | `inspect.getsource()`                                   | CONFIRMED |
| 20  | `exit_handler()` tears down destinations, links, propagation, persists state                                                 | `inspect.getsource()`                                   | CONFIRMED |
| 21  | `exit_handler()` guarded against double-entry via `exit_handler_running` flag                                                | Source inspection                                       | CONFIRMED |
| 22  | `enable_propagation()` indexes message store (blocking disk scan)                                                            | `inspect.getsource()`                                   | CONFIRMED |
| 23  | `disable_propagation()` sets `self.propagation_node = False`                                                                 | `inspect.getsource()`                                   | CONFIRMED |
| 24  | `LXMessage(dest, source, content, title, fields, desired_method, destination_hash, source_hash, stamp_cost, include_ticket)` | `inspect.signature()`                                   | CONFIRMED |
| 25  | LXMessage states: GENERATING=0, OUTBOUND=1, SENDING=2, SENT=4, DELIVERED=8, FAILED=255, CANCELLED=254, REJECTED=253          | `dir(LXMF.LXMessage)`                                   | CONFIRMED |
| 26  | LXMessage methods: OPPORTUNISTIC=1, DIRECT=2, PROPAGATED=3, PAPER=5                                                          | `dir(LXMF.LXMessage)`                                   | CONFIRMED |
| 27  | `LXMessage.register_delivery_callback(cb)` sets private `__delivery_callback`                                                | `inspect.getsource()`                                   | CONFIRMED |
| 28  | `LXMessage.register_failed_callback(cb)` sets public `failed_callback`                                                       | `inspect.getsource()`                                   | CONFIRMED |
| 29  | `fail_message()` calls `lxmessage.failed_callback(lxmessage)` if set                                                         | `inspect.getsource()`                                   | CONFIRMED |
| 30  | `RNS.exit(code=0)` calls `Reticulum.exit_handler()` then `os._exit(code)`                                                    | `inspect.getsource(RNS.exit)`                           | CONFIRMED |
| 31  | `RNS.Reticulum.exit_handler()` is `@staticmethod`                                                                            | `inspect.getsource()`                                   | CONFIRMED |
| 32  | RNodeInterface requires `pyserial`, panics without it                                                                        | `inspect.getsource(RNodeInterface.__init__)`            | CONFIRMED |
| 33  | `pyserial` 3.5 installed                                                                                                     | `python3 -c "import serial; print(serial.__version__)"` | CONFIRMED |
| 34  | RNodeInterface HW_MTU=508, serial default 115200 baud                                                                        | Source inspection                                       | CONFIRMED |
| 35  | `LXMF.APP_NAME = "lxmf"`                                                                                                     | `python3 -c "import LXMF; print(LXMF.APP_NAME)"`        | CONFIRMED |
| 36  | LXMRouter constructor has `enforce_ratchets`, `enforce_stamps`, `name` params                                                | `inspect.signature(LXMF.LXMRouter.__init__)`            | CONFIRMED |
| 37  | LXMRouter default sync_strategy = `LXMPeer.STRATEGY_PERSISTENT`                                                              | Source inspection                                       | CONFIRMED |
| 38  | `MAX_DELIVERY_ATTEMPTS = 5`, `DELIVERY_RETRY_WAIT = 10` (seconds)                                                            | `dir(LXMF.LXMRouter)` constants                         | CONFIRMED |
| 39  | `RNS.Reticulum.MTU = 500`, `MDU = 464`                                                                                       | `dir(RNS.Reticulum)` constants                          | CONFIRMED |
| 40  | Callback timing, ordering under load                                                                                         | Requires live testing                                   | UNKNOWN   |
| 41  | Path discovery latency on real network                                                                                       | Requires live testing                                   | UNKNOWN   |
| 42  | Multi-hop delivery reliability                                                                                               | Requires live testing                                   | UNKNOWN   |
| 43  | Behavior with `rnsd` shared instance co-existing                                                                             | Requires live testing                                   | UNKNOWN   |
| 44  | Resource transfer for large messages                                                                                         | Requires live testing                                   | UNKNOWN   |

## 6. Current Adapter Scaffold Status

The MEDRE LXMF adapter (`src/medre/adapters/lxmf/`) is in tranche 1.
Here is exactly what exists and what does not.

### 6.1 What Exists

| Component              | File                   | Status                                                                                                                                                                                                                             |
| ---------------------- | ---------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------ |
| `LxmfAdapter`          | `adapter.py`           | Scaffold. `start()` only supports `connection_type="fake"`. `deliver()` returns `None`.                                                                                                                                            |
| `LxmfConfig`           | `config.py`            | Functional. `connection_type` accepts `"fake"` and `"reticulum"` as valid shapes. `reticulum` validates at config level (shape only); runtime `start()` raises `LxmfConnectionError` for non-fake modes. `identity_path` is a `str | None` placeholder. |
| `LxmfCodec`            | `codec.py`             | Functional against dict-shaped test data. Does not accept `LXMessage` objects.                                                                                                                                                     |
| `LxmfRenderer`         | `renderer.py`          | Functional. Produces content/title/fields dicts.                                                                                                                                                                                   |
| `LxmfFieldsHelper`     | `fields.py`            | Functional. Embeds/extracts MEDRE envelope under `FIELD_CUSTOM_META` (0xFD).                                                                                                                                                       |
| `LxmfPacketClassifier` | `packet_classifier.py` | Functional against dict-shaped test data.                                                                                                                                                                                          |
| Error hierarchy        | `errors.py`            | Complete. `LxmfError` base, `LxmfConnectionError`, `LxmfSendError`, `LxmfConfigError`, `LxmfCodecError`, `LxmfPacketError`.                                                                                                        |

### 6.2 What Does NOT Exist

| Missing Piece                             | Impact                                      |
| ----------------------------------------- | ------------------------------------------- |
| No real `RNS.Identity` loading            | `identity_path` in config is unwired.       |
| No `RNS.Reticulum()` initialization       | No Reticulum transport layer.               |
| No `LXMF.LXMRouter()` creation            | No LXMF message routing.                    |
| No `register_delivery_identity` call      | No inbound message destination.             |
| No `register_delivery_callback`           | No message reception.                       |
| No `router.announce()`                    | No presence on the network.                 |
| No `router.handle_outbound()`             | No real message sending.                    |
| No `router.exit_handler()` / `RNS.exit()` | No clean shutdown.                          |
| No `RNS.Transport` path management        | No path discovery.                          |
| No `LXMessage` to dict conversion         | Codec expects dicts, not LXMessage objects. |

### 6.3 Config Gaps

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

## 7. Delivery Method Semantics

| Method        | Wire Behavior                                                      | Reliability                                                                       | Size Limit                                                      | Use Case                                      |
| ------------- | ------------------------------------------------------------------ | --------------------------------------------------------------------------------- | --------------------------------------------------------------- | --------------------------------------------- |
| DIRECT        | Establishes `RNS.Link`, sends via link packet or `RNS.Resource`    | High. Retries up to `MAX_DELIVERY_ATTEMPTS` (5). Proof receipts confirm delivery. | Link packet: 319B. Resource: arbitrary.                         | Default for most messaging.                   |
| OPPORTUNISTIC | Single RNS packet, no link. Embedded in opportunistic route.       | Best-effort. No ACK, no retry. Max 1 attempt (`MAX_PATHLESS_TRIES=1`).            | 295 bytes encrypted content.                                    | Quick, low-overhead messages to online peers. |
| PROPAGATED    | Delivered to propagation node via link. Node stores for recipient. | Moderate. Delivery to node is reliable. Recipient must sync from node.            | Limited by propagation node config (`PROPAGATION_LIMIT=256KB`). | Offline recipients, delayed delivery.         |
| PAPER         | Encoded as QR code or `lxm://` URI. No network transport.          | None. Physical delivery only.                                                     | `PAPER_MDU` varies, roughly 2KB.                                | Air-gapped transfer, QR scanning.             |

### 7.1 Method Selection Logic

When `desired_method` is set on an `LXMessage`, the router respects it
if feasible. If `desired_method=DIRECT` but no path exists, the router
requests a path and retries. If `desired_method=OPPORTUNISTIC` and no
path exists, the router requests a path and waits
(`PATH_REQUEST_WAIT=7s`), then fails if still no path.

If `desired_method` is `None`, the router selects the best available
method. The message is packed first, then the router decides based on
size and path availability.

### 7.2 Implications for MEDRE

The `LxmfConfig.default_delivery_method` field maps to these methods.
For a mesh messaging scenario:

- DIRECT is the best default for online peers.
- PROPAGATED is essential for offline delivery.
- OPPORTUNISTIC is useful for quick status messages.
- PAPER is unlikely to be needed in MEDRE's bridging use case.

## 8. Storage Paths

Reticulum and LXMF both use filesystem directories for persistent state.
These are separate from each other and from MEDRE's storage.

| System              | Path                      | Contents                                                                                                           |
| ------------------- | ------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| Reticulum config    | `~/.reticulum/` (default) | `config` file, `storage/`, `interfaces/`                                                                           |
| Reticulum storage   | `{configdir}/storage/`    | `known_destinations`, `cache/`, `resources/`, `identities/`                                                        |
| LXMF router storage | `{storagepath}/lxmf/`     | `local_deliveries`, `locally_processed`, `outbound_stamp_costs`, `available_tickets`, `messagestore/`, `ratchets/` |
| MEDRE SQLite        | Configured by MEDRE       | Events, receipts, native refs, route configs                                                                       |

The LXMF `storagepath` is passed to `LXMRouter(storagepath=...)`. It
must be a writable directory. The router creates `lxmf/` inside it.

For MEDRE, these would be configured via adapter config:

- `reticulum_configdir` -> `RNS.Reticulum(configdir=...)`
- `storage_path` -> `LXMRouter(storagepath=...)`
- `identity_path` -> `RNS.Identity.from_file(...)`

## 9. Message Lifecycle: Inbound

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

### 9.1 Conversion Gap

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

## 10. Message Lifecycle: Outbound

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

| Topic                                                               | Contract                                  |
| ------------------------------------------------------------------- | ----------------------------------------- |
| LXMF source audit (identity, wire format, fields, delivery methods) | `13-lxmf-source-audit.md`                 |
| LXMF adapter tranche 1 scope, config, capabilities, pipeline        | `14-lxmf-tranche-1.md`                    |
| Production connectivity readiness per adapter (LXMF section)        | `16-production-connectivity-readiness.md` |
| Operational readiness gaps (LXMF section)                           | `18-operational-readiness-gaps.md`        |

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

| Task                         | SDK API Available                                 | Adapter Code Exists | Blocked By                                        |
| ---------------------------- | ------------------------------------------------- | ------------------- | ------------------------------------------------- |
| Identity load/create         | `RNS.Identity.from_file()`, `RNS.Identity()`      | No                  | `identity_path` wiring                            |
| Reticulum init               | `RNS.Reticulum(configdir)`                        | No                  | `reticulum_configdir` config field                |
| Router creation              | `LXMF.LXMRouter(storagepath)`                     | No                  | `storage_path` config field                       |
| Register delivery identity   | `router.register_delivery_identity()`             | No                  | Identity + router creation                        |
| Register delivery callback   | `router.register_delivery_callback()`             | No                  | Router creation                                   |
| Announce presence            | `router.announce()`                               | No                  | Delivery identity registration                    |
| Send message                 | `router.handle_outbound()`                        | No                  | Router + destination resolution                   |
| LXMessage to dict conversion | N/A (application code)                            | No                  | Need to decide: modify codec or add adapter layer |
| Clean shutdown               | `router.exit_handler()`, `RNS.exit()`             | No                  | Router creation                                   |
| Propagation node sync        | `router.request_messages_from_propagation_node()` | No                  | Propagation node config                           |

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

_This document was produced by auditing the LXMF 0.9.6 and Reticulum
1.2.4 source code. It does not replace live transport testing. All
findings are based on source code analysis, not running Reticulum
network captures._
