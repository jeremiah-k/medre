# LXMF Live Smoke Test Runbook

> Last updated: 2026-05-10
> Status: **Live harness exists. Real mode requires `lxmf`/`RNS` packages and a live Reticulum instance.**
> See: `docs/contracts/20-lxmf-connectivity-readiness.md`
> Alpha operation runbook: `docs/runbooks/lxmf-alpha-operation.md`
> Metadata normalization audit: `docs/contracts/26-metadata-normalization-audit.md`

This runbook describes how to test LXMF/Reticulum connectivity against
a real Reticulum network. It documents the SDK's connection methods,
required environment variables, and expected behaviors so that when
someone sits down with Reticulum networking, they have a verified
procedure to follow.

The MEDRE adapter has session-backed real Reticulum/LXMF support via
`LxmfSession`. When `connection_type="reticulum"` and the `lxmf`/`RNS`
packages are installed, the adapter initializes a real Reticulum
instance, loads an identity, creates an LXMRouter, and registers
delivery callbacks. Without a live Reticulum network present, real-mode
tests skip with `pytest.skip()`. Fake-mode tests run unconditionally.

**Production LXMF/Reticulum deployment readiness is not claimed.**
This is alpha operation requiring installed/configured SDK and live
environment.


## Purpose

The live smoke harness in `tests/test_lxmf_live.py` validates:

- The `lxmf` and `rns` packages install and import correctly.
- A `RNS.Reticulum` instance initializes with a valid config directory.
- An `RNS.Identity` can be created and persisted to disk.
- An `LXMF.LXMRouter` can be created with a valid storage path.
- `router.register_delivery_callback()` receives incoming messages.
- `router.handle_outbound()` sends a message that arrives at a
  destination.
- The full adapter lifecycle (start, health, deliver, stop, restart)
  works cleanly via `LxmfSession`.
- Idempotent start/stop, double-start, double-stop, and rapid cycles
  are stable.
- The inbound pipeline (`simulate_inbound` → codec → `publish_inbound`)
  works end to end.

### What live smoke does NOT prove

- Synchronous delivery confirmation. Outbound returns `OUTBOUND` state,
  not `DELIVERED`. Actual delivery is asynchronous via LXMF callbacks.
- Inbound message reception from a separate, independent Reticulum
  instance (requires a second process not available in this harness).
- Propagation node store-and-forward across independent peers.
- Multi-hop mesh delivery across heterogeneous transports.
- Resource transfer for large messages (attachments, images).
- Ticket-based reply correlation.
- Reconnection handling under real network failure conditions.
- Compatibility with all Reticulum transport interface types.
- Production deployment readiness.


## Dependency Installation

The LXMF and Reticulum packages are optional dependencies. Core MEDRE
tests pass without them.

```bash
pip install lxmf rns
```

**Notes:**

- **LXMF package name:** `lxmf` on PyPI. Version 0.9.6 audited.
- **LXMF import:** `import LXMF`. The `LXMF` package bundles its own
  copy of `RNS.vendor.umsgpack`.
- **Reticulum package name:** `rns` on PyPI. Version 1.2.4 audited.
- **Reticulum import:** `import RNS`. The `RNS` package depends on
  `pyca/cryptography` and `pyserial`.
- **Alternative:** `pip install rnspure` for a no-external-dependency
  Reticulum (uses internal pure-Python crypto primitives, slower and
  less audited).
- **Optional:** Core MEDRE tests pass without `lxmf` or `rns`. Only
  live smoke tests require them.
- **LXMF pulls in Reticulum:** `pip install lxmf` installs `rns` as a
  dependency. You do not need to install both separately.
- **Author:** Mark Qvist. Both packages share the same author and
  license (Reticulum License).


## Identity Setup

Reticulum identities are dual-keypair (X25519 for encryption, Ed25519
for signing) derived from a single 64-byte private key.

### First Run: Create and Save

```python
import RNS

# Create a fresh identity
identity = RNS.Identity()

# Persist to a file (writes 64 bytes of private key)
# WARNING: anyone with this file can decrypt all communication
identity.to_file("/path/to/identity")

# The identity hash (16 bytes) is the address
print(f"Identity hash: {identity.hexhash}")
# Example output: "6b3362bd2c1dbf87b66a85f79a8d8c75"
```

### Subsequent Runs: Load

```python
import RNS

identity = RNS.Identity.from_file("/path/to/identity")
if identity is None:
    raise RuntimeError("Failed to load identity")
```

**Important notes:**

- `to_file()` writes a raw 64-byte private key file. No encryption, no
  header, no metadata. It is not a directory.
- `from_file()` returns `None` on failure, not an exception. Always
  check for `None`.
- The identity hash (`identity.hexhash`, 32 hex chars) is the
  human-readable address. This is what MEDRE would use as
  `source_transport_id`.
- **First-run consideration:** If no identity file exists, create one
  and save it. On subsequent runs, load from the file. Do not create
  a new identity each run or you lose your address.


## Reticulum Configuration

Reticulum uses a configuration file to define transport interfaces.
Without any config, it creates a default at `~/.reticulum/config`.

### Default Config Path Search Order

1. `/etc/reticulum/config` (system-wide)
2. `~/.config/reticulum/config` (XDG)
3. `~/.reticulum/config` (fallback)

If none exist, Reticulum creates a minimal default config at
`~/.reticulum/config` on first run. This default config typically
enables an `AutoInterface` that discovers local peers on the same
network segment.

### Minimal Test Config

For testing, you can use a custom config directory:

```ini
# ~/.reticulum/config (or custom path)

# Enable the AutoInterface for local peer discovery
# This works on LAN/WiFi without any special hardware
[[Default Interface]]
  type = AutoInterface
  enabled = yes

# Or use TCP for connecting to a remote Reticulum node
# [[TCP Client]]
#   type = TCPClientInterface
#   target_host = reticulum.example.com
#   target_port = 4242
```

### Shared Instance

If `rnsd` (the Reticulum daemon) is already running, other programs on
the same system connect to it automatically via local IPC (port 37428).
You do not need to configure interfaces in your application config if
the shared instance is running.

```bash
# Start the Reticulum daemon (background service)
rnsd &
```

When the shared instance is running, `RNS.Reticulum()` connects to it
instead of initializing its own interfaces.


## Connection: How Reticulum Works

Reticulum is not connection-oriented in the traditional sense. There is
no host/port to connect to for LXMF messaging specifically. The flow
is:

1. **Initialize Reticulum** with a config directory that defines
   transport interfaces.
2. **Create or load an identity.**
3. **Create an LXMF router** with the identity and a storage path.
4. **Register a delivery identity** to get a destination.
5. **Announce** your presence on the network.
6. **Send messages** to destinations you know about (via their
   identity hash or destination hash).
7. **Receive messages** via the delivery callback.

There is no explicit "connect" step. Reticulum auto-discovers peers on
configured interfaces. Path requests and announces handle routing
automatically.

### One Machine, Two Instances

For testing send/receive on a single machine:

1. Run `rnsd` as the shared instance (master).
2. Run the sender process, which connects to the shared instance.
3. Run the receiver process, which also connects to the shared instance.

Both processes share the same Reticulum transport via the local IPC
mechanism. No special interface configuration needed.

Alternatively, use two separate machines or two separate config
directories with TCP interfaces pointing at each other.


## Required Environment Variables

The live smoke tests use these environment variables:

| Variable | Required | Example | Description |
|----------|----------|---------|-------------|
| `LXMF_CONNECTION_TYPE` | Yes | `reticulum` | Must be `"reticulum"`. Any other value causes tests to skip. |
| `LXMF_IDENTITY_PATH` | Yes | `/tmp/lxmf_test_identity` | Path to Reticulum identity file. Created on first run if missing. |
| `LXMF_DISPLAY_NAME` | No | `MEDRE Smoke Test` | Display name for LXMF announces. Defaults to empty string. |
| `LXMF_DESTINATION_HASH` | No | (32-hex-char hash) | Destination hexhash for outbound send tests. |

At minimum, `LXMF_CONNECTION_TYPE` and `LXMF_IDENTITY_PATH` must
be set. If any required variable is missing, every test in the file
skips with a descriptive reason.


## How to Run Live Tests

```bash
# Install dependencies
pip install lxmf rns

# Set environment variables
export LXMF_CONNECTION_TYPE="reticulum"
export LXMF_IDENTITY_PATH="/tmp/lxmf_test_identity"
export LXMF_DISPLAY_NAME="MEDRE Smoke Test"
# export LXMF_DESTINATION_HASH="6b3362bd2c1dbf87b66a85f79a8d8c75"

# Run live tests only
pytest tests/test_lxmf_live.py -m live -v

# Run all tests EXCEPT live (default behavior)
pytest

# Run everything including live
pytest -m ""
```

### Manual SDK Smoke Test

You can also verify the SDK manually outside the test harness:

```python
#!/usr/bin/env python3
"""Manual LXMF SDK smoke test. No network required for identity/router creation."""

import os
import sys

# Verify imports
try:
    import RNS
    import LXMF
    print(f"RNS imported. Version: {RNS.__version__ if hasattr(RNS, '__version__') else 'unknown'}")
    print(f"LXMF imported.")
except ImportError as e:
    print(f"Import failed: {e}")
    sys.exit(1)

# Create identity
identity = RNS.Identity()
print(f"Identity created. Hash: {identity.hexhash}")

# Save and reload
test_path = "/tmp/lxmf_smoke_identity"
identity.to_file(test_path)
loaded = RNS.Identity.from_file(test_path)
assert loaded is not None, "Failed to reload identity"
assert loaded.hexhash == identity.hexhash, "Identity hash mismatch"
print(f"Identity round-trip OK: {loaded.hexhash}")

# Initialize Reticulum (uses default config, may create ~/.reticulum)
try:
    reticulum = RNS.Reticulum()
    print(f"Reticulum initialized. Configdir: {RNS.Reticulum.configdir}")
except OSError as e:
    print(f"Reticulum already running in this process (expected if rnsd is active): {e}")
    reticulum = RNS.Reticulum.get_instance()

# Create LXMF router
storage_path = "/tmp/lxmf_smoke_storage"
os.makedirs(storage_path, exist_ok=True)
router = LXMF.LXMRouter(identity=identity, storagepath=storage_path)
print(f"LXMRouter created. Storage: {storage_path}/lxmf")

# Register delivery identity
dest = router.register_delivery_identity(identity, display_name="Smoke Test")
assert dest is not None, "Failed to register delivery identity"
print(f"Delivery destination: {RNS.hexrep(dest.hash, delimit=False)}")

# Register a no-op callback
def on_message(msg):
    print(f"Received message: {msg}")
router.register_delivery_callback(on_message)

# Announce (this may or may not reach anyone, depending on network)
router.announce(dest.hash)
print("Announce sent.")

# Create a message (not sending, just verifying construction)
source_dest = dest  # Using self as source for testing
test_msg = LXMF.LXMessage(
    destination=dest,  # send to self
    source=dest,
    content="MEDRE live smoke test message",
    title="Smoke Test",
    desired_method=LXMF.LXMessage.DIRECT,
)
print(f"LXMessage created. State: {test_msg.state}")

# Clean shutdown
router.exit_handler()
print("Router shut down cleanly.")

# Cleanup
os.remove(test_path)
print("Cleanup done. All smoke checks passed.")
```

This script verifies imports, identity creation/persistence, Reticulum
initialization, router creation, delivery identity registration, and
message construction. It does not send anything over a real network
(unless Reticulum has active interfaces).


## What the MEDRE Adapter Proves / Does Not Prove

### Proves (via the test harness)

- `LxmfConfig` validates identity_path shape and rejects empty strings.
- `LxmfAdapter.start()` succeeds in fake mode without SDK.
- `LxmfAdapter.start()` succeeds in reticulum mode when SDK and
  identity are available.
- `health_check()` returns `"healthy"` after start, `"unknown"` before
  start and after stop.
- `LxmfAdapter.stop()` is idempotent and does not raise on a
  never-started adapter.
- `deliver()` raises `TypeError` for non-`RenderingResult` input.
- `deliver()` returns `AdapterDeliveryResult` with `delivery_state`
  `"outbound"` (honest pending, not delivered).
- `simulate_inbound()` publishes a canonical event via
  `publish_inbound`.
- Start → stop → start → stop restart cycle works without state leaks.
- Double-start and double-stop are idempotent.
- Rapid start/stop cycles (5 iterations) are stable.

### Does Not Prove

- Messages actually traverse a real Reticulum network to a remote peer.
- Delivery callbacks fire for inbound messages from an independent
  second process.
- Path discovery works across real multi-hop transport interfaces.
- Propagation node sync works across independent peers.
- Resource transfer for large messages works.
- Stamp validation and generation works at scale.
- Reconnection recovers correctly under real network failure.
- Multiple LXMRouter instances in the same process work (they probably
  don't, since Reticulum is a singleton).
- Performance under load.


## Common Failures

| Symptom | Cause | Fix |
|---------|-------|-----|
| `ImportError: No module named 'RNS'` | `rns` not installed | `pip install rns` |
| `ImportError: No module named 'LXMF'` | `lxmf` not installed | `pip install lxmf` |
| `OSError: Attempt to reinitialise Reticulum` | `RNS.Reticulum()` called twice in same process | Use `RNS.Reticulum.get_instance()` for subsequent access, or ensure only one init call. |
| `ValueError: LXMF cannot be initialised without a storage path` | `storagepath=None` | Pass a valid directory path to `LXMRouter(storagepath=...)`. |
| `router.register_delivery_identity()` returns `None` | Called more than once per router | Only one delivery identity per router instance is supported. Create a new router if you need another identity. |
| No peers discovered | No transport interfaces configured, or no other Reticulum nodes on the network | Check Reticulum config file. Enable `AutoInterface` for LAN discovery. Start `rnsd` on another machine. |
| Path request timeouts | No route to destination | Ensure both peers have active interfaces. Check `rnstatus` for interface status. |
| `ModuleNotFoundError: No module named 'RNS.Interfaces...'` | Missing `pyserial` dependency | `pip install pyserial` or `pip install rns` (includes it). |
| Reticulum creates `~/.reticulum` unexpectedly | No custom configdir provided | Pass `configdir` to `RNS.Reticulum(configdir=...)` for test isolation. |
| Stale identity file | Previous test left identity file | Delete the file at `LXMF_IDENTITY_PATH` to force fresh creation. |


## Reticulum Transport Interface Types

For reference, Reticulum supports these interface types (configured in
the Reticulum config file, not in MEDRE config):

| Type | Config Name | Description |
|------|-------------|-------------|
| Auto | `AutoInterface` | Discovers peers on local network via multicast. Zero config for LAN testing. |
| TCP Client | `TCPClientInterface` | Connects to a remote Reticulum node via TCP. Requires `target_host` and `target_port`. |
| TCP Server | `TCPServerInterface` | Listens for incoming TCP connections. |
| UDP | `UDPInterface` | Sends/receives via UDP broadcast or unicast. |
| RNode | `RNodeInterface` | LoRa radio transceiver via USB serial. |
| Serial | `SerialInterface` | Generic serial port transport. |
| KISS | `KISSInterface` | KISS-compatible TNC/modem. |
| Pipe | `PipeInterface` | External program via stdio. |
| Custom | Custom modules | User-supplied interface types. |

For smoke testing, `AutoInterface` (LAN) or `TCPClientInterface`
(point-to-point) are the simplest options.


## Safety Notes

1. **Identity files are sensitive.** The identity file contains the
   private key. Anyone with access to it can impersonate the node and
   decrypt messages. Clean up test identity files after use.

2. **Reticulum config changes affect system connectivity.** If you
   modify `~/.reticulum/config`, it affects all Reticulum programs on
   the system. Use a custom configdir for testing:
   `RNS.Reticulum(configdir="/tmp/test_reticulum")`.

3. **Reticulum is a singleton.** If `rnsd` or any other Reticulum
   program is already running in the process, you cannot initialize
   another instance. This affects test isolation.

4. **Network traffic.** Reticulum sends announce packets and path
   requests on configured interfaces. These are small and infrequent,
   but be aware of them on constrained networks.

5. **LXMF propagation node data.** If `enable_propagation()` is called,
   the router indexes and serves messages from disk. Test storage
   directories should be cleaned up after use.

6. **Signal handlers.** `LXMRouter.__init__` registers SIGINT and
   SIGTERM handlers. This can interfere with test frameworks that
   handle signals. Consider this when designing the live harness.


## Explicit Scope Exclusions

The following are explicitly **out of scope** for the live smoke
harness and the LXMF adapter alpha:

- Production LXMF/Reticulum deployment readiness
- Propagation node store-and-forward across independent peers
- Resource transfer for large messages (attachments, images)
- Ticket-based reply correlation
- Multi-hop mesh delivery testing
- Multiple transport interface testing
- BLE, serial, LoRa hardware testing
- Reconnection under real network failure conditions
- LXST (LXMF Streaming Transport)
- Production deployment instructions
- Integration with Sideband, MeshChat, Nomad Network, or other LXMF
  clients (though such integration is a future goal)
- Reticulum as a standalone MEDRE adapter (not planned)
