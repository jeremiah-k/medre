# LXMF Transport Setup

Setting up and running the MEDRE LXMF adapter against a real Reticulum network. Alpha status — not production.

## Prerequisites

| Requirement         | Details                                                                                                                                                                         |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Reticulum instance  | A running Reticulum transport layer (local `rnsd`, `AutoInterface` on LAN, or TCP to remote node)                                                                               |
| LXMF router storage | A writable directory for `LXMRouter` persistent state                                                                                                                           |
| Reticulum identity  | A 64-byte private key file. Created on first run if none exists.                                                                                                                |
| Python              | 3.12 or later                                                                                                                                                                   |
| Package install     | Core: `pip install -e .` (fake mode). Real connectivity: `pip install lxmf` (installs `rns` as dependency). Alternative: `pip install rnspure` for pure-Python crypto (slower). |
| Network access      | At least one Reticulum transport interface configured                                                                                                                           |

Fake mode is the default and recommended path for all development and testing. Real Reticulum connectivity is opt-in for live validation.

**LXMF is not connection-oriented.** There is no "connect to a server" step. Reticulum auto-discovers peers on configured interfaces. Path requests and announces handle routing automatically.

## Identity Setup

Reticulum identities are dual-keypair (X25519 for encryption, Ed25519 for signing) derived from a single 64-byte private key.

### Identity File

The identity file is a raw 64-byte private key — not encrypted, not wrapped, no metadata. Anyone with this file can impersonate the node and decrypt all messages. Treat it as a secret.

```python
import RNS

# First run: create and save
identity = RNS.Identity()
identity.to_file("/path/to/identity")  # writes 64 bytes
print(f"Identity hash: {identity.hexhash}")  # 32 hex chars

# Subsequent runs: load
identity = RNS.Identity.from_file("/path/to/identity")
if identity is None:
    raise RuntimeError("Failed to load identity")
```

### MEDRE Config

```python
from medre.config.adapters.lxmf import LxmfConfig

config = LxmfConfig(
    adapter_id="lxmf-alpha",
    connection_type="reticulum",
    identity_path="/path/to/identity",
    display_name="MEDRE Alpha Node",
)
```

`LxmfConfig.identity_path` is validated as a non-empty string when provided. When `connection_type="reticulum"`, the `LxmfSession` loads the identity from this path at startup. If the file cannot be loaded, `start()` raises `LxmfConnectionError`. If `identity_path` is `None`, the session auto-generates a fresh identity (not persisted across restarts).

### First-Run Consideration

Do not create a new identity each run or you lose your address. The identity hash (16 bytes, displayed as 32 hex chars) is your permanent LXMF address.

### What MEDRE Sees

MEDRE never handles `RNS.Identity` or `RNS.Destination` objects directly. The adapter converts them to strings:

- `source_transport_id`: the sender's identity hash as a 32-char hex string.
- `native_message_id`: the LXMF message hash as a 64-char hex string.
- Core and pipeline code never import from `lxmf` or `RNS`.

## Delivery Mode Semantics

LXMF supports four delivery methods. The semantics are fundamentally asynchronous and store-and-forward.

| Method        | Code   | Behavior                                                        | Reliability                                                    | Latency                |
| ------------- | ------ | --------------------------------------------------------------- | -------------------------------------------------------------- | ---------------------- |
| DIRECT        | `0x02` | Establishes `RNS.Link`, sends via link packet or `RNS.Resource` | High. Retries up to 5. Proof receipts confirm delivery.        | Seconds to minutes     |
| OPPORTUNISTIC | `0x01` | Single RNS packet, no link. Embedded in route data.             | Best-effort. No ACK, no retry. Max 1 attempt.                  | Seconds if peer online |
| PROPAGATED    | `0x03` | Delivered to propagation node. Node stores for recipient.       | Moderate. Delivery to node is reliable. Recipient syncs later. | Minutes to hours       |
| PAPER         | `0x05` | Encoded as QR code or `lxm://` URI. No network.                 | None. Physical delivery only.                                  | N/A                    |

### MEDRE Config

```python
config = LxmfConfig(
    adapter_id="lxmf-alpha",
    default_delivery_method="direct",  # "direct" | "opportunistic" | "propagated" | "paper"
)
```

**DIRECT is the expected default for alpha.** It provides the best reliability trade-off: link-based delivery with retries and proof receipts. However:

- Path discovery is asynchronous. No guarantee a path exists.
- Link establishment takes time. First delivery to a new peer may take seconds to minutes.
- No "instant delivered" guarantee. `deliver()` returning means the message was handed off, not received.

**OPPORTUNISTIC** is fire-and-forget. Use only for quick status messages where loss is acceptable.

**PROPAGATED** is store-and-forward. The recipient must explicitly sync from the propagation node. Latency depends entirely on when the recipient checks in.

## Async Delivery Caveats

### Threading Model

Reticulum and LXMF use background daemon threads, not asyncio. The `LxmfSession` bridges this boundary: SDK callbacks fire in Reticulum threads, the session normalizes to plain dicts, then schedules onto the captured asyncio loop.

### Outbound Async Behavior

`LxmfSession.send_text()` creates an `LXMessage`, registers delivery state callbacks, and calls `router.handle_outbound(lxm)`. It returns `(native_message_id, initial_state)` immediately — typically `OUTBOUND` or `SENDING`, not `DELIVERED`. Actual delivery happens asynchronously.

Outbound retry is bounded: 3 retries with short linear backoff. After exhaustion, the send raises `LxmfSendError`, which the adapter normalizes to `AdapterSendError`.

### Reticulum Singleton

`RNS.Reticulum()` is a singleton per process. Calling it twice raises `OSError`. This means:

- Only one LXMRouter instance per process.
- Multiple adapters wanting separate identities need separate processes.
- Test isolation requires custom config directories.

## Minimum Viable Reticulum Topology

### What Is a Reticulum Network?

A Reticulum network is one or more Reticulum instances that can reach each other via at least one shared interface. There is no central server, no broker, and no enrollment authority. A single instance with no peers is a valid Reticulum instance — it just cannot send or receive messages.

### Default Configuration

On first run, Reticulum creates a default config at `~/.reticulum/config` with `AutoInterface` (IPv6 link-local multicast over UDP). This discovers other Reticulum nodes on the same LAN segment automatically. No IP infrastructure required.

### Two-Node Minimum for Delivery Validation

| Setup                       | How                                                           | Complexity |
| --------------------------- | ------------------------------------------------------------- | ---------- |
| Two processes, same machine | Custom config dirs with TCPClientInterface/TCPServerInterface | Medium     |
| Two machines, same LAN      | Both use default AutoInterface                                | Low        |
| Two machines, TCP           | One runs TCPServerInterface, other TCPClientInterface         | Low        |
| Radio link                  | Both have RNode or compatible radio hardware                  | High       |

**Simplest viable topology:** two machines on the same LAN with default AutoInterface configs.

### Path Discovery Timeline

- **Same LAN (AutoInterface):** 1–5 seconds.
- **TCP link, online peer:** Seconds.
- **Multi-hop mesh:** Accumulates per-hop.
- **Offline peer:** No path possible. Use PROPAGATED delivery via propagation node.

## rnsd Usage

`rnsd` is the Reticulum Network Stack daemon — it holds a `RNS.Reticulum()` instance alive for transport and announce propagation. It does not handle LXMF messages or provide LXMF services.

| Scenario                         | Use rnsd?  | Why                                                           |
| -------------------------------- | ---------- | ------------------------------------------------------------- |
| MEDRE runs continuously          | Optional   | The adapter's `LxmfSession` creates its own `RNS.Reticulum()` |
| Multiple local programs need RNS | Yes        | rnsd acts as shared instance master                           |
| Development/testing              | Usually no | MEDRE creates its own instance; rnsd conflicts (singleton)    |

Do not run rnsd during MEDRE live harness execution — the harness needs to own its Reticulum instance with a custom `configdir` for test isolation.

## Reconnect Behavior

The `LxmfSession` implements bounded exponential backoff reconnection:

- Base delays: 1 s, 2 s, 4 s, 8 s, ... capped at 30 s.
- ±25% jitter to avoid thundering-herd synchronization.
- Maximum 10 consecutive attempts.
- `start()` and `stop()` are idempotent.

## Env-First Adapter Creation

```bash
export MEDRE_ADAPTER__LXMF_SENDER__TRANSPORT=lxmf
export MEDRE_ADAPTER__LXMF_SENDER__CONNECTION_TYPE=reticulum
export MEDRE_ADAPTER__LXMF_SENDER__IDENTITY_PATH=/safe/path/sender.identity
export MEDRE_ADAPTER__LXMF_SENDER__DISPLAY_NAME=sender
```

Legacy `MEDRE_LXMF_*` runtime config vars are unsupported. Migrate to `MEDRE_ADAPTER__<TOKEN>__<FIELD>`.

## Known Limitations

1. **No synchronous delivery confirmation.** Even DIRECT's proof receipt is asynchronous.
2. **Singleton constraint.** Only one `RNS.Reticulum()` per process.
3. **No propagation node config in LxmfConfig yet.** Propagation requires manual router setup.
4. **No native reply mechanism.** Replies are rendered as plain text with optional quoted prefix.
5. **Identity file is unencrypted.** Protect it as a secret.
6. **Outbound state at return time is typically `OUTBOUND`, not `DELIVERED`.**
7. **Path discovery time is outside MEDRE's control.** First message to a new peer may take seconds to minutes.

## See Also

- [live-validation/lxmf.md](../live-validation/lxmf.md) — live smoke test procedures
- [diagnostics-and-evidence.md](../diagnostics-and-evidence.md) — evidence provenance and bundle collection
- [recovery-and-replay.md](../recovery-and-replay.md) — crash recovery and replay
