# LXMF Alpha Operation Runbook

> Last updated: 2026-05-10
> Scope: Real LXMF/Reticulum Operation Alpha (Track 8)
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

This runbook complements `docs/runbooks/lxmf-live-smoke.md`. The smoke test documents SDK connectivity procedures and manual verification scripts. Alpha operation would validate the full wiring: config, adapter, codec, inbound callback dispatch, outbound delivery, and health, running together.


## 2. Prerequisites

| Requirement | Details |
|------------|---------|
| Reticulum instance | A running Reticulum transport layer. Can be a local `rnsd` daemon, a custom config with `AutoInterface` (LAN), or a TCP connection to a remote Reticulum node. |
| LXMF router storage | A writable directory for `LXMRouter` persistent state. |
| Reticulum identity | A 64-byte private key file created by `RNS.Identity.to_file()`. Created on first run if none exists. |
| Python | 3.11 or later |
| Package install | Core MEDRE: `pip install -e .` (no extra required for fake mode). Real connectivity: `pip install lxmf` (installs `rns` as a dependency). Alternative: `pip install rnspure` for pure-Python crypto (slower). |
| Network access | At least one Reticulum transport interface configured (AutoInterface for LAN, TCPClientInterface for remote, etc.) |

You do not need Docker for basic alpha operation. Docker guidance is in section 12.

**Critical: LXMF is not connection-oriented.** There is no "connect to a server" step. Reticulum auto-discovers peers on configured interfaces. Path requests and announces handle routing automatically. See section 5 for delivery semantics.


## 3. Fake Mode (Default)

Fake mode is the default development and testing path.

```python
from medre.adapters.lxmf.config import LxmfConfig
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


## 4. Identity Setup

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


## 5. Delivery Mode Semantics

LXMF supports four delivery methods. The semantics are fundamentally asynchronous and store-and-forward. **Do not assume "instant delivered" or "realtime" guarantees.**

### 5.1 Method Comparison

| Method | Code | Wire Behavior | Reliability | Latency | Size Limit |
|--------|------|--------------|-------------|---------|------------|
| DIRECT | `0x02` | Establishes `RNS.Link`, sends via link packet or `RNS.Resource` | High. Retries up to `MAX_DELIVERY_ATTEMPTS` (5). Proof receipts confirm delivery. | Seconds to minutes (depends on path availability) | Link packet: 319B. Resource: arbitrary (multi-KB typical). |
| OPPORTUNISTIC | `0x01` | Single RNS packet, no link. Embedded in route data. | Best-effort. No ACK, no retry. Max 1 attempt (`MAX_PATHLESS_TRIES=1`). | Seconds if peer is online; fails otherwise. | 295 bytes encrypted content. |
| PROPAGATED | `0x03` | Delivered to propagation node via link. Node stores for recipient. | Moderate. Delivery to node is reliable. Recipient must sync from node. | Minutes to hours (recipient must check in). | Limited by propagation node config (`PROPAGATION_LIMIT=256KB`). |
| PAPER | `0x05` | Encoded as QR code or `lxm://` URI. No network transport. | None. Physical delivery only. | N/A | Roughly 2KB (`PAPER_MDU`). |

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


## 6. Async Delivery Caveats

### 6.1 Threading Model

Reticulum and LXMF use background daemon threads, not asyncio:

- `LXMRouter` starts a `jobloop` daemon thread for processing outbound messages.
- Reticulum transport processing runs in threads.
- MEDRE's asyncio event loop runs in the main thread.

The `LxmfSession` bridges this boundary. When the SDK delivery callback fires in a Reticulum thread, the session normalises the `LXMessage` into a plain dict (no SDK objects leak), then invokes the adapter's `_on_packet` callback. If the callback is async, the session schedules it on the running event loop via `loop.create_task()`. This is the same pattern used by the Meshtastic adapter.

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

Outbound retry is bounded: `_SEND_MAX_RETRIES = 3` with a short linear backoff (`0.1 * attempt` seconds between retries). After exhausting retries, the send raises `LxmfSendError`.

### 6.4 Reticulum Singleton

`RNS.Reticulum()` is a singleton per process. Calling it twice raises `OSError("Attempt to reinitialise Reticulum...")`. This means:

- Only one LXMRouter instance per process (in practice).
- Multiple adapters wanting separate identities would need separate processes.
- Test isolation requires careful setup/teardown or custom config directories.


## 7. Propagation Node Expectations

### 7.1 What Propagation Nodes Do

A propagation node is an LXMF router that stores messages on behalf of offline recipients. When a message is sent via PROPAGATED method:

1. The sender's router delivers the message to the propagation node.
2. The node stores it indexed by recipient identity hash.
3. When the recipient's router syncs with the node, it receives stored messages.

### 7.2 Configuration

```python
# In the real adapter (not yet implemented):
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


## 8. Reconnect/Restart Expectations

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

1. Computes delay = min(base * 2^attempt, 30s) ± jitter.
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


## 9. Diagnostics

### 9.1 Implementation

The `LxmfSession` exposes a `diagnostics()` method returning `LxmfSessionDiagnostics`, a frozen dataclass. The adapter exposes the session via its `session` property. The `LxmfAdapter` itself does not wrap this in a top-level `diagnostics()` method; consumers access it via `adapter.session.diagnostics()`.

### 9.2 Diagnostics Keys

| Key | Type | Description |
|-----|------|-------------|
| `connected` | `bool` | Whether the LXMRouter is initialized and the session is active. |
| `router_running` | `bool` | Whether the LXMF router's `jobloop` thread is operational. |
| `reconnecting` | `bool` | Whether the session is currently in the reconnect loop. |
| `reconnect_attempts` | `int` | Number of consecutive reconnect attempts since last successful connection. |
| `last_message_time` | `str \| None` | ISO 8601 timestamp of last successful inbound message. |
| `transient_delivery_failures` | `int` | Count of temporary delivery failures (retriable). |
| `permanent_delivery_failures` | `int` | Count of permanent delivery failures (not retriable). |
| `last_error` | `str \| None` | Description of the most recent session-level error. |
| `known_path_count` | `int \| None` | Number of known paths in the router's path table (`None` if router not available). |
| `propagation_enabled` | `bool \| None` | Whether a propagation node is configured (`None` if router not available). |
| `pending_delivery_count` | `int \| None` | Number of outbound deliveries currently being tracked (`None` if none). |
| `mode` | `str` | Current connection mode (`"fake"` or `"reticulum"`). |

The session also provides `delivery_state_counts()` returning a `dict[str, int]` of outbound delivery counts per state (`"outbound"`, `"sending"`, `"sent"`, `"delivered"`, `"failed"`, etc.).

### 9.3 What Diagnostics Does NOT Expose

- Private keys, identity file contents, or raw key material.
- Raw `RNS.Destination` or `RNS.Identity` objects.
- Raw LXMF message payloads.
- Reticulum transport interface internals.
- Peer identity dumps or cryptographic material.

### 9.4 Interpreting Diagnostics

| Symptom | Likely Cause |
|---------|-------------|
| `connected=False` | Reticulum not initialized or router creation failed. |
| `reconnecting=True` with high `reconnect_attempts` | Persistent network or interface failure. |
| `known_path_count=0` or `None` | No peers discovered. Check interface config. |
| `pending_delivery_count` growing | Outbound messages queued but not reaching terminal state. |
| `transient_delivery_failures` climbing | Network instability, path timeouts. |
| `permanent_delivery_failures` climbing | Misconfigured destination, invalid identity, or permanent path failure. |
| `last_message_time` stale | No traffic for an extended period. Not necessarily an error. |
| `router_running=False` with `connected=True` | Router teardown incomplete or crashed `jobloop` thread. |

### 9.5 Delivery State Model

The session tracks outbound delivery states via the `LxmfDeliveryState` enum:

```
GENERATING → OUTBOUND → SENDING → SENT → DELIVERED
                                       ↘ FAILED
                                       ↘ REJECTED
                                       ↘ CANCELLED
```

`delivery_state_counts()` returns counts of tracked outbound deliveries per state. These are snapshots at call time. Terminal deliveries (`DELIVERED`, `FAILED`, `REJECTED`, `CANCELLED`) are cleaned up after processing, so the count reflects active/pending deliveries predominantly.


## 10. Troubleshooting

### 10.1 Common Failures

| Symptom | Cause | Fix |
|---------|-------|-----|
| `ImportError: No module named 'RNS'` | `rns` not installed | `pip install rns` or `pip install lxmf` |
| `ImportError: No module named 'LXMF'` | `lxmf` not installed | `pip install lxmf` |
| `OSError: Attempt to reinitialise Reticulum` | `RNS.Reticulum()` called twice | Use `RNS.Reticulum.get_instance()` for subsequent access, or ensure only one init call per process |
| `ValueError: LXMF cannot be initialised without a storage path` | `storagepath=None` | Pass a valid directory path to `LXMRouter(storagepath=...)` |
| `register_delivery_identity()` returns `None` | Called more than once per router | Only one delivery identity per router instance. |
| No peers discovered | No transport interfaces configured | Check Reticulum config. Enable `AutoInterface` for LAN. Start `rnsd` on another machine. |
| Path request timeouts | No route to destination | Ensure both peers have active interfaces. Use `rnstatus` to check. |
| `Reticulum creates ~/.reticulum` unexpectedly | No custom configdir | Pass `configdir` to `RNS.Reticulum(configdir=...)` for test isolation. |
| Messages stuck in OUTBOUND state | No path to destination, or link establishment in progress | Wait for path discovery (seconds to minutes). Check `RNS.Transport.has_path()`. |
| `LxmfConnectionError: Failed to load identity from ...` | Identity file missing or corrupted | Re-create the identity file, or check the path in `identity_path` config. |
| `LxmfConnectionError: Failed to initialise LXMF session: ...` | Reticulum init or router creation failed | Check Reticulum config, available interfaces, and that no other process holds the singleton. |
| Live tests skip with "Set LXMF_CONNECTION_TYPE" | Environment variables not set | Set `LXMF_CONNECTION_TYPE=reticulum` and `LXMF_IDENTITY_PATH=/path/to/identity`. |

### 10.2 Reticulum-Specific Issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| `ModuleNotFoundError: No module named 'RNS.Interfaces...'` | Missing `pyserial` dependency | `pip install pyserial` or `pip install rns` (includes it). |
| Slow identity creation | High `stamp_cost` on first run | Stamp cost affects inbound validation, not identity creation. If identity creation is slow, check system entropy. |
| Announce not reaching peers | Interface misconfiguration or firewall | Verify interface in Reticulum config. Check `rnstatus` output. AutoInterface requires multicast-enabled LAN. |

### 10.3 Diagnostic Commands

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


## 11. Live Harness Instructions

### 11.1 Test Markers

All live tests in `tests/test_lxmf_live.py` are tagged with `pytest.mark.live`. They are excluded by default via `pyproject.toml` addopts (`-m 'not live'`).

### 11.2 Environment Gating

Live tests require explicit opt-in via environment variables:

| Variable | Required | Example | Description |
|----------|----------|---------|-------------|
| `LXMF_CONNECTION_TYPE` | Yes | `reticulum` | Must be `"reticulum"`. Any other value skips. |
| `LXMF_IDENTITY_PATH` | Yes | `/path/to/identity` | Path to Reticulum identity file. Must be non-empty. |
| `LXMF_DISPLAY_NAME` | No | `MEDRE Live Test` | Display name for LXMF announces. |

If any required variable is unset, every test in the file skips with a descriptive reason.

### 11.3 Running Live Tests

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

### 11.4 Current Test Status

Live tests in `tests/test_lxmf_live.py` are structured as dual-mode:

- **Real-mode tests** (`connection_type="reticulum"`): catch `LxmfConnectionError` and `pytest.skip()` when no Reticulum is available. When a live Reticulum instance is present, these tests exercise the full lifecycle: start, health check, stop, restart, and idempotency.
- **Fake-mode tests** (`connection_type="fake"`): run without any SDK dependency. Exercise the complete lifecycle in fake mode including rapid start/stop cycles, idempotency, and the inbound pipeline via `simulate_inbound`.
- **Documentation tests**: always-pass tests that record current constraints (no E2EE, no inbound from second identity in this harness).

### 11.5 Identity Path Setup for Tests

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

### 11.6 Startup/Shutdown Test Pattern

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

### 11.7 Send Test Pattern

```python
# 1. Start adapter with real Reticulum
# 2. Create RenderingResult with LXMF content dict
# 3. Call deliver() — returns AdapterDeliveryResult with delivery_state "outbound"
# 4. delivery_state is honest pending, not "delivered"
# 5. Track delivery via session.delivery_state_counts() over time
```

### 11.8 Callback Test Pattern

```python
# 1. Start adapter with real LXMRouter
# 2. From a second Reticulum instance, send a message to the adapter's identity hash
# 3. The session's _on_lxmf_delivery callback normalises the LXMessage into a plain dict
# 4. The adapter's _on_packet classifies, decodes, and publishes the canonical event
# 5. Verify publish_inbound was called with the decoded event
```

### 11.9 Restart/Repeated Start-Stop

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


## 12. Docker Guidance

Docker is not required for alpha operation. If desired for isolation:

### 12.1 Key Considerations

- **Reticulum singleton**: one `RNS.Reticulum` per container. If you need multiple routers, use multiple containers or processes.
- **Network access**: Reticulum's `AutoInterface` uses multicast for LAN discovery. Docker's default bridge network does not forward multicast. You need `--network host` or a custom network with multicast enabled.
- **TCP interfaces**: `TCPClientInterface` works fine in Docker. Configure the target host/port in the Reticulum config.
- **Storage paths**: mount LXMF storage and Reticulum config as volumes for persistence.
- **Identity files**: mount the identity file as a read-only volume or secret.

### 12.2 Minimal Docker Compose (Sketch)

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
    network_mode: host  # required for AutoInterface
volumes:
  lxmf-storage:
secrets:
  identity:
    file: ./identity
```

This is not production guidance. It is a starting point for alpha isolation testing.


## 13. Reticulum Config Guidance

### 13.1 Config Directory Search Order

1. `/etc/reticulum/config` (system-wide)
2. `~/.config/reticulum/config` (XDG)
3. `~/.reticulum/config` (fallback)

If none exist, Reticulum creates a minimal default at `~/.reticulum/config` on first run.

### 13.2 Minimal Alpha Config

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

### 13.3 Shared Instance

If `rnsd` is already running, other programs connect to it via local IPC (port 37428 by default). No interface configuration needed in the application config.

```bash
rnsd &
```

### 13.4 Storage Paths

| System | Path | Contents |
|--------|------|----------|
| Reticulum config | `~/.reticulum/` (default) | `config`, `storage/`, `interfaces/` |
| Reticulum storage | `{configdir}/storage/` | `known_destinations`, `cache/`, `resources/`, `identities/` |
| LXMF router storage | `{storagepath}/lxmf/` | `local_deliveries`, `messagestore/`, `ratchets/` |
| MEDRE SQLite | Configured by MEDRE | Events, receipts, native refs |

### 13.5 Safety

- **Do not use production identity files for testing.** Create separate test identities.
- **Use custom configdir for testing.** `RNS.Reticulum(configdir="/tmp/test_reticulum")` avoids modifying system-wide config.
- **Clean up test directories.** LXMF storage and Reticulum config dirs accumulate state.


## 14. Explicit Non-Claims

- **No production LXMF/Reticulum deployment readiness is claimed.** Alpha mode requires installed SDK and live environment.
- **No realtime delivery guarantees.** LXMF is asynchronous store-and-forward. Latency ranges from seconds to hours.
- **No multi-hop delivery has been tested end-to-end** beyond what the local Reticulum instance provides.
- **No E2EE is implemented by MEDRE.** Reticulum handles link-level encryption natively. MEDRE sends plaintext to the LXMF layer.
- **No propagation node testing has been performed.** The session detects propagation state for diagnostics but does not configure propagation nodes.
- **No compatibility with Sideband, MeshChat, Nomad Network, or other LXMF clients is claimed.**
- **No LXST (LXMF Streaming Transport) support.**
- **No attachment, image, audio, or media transfer support.**


## 15. Cross-References

| Topic | Document |
|-------|----------|
| LXMF source audit (identity, wire format, fields, delivery methods) | `docs/contracts/13-lxmf-source-audit.md` |
| LXMF adapter tranche 1 scope, config, capabilities | `docs/contracts/14-lxmf-tranche-1.md` |
| LXMF/Reticulum connectivity readiness (full SDK API surface) | `docs/contracts/20-lxmf-connectivity-readiness.md` |
| Delivery semantics comparison across all transports | `docs/contracts/22-delivery-semantics-matrix.md` |
| Live smoke test procedures and SDK verification | `docs/runbooks/lxmf-live-smoke.md` |
| Metadata embedding contract | `docs/contracts/06-metadata-embedding-contract.md` |
| Constrained transport comparison | `docs/contracts/12-constrained-transport-comparison.md` |
| Production connectivity readiness | `docs/contracts/16-production-connectivity-readiness.md` |
| Operational readiness gaps | `docs/contracts/18-operational-readiness-gaps.md` |
