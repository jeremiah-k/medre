# LXMF Alpha Operation Runbook

> Last updated: 2026-05-26
> Scope: Real LXMF/Reticulum Operation Alpha (Track 8) + LXMF Operational Clarification (Track 3)
> Status: **Alpha. Not production. Not hardened. Not complete.** Fake mode is the default development path. Real Reticulum/LXMF mode is implemented and available when optional dependencies (`pip install lxmf`) and valid configuration are present. It requires a live Reticulum transport to route actual messages.

This runbook describes how the MEDRE LXMF adapter operates against a real Reticulum network in alpha mode. Alpha mode means the `LxmfAdapter` — via its owned `LxmfSession` — initializes a real `RNS.Reticulum` instance, loads or creates an `RNS.Identity`, creates an `LXMF.LXMRouter`, registers delivery callbacks, sends real LXMF messages, and receives real inbound traffic. It does not mean the system is ready for anything beyond a single operator on a single Reticulum node.

Everything in this document is conservative. If something has not been tested against a real Reticulum network and confirmed working, this document says so. If something is known to be broken or missing, this document says that too.

**Fake mode** is the default and recommended path for all development and testing. Real Reticulum connectivity is opt-in for live validation, requires the `lxmf`/`RNS` packages, and needs a configured Reticulum transport layer.

## 1. Purpose

Alpha operation validates that the MEDRE LXMF adapter works end to end against a real Reticulum network with real LXMF message traffic. The adapter delegates all SDK interaction to its owned `LxmfSession` instance, which owns the `RNS.Reticulum`, `RNS.Identity`, and `LXMF.LXMRouter` lifecycle.

Scope boundaries:

- One transport: LXMF over Reticulum. No other transports are in scope for this runbook.
- One operator: a single person running against a local or network-accessible Reticulum instance.
- Text messages only. No attachments, no images, no audio, no telemetry, no commands, no streaming.
- No production deployment, no scaling, no monitoring, no alerting.
- No claims about reliability, durability, or correctness beyond what manual testing confirms.
- No multi-hop mesh delivery testing beyond what the local Reticulum instance provides.
- No E2EE beyond what Reticulum provides natively at the link layer.

This runbook complements `docs/runbooks/lxmf-live-smoke.md`. The smoke test validates adapter lifecycle and SDK connectivity. Alpha operation validates the full wiring: config, adapter, codec, inbound callback dispatch, outbound delivery, and health, running together.

## 2. Prerequisites

| Requirement         | Details                                                                                                                                                                                                                                                    |
| ------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Reticulum instance  | A running Reticulum transport layer. Can be a local `rnsd` daemon, a custom config with `AutoInterface` (LAN), or a TCP connection to a remote Reticulum node.                                                                                             |
| LXMF router storage | A writable directory for `LXMRouter` persistent state.                                                                                                                                                                                                     |
| Reticulum identity  | A 64-byte private key file created by `RNS.Identity.to_file()`. Created on first run if none exists.                                                                                                                                                       |
| Python              | 3.12 or later (CONFIRMED: 3.12 installed in dev environment)                                                                                                                                                                                               |
| Package install     | Core MEDRE: `pip install -e .` (no extra required for fake mode). Real connectivity: `pip install lxmf` (installs `rns` as a dependency, CONFIRMED: lxmf 0.9.7 + rns 1.2.5 installed). Alternative: `pip install rnspure` for pure-Python crypto (slower). |
| Network access      | At least one Reticulum transport interface configured (AutoInterface for LAN, TCPClientInterface for remote, etc.)                                                                                                                                         |

You do not need Docker for basic alpha operation. Docker guidance is in section 17.

**Critical: LXMF is not connection-oriented.** There is no "connect to a server" step. Reticulum auto-discovers peers on configured interfaces. Path requests and announces handle routing automatically. See section 5 for delivery semantics.

## 3. Fake Mode (Default)

Fake mode is the default development and testing path.

```python
from medre.config.adapters.lxmf import LxmfConfig
from medre.adapters.lxmf.adapter import LxmfAdapter

config = LxmfConfig(
    adapter_id="lxmf-test",
    connection_type="fake",    # default
)
adapter = LxmfAdapter(config)
await adapter.start(ctx)

# Simulate inbound (testing only)
await adapter.simulate_inbound(packet_dict)

# Outbound in fake mode returns AdapterDeliveryResult with OUTBOUND state
# (not None — honest pending semantics, not a delivered guarantee)
result = await adapter.deliver(rendering_result)
assert result.metadata["lxmf"]["delivery_state"] == "outbound"

await adapter.stop()
```

Fake mode facts:

- No `lxmf` or `RNS` imports are required. Core MEDRE tests pass without them.
- `compat.py` is the sole import site for `lxmf`/`RNS`. When absent, `HAS_LXMF` is `False`.
- `start()` succeeds for `connection_type="fake"` regardless of SDK availability.
- `deliver()` returns `AdapterDeliveryResult` with state `OUTBOUND` and a deterministic fake message ID. This is honest pending semantics — the message is queued, not delivered.
- `simulate_inbound()` processes packets through the real codec/classifier pipeline.
- `health_check()` returns `"healthy"` when started.
- Background tasks from `_on_packet` are tracked and drained on `stop()`.

## 4. Identity Setup [CONFIRMED]

Reticulum identities are dual-keypair (X25519 for encryption, Ed25519 for signing) derived from a single 64-byte private key.

### 4.1 Identity File

The identity file is a raw 64-byte private key. It is not encrypted, not wrapped, and has no metadata. Anyone with this file can impersonate the node and decrypt all messages. Treat it as a secret.

```python
import RNS

# First run: create and save
identity = RNS.Identity()
identity.to_file("/path/to/identity")  # writes 64 bytes
print(f"Identity hash: {identity.hexhash}")  # 32 hex chars, e.g. "6b3362bd2c1dbf87b66a85f79a8d8c75"

# Subsequent runs: load
identity = RNS.Identity.from_file("/path/to/identity")
if identity is None:
    raise RuntimeError("Failed to load identity from /path/to/identity")
```

### 4.2 MEDRE Config

`LxmfConfig.identity_path` is a string path to the identity file. It is validated as a non-empty string when provided. When `connection_type="reticulum"`, the `LxmfSession` loads the identity from this path at startup via `RNS.Identity.from_file()`. If the path is set but the file cannot be loaded, `start()` raises `LxmfConnectionError`. If `identity_path` is `None`, the session auto-generates a fresh identity (which will not persist across restarts).

```python
config = LxmfConfig(
    adapter_id="lxmf-alpha",
    connection_type="reticulum",
    identity_path="/path/to/identity",
    display_name="MEDRE Alpha Node",
)
```

### 4.3 First-Run Consideration

If no identity file exists at the configured path, the adapter would need to create one and save it. On subsequent runs, load from the file. Do **not** create a new identity each run or you lose your address. The identity hash (16 bytes, displayed as 32 hex chars) is your permanent LXMF address.

### 4.4 What MEDRE Sees

MEDRE never handles `RNS.Identity` or `RNS.Destination` objects directly. The adapter converts them to strings:

- `source_transport_id`: the sender's identity hash as a 32-char hex string (e.g. `"a1b2c3d4e5f6a7b8"`).
- `native_message_id`: the LXMF message hash as a 64-char hex string.
- Core and pipeline code never import from `lxmf` or `RNS`.

### 4.5 Runtime Configuration via Environment Variables

#### 4.5.1 Env-First Adapter Creation

LXMF adapters can be created entirely from environment variables:

```bash
export MEDRE_ADAPTER__LXMF_SENDER__TRANSPORT=lxmf
export MEDRE_ADAPTER__LXMF_SENDER__CONNECTION_TYPE=reticulum
export MEDRE_ADAPTER__LXMF_SENDER__IDENTITY_PATH=/safe/path/sender.identity
export MEDRE_ADAPTER__LXMF_SENDER__DISPLAY_NAME=sender
```

The `<TOKEN>` becomes the adapter's `adapter_id`. No TOML section is needed.

Simple routes can also be created with `MEDRE_ROUTE__<TOKEN>__<FIELD>` env vars.
Route tokens may contain only letters, numbers, and underscores. Advanced route
features may still require TOML. Route adapter references are adapter IDs, not
env tokens. Legacy `MEDRE_LXMF_*` runtime config vars are **unsupported** —
migrate to `MEDRE_ADAPTER__<TOKEN>__<FIELD>`.

## 5. Delivery Mode Semantics [CONFIRMED]

LXMF supports four delivery methods. The semantics are fundamentally asynchronous and store-and-forward. **Do not assume "instant delivered" or "realtime" guarantees.**

### 5.1 Method Comparison

| Method        | Code   | Wire Behavior                                                      | Reliability                                                                       | Latency                                           | Size Limit                                                      |
| ------------- | ------ | ------------------------------------------------------------------ | --------------------------------------------------------------------------------- | ------------------------------------------------- | --------------------------------------------------------------- |
| DIRECT        | `0x02` | Establishes `RNS.Link`, sends via link packet or `RNS.Resource`    | High. Retries up to `MAX_DELIVERY_ATTEMPTS` (5). Proof receipts confirm delivery. | Seconds to minutes (depends on path availability) | Link packet: 319B. Resource: arbitrary (multi-KB typical).      |
| OPPORTUNISTIC | `0x01` | Single RNS packet, no link. Embedded in route data.                | Best-effort. No ACK, no retry. Max 1 attempt (`MAX_PATHLESS_TRIES=1`).            | Seconds if peer is online; fails otherwise.       | 295 bytes encrypted content.                                    |
| PROPAGATED    | `0x03` | Delivered to propagation node via link. Node stores for recipient. | Moderate. Delivery to node is reliable. Recipient must sync from node.            | Minutes to hours (recipient must check in).       | Limited by propagation node config (`PROPAGATION_LIMIT=256KB`). |
| PAPER         | `0x05` | Encoded as QR code or `lxm://` URI. No network transport.          | None. Physical delivery only.                                                     | N/A                                               | Roughly 2KB (`PAPER_MDU`).                                      |

### 5.2 Method Selection

When `desired_method` is set on an `LXMessage`, the router respects it if feasible:

- `desired_method=DIRECT` with no path: router requests path, retries.
- `desired_method=OPPORTUNISTIC` with no path: router waits `PATH_REQUEST_WAIT` (7s), then fails.
- `desired_method=PROPAGATED` requires a propagation node to be configured.

If `desired_method` is `None`, the router selects the best available method based on size and path availability.

### 5.3 MEDRE Config

```python
config = LxmfConfig(
    adapter_id="lxmf-alpha",
    default_delivery_method="direct",  # "direct" | "opportunistic" | "propagated" | "paper"
)
```

### 5.4 Honest Assessment

**DIRECT is the expected default for alpha.** It provides the best reliability trade-off: link-based delivery with retries and proof receipts. However:

- Path discovery is asynchronous. There is no guarantee a path exists.
- Link establishment takes time. First delivery to a new peer may take seconds to minutes.
- Multi-hop routing adds latency at each hop.
- There is no "instant delivered" guarantee. The message is "sent" when `handle_outbound()` returns, but delivery confirmation is asynchronous via callbacks.

**OPPORTUNISTIC** is fire-and-forget. Use only for quick status messages where loss is acceptable.

**PROPAGATED** is store-and-forward. The message is stored at a propagation node. The recipient must explicitly sync from that node. Delivery latency is entirely dependent on when the recipient checks in. This can be seconds (if actively syncing) or hours/days (if offline).

**No LXMF delivery method provides synchronous confirmation.** Even DIRECT's proof receipt is asynchronous. Do not build features that assume `deliver()` returning means the message was received.

## 6. Async Delivery Caveats [CONFIRMED + INFERRED]

### 6.1 Threading Model

Reticulum and LXMF use background daemon threads, not asyncio:

- `LXMRouter` starts a `jobloop` daemon thread for processing outbound messages.
- Reticulum transport processing runs in threads.
- MEDRE's asyncio event loop runs in the main thread.

The `LxmfSession` bridges this boundary. When the SDK delivery callback fires in a Reticulum thread, the session normalises the `LXMessage` into a plain dict (no SDK objects leak), then schedules the adapter's `_on_packet` callback onto the captured asyncio loop via `loop.call_soon_threadsafe()`. Callbacks originate on Reticulum/LXMF threads; the session normalises to plain dicts and bridges onto the captured asyncio loop. This is the same pattern used by the Meshtastic adapter.

### 6.2 Callback Timing

Inbound messages arrive via `router.register_delivery_callback(cb)`. The callback fires in a Reticulum thread. The `LxmfSession._on_lxmf_delivery` method receives the raw `LXMessage`, normalises it into a plain dict (stripping all SDK objects), then forwards it to the adapter's `_on_packet` callback. The adapter then decodes and publishes the canonical event on the asyncio loop.

There is no guaranteed ordering of callbacks. Two messages arriving close together may be processed in any order.

### 6.3 Outbound Async Behavior

`LxmfSession.send_text()` (called by `LxmfAdapter.deliver()`) creates an `LXMessage`, registers delivery state callbacks, and calls `router.handle_outbound(lxm)`. It returns `(native_message_id, initial_state)` immediately — the initial state is typically `OUTBOUND` or `SENDING`, not `DELIVERED`. The actual delivery happens asynchronously in the router's `jobloop` thread.

Per-message callbacks update the `LxmfSession`'s outbound tracking dict:

- `_on_delivery_state_update` tracks state transitions for each tracked message.
- Terminal states (`DELIVERED`, `FAILED`, `REJECTED`, `CANCELLED`) are recorded.
- `FAILED` increments `transient_delivery_failures`. `REJECTED`/`CANCELLED` increment `permanent_delivery_failures`.

MEDRE's `deliver()` returns an `AdapterDeliveryResult` with the message hash and delivery state metadata. The state is honest: typically `"outbound"` at return time. The pipeline does not wait for delivery confirmation.

Outbound retry is bounded: `_SEND_MAX_RETRIES = 3` with a short linear backoff (`0.1 * attempt` seconds between retries). After exhausting retries, the send raises a session/internal `LxmfSendError`, which the adapter normalizes to `AdapterSendError` at the runtime boundary before the pipeline classifies it.

### 6.4 Reticulum Singleton

`RNS.Reticulum()` is a singleton per process. Calling it twice raises `OSError("Attempt to reinitialise Reticulum...")`. This means:

- Only one LXMRouter instance per process (in practice).
- Multiple adapters wanting separate identities would need separate processes.
- Test isolation requires careful setup/teardown or custom config directories.

## 7. Propagation Node Expectations [CONFIRMED API, INFERRED operational]

### 7.1 What Propagation Nodes Do

A propagation node is an LXMF router that stores messages on behalf of offline recipients. When a message is sent via PROPAGATED method:

1. The sender's router delivers the message to the propagation node.
2. The node stores it indexed by recipient identity hash.
3. When the recipient's router syncs with the node, it receives stored messages.

### 7.2 Configuration

```python
# Propagation node configuration (not yet implemented in LxmfSession):
router.set_outbound_propagation_node(node_hash)  # 16-byte destination hash
```

The propagation node's destination hash would need to be configured in `LxmfConfig`. No such config field exists yet. Expected additions:

- `propagation_node` (str): 32-hex-char destination hash of the propagation node.
- `enable_propagation` (bool): whether to run a local propagation node.

### 7.3 Alpha Expectations

For alpha, propagation is a secondary concern. DIRECT delivery to an online peer is the primary test path. Propagation node testing would require:

- A running propagation node (could be a second `LXMRouter` instance in a separate process).
- The recipient router to explicitly call `router.request_messages_from_propagation_node()`.
- Waiting for the sync to complete, which takes an indeterminate amount of time.

### 7.4 No Realtime Guarantee

Even with propagation, delivery is not instantaneous. The message sits at the propagation node until the recipient syncs. There is no push notification. This is fundamentally asynchronous store-and-forward, not realtime messaging.

## 8. Reconnect/Restart Expectations [INFERRED]

### 8.1 Current State

The `LxmfSession` implements bounded exponential backoff reconnection. On unexpected disconnect, the session's `_reconnect_loop` runs:

- Base delays: 1 s, 2 s, 4 s, 8 s, … capped at 30 s.
- ±25 % jitter on each delay to avoid thundering-herd synchronisation.
- Maximum 10 consecutive attempts.
- On `stop()`, a `_stop_requested` guard prevents further reconnects.
- On successful reconnect, `reconnect_attempts` resets and `reconnecting` becomes `False`.

`start()` and `stop()` are idempotent (no-op if already in the target state).

### 8.2 Implementation Details

Reconnect is triggered by `_trigger_reconnect()` which spawns an `asyncio.Task` running `_reconnect_loop`. The loop:

1. Computes delay = min(base \* 2^attempt, 30s) ± jitter.
2. Sleeps for the delay.
3. Tears down old SDK objects via `_teardown_sdk()`.
4. Reconnects via `_connect_real()`.
5. On success, exits the loop.
6. On failure, increments `reconnect_attempts` and loops.
7. After 10 failed attempts, logs an error and sets `reconnecting = False`.

Reticulum itself handles path rediscovery at the transport layer. The LXMF router's `process_outbound` thread retries failed deliveries up to `MAX_DELIVERY_ATTEMPTS` (5). These are SDK-level retries, in addition to the session-level outbound retry (`_SEND_MAX_RETRIES = 3`).

### 8.3 Restart

Restarting the adapter (stop then start):

1. `adapter.stop()` calls `session.stop(timeout=5.0)`.
2. `session.stop()` sets `_stop_requested`, cancels announce and reconnect tasks, unsubscribes callbacks, calls `_teardown_sdk()` (releases router/identity/reticulum), clears outbound tracking.
3. `adapter.start(ctx)` calls `session.start(message_callback)`.
4. `session.start()` (for `"reticulum"` mode) calls `_connect_real()`: initializes `RNS.Reticulum`, loads or creates identity, creates `LXMRouter`, registers delivery and announce callbacks.

The identity file persists across restarts. The outbound tracking dict is cleared on stop.

### 8.4 Repeated Start/Stop

`start()` and `stop()` are idempotent. Repeated start-stop cycles should be safe provided:

- Reticulum shutdown is complete before re-initialization (singleton constraint).
- Background tasks from previous runs are fully drained.
- The identity file is not deleted between cycles.

## 9. Minimum Viable Reticulum Topology [CONFIRMED API, INFERRED topology]

This section describes what constitutes a real Reticulum "network" for MEDRE LXMF operation. It is grounded in the Reticulum source code at the installed package path (v1.2.5, CONFIRMED) and does not assume prior Reticulum operational experience.

### 9.1 What Is a Reticulum Network?

A Reticulum network is **one or more Reticulum instances that can reach each other via at least one shared interface**. There is no central server, no broker, and no enrollment authority. A "network" exists when:

1. At least one `RNS.Reticulum()` instance is running with at least one interface configured.
2. If a second instance exists, it shares at least one interface (directly or via intermediate hops) with the first.

A **single instance with no peers is still a valid Reticulum instance** — it just cannot send or receive messages to/from anyone else.

### 9.2 Default Configuration Reality

On first run, Reticulum creates a default config at `~/.reticulum/config` (`Reticulum.py` line 1790, `__default_rns_config__`). The default config contains:

```ini
[reticulum]
enable_transport = False
share_instance = Yes
instance_name = default

[interfaces]
  [[Default Interface]]
    type = AutoInterface
    enabled = Yes
```

**What this means operationally:**

- `AutoInterface` uses IPv6 link-local multicast over UDP to discover other Reticulum nodes on the same LAN segment. No IP infrastructure (router, DHCP, DNS) is required. Link-local IPv6 must be enabled in the OS (default on nearly all Linux/macOS systems).
- `enable_transport = False` means this node will NOT route traffic for other peers or forward announces beyond what it needs for its own communication. It is an endpoint, not a router.
- `share_instance = Yes` means the first Reticulum process on this machine becomes the "master" instance. Subsequent programs on the same machine connect to it via local IPC (TCP port 37428 or AF_UNIX socket), not via separate interface hardware.

**The default config gives you a node that can discover and communicate with other Reticulum nodes on the same LAN, and nothing more.** No TCP, no serial, no radio. For anything beyond LAN scope, interfaces must be manually configured.

### 9.3 Interface Types Available

Confirmed from `Reticulum.py` imports and source (lines 33–47):

| Interface             | Transport                     | Configuration Required            | Scope                           |
| --------------------- | ----------------------------- | --------------------------------- | ------------------------------- |
| `AutoInterface`       | IPv6 link-local UDP/multicast | Minimal (default)                 | Same LAN segment                |
| `TCPClientInterface`  | TCP                           | Target host + port                | Any reachable TCP endpoint      |
| `TCPServerInterface`  | TCP                           | Listen host + port                | Accepts inbound TCP connections |
| `UDPInterface`        | UDP                           | Target host + port                | Point-to-point or broadcast UDP |
| `RNodeInterface`      | Serial/USB radio (RNode)      | Serial device path + radio params | LoRa radio                      |
| `SerialInterface`     | Serial                        | Device path + baud                | Serial cable                    |
| `KISSInterface`       | Serial KISS TNC               | Device path + baud                | Ham radio TNC                   |
| `I2PInterface`        | I2P                           | I2P settings                      | I2P overlay network             |
| `BackboneInterface`   | TCP (backbone)                | Target host + port                | Inter-network backbone          |
| `RNodeMultiInterface` | Multi-port RNode              | Serial device                     | Multiple LoRa channels          |
| `PipeInterface`       | Pipe                          | Command configuration             | Local process                   |
| `WeaveInterface`      | Weave                         | Weave settings                    | Weave network                   |

CONFIRMED: Interface list from `dir(RNS.Interfaces)`. RNodeInterface confirmed
to require `pyserial` (v3.5 installed). HW_MTU=508 for RNode.

For MEDRE alpha operation, `AutoInterface` (LAN) and `TCPClientInterface`/`TCPServerInterface` (remote nodes) are the practical choices. Radio interfaces require physical hardware.

### 9.4 Single-Node Setup

A single Reticulum node is sufficient for:

- Validating MEDRE adapter lifecycle (start, health, stop, restart).
- Confirming SDK integration (`RNS.Reticulum` init, identity load, `LXMRouter` creation).
- Self-loop testing if the adapter sends to its own identity (path discovery to self is immediate).

A single node is **insufficient** for:

- Testing actual message delivery between independent identities.
- Validating path discovery across the network.
- Testing propagation node store-and-forward.
- Any multi-hop behavior.

### 9.5 Two-Node Minimum for Delivery Validation

To test actual LXMF message delivery, you need **two independent Reticulum instances**, each with a separate identity, connected via at least one shared interface. Options:

| Setup                           | How                                                                                                                                                                              | Complexity                                                                     |
| ------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| **Two processes, same machine** | Process A: `RNS.Reticulum(configdir="/tmp/ret_a")`. Process B: `RNS.Reticulum(configdir="/tmp/ret_b")` with a TCPClientInterface pointing to a TCPServerInterface in A's config. | Medium. Requires custom configs and separate processes (singleton constraint). |
| **Two machines, same LAN**      | Both use default `AutoInterface` config. They discover each other automatically.                                                                                                 | Low. Zero config if on same subnet.                                            |
| **Two machines, TCP**           | One runs `TCPServerInterface`, the other `TCPClientInterface` pointing at it.                                                                                                    | Low. Requires one manual interface entry.                                      |
| **Radio link**                  | Both have RNode or compatible radio hardware.                                                                                                                                    | High. Requires hardware and physical proximity.                                |

**The simplest viable test topology is two machines on the same LAN with default AutoInterface configs.** No manual configuration required beyond installing Reticulum.

### 9.6 Path Discovery Timeline

Reticulum discovers paths via announce propagation. When a node announces, the announce propagates through connected interfaces. Path discovery time depends on:

- **Same LAN (AutoInterface):** Typically 1–5 seconds for announce propagation.
- **TCP link, online peer:** Seconds, depending on link latency.
- **Multi-hop mesh:** Accumulates per-hop. Each hop adds announce processing time.
- **Offline peer:** No path possible. Messages to offline peers require PROPAGATED delivery via a propagation node.

MEDRE does not control or accelerate path discovery. The `LxmfSession` cannot make path discovery faster than the underlying Reticulum transport allows. First message to a newly discovered peer may take seconds to minutes for path establishment before delivery begins.

## 10. rnsd Usage Expectations [CONFIRMED]

### 10.1 What Is rnsd?

`rnsd` is the Reticulum Network Stack daemon — a minimal Python program (`RNS/Utilities/rnsd.py`) that instantiates a `RNS.Reticulum()` object and holds it alive indefinitely:

```python
# Simplified from rnsd.py
reticulum = RNS.Reticulum(configdir=configdir, verbosity=targetverbosity, logdest=targetlogdest)
while True:
    time.sleep(1)
```

That is the entire program. It does not route LXMF messages, handle identities, or run an LXMRouter. It simply keeps a Reticulum transport instance running so that:

1. Transport interfaces stay active and connected.
2. Announce propagation continues.
3. Path tables are maintained.
4. Other local programs can connect to the shared instance.

### 10.2 When to Use rnsd

| Scenario                             | Use rnsd?  | Rationale                                                                                                                                                                                   |
| ------------------------------------ | ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **MEDRE adapter runs continuously**  | Optional   | The adapter's `LxmfSession` already creates its own `RNS.Reticulum()` instance. Running rnsd alongside it would conflict (singleton). The adapter IS the Reticulum instance.                |
| **Multiple local programs need RNS** | Yes        | rnsd acts as the shared instance master. Other programs (Sideband, Nomad Network, MEDRE) connect as clients.                                                                                |
| **Headless routing node**            | Yes        | A dedicated machine running rnsd with `enable_transport = True` and radio/TCP interfaces acts as a network router for other nodes.                                                          |
| **Development/testing**              | Usually no | The MEDRE live harness creates its own Reticulum instance. Running rnsd on the same machine would cause the harness to connect to the shared instance instead of owning its own interfaces. |
| **CI/CD**                            | No         | Test isolation requires dedicated `configdir` per test process.                                                                                                                             |

### 10.3 Shared Instance Model

Reticulum uses a master-client model on the same machine (`Reticulum.py` lines 282–289):

1. The **first** `RNS.Reticulum()` instance in a process becomes the "master" (shared instance).
2. It listens on TCP port 37428 (default) or an AF_UNIX socket for local client connections.
3. **Subsequent** `RNS.Reticulum()` calls in other processes detect the running master and connect as clients. They do NOT open their own hardware interfaces.
4. `is_connected_to_shared_instance` is `True` for client instances.
5. The master instance owns all hardware interfaces (serial, radio, TCP servers).

**Implication for MEDRE:** If rnsd is running when the MEDRE adapter starts, the adapter will connect as a client to rnsd's shared instance. It will NOT directly control any interfaces. This is fine for LAN operation (AutoInterface is shared) but may cause confusion during testing where the adapter expects to own its interfaces.

rnsd warns when started as a client to another shared instance (`rnsd.py` line 51):

```text
"Started rnsd version {version} connected to another shared local instance,
this is probably NOT what you want!"
```

### 10.4 When NOT to Use rnsd

- **During MEDRE live harness execution.** The harness needs to own its Reticulum instance with a custom `configdir` for test isolation.
- **When MEDRE is the only RNS program on the machine.** The adapter's built-in `RNS.Reticulum()` is sufficient.
- **When you need test isolation.** Use `RNS.Reticulum(configdir="/tmp/test_reticulum")` instead of relying on rnsd's shared instance.

### 10.5 rnsd Does Not Provide LXMF Services

Running rnsd does NOT provide:

- An LXMF propagation node (that requires `LXMRouter.enable_propagation()` — typically `lxmd`).
- An LXMF delivery endpoint.
- Message store-and-forward.
- Any LXMF-specific functionality.

rnsd provides Reticulum **transport** only. LXMF runs as a separate layer on top of Reticulum. To run a propagation node, use `lxmd` (the LXMF propagation daemon from the LXMF package) or an application that calls `LXMRouter.enable_propagation()`.

## 11. Propagation Node Realities

### 11.1 Propagation Is an LXMF Concept, Not a Reticulum Concept

Reticulum itself provides point-to-point and multi-hop **transport**. It has no concept of "propagation nodes" or message storage. Propagation is entirely an LXMF-layer feature implemented in `LXMRouter` (source: `/home/jeremiah/dev/LXMF/LXMF/LXMRouter.py` lines 535–673).

A propagation node is an `LXMRouter` that has called `enable_propagation()`. This:

1. Creates a message store at `{storagepath}/messagestore/`.
2. Indexes all existing messages on disk (blocking operation — can be slow with large stores).
3. Registers a `lxmf.propagation` destination for sync with other propagation nodes.
4. Accepts inbound PROPAGATED messages from senders and stores them indexed by recipient identity hash.
5. Syncs stored messages with other propagation nodes (encrypted, distributed).

### 11.2 What Propagation Nodes Provide

| Capability                               | Reality                                                                                                                |
| ---------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| Store-and-forward for offline recipients | Yes. Message is stored until the recipient syncs.                                                                      |
| Guaranteed delivery                      | No. Recipient must explicitly sync. No push notification.                                                              |
| Distributed message store                | Yes. Propagation nodes sync with each other.                                                                           |
| Encrypted storage                        | Yes. Messages are encrypted to the recipient's identity. The propagation node cannot read them.                        |
| Realtime delivery                        | No. Delivery latency depends entirely on when the recipient's router calls `request_messages_from_propagation_node()`. |

### 11.3 Running a Propagation Node

To run a propagation node for MEDRE testing:

1. Create a dedicated `LXMRouter` with `enable_propagation()`.
2. This requires a **separate process** from the MEDRE adapter (singleton constraint: one `RNS.Reticulum` per process, and one delivery identity per `LXMRouter`).
3. The propagation node's identity hash must be configured in the sender's router via `router.set_outbound_propagation_node(node_hash)`.
4. The recipient must call `router.request_messages_from_propagation_node(identity)` to sync.

The LXMF package provides `lxmd` (`LXMF/Utilities/lxmd.py`) — a standalone propagation node daemon. It runs as a separate process, creates its own Reticulum instance and LXMRouter, and provides propagation services.

### 11.4 MEDRE Alpha: Propagation Is Not Required

For alpha validation, DIRECT delivery to an online peer is the primary test path. Propagation adds significant operational complexity:

- Requires a third process (the propagation node) in addition to sender and receiver.
- Requires configuring the propagation node's destination hash in both sender and receiver.
- Adds indeterminate latency (recipient must actively sync).
- Has not been tested against the MEDRE adapter in any configuration.

Propagation is architecturally important for real-world LXMF operation (offline delivery is a core feature) but is **not a blocker for alpha or beta-readiness validation**. The adapter's `propagation_enabled` diagnostic key detects propagation state; the adapter does not currently configure or manage propagation nodes.

### 11.5 Propagation Node Sync Protocol

Confirmed from `LXMRouter.py`:

1. Sender's router delivers PROPAGATED message to the propagation node via a direct link.
2. The propagation node stores the message, indexed by recipient's destination hash.
3. Other propagation nodes sync with this node, creating a distributed store.
4. The recipient's router periodically calls `request_messages_from_propagation_node()`.
5. The propagation node responds with messages addressed to the recipient's identity.
6. The recipient's router unpacks, validates, and fires delivery callbacks.

This is a **pull model**. There is no push notification to the recipient. The recipient must actively request messages. The sync interval is configured in the recipient's router, not in MEDRE.

## 12. Single-Node vs Multi-Node Expectations

### 12.1 What Works with a Single Node

| Capability                            | Works? | Notes                                                              |
| ------------------------------------- | ------ | ------------------------------------------------------------------ |
| Adapter lifecycle (start/stop/health) | ✅     | No network required.                                               |
| Identity creation/loading             | ✅     | Local operation.                                                   |
| LXMRouter creation                    | ✅     | Local operation. Requires `storagepath`.                           |
| SDK import verification               | ✅     | `import RNS; import LXMF`                                          |
| Self-send (identity to self)          | ⚠️     | Path discovery to self is immediate but this is a degenerate case. |
| Outbound to unknown peer              | ❌     | No path exists. Message stays in OUTBOUND state indefinitely.      |
| Inbound from external peer            | ❌     | No external peers exist.                                           |
| Propagation node operation            | ❌     | No other peers to store messages for or sync with.                 |

### 12.2 What Requires Two Nodes

| Capability                                      | Minimum Topology                                                                        |
| ----------------------------------------------- | --------------------------------------------------------------------------------------- |
| Direct message delivery                         | Two nodes on shared interface. Seconds to minutes for path establishment on first send. |
| Path discovery validation                       | Two nodes. Announce propagation confirms path.                                          |
| Inbound delivery callback                       | Two nodes. Sender in process A, receiver in process B.                                  |
| Delivery state progression (OUTBOUND→DELIVERED) | Two nodes. DIRECT delivery with proof receipt.                                          |

### 12.3 What Requires Three+ Nodes

| Capability                                | Minimum Topology                                                                                           |
| ----------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| Multi-hop routing                         | Three nodes in a line (A→B→C). Node B must have `enable_transport = True`.                                 |
| Propagation node store-and-forward        | Three nodes: sender, propagation node, recipient. Propagation node is a dedicated LXMRouter.               |
| Propagation node sync (distributed store) | Four nodes: sender, propagation node A, propagation node B, recipient. Nodes A and B sync with each other. |

### 12.4 What Has Not Been Tested at Any Topology

As of 2026-05-10, the following have **no live evidence** in the MEDRE project:

- Any message delivery between two independent Reticulum instances.
- Delivery state progression beyond OUTBOUND against a real network.
- Path discovery time or reliability.
- Propagation node store-and-forward.
- Multi-hop routing.
- Reconnect behavior after real network interruption.
- Announce propagation latency.
- Message delivery under concurrent load.

All validation to date is mock-based (fake mode). The live harness (`tests/test_lxmf_live.py`, 829 LOC) exists but has not been run against a real Reticulum network. See `docs/runbooks/operational-evidence.md` §3 for the LXMF evidence placeholder.

## 13. Realistic Operational Constraints

This section documents honest constraints that operators and developers should expect when running the MEDRE LXMF adapter against real Reticulum networks. It prevents false expectations about deployment maturity.

### 13.1 Deployment Maturity

| Constraint                      | Reality                                                                                                                                         |
| ------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| Production readiness            | **Not ready.** Alpha-operational (Tier 2). Unit-tested only. No live evidence.                                                                  |
| Live harness status             | Exists (829 LOC) but **not executed** against real Reticulum.                                                                                   |
| Delivery state validation       | State model (`OUTBOUND → DELIVERED`) is implemented but **not confirmed** against real network. Timing/state assumptions may break in practice. |
| Compatibility with LXMF clients | No compatibility tested or claimed with Sideband, MeshChat, Nomad Network, or any other LXMF application.                                       |

### 13.2 Network Constraints

| Constraint                   | Reality                                                                                                           |
| ---------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| Path discovery latency       | Seconds to minutes. Not instant. First send to a new peer always incurs path discovery overhead.                  |
| Offline peer delivery        | Only via PROPAGATED method with a configured propagation node. No fallback.                                       |
| Delivery confirmation        | Asynchronous only. `deliver()` returns before the message is delivered. Proof receipts arrive via callbacks.      |
| Message ordering             | No guaranteed ordering. Two messages sent in sequence may arrive in any order.                                    |
| Payload size (DIRECT)        | 319 bytes per link packet. Larger payloads use `RNS.Resource` (multi-packet transfer).                            |
| Payload size (OPPORTUNISTIC) | 295 bytes encrypted content. Hard limit. No fragmentation.                                                        |
| Bandwidth                    | Depends entirely on interface type. LoRa: bytes/second. LAN: megabytes/second. Reticulum adapts to interface MTU. |

### 13.3 Operational Constraints

| Constraint        | Reality                                                                                                                                                                                                                                                                                                                          |
| ----------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Process model     | One `RNS.Reticulum` per process (singleton). One delivery identity per `LXMRouter`. Multiple identities require multiple processes.                                                                                                                                                                                              |
| Identity security | 64-byte raw private key file. No encryption. No passphrase. Anyone with the file can impersonate the identity and decrypt all messages.                                                                                                                                                                                          |
| Threading         | Reticulum and LXMF use daemon threads, not asyncio. MEDRE bridges via `call_soon_threadsafe()`. Callbacks originate on Reticulum/LXMF threads; the session normalises to plain dicts and schedules onto the captured asyncio loop. Source/mock tested, not live Reticulum validated. Potential GIL/contention issues under load. |
| Storage           | `LXMRouter` requires a writable `storagepath`. Message store grows over time. No automatic cleanup.                                                                                                                                                                                                                              |
| License           | Reticulum uses a custom license that restricts AI training data usage and certain applications. Not OSI-approved. Review for downstream distribution.                                                                                                                                                                            |
| Daemon dependency | Reticulum is designed for long-running processes. Short-lived scripts may not establish stable connectivity before exiting.                                                                                                                                                                                                      |

### 13.4 What MEDRE Does NOT Provide

- **No E2EE beyond Reticulum's link-layer encryption.** MEDRE sends plaintext to the LXMF layer. Reticulum encrypts at the link/transport level.
- **No propagation node management.** The adapter does not configure, start, or manage propagation nodes.
- **No LXMF client compatibility.** No testing against Sideband, MeshChat, Nomad Network, or other LXMF applications.
- **No attachment or media transfer.** Text messages only.
- **No message queue persistence.** If the adapter stops, in-flight outbound messages are lost. The LXMF router persists outbound state, but MEDRE does not re-sync on restart.
- **No topology management.** MEDRE does not configure Reticulum interfaces, manage announces, or control path discovery.
- **No monitoring or alerting.** No Prometheus, no health alerts, no uptime tracking.

### 13.5 Honest Beta Assessment

For LXMF to move from alpha-operational (Tier 2) to beta-candidate (Tier 3), the following must happen (per `docs/contracts/37-transport-maturity-classification.md` §9.2):

1. **Run live harness** against a real two-node Reticulum network. Record results in `operational-evidence.md`.
2. **Confirm delivery state progression** (`OUTBOUND → SENDING → SENT → DELIVERED`) against a real network.
3. **Document identity file protection** requirements in a security runbook or operational guide.

These are not optional. Without live evidence, the delivery state model is speculative regardless of how well it tests against mocks. The live harness exists and is comprehensive (829 LOC, 19 test cases). What is missing is the operational act of running it against a real Reticulum instance and recording results.

## 14. Diagnostics

### 14.1 Implementation

The `LxmfSession` exposes a `diagnostics()` method returning `LxmfSessionDiagnostics`, a frozen dataclass. The adapter exposes the session via its `session` property. The `LxmfAdapter` itself does not wrap this in a top-level `diagnostics()` method; consumers access it via `adapter.session.diagnostics()`.

### 14.2 Diagnostics Keys

| Key                           | Type           | Description                                                                        |
| ----------------------------- | -------------- | ---------------------------------------------------------------------------------- |
| `connected`                   | `bool`         | Whether the LXMRouter is initialized and the session is active.                    |
| `router_running`              | `bool`         | Whether the LXMF router's `jobloop` thread is operational.                         |
| `reconnecting`                | `bool`         | Whether the session is currently in the reconnect loop.                            |
| `reconnect_attempts`          | `int`          | Number of consecutive reconnect attempts since last successful connection.         |
| `last_message_time`           | `str \| None`  | ISO 8601 timestamp of last successful inbound message.                             |
| `transient_delivery_failures` | `int`          | Count of temporary delivery failures (retriable).                                  |
| `permanent_delivery_failures` | `int`          | Count of permanent delivery failures (not retriable).                              |
| `last_error`                  | `str \| None`  | Description of the most recent session-level error.                                |
| `known_path_count`            | `int \| None`  | Number of known paths in the router's path table (`None` if router not available). |
| `propagation_enabled`         | `bool \| None` | Whether a propagation node is configured (`None` if router not available).         |
| `pending_delivery_count`      | `int \| None`  | Number of outbound deliveries currently being tracked (`None` if none).            |
| `mode`                        | `str`          | Current connection mode (`"fake"` or `"reticulum"`).                               |

The session also provides `delivery_state_counts()` returning a `dict[str, int]` of outbound delivery counts per state (`"outbound"`, `"sending"`, `"sent"`, `"delivered"`, `"failed"`, etc.).

### 14.3 What Diagnostics Does NOT Expose

- Private keys, identity file contents, or raw key material.
- Raw `RNS.Destination` or `RNS.Identity` objects.
- Raw LXMF message payloads.
- Reticulum transport interface internals.
- Peer identity dumps or cryptographic material.

### 14.4 Interpreting Diagnostics

| Symptom                                            | Likely Cause                                                            |
| -------------------------------------------------- | ----------------------------------------------------------------------- |
| `connected=False`                                  | Reticulum not initialized or router creation failed.                    |
| `reconnecting=True` with high `reconnect_attempts` | Persistent network or interface failure.                                |
| `known_path_count=0` or `None`                     | No peers discovered. Check interface config.                            |
| `pending_delivery_count` growing                   | Outbound messages queued but not reaching terminal state.               |
| `transient_delivery_failures` climbing             | Network instability, path timeouts.                                     |
| `permanent_delivery_failures` climbing             | Misconfigured destination, invalid identity, or permanent path failure. |
| `last_message_time` stale                          | No traffic for an extended period. Not necessarily an error.            |
| `router_running=False` with `connected=True`       | Router teardown incomplete or crashed `jobloop` thread.                 |

### 14.5 Delivery State Model

The session tracks outbound delivery states via the `LxmfDeliveryState` enum:

```text
GENERATING → OUTBOUND → SENDING → SENT → DELIVERED
                                       ↘ FAILED
                                       ↘ REJECTED
                                       ↘ CANCELLED
```

`delivery_state_counts()` returns counts of tracked outbound deliveries per state. These are snapshots at call time. Terminal deliveries (`DELIVERED`, `FAILED`, `REJECTED`, `CANCELLED`) are cleaned up after processing, so the count reflects active/pending deliveries predominantly.

The `delivery_state_counts()` method returns a `dict[str, int]` where keys are delivery state names (e.g. `"outbound"`, `"sending"`, `"sent"`, `"delivered"`, `"failed"`) and values are the count of outbound deliveries in each state. This is useful for monitoring pending deliveries and detecting stuck messages.

Example usage:

```python
counts = adapter.session.delivery_state_counts()
# {"outbound": 2, "delivered": 5, "failed": 0}
```

### 14.6 Two-Process Topology Testing

Two-process topology tests validate LXMF adapter behaviour in a real
two-node Reticulum network. Each process runs in a separate terminal
with its own identity and role (sender or receiver).

For detailed setup instructions, required environment variables, and
step-by-step procedures, see the **Two-Process Topology Testing** section
in `docs/runbooks/lxmf-live-smoke.md`.

Key points:

- **Process B (receiver) must be started first** so the sender can
  discover it via AutoInterface announce propagation.
- Both processes need `LXMF_TOPOLOGY_LIVE=1` and `LXMF_CONNECTION_TYPE=reticulum`.
- The sender needs `LXMF_DESTINATION_HASH` set to the receiver's identity hash.
- The sender's send tests require `LXMF_LIVE_SEND=1`.
- Tests are in the `TestLxmfTopologyLive` class in `tests/test_lxmf_live.py`.
- All topology tests are skipped by default unless `LXMF_TOPOLOGY_LIVE=1`.

## 15. Troubleshooting

### 15.1 Common Failures

| Symptom                                                         | Cause                                                     | Fix                                                                                                |
| --------------------------------------------------------------- | --------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| `ImportError: No module named 'RNS'`                            | `rns` not installed                                       | `pip install rns` or `pip install lxmf`                                                            |
| `ImportError: No module named 'LXMF'`                           | `lxmf` not installed                                      | `pip install lxmf`                                                                                 |
| `OSError: Attempt to reinitialise Reticulum`                    | `RNS.Reticulum()` called twice                            | Use `RNS.Reticulum.get_instance()` for subsequent access, or ensure only one init call per process |
| `ValueError: LXMF cannot be initialised without a storage path` | `storagepath=None`                                        | Pass a valid directory path to `LXMRouter(storagepath=...)`                                        |
| `register_delivery_identity()` returns `None`                   | Called more than once per router                          | Only one delivery identity per router instance.                                                    |
| No peers discovered                                             | No transport interfaces configured                        | Check Reticulum config. Enable `AutoInterface` for LAN. Start `rnsd` on another machine.           |
| Path request timeouts                                           | No route to destination                                   | Ensure both peers have active interfaces. Use `rnstatus` to check.                                 |
| `Reticulum creates ~/.reticulum` unexpectedly                   | No custom configdir                                       | Pass `configdir` to `RNS.Reticulum(configdir=...)` for test isolation.                             |
| Messages stuck in OUTBOUND state                                | No path to destination, or link establishment in progress | Wait for path discovery (seconds to minutes). Check `RNS.Transport.has_path()`.                    |
| `LxmfConnectionError: Failed to load identity from ...`         | Identity file missing or corrupted                        | Re-create the identity file, or check the path in `identity_path` config.                          |
| `LxmfConnectionError: Failed to initialise LXMF session: ...`   | Reticulum init or router creation failed                  | Check Reticulum config, available interfaces, and that no other process holds the singleton.       |
| Live tests skip with "Set LXMF_CONNECTION_TYPE"                 | Environment variables not set                             | Set `LXMF_CONNECTION_TYPE=reticulum` and `LXMF_IDENTITY_PATH=/path/to/identity`.                   |

### 15.2 Reticulum-Specific Issues

| Symptom                                                    | Cause                                  | Fix                                                                                                               |
| ---------------------------------------------------------- | -------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `ModuleNotFoundError: No module named 'RNS.Interfaces...'` | Missing `pyserial` dependency          | `pip install pyserial` or `pip install rns` (includes it).                                                        |
| Slow identity creation                                     | High `stamp_cost` on first run         | Stamp cost affects inbound validation, not identity creation. If identity creation is slow, check system entropy. |
| Announce not reaching peers                                | Interface misconfiguration or firewall | Verify interface in Reticulum config. Check `rnstatus` output. AutoInterface requires multicast-enabled LAN.      |

### 15.3 Diagnostic Commands

```bash
# Check Reticulum interface status
rnstatus

# Check if rnsd is running
ps aux | grep rnsd

# Manually verify SDK installation
python -c "import RNS; import LXMF; print('OK')"

# Create a test identity
python -c "import RNS; i = RNS.Identity(); print(i.hexhash)"
```

## 16. Live Harness Instructions

### 16.1 Test Markers

All live tests in `tests/test_lxmf_live.py` are tagged with `pytest.mark.live`. They are excluded by default via `pyproject.toml` addopts (`-m 'not live'`).

### 16.2 Environment Gating

Live tests require explicit opt-in via environment variables:

| Variable               | Required | Example             | Description                                         |
| ---------------------- | -------- | ------------------- | --------------------------------------------------- |
| `LXMF_CONNECTION_TYPE` | Yes      | `reticulum`         | Must be `"reticulum"`. Any other value skips.       |
| `LXMF_IDENTITY_PATH`   | Yes      | `/path/to/identity` | Path to Reticulum identity file. Must be non-empty. |
| `LXMF_DISPLAY_NAME`    | No       | `MEDRE Live Test`   | Display name for LXMF announces.                    |

If any required variable is unset, every test in the file skips with a descriptive reason.

### 16.3 Running Live Tests

```bash
# Install dependencies
pip install lxmf

# Set environment variables
export LXMF_CONNECTION_TYPE="reticulum"
export LXMF_IDENTITY_PATH="/tmp/lxmf_test_identity"

# Run live tests only
pytest tests/test_lxmf_live.py -m live -v

# Run all tests EXCEPT live (default behavior)
pytest

# Run everything including live
pytest -m ""
```

### 16.4 Current Test Status

Live tests in `tests/test_lxmf_live.py` are structured as dual-mode:

- **Real-mode tests** (`connection_type="reticulum"`): catch `LxmfConnectionError` and `pytest.skip()` when no Reticulum is available. When a live Reticulum instance is present, these tests exercise the full lifecycle: start, health check, stop, restart, and idempotency.
- **Fake-mode tests** (`connection_type="fake"`): run without any SDK dependency. Exercise the complete lifecycle in fake mode including rapid start/stop cycles, idempotency, and the inbound pipeline via `simulate_inbound`.
- **Documentation tests**: always-pass tests that record current constraints (no E2EE, no inbound from second identity in this harness).

### 16.5 Identity Path Setup for Tests

```bash
# Create a test identity
python -c "
import RNS
i = RNS.Identity()
i.to_file('/tmp/lxmf_test_identity')
print(f'Identity hash: {i.hexhash}')
"

# Set the env var
export LXMF_IDENTITY_PATH="/tmp/lxmf_test_identity"
```

### 16.6 Startup/Shutdown Test Pattern

Test lifecycle (matches `tests/test_lxmf_live.py`):

```python
# 1. Create config from env vars
config = _make_config()

# 2. Create adapter (which creates LxmfSession internally)
adapter = LxmfAdapter(config)

# 3. Create context
ctx = _make_context()

# 4. Start (session initializes Reticulum/Identity/Router)
await adapter.start(ctx)

# 5. Check health
info = await adapter.health_check()
assert info.health in ("healthy", "unknown")

# 6. Stop (session tears down SDK objects, cancels tasks)
await adapter.stop()

# 7. Verify stop is idempotent
await adapter.stop()  # should be no-op
```

### 16.7 Send Test Pattern

```python
# 1. Start adapter with real Reticulum
# 2. Create RenderingResult with LXMF content dict
# 3. Call deliver() — returns AdapterDeliveryResult with delivery_state "outbound"
# 4. delivery_state is honest pending, not "delivered"
# 5. Track delivery via session.delivery_state_counts() over time
```

### 16.8 Callback Test Pattern

```python
# 1. Start adapter with real LXMRouter
# 2. From a second Reticulum instance, send a message to the adapter's identity hash
# 3. The session's _on_lxmf_delivery callback normalises the LXMessage into a plain dict
# 4. The adapter's _on_packet classifies, decodes, and publishes the canonical event
# 5. Verify publish_inbound was called with the decoded event
```

### 16.9 Restart/Repeated Start-Stop

```python
# Verify idempotency
await adapter.start(ctx)  # starts
await adapter.start(ctx)  # no-op
await adapter.stop()      # stops
await adapter.stop()      # no-op

# Verify restart
await adapter.start(ctx)  # starts again
await adapter.stop()
```

## 17. Docker Guidance

### 17.1 Key Considerations

- **Reticulum singleton**: one `RNS.Reticulum` per container. If you need multiple routers, use multiple containers or processes.
- **Network access**: Reticulum's `AutoInterface` uses multicast for LAN discovery. Docker's default bridge network does not forward multicast. You need `--network host` or a custom network with multicast enabled.
- **TCP interfaces**: `TCPClientInterface` works fine in Docker. Configure the target host/port in the Reticulum config.
- **Storage paths**: mount LXMF storage and Reticulum config as volumes for persistence.
- **Identity files**: mount the identity file as a read-only volume or secret.

### 17.2 Minimal Docker Compose (Sketch)

```yaml
# This is a sketch, not a tested configuration.
services:
  medre-lxmf:
    build: .
    environment:
      - LXMF_CONNECTION_TYPE=reticulum
      - LXMF_IDENTITY_PATH=/run/secrets/identity
    volumes:
      - ./reticulum-config:/etc/reticulum
      - lxmf-storage:/var/lib/lxmf
    secrets:
      - identity
    network_mode: host # required for AutoInterface
volumes:
  lxmf-storage:
secrets:
  identity:
    file: ./identity
```

This is not production guidance. It is a starting point for alpha isolation testing.

## 18. Reticulum Config Guidance

### 18.1 Config Directory Search Order

1. `/etc/reticulum/config` (system-wide)
2. `~/.config/reticulum/config` (XDG)
3. `~/.reticulum/config` (fallback)

If none exist, Reticulum creates a minimal default at `~/.reticulum/config` on first run.

### 18.2 Minimal Alpha Config

```ini
# /tmp/reticulum_alpha/config

# AutoInterface discovers local peers via multicast
# Works on LAN/WiFi without special hardware
[[Default Interface]]
  type = AutoInterface
  enabled = yes

# Optional: TCP connection to a remote Reticulum node
# [[TCP Client]]
#   type = TCPClientInterface
#   target_host = reticulum.example.com
#   target_port = 4242
```

### 18.3 Shared Instance

If `rnsd` is already running, other programs connect to it via local IPC (port 37428 by default). No interface configuration needed in the application config.

```bash
rnsd &
```

### 18.4 Storage Paths

| System              | Path                      | Contents                                                    |
| ------------------- | ------------------------- | ----------------------------------------------------------- |
| Reticulum config    | `~/.reticulum/` (default) | `config`, `storage/`, `interfaces/`                         |
| Reticulum storage   | `{configdir}/storage/`    | `known_destinations`, `cache/`, `resources/`, `identities/` |
| LXMF router storage | `{storagepath}/lxmf/`     | `local_deliveries`, `messagestore/`, `ratchets/`            |
| MEDRE SQLite        | Configured by MEDRE       | Events, receipts, native refs                               |

### 18.5 Safety

- **Do not use production identity files for testing.** Create separate test identities.
- **Use custom configdir for testing.** `RNS.Reticulum(configdir="/tmp/test_reticulum")` avoids modifying system-wide config.
- **Clean up test directories.** LXMF storage and Reticulum config dirs accumulate state.

### 18.6 Live Validation Evidence

### Test Results

- **File:** `tests/test_lxmf_live.py`
- **Last run:** Not yet run
- **Command:** `pytest tests/test_lxmf_live.py -m live -v`
- **Result:** Not yet run
- **Environment:**
  - `LXMF_CONNECTION_TYPE`: required (reticulum), not set
  - `LXMF_IDENTITY_PATH`: optional (auto-generated if empty), not set
  - `LXMF_DISPLAY_NAME`: optional, not set
  - `LXMF_DESTINATION_HASH`: optional (required for outbound delivery test), not set
- **Hardware/Network:** Not available (no Reticulum network instance running)
- **Failures/Notes:** Live validation has not been performed in this environment. Alpha operation requires a running Reticulum transport layer with the environment variables configured. Without these, all live tests skip automatically. See the smoke test runbook (`docs/runbooks/lxmf-live-smoke.md`) for detailed setup and environment variable instructions.

## 19. Explicit Non-Claims

- **No production LXMF/Reticulum deployment readiness is claimed.** Alpha mode requires installed SDK and live environment.
- **No realtime delivery guarantees.** LXMF is asynchronous store-and-forward. Latency ranges from seconds to hours.
- **No multi-hop delivery has been tested end-to-end** beyond what the local Reticulum instance provides.
- **No E2EE is implemented by MEDRE.** Reticulum handles link-level encryption natively. MEDRE sends plaintext to the LXMF layer.
- **No propagation node testing has been performed.** The session detects propagation state for diagnostics but does not configure propagation nodes.
- **No compatibility with Sideband, MeshChat, Nomad Network, or other LXMF clients is claimed.**
- **No LXST (LXMF Streaming Transport) support.**
- **No attachment, image, audio, or media transfer support.**

## 20. Tranche 5: Hardening Summary

> **Added:** 2026-05-26
> **Scope:** Delivery semantics hardening (threading bridge in session.py, honest delivery_note in adapter.py), plus test coverage and doc hardening. Source was changed for delivery/threading hardening.

### Test Coverage Added

Tranche 5 adds test classes covering areas previously only implied by existing tests:

| Test Class                             | Area Covered                                                        |
| -------------------------------------- | ------------------------------------------------------------------- |
| `TestTranche5CallbackThreadingSafety`  | Sync and async callback dispatch, exception tolerance               |
| `TestTranche5SendReturnSemantics`      | Honest OUTBOUND return, not DELIVERED/SENT/SENDING; unique IDs      |
| `TestTranche5DeliveryStateTransitions` | Full OUTBOUND→SENDING→SENT→DELIVERED chain; FAILED/REJECTED cleanup |
| `TestTranche5BoundedOutboundCleanup`   | Terminal-state untracking, partial delivery counts                  |
| `TestTranche5SignatureValidated`       | Codec handles signature_validated true/false/missing                |
| `TestTranche5MissingOptionalFields`    | Codec handles missing source_hash, timestamp, fields, etc.          |
| `TestTranche5DeliveryMethodMetadata`   | delivery_method and has_fields in native metadata                   |

### Source Changes in Tranche 5

Tranche 5 includes source changes to `session.py` (threading bridge via `call_soon_threadsafe`, post-stop callback guard clearing `_message_callback`/`_loop` on stop, early return in `_on_lxmf_delivery`) and `adapter.py` (honest delivery_note). The codec, renderer, and config modules are unchanged.

### What Was Not Done

- No live Reticulum testing performed.
- No new SDK APIs discovered or documented.

### Operational Impact

Source changes affect runtime behaviour: the threading bridge now uses `call_soon_threadsafe` for delivery-state updates from Reticulum threads, and the session clears callback state on failed start. Existing tests were updated with `await asyncio.sleep(0)` to accommodate the bridge timing. No status changes in the capability matrix.

## 21. Tranche 6: Session Edge-Case Hardening

> **Added:** 2026-05-26
> **Scope:** Session edge-case fixes (failed-start cleanup, no-callback-without-loop guard, delivery-state thread bridging, async callback exception handling), plus test coverage and doc cleanup.

### Source Changes in Tranche 6

`LxmfSession` (`src/medre/adapters/lxmf/session.py`):

- **Failed-start cleanup**: `start()` wraps `_connect_real()` in try/except; on failure clears `_message_callback`, `_loop`, and diagnostics flags.
- **No callback without loop**: `_on_lxmf_delivery()` logs warning and returns without invoking callback when `loop` is `None` or not running. Removes unsafe direct-callback fallback.
- **Delivery-state thread bridging**: `_on_delivery_state_update()` bridges via `call_soon_threadsafe` to `_apply_delivery_state_update()`. Drops the update when loop is not running (no direct-apply fallback).
- **Async callback exception handling**: `_log_task_exception()` done callback added to fire-and-forget tasks. `inject_inbound()` sync callback wrapped in try/except.

### Test Coverage Added (Tranche 6)

| Test Class                           | What it verifies                                                     |
| ------------------------------------ | -------------------------------------------------------------------- |
| `TestTranche6FailedStartCleanup`     | Failed start clears callback/loop, diagnostics clean                 |
| `TestTranche6AsyncCallbackException` | Async callback exception consumed, sync callback exception caught    |
| `TestTranche6NoCallbackWithoutLoop`  | No callback when loop=None or not running, warning logged            |
| `TestTranche6DeliveryStateBridging`  | State update via bridge works, unknown hash ignored, no thread error |

### Tranche 6: What Was Not Done

- No live Reticulum testing performed.
- No status changes in the capability matrix.

## 22. Cross-References

| Topic                                                               | Document                                                 |
| ------------------------------------------------------------------- | -------------------------------------------------------- |
| LXMF source audit (identity, wire format, fields, delivery methods) | `docs/contracts/13-lxmf-source-audit.md`                 |
| LXMF adapter tranche 1 scope, config, capabilities                  | `docs/contracts/14-lxmf-tranche-1.md`                    |
| LXMF/Reticulum connectivity readiness (full SDK API surface)        | `docs/contracts/20-lxmf-connectivity-readiness.md`       |
| Delivery semantics comparison across all transports                 | `docs/contracts/22-delivery-semantics-matrix.md`         |
| Live smoke test procedures and SDK verification                     | `docs/runbooks/lxmf-live-smoke.md`                       |
| Metadata embedding contract                                         | `docs/contracts/06-metadata-embedding-contract.md`       |
| Constrained transport comparison                                    | `docs/contracts/65-constrained-transport-comparison.md`  |
| Production connectivity readiness                                   | `docs/contracts/16-production-connectivity-readiness.md` |
| Operational readiness gaps                                          | `docs/contracts/18-operational-readiness-gaps.md`        |
