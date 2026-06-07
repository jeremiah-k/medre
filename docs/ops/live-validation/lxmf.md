# LXMF Live Validation

Live smoke test procedures for the LXMF adapter against a real Reticulum network.

## Quick Validation

```bash
pip install lxmf

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

Confirmed versions: lxmf 0.9.7 + rns 1.2.5.

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

| Tier | Sub-class           | Date | Result                                                                    |
| ---- | ------------------- | ---- | ------------------------------------------------------------------------- |
| S    | Fake callback       | —    | Proven: simulate_inbound → codec → pipeline → fake outbound               |
| S    | Wrapper callback    | —    | Proven: \_on_packet → LxmfCodec.decode → pipeline routing → fake outbound |
| —    | Docker SDK-boundary | —    | Not proven (no containerized Reticulum/LXMF router)                       |
| —    | Live network        | —    | Not proven                                                                |

## Known Gaps

- No Docker setup for Reticulum/LXMF. No containerized router for Docker SDK-boundary tests.
- Propagation node config not in LxmfConfig yet.
- No live hardware smoke test recorded.
- `RNS.Reticulum` and `LXMF` packages available locally at `/home/jeremiah/dev` but live path setup pending.
- No native reply mechanism — replies rendered as plain text.

## See Also

- [transport-setup/lxmf.md](../transport-setup/lxmf.md) — adapter setup, delivery modes, Reticulum topology
- [diagnostics-and-evidence.md](../diagnostics-and-evidence.md) — evidence provenance and bundle collection
