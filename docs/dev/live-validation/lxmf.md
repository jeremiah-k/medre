# LXMF Live Validation

Live smoke test procedures for the LXMF adapter against a real Reticulum network.

These are reproducible bench procedures, not a claim that a native service or
hardware tier passes on every new Git revision. Historical counts and device
observations below apply only to the dated runs; capture fresh evidence for
your current core, adapter commits, SDKs, and endpoints.

For a bounded multi-adapter campaign, use the
[short iteration sequence](short-iterations.md).

## Slow-Medium Peer Budgets

The physical-pair, cross-transport bridge, and radio-matrix harnesses share a
per-message native-peer delivery ceiling. It defaults to 90 seconds. Set a
larger finite value for a radio configuration whose path discovery, link
establishment, and delivery retries need more time:

```bash
export LXMF_PEER_DELIVERY_TIMEOUT_SECONDS=300
```

The harness derives subprocess deadlines, LXMF observation windows, and pytest
caps from that ceiling. Subprocess budgets also cover SDK startup, identity
recall, pacing, and process exit; raising only the subprocess timeout would
leave the child's delivery wait too short. A ceiling is a test budget, not an
SDK retry setting or a guarantee that any link can deliver within that time.

Peer JSON distinguishes a terminal provider state from a bounded observation
that ended with a pending message: `delivered` reports provider confirmation,
and `timed_out` reports that the message was nonterminal at the ceiling. A
timed-out observation does not manufacture an LXMF `FAILED` state. Rejected and
cancelled messages return promptly along with delivered and failed messages.

## Quick Validation

```bash
# Install lxmf in the MEDRE environment; see [installation](../../ops/install.md)
source .venv/bin/activate

# Configure a Reticulum transport (AutoInterface for LAN is default)
# Set the adapter connection type to reticulum
export MEDRE_ADAPTER__LXMF_SENDER__TRANSPORT=lxmf
export MEDRE_ADAPTER__LXMF_SENDER__CONNECTION_TYPE=reticulum
export MEDRE_ADAPTER__LXMF_SENDER__IDENTITY_PATH=/safe/path/sender.identity
export MEDRE_ADAPTER__LXMF_SENDER__DISPLAY_NAME=sender

pytest ../medre-adapter-lxmf/tests/test_lxmf_live.py -m live -v
```

## Prerequisites for Live Validation

1. **Reticulum transport active.** Either:
   - Default AutoInterface on LAN (discovers peers automatically).
   - TCPClientInterface to a remote Reticulum node.
   - Local `rnsd` daemon (but be aware of singleton constraint — MEDRE may conflict).

2. **Identity file.** Create one before first run:

   ```python
   import RNS
   identity = RNS.Identity()
   identity.to_file("/safe/path/sender.identity")
   print(f"Identity hash: {identity.hexhash}")
   ```

3. **At least one peer.** For delivery validation, a second Reticulum instance with a separate identity is needed.

## Two-Node Test Topology

**Simplest option:** two machines on the same LAN with default AutoInterface configs.

1. Machine A: MEDRE with LXMF adapter (sender).
2. Machine B: Any Reticulum+LXMF client (e.g., Sideband, Nomad Network, or a second MEDRE instance).

Both machines auto-discover each other via IPv6 link-local multicast. No manual configuration required.

**Same-machine option:** Two separate processes with custom config dirs:

```bash
# Process A
python -c "import RNS; r = RNS.Reticulum('/tmp/ret_a'); import time; time.sleep(9999)" &

# Process B
python -c "import RNS; r = RNS.Reticulum('/tmp/ret_b'); import time; time.sleep(9999)" &
```

Requires TCPClientInterface/TCPServerInterface in configs. More complex.

## SDK Dependency Check

```bash
python -c "import RNS; print(RNS.__version__)"
python -c "import LXMF; print(LXMF.__version__)"
```

Confirmed: lxmf and rns SDK versions present and importable.

## Wrapper Callback Bridge Evidence

The adapter-wrapper callback bridge is proven at the fake-pipeline level:

- `_on_packet` → `LxmfCodec.decode` → pipeline routing → fake outbound delivery.
- Full callback-to-delivery path with real adapter code.
- Docker SDK-boundary: no containerized Reticulum/LXMF router exists.

## Delivery Method Testing

### DIRECT (recommended)

```python
config = LxmfConfig(
    adapter_id="lxmf-alpha",
    default_delivery_method="direct",
)
```

- Link-based delivery with retries up to 5.
- Proof receipts confirm delivery.
- First message to a new peer may take seconds to minutes for path establishment.

### OPPORTUNISTIC

```python
config = LxmfConfig(
    adapter_id="lxmf-alpha",
    default_delivery_method="opportunistic",
)
```

- Fire-and-forget. No ACK. Max 1 attempt.
- Use for quick status messages where loss is acceptable.

## Path Discovery Timeline

- **Same LAN (AutoInterface):** 1–5 seconds.
- **TCP link, online peer:** Seconds.
- **Offline peer:** A configured propagation node can carry PROPAGATED delivery
  to a previously natively discovered destination; an unobserved destination
  is refused before handoff until native discovery succeeds.

## Singleton Constraint

`RNS.Reticulum()` is a singleton per process. Do not run `rnsd` on the same machine during live harness execution — the harness needs to own its Reticulum instance.

## RNode Bring-Up Notes (2026-09-19, campaign `buildout/hardware-readiness`)

- Two LilyGO T-LoRa V2.1-1.6 (SX1276) boards commissioned as RNode Firmware
  1.86 (`lora32v21`, model B9 850-950 MHz) via `rnodeconf` 2.5.0 (the pinned RNS package);
  both validate signature/EEPROM. Post-install the boards needed one physical
  power cycle before the console answered.
- The pinned RNS release requires an explicit `enabled = yes` on each interface section —
  interfaces without it are silently skipped ("Skipping disabled interface").

## Physical RNode Pair Validation (2026-09-19, `LXMF_PAIR=1`)

Opt-in physical pair suite: `tests/test_lxmf_pair_live.py` (N1 readiness,
N2 ingress with native-hash/identity correlation, N3 routed egress with
peer receipt, N4 unicode/newline/multi-frame boundaries, N5 identical-text
distinct-hash, N7 relation envelope over the real inbound codec, N6 RF-off
absence + restored positive; `MEDRE_LIVE_QUICK=1` trims to core positives).
Env keys: `LXMF_PAIR`, `LXMF_MEDRE_RNS_CONFIG`, `LXMF_PEER_RNS_CONFIG`,
`LXMF_MEDRE_IDENTITY`, `LXMF_PEER_IDENTITY`, and optionally
`LXMF_PEER_HUB`/`LXMF_PEER_HUB_PORT` for the power control. Private lab
values (config dirs, identity files, hub map) live in the restricted lab
tree; no secrets are embedded in the module.

Relation and native-identity assertions run independently of the optional
power testcase. Configure hub control only after verifying that its mapped
port actually removes peer radio power; a VBUS status alone does not establish
that a battery-backed device is off.

The pinned RNS release on Python 3.14 raises the deprecated `threading.setDaemon`
warning; the pinned-SDK live modules filter exactly that warning (the
project-wide `filterwarnings = ["error"]` would otherwise kill the runtime).

## Known Gaps

- No Docker setup for Reticulum/LXMF. No containerized router for Docker SDK-boundary tests.
- Propagation node config not in LxmfConfig yet.
- Physical RNode pair proven (see above); external/multi-hop peer reachability
  beyond the two-board bench pair remains unproven.
- Adapter health covers the local session/router only — it cannot observe
  whether any peer is reachable.
- No native reply mechanism — replies rendered as plain text.

## Physical Cross-Transport Bridge (2026-09-19)

Opt-in `tests/test_lxmf_bridge_live.py` (`MEDRE_LX_BRIDGE=1` + owned
endpoints per its `_REQUIRE` docstring) runs four directed routes through
one in-process MEDRE runtime owning MT-A (serial), MC-A (BLE) and LX-A
(RNode, isolated `reticulum_config_dir`), with independent native peers
MT-B/MC-B/LX-B: `mt_to_lx`, `lx_to_mt`, `mc_to_lx`, `lx_to_mc`, plus a
controlled fan-out (`mt_to_lx` + `mt_to_mc`) asserting per-target receipts
and per-target far-peer RF observation.
Operating note: the T-Beam BLE stack refuses central connects after a few
rapid cycles (~2-4 observed); power-cycle the owned hub port and re-sync
the board clock, then run the affected case immediately.

## Operator Configuration Seam (2026-09-19)

The serialized path is proven end-to-end, not just programmatic
construction: a private operator YAML (storage/identity/`reticulum_config_dir`
under a restricted lab tree) validates through `medre config check --config`,
then an actual managed `medre run --config` with `MEDRE_HOME` isolation
delivers a native MT-B RF message through the routed LXMF `dest_channel`;
the durable receipt `adapter_message_id` matches the independently observed
LX-B message hash. Fault/recovery gates on the same runtime: owned-port
power loss (hub VBUS, labelled as such) breaks LX egress while MT admission
stays usable (acceptance receipts, no false delivery claims; RNS
reconnects the interface on power restore); crash termination at a pending
boundary preserves events/receipts/outbox and restarts clean on the same
DB; read-only `recover`/`inspect` pages leave the DB bit-identical
(WAL/SHM sidecars excepted); `replay --mode dry_run` writes no receipts;
an executed `--mode best_effort` replay resolves the pending delivery with
independent RF confirmation. That replay starts the runtime in a dedicated
replay-delivery scope (`StartupScope.REPLAY`): the selected replay never
dispatches unrelated due outbox work or routes live ingress received
during the window — deferred work is processed by the next live start.

## Deterministic Local Integration

Run the real pinned RNS/LXMF stack in a process-isolated local probe before
external Reticulum testing:

```bash
# Install lxmf in the MEDRE environment; see [installation](../../ops/install.md)
pytest ../medre-adapter-lxmf/tests/test_lxmf_local_integration.py \
  -m "local_integration and lxmf_sdk and not soak" -v
```

The probe verifies repeated real-router lifecycle, persisted identity/delivery
destination, malformed and late callback containment, and cleanup after a
partial startup failure. It intentionally does not claim remote delivery or
multi-hop behavior. The `soak` selection repeats the real-router lifecycle.

## See Also

- [transport-setup/lxmf.md](../../ops/transport-setup/lxmf.md) — adapter setup, delivery modes, Reticulum topology
- [diagnostics-and-evidence.md](../../ops/diagnostics-and-evidence.md) — evidence provenance and bundle collection
