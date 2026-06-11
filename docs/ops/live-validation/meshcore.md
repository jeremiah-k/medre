# MeshCore Live Validation

Live smoke test procedures for the MeshCore adapter against a real radio node.

## Quick Validation

```bash
pip install meshcore

export MESHCORE_CONNECTION_TYPE="tcp"
export MESHCORE_HOST="192.168.1.100"
export MESHCORE_CHANNEL_INDEX="0"

pytest tests/test_meshcore_live.py -m live -v
```

## Connection Verification

Before running live tests, verify SDK connectivity directly:

```python
import asyncio
from meshcore import MeshCore

async def check():
    mc = await MeshCore.create_tcp("192.168.1.100", 4000)
    if mc is None:
        print("ERROR: create_tcp returned None (appstart failed)")
        return
    print(f"Connected: {mc.is_connected}")
    print(f"Self info: {mc.self_info}")
    await mc.disconnect()

asyncio.run(check())
```

## Environment Variables

| Variable                   | Required | Default | Description                                 |
| -------------------------- | -------- | ------- | ------------------------------------------- |
| `MESHCORE_CONNECTION_TYPE` | Yes      |         | `tcp`, `serial`, or `ble`                   |
| `MESHCORE_HOST`            | TCP      |         | Node hostname or IP                         |
| `MESHCORE_PORT`            | TCP      | `4000`  | TCP port                                    |
| `MESHCORE_SERIAL_PORT`     | Serial   |         | Serial device path                          |
| `MESHCORE_BLE_ADDRESS`     | BLE      |         | BLE MAC address                             |
| `MESHCORE_CHANNEL_INDEX`   | No       | `0`     | Channel for test messages                   |
| `MESHCORE_DESTINATION`     | DM tests |         | Hex pubkey prefix for direct message target |

## Wrapper Callback Bridge Evidence

The adapter-wrapper callback bridge is proven at the fake-pipeline level:

- `simulate_inbound` → `_on_message` → `MeshCoreCodec.decode` → pipeline routing → fake outbound delivery.
- Full callback-to-delivery path with real adapter code.
- Docker SDK-boundary: no containerized MeshCore node exists.

## BLE Validation

Mock-based BLE validation tests exist in `tests/test_meshcore_live.py::TestMeshCoreBLEValidation` and pass without hardware. Live BLE validation was completed June 2026 against a MeshCore-92C8B4E7 node on Linux BlueZ. This was the first live 3-way bridge across Matrix, Meshtastic, and MeshCore BLE.

Results:

- Matrix to MeshCore: bidirectional routing observed where actually observed.
- Meshtastic to MeshCore: bidirectional routing observed where actually observed.
- All messages routed on channel index 0.
- Connection and reconnect bugs were observed during testing and are being tracked.
- BLE connection required pre-scan and stale BlueZ device cleanup before connecting (pattern sourced from mmrelay).
- BLE remains less reliable than serial or TCP on Linux BlueZ. Expect intermittent disconnects and reconnect cycles.

Bidirectional routing was confirmed by observing messages arrive on the second MeshCore device independently. `send_text` is accepted by the local MeshCore SDK/node; remote receipt is only proven by observing the second device, not by any RF confirmation from the SDK.

```bash
export MESHCORE_CONNECTION_TYPE="ble"
export MESHCORE_BLE_ADDRESS="AA:BB:CC:DD:EE:FF"
pytest tests/test_meshcore_live.py -m live -v
```

## Evidence Tiers Achieved

| Tier | Sub-class           | Date       | Result                                                                                                                              |
| ---- | ------------------- | ---------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| S    | Fake callback       | —          | Proven: simulate_inbound → codec → pipeline → fake outbound                                                                         |
| S    | Wrapper callback    | —          | Proven: \_on_message → MeshCoreCodec.decode → pipeline routing → fake outbound                                                      |
| —    | Docker SDK-boundary | —          | Not proven (no containerized MeshCore node)                                                                                         |
| L    | Live network/radio  | 2026-06-11 | First live 3-way bridge (Matrix + Meshtastic + MeshCore BLE). Bidirectional routing observed with connection/reconnect bugs present |

## Known Gaps

- No Docker setup for MeshCore. No containerized node for Docker SDK-boundary tests.
- BLE live validation complete with known instability. Pre-scan and stale BlueZ cleanup required for a reliable connection. Connection/reconnect bugs observed and tracked.
- Live hardware smoke test recorded (BLE, June 2026). BLE is the least reliable transport; prefer TCP or serial for stable deployments.
- Real TCP/serial connections work via `MeshCoreSession` but have not been exercised in a full live smoke test.

## Serial-First Three-Transport Bridge

For a serial-first bring-up procedure that wires Matrix, Meshtastic, and
MeshCore together with four unidirectional routes, see
[matrix-meshtastic-meshcore.md](matrix-meshtastic-meshcore.md).

## See Also

- [matrix-meshtastic-meshcore.md](matrix-meshtastic-meshcore.md) -- serial-first 3-way bridge bring-up (Matrix + Meshtastic + MeshCore)
- [transport-setup/meshcore.md](../transport-setup/meshcore.md) — adapter setup, config, delivery semantics
- [diagnostics-and-evidence.md](../diagnostics-and-evidence.md) — evidence provenance and bundle collection
