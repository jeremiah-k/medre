# LXMF Transport Setup

Install the provider in the same environment as MEDRE using the
[installation guide](../install.md). Activate a project environment before
invoking CLI tools; persistent uv tools already expose `medre` on PATH.
The provider supplies both its SDK-free fake and its native SDK dependencies.

Setting up and running the MEDRE LXMF adapter against a real Reticulum network.

Fake mode is the default and recommended path for all development and testing. Real Reticulum connectivity is opt-in for live validation.

**LXMF is not connection-oriented.** There is no "connect to a server" step. Reticulum auto-discovers peers on configured interfaces. Path requests and announces handle routing automatically.

## Before you begin

### Prerequisites

| Requirement         | Details                                                                                                               |
| ------------------- | --------------------------------------------------------------------------------------------------------------------- |
| Reticulum instance  | An available Reticulum transport setup; MEDRE can attach to a running shared `rnsd` or initialize an instance         |
| LXMF router storage | A writable directory for `LXMRouter` persistent state                                                                 |
| Reticulum identity  | A 64-byte private key file. Created on first run if none exists.                                                      |
| Python              | 3.11 or later                                                                                                         |
| Package install     | Install lxmf in the MEDRE environment; see [installation](../install.md) (exact LXMF/RNS pins from project metadata). |
| Network access      | At least one Reticulum transport interface configured                                                                 |

### Minimum Viable Reticulum Topology

#### What Is a Reticulum Network?

A Reticulum network is one or more Reticulum instances that can reach each other via at least one shared interface. There is no central server, no broker, and no enrollment authority. A single instance with no peers is a valid Reticulum instance — it just cannot send or receive messages.

#### Default Configuration

On first run, Reticulum creates a default config at `~/.reticulum/config` with `AutoInterface` (IPv6 link-local multicast over UDP). This discovers other Reticulum nodes on the same LAN segment automatically. No IP infrastructure required.

#### Discovered Network Interfaces

Reticulum's optional network-interface discovery and autoconnection are separate
from AutoInterface LAN neighbor discovery and LXMF delivery-destination announces.
With discovered-interface autoconnection enabled, the pinned SDK's default policy
requires advertised `transport=True`, a recognized `RNS` implementation, and an
advertised version at least `1.5.2`. Missing or false transport metadata prevents
automatic attachment, as does missing, unrecognized, or older version metadata.
Other eligibility checks still apply; passing these checks does not establish a
connection.

Upgrade the advertising node or configure a compatible static interface when a
discovered endpoint does not qualify. This filter does not affect an explicitly
configured RNodeInterface or static TCP interface. Reticulum's
`autoconnect_unverified_implementations` option overrides the implementation and
version filter, but does not bypass the advertised transport requirement; MEDRE
does not enable it.

When a non-transport Reticulum instance configures an interface with
`discoverable = Yes`, the SDK automatically enables a static transport identity.
This makes the advertising identity persist across process restarts without
enabling transport routing. Reticulum owns this identity; it is separate from
MEDRE's LXMF delivery identity. A discoverable non-transport instance still does
not qualify for automatic attachment by other nodes under the transport filter.

#### Two-Node Minimum for Delivery Validation

| Setup                       | How                                                           | Complexity |
| --------------------------- | ------------------------------------------------------------- | ---------- |
| Two processes, same machine | Custom config dirs with TCPClientInterface/TCPServerInterface | Medium     |
| Two machines, same LAN      | Both use default AutoInterface                                | Low        |
| Two machines, TCP           | One runs TCPServerInterface, other TCPClientInterface         | Low        |
| Radio link                  | Both have RNode or compatible radio hardware                  | High       |

**Simplest viable topology:** two machines on the same LAN with default AutoInterface configs.

#### Path Discovery Timeline

- **Same LAN (AutoInterface):** 1–5 seconds.
- **TCP link, online peer:** Seconds.
- **Multi-hop mesh:** Accumulates per-hop.
- **Offline peer:** PROPAGATED delivery via a configured propagation node can
  reach a previously natively discovered destination; discovery is still
  required before handoff.

### rnsd Usage

`rnsd` is the Reticulum Network Stack daemon. It can provide an existing,
shared transport service while MEDRE owns a separate LXMF identity and router.
It does not itself handle LXMF messages or provide LXMF delivery services.

| Scenario                         | Use `rnsd`?   | Reason                                                    |
| -------------------------------- | ------------- | --------------------------------------------------------- |
| MEDRE runs continuously          | Optional      | Attach to shared transport or initialize a local instance |
| Multiple local programs need RNS | Often yes     | Avoid competing transport owners                          |
| Isolated native tests            | Test-specific | Use a private test instance, not production state         |

For the isolated native harness, do not reuse a production `rnsd` profile:
its private test service uses its own `configdir` and instance name. Ordinary
MEDRE operation **can** share a running `rnsd` when configured for the same
Reticulum environment. A core host-file snapshot does not capture or stop that
external service; restore it independently if necessary.

## Set up

Install the adapter:

```bash
medre adapters install lxmf
```

Save a private `connections.yaml` with one real instance. The common case
needs the private storage path; Reticulum uses its default configuration
locations when `reticulum_config_dir` is omitted (see [Options](#options)):

```yaml
adapters:
  lxmf:
    relay:
      adapter_kind: real
      connection_type: reticulum
      storage_path: /private/medre/lxmf/relay
routes: {}
```

Add another adapter instance, then build routes with
[guided configuration](../guided-configuration.md).

### Identity Setup

Reticulum identities are dual-keypair (X25519 for encryption, Ed25519 for signing) derived from a single 64-byte private key.

#### Identity File

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

#### MEDRE Config (Connection)

```python
from medre.config.adapters.lxmf import LxmfConfig

config = LxmfConfig(
    adapter_id="lxmf-alpha",
    connection_type="reticulum",
    storage_path="/path/to/private/lxmf-alpha",  # Required for real mode
    # identity_path="/path/to/identity",  # Optional: defaults to private profile
    display_name="MEDRE Alpha Node",
)
```

`LxmfConfig.identity_path` is validated as a non-empty string when provided.
For real mode, the owned native worker opens the explicit file under an exclusive
identity lease; a missing or invalid explicit identity fails startup rather
than silently replacing the address. If no path is configured, the worker
atomically creates `{storage_path}/.medre-identity` on first use, leases it,
and reopens **the same identity on restart**. Keep the private worker storage
path stable, and back up its identity together with the handoff journal.
The direct `LxmfSession` is a native implementation detail: MEDRE operates it
inside a worker process, not in the core process.

#### First-Run Consideration

Do not delete, rotate, or overwrite the generated `.medre-identity` between
runs: doing so changes the destination address and may invalidate reply
references and handoff correlation. The delivery destination hash is 16 bytes,
displayed as 32 hexadecimal characters. Preserve the profile journal and
native router state as well; copying the identity alone is not a complete
stopped-state capture.

### Env-First Adapter Creation

```bash
export MEDRE_ADAPTER__LXMF_SENDER__TRANSPORT=lxmf
export MEDRE_ADAPTER__LXMF_SENDER__ADAPTER_KIND=real
export MEDRE_ADAPTER__LXMF_SENDER__CONNECTION_TYPE=reticulum
export MEDRE_ADAPTER__LXMF_SENDER__STORAGE_PATH=/private/medre/lxmf/sender
export MEDRE_ADAPTER__LXMF_SENDER__IDENTITY_PATH=/safe/path/sender.identity
export MEDRE_ADAPTER__LXMF_SENDER__DISPLAY_NAME=sender
```

`MEDRE_LXMF_*` runtime config variables are unsupported. Use
`MEDRE_ADAPTER__<TOKEN>__<FIELD>` instead.

## Verify

Check and run the bridge configuration saved by guided configuration:

```bash
medre config check --config bridge.yaml
medre routes plan --config bridge.yaml
medre run --config bridge.yaml
```

A healthy start settles at the adapter's healthy lifecycle state; the local
health flags describe the session/router lifecycle, not interface
connectivity or peer reachability — see
[Connection Recovery](#connection-recovery). A first message to a new peer
can take seconds to minutes while path discovery completes; a short
observation window does not establish delivery failure. Delivery
observation semantics are described in the
[LXMF internals page](../../dev/transport-internals/lxmf.md).

## Options

| Key                         | Default            | What it changes                                                       | When to touch it                                                   |
| --------------------------- | ------------------ | --------------------------------------------------------------------- | ------------------------------------------------------------------ |
| `storage_path`              | unset              | Private worker/router state directory (required in real mode)         | Always set it; keep it stable across restarts                      |
| `identity_path`             | private profile    | Explicit 64-byte private identity file (sensitive)                    | When the address must come from an existing identity               |
| `reticulum_config_dir`      | Reticulum defaults | Reticulum configuration directory the worker attaches through         | A shared `rnsd` uses a non-default location                        |
| `display_name`              | empty              | LXMF display name announced with the delivery destination             | Naming the bridge endpoint                                         |
| `default_delivery_method`   | `direct`           | Requested LXMF delivery method                                        | Opportunistic single-packet links or store-and-forward propagation |
| `outbound_propagation_node` | unset              | Outbound propagation-node destination hash (required when propagated) | Selecting propagated delivery                                      |
| `message_delay_seconds`     | `0.5`              | Minimum spacing between outbound messages                             | Pacing on slow links                                               |
| `origin_label`              | empty              | Prefix identifying this origin on the other side                      | Labeling in mixed bridges                                          |
| `relay_prefix`              | empty              | Custom rendering template for relayed text                            | Custom presentation                                                |

### Delivery Mode Semantics

LXMF supports four delivery methods. The semantics are fundamentally asynchronous and store-and-forward.

| Method        | Code   | Behavior                                                        | Reliability                                                       | Latency                    |
| ------------- | ------ | --------------------------------------------------------------- | ----------------------------------------------------------------- | -------------------------- |
| DIRECT        | `0x02` | Establishes `RNS.Link`, sends via link packet or `RNS.Resource` | Bounded SDK retries; asynchronous delivery receipts.              | Seconds to minutes         |
| OPPORTUNISTIC | `0x01` | Single RNS packet without a link when the packed payload fits.  | SDK retry scheduling and packet delivery receipts when available. | Depends on path and medium |
| PROPAGATED    | `0x03` | Delivered to propagation node. Node stores for recipient.       | Moderate. Delivery to node is reliable. Recipient syncs later.    | Minutes to hours           |
| PAPER         | `0x05` | Encoded as QR code or `lxm://` URI. No network.                 | None. Physical delivery only.                                     | N/A                        |

#### MEDRE Config — Delivery Method

```python
config = LxmfConfig(
    adapter_id="lxmf-alpha",
    connection_type="reticulum",
    storage_path="/path/to/private/lxmf-alpha",
    default_delivery_method="direct",  # direct | opportunistic | propagated | paper
    # Required when selecting propagated delivery in real mode:
    # outbound_propagation_node="0123456789abcdef0123456789abcdef",
)
```

For propagated delivery, configure the outbound LXMF propagation-node destination
hash explicitly. `LXMRouter` starts with no outbound propagation node selected;
MEDRE will refuse a propagated handoff until one is configured. Native
destination discovery is also required; a propagation node alone does not
make an unobserved destination addressable.

**DIRECT is the recommended default.** It provides the best reliability trade-off: link-based delivery with retries and proof receipts. However:

- Path discovery is asynchronous. No guarantee a path exists.
- Link establishment takes time. First delivery to a new peer may take seconds to minutes.
- No "instant delivered" guarantee. `deliver()` returning means the message was handed off, not received.

**OPPORTUNISTIC** requests single-packet delivery. The pinned SDK supports packet
delivery receipts and bounded retry scheduling; it is not a guarantee of arrival.
For MEDRE's encrypted single-destination messages, a packed payload above the
SDK's 287-byte content budget automatically falls back to DIRECT delivery. The
title and packed fields count toward that budget, including the MEDRE metadata
envelope. A short body can therefore require a link. The budget is measured in
packed bytes, not text characters, and does not replace MEDRE's renderer limits.

The configured delivery method expresses the requested SDK method. It does not
force an oversized encrypted message into one packet or suppress the SDK's
fallback. DIRECT fallback preserves the body, title, and metadata.

**PROPAGATED** is store-and-forward. The recipient must explicitly sync from the propagation node. Latency depends entirely on when the recipient checks in.

#### Retry and Observation Windows

LXMF derives path-request waits and delivery retry spacing from Reticulum's
medium and destination timing. Slow interfaces can extend those waits and alter
the attempt limit. A fixed short observation window can expire while the SDK is
still working; pending at the window's end does not establish delivery failure.
Use asynchronous delivery observations for outcomes and the
[LXMF live-validation guide](../../dev/live-validation/lxmf.md) for finite test budgets.

### Selecting the Reticulum configuration

Set `reticulum_config_dir` to the configuration directory used by `rnsd` when
that service uses a non-default location. This selects the directory containing
Reticulum's `config` file, not the LXMF `storage_path` or an individual file:

```yaml
adapters:
  lxmf:
    relay:
      adapter_kind: real
      connection_type: reticulum
      storage_path: /private/medre/lxmf/relay
      reticulum_config_dir: /private/reticulum
```

When omitted, Reticulum checks `/etc/reticulum/config`, then
`~/.config/reticulum/config`, and otherwise uses `~/.reticulum`. The worker uses
that configuration's shared-instance settings to attach to an available
service or start its own Reticulum instance. Matching the service's directory
is necessary to select the intended environment; it does not force a shared
connection or override `share_instance`/interface settings.

## Troubleshooting

### Connection Recovery

Reticulum owns reconnecting its physical interfaces, including supported RNode
serial/BLE and TCP connections. Recovery timing and availability depend on the
configured interface. An interface can go offline and recover while MEDRE keeps
the same LXMF router and delivery destination alive. Stopping the native
worker does not shut down a separately running Reticulum service.

`LxmfSession` contains a private bounded reconnect helper, but no production
disconnect signal invokes it. MEDRE therefore does not automatically rebuild the
session when a physical connection drops. Its local health flags describe the
session/router lifecycle; they do not track interface connectivity or peer
reachability. Observe RNS interface status and asynchronous delivery evidence
when checking recovery.

The three-attempt local retry in `send_text()` handles transient router-handoff
exceptions. It does not reconnect an interface or wait for remote delivery.
LXMF separately schedules delivery and path requests using medium-aware timing;
slow links can remain pending beyond a short bench observation window.

#### Interface Management on a Shared Instance

The pinned RNS SDK supports attaching, detaching, and reloading a configured
interface by name. For a shared instance, use its configuration directory and
the exact interface name:

```bash
rnstatus --config /path/to/shared-reticulum --detach "Radio uplink"
rnstatus --config /path/to/shared-reticulum --attach "Radio uplink"
rnstatus --config /path/to/shared-reticulum --reload "Radio uplink"
```

Detaching removes the interface and marks it detached; reconnect calls started
afterward do not enter the retry loop. RNode BLE detachment also closes its
connection and stops its BLE jobs. The pinned RNS reconnect loop checks the
detached flag before a retry wait, but does not check it again before opening the
port. A reconnect already in that wait can therefore make one further open
attempt after detachment. This is an SDK lifecycle limitation, so detachment is
not a guarantee that every in-flight physical-interface operation has stopped.

Attaching creates the interface from configuration; reloading replaces it using
the current configuration while retaining the Reticulum instance. Interface
management must be enabled on that instance.

Local `rnstatus` management commands require a shared Reticulum instance. They
cannot control the MEDRE live harness's isolated `share_instance = No` process.
MEDRE exposes no interface-management CLI of its own; restart that isolated
runtime to apply interface configuration changes. `start()` and `stop()` remain
idempotent session operations.

### Known Limitations

1. **No synchronous delivery confirmation.** Even DIRECT's proof receipt is asynchronous.
2. **Singleton constraint.** Only one `RNS.Reticulum()` per process.
3. **Propagation requires explicit node selection.** Configure
   `outbound_propagation_node`; MEDRE rejects propagated handoff when no node
   is selected.
4. **No native reply mechanism.** Replies are rendered as plain text with optional quoted prefix.
5. **Identity file is unencrypted.** Protect it as a secret.
6. **Outbound state at return time is typically `OUTBOUND`, not `DELIVERED`.**
7. **Path discovery time is outside MEDRE's control.** First message to a new peer may take seconds to minutes.

## Reference

- What MEDRE sees from Reticulum objects, delivery threading, and router
  shutdown ownership:
  [LXMF transport internals](../../dev/transport-internals/lxmf.md)
- [guided configuration](../guided-configuration.md) — the route builder used
  to connect two adapters

### See Also

- [live-validation/lxmf.md](../../dev/live-validation/lxmf.md) — live smoke test procedures
- [diagnostics-and-evidence.md](../diagnostics-and-evidence.md) — evidence provenance and bundle collection
- [recovery-and-replay.md](../recovery-and-replay.md) — crash recovery and replay
