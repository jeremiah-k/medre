# LXMF Live Validation

Live smoke test procedures for the LXMF adapter against a real Reticulum network.

## Quick Validation

```bash
pip install -e ".[lxmf]"

# Configure a Reticulum transport (AutoInterface for LAN is default)
# Set the adapter connection type to reticulum
export MEDRE_ADAPTER__LXMF_SENDER__TRANSPORT=lxmf
export MEDRE_ADAPTER__LXMF_SENDER__CONNECTION_TYPE=reticulum
export MEDRE_ADAPTER__LXMF_SENDER__IDENTITY_PATH=/safe/path/sender.identity
export MEDRE_ADAPTER__LXMF_SENDER__DISPLAY_NAME=sender

pytest tests/test_lxmf_live.py -m live -v
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
- **Offline peer:** No path. Use PROPAGATED via propagation node (not yet supported in LxmfConfig).

## Singleton Constraint

`RNS.Reticulum()` is a singleton per process. Do not run `rnsd` on the same machine during live harness execution — the harness needs to own its Reticulum instance.

## Evidence Tiers Achieved

| Tier      | Sub-class           | Date    | Result                                                                                                                                                                                    |
| --------- | ------------------- | ------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| synthetic | Fake callback       | —       | Proven: simulate_inbound → codec → pipeline → fake outbound                                                                                                                               |
| synthetic | Wrapper callback    | —       | Proven: \_on_packet → LxmfCodec.decode → pipeline routing → fake outbound                                                                                                                 |
| local-int | Pinned loopback     | 2026-09 | Proven: two distinct processes over loopback at the declared pinned SDK versions — real-router lifecycle, cross-process relation linking, local session/router health (all verdicts true) |
| —         | Docker SDK-boundary | —       | Not proven (no containerized Reticulum/LXMF router)                                                                                                                                       |
| —         | Live network        | —       | Not proven (no external peer reachability claimed)                                                                                                                                        |

## RNode Bring-Up Notes (2026-09-19, campaign `buildout/hardware-readiness`)

- Two LilyGO T-LoRa V2.1-1.6 (SX1276) boards commissioned as RNode Firmware
  1.86 (`lora32v21`, model B9 850-950 MHz) via `rnodeconf` 2.5.0 (`rns==1.5.4`);
  both validate signature/EEPROM. Post-install the boards needed one physical
  power cycle before the console answered.
- RNS 1.5.4 requires an explicit `enabled = yes` on each interface section —
  interfaces without it are silently skipped ("Skipping disabled interface").

## Physical RNode Pair Validation (2026-09-19, `LXMF_PAIR=1`)

**NO_PATH resolution.** The earlier "NO_PATH in 3 runs" observation was a lab
probe defect, not an RF/RNS/MEDRE fault: `RNS.Reticulum.get_path_table()`
returns a **list** of entry dicts in RNS 1.5.4, and the probe checked
`isinstance(table, dict)` (always false → NO_PATH regardless of RF), plus a
fragile first-line log parse that crashed the listener on the RNS notice
line. With the corrected predicate and a paced, sequenced loop:

- Radio layer: firmware-reported 916.0 MHz / BW 125 kHz / TX 8 dBm / SF8 /
  CR5 on both boards; native frame reception at RSSI −55…−56 dBm,
  SNR 12.25 dB.
- RNS layer: validated announces recorded in the path table on
  `RNodeInterface[Lab RNode]` (1 hop), announced identity hash matching the
  announcer's printed identity, nonce-tagged announce app_data observed.
- LXMF layer: DIRECT delivery confirmed from both sides — the sender's
  LXMessage reaches `DELIVERED` and the receiver's delivery callback fires
  with the exact message hash the sender reported (cross-verified).
- Negative control: hub per-port VBUS cut to the peer radio (physical power
  loss, status `0000`) → bounded announce absence with the listener armed,
  and MEDRE never claims delivery while the RF path is physically dead;
  restored power → fresh listener receives a fresh nonce.

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

RNS 1.5.4 on Python 3.14 raises the deprecated `threading.setDaemon`
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
and per-target far-peer RF observation. All five cases green in full mode
(individual runs logged under private lab evidence `lx_bridge_run*.log`).
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
independent RF confirmation. The replay path first needed the startup/drain
fix (changelog 193: side-effect replay must actually start adapters), then
a scoping correction: `best_effort` now starts the runtime in a dedicated
replay-delivery scope (`StartupScope.REPLAY`) so the selected replay never
dispatches unrelated due outbox work or routes live ingress received
during the window — deferred work is processed by the next live start.

## Deterministic Local Integration

Run the real pinned RNS/LXMF stack in a process-isolated local probe before
external Reticulum testing:

```bash
pip install -e ".[lxmf,dev]"
pytest tests/integration/test_lxmf_local_integration.py \
  -m "local_integration and lxmf_sdk and not soak" -v
```

The probe verifies repeated real-router lifecycle, persisted identity/delivery
destination, malformed and late callback containment, and cleanup after a
partial startup failure. It intentionally does not claim remote delivery or
multi-hop behavior. The `soak` selection repeats the real-router lifecycle.

## Three-Transport Steady-Session Pass (2026-09-20)

One fresh 19-minute low-rate pass (`soak4`) through a single managed
`medre run` runtime owning MT-A (serial), MC-A (BLE) and LX-A (RNode,
isolated `reticulum_config_dir`, RNode-only interface), with independent
native peers MT-B/MC-B/LX-B held steady for the whole window. Acyclic
routes only: `mt→mc`, `mt→lx` (fan-out), `mc→lx`; LX ingress has no
outbound route. 27 source messages total (3 preflight + 24 phase), 25 s
source spacing, MT egress pacing 2.5 s.

| Path (source → far peer over RF)    | Sources | Target outcomes observed                    |
| ----------------------------------- | ------- | ------------------------------------------- |
| MT-B → MC-B (group text via MEDRE)  | 8       | 8/8 received once, exact content            |
| MT-B → LX-B (LXMF direct via MEDRE) | 8       | 8/8 received once, exact content            |
| MC-B → LX-B (LXMF direct via MEDRE) | 8       | 8/8 received once, exact content            |
| LX-B → MEDRE admission (no route)   | 8       | 8/8 durably admitted; zero outbound traffic |

All 34 outbox rows terminal `sent` at attempt 1; 34/34 receipts `sent`
(live); zero error rows, zero retries, zero reconnects, zero WARNING/ERROR
log lines across the window. Runtime RSS 64.4 MB → 59.6 MB (34 min
uptime). Each far-peer delivery was correlated by unique nonce and, for
LXMF, by unique message hash; MC group texts carry the sending board's
name prefix on the wire (`MEDRE-MC-A:` / `MEDRE-MC-B:`) and no per-packet
sender pubkey. Preflight proofs ran on the exact long-lived sessions later
kept for the pass: one MT source reaching BOTH MC-B and LX-B, one MC source
reaching LX-B, and one LX source admitted without routing.

Two LX-B deliveries were operator-induced duplicates (two source texts sent
twice from an overlapping helper process schedule): distinct native sender
timestamps produced two canonical events and two deliveries — the
documented identity/dedup contract, not an RF or MEDRE defect. Three
stale soak3-era group texts replayed from the MC-B firmware buffer on the
first post-restart connect were re-admitted as new events and delivered
once each; old `SOAK-MC-*` nonces are distinguished from the fresh run's
nonces. One earlier MT→MC target receipt (preflight) was keyed at the
board (local accept) but never collected by the peer listener; the same
path was proven twice afterwards.

An earlier partial soak (`soak3`, 2026-09-20) is retained as honest
history and is NOT a pass: its second runtime start attempted a MeshCore
BLE connect ~47 s after the previous disconnect, exhausted the 3-attempt
startup ladder ("Failed to connect to device"), and continued DEGRADED;
10 subsequent MT→MC targets dead-lettered with the adapter's own
`Session not initialised` error from the first event. Root cause: board
BLE-stack startup refusal after a too-quick restart, proceeding into a
DEGRADED runtime — compounded by a since-fixed MEDRE gap where startup
readiness assessed routes into failed adapters as SKIPPED but the router
kept delivering into them (changelog 195). Steady-session discipline
(one central per board, verified clock, settle time between runtime
restarts, healthy 3/3 start before arming sources) is the proven setup.

## See Also

- [transport-setup/lxmf.md](../transport-setup/lxmf.md) — adapter setup, delivery modes, Reticulum topology
- [diagnostics-and-evidence.md](../diagnostics-and-evidence.md) — evidence provenance and bundle collection
