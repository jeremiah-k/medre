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
  beyond the two-board bench pair remains unproven. The cross-transport
  LXMF bridge directions and fan-out live harness are the next milestone.
- Adapter health covers the local session/router only — it cannot observe
  whether any peer is reachable.
- No native reply mechanism — replies rendered as plain text.

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

## See Also

- [transport-setup/lxmf.md](../transport-setup/lxmf.md) — adapter setup, delivery modes, Reticulum topology
- [diagnostics-and-evidence.md](../diagnostics-and-evidence.md) — evidence provenance and bundle collection
