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

| Variable                   | Required       | Default | Description                                 |
| -------------------------- | -------------- | ------- | ------------------------------------------- |
| `MESHCORE_CONNECTION_TYPE` | Yes            |         | `tcp`, `serial`, or `ble`                   |
| `MESHCORE_HOST`            | TCP            |         | Node hostname or IP                         |
| `MESHCORE_PORT`            | TCP            | `4000`  | TCP port                                    |
| `MESHCORE_SERIAL_PORT`     | Serial         |         | Serial device path                          |
| `MESHCORE_BLE_ADDRESS`     | BLE            |         | BLE MAC address                             |
| `MESHCORE_BLE_PIN`         | BLE (optional) |         | BLE pairing PIN                             |
| `MESHCORE_CHANNEL_INDEX`   | No             | `0`     | Channel for test messages                   |
| `MESHCORE_DESTINATION`     | DM tests       |         | Hex pubkey prefix for direct message target |

## Wrapper Callback Bridge Evidence

The adapter-wrapper callback bridge is proven at the fake-pipeline level:

- `simulate_inbound` → `_on_message` → `MeshCoreCodec.decode` → pipeline routing → fake outbound delivery.
- Full callback-to-delivery path with real adapter code.
- Docker SDK-boundary: no containerized MeshCore node exists.

## BLE Validation

Mock-based BLE validation tests exist in `tests/test_meshcore_live.py::TestMeshCoreBLEValidation` and pass without hardware. Hardware validation against a real BLE node is pending.

```bash
export MESHCORE_CONNECTION_TYPE="ble"
export MESHCORE_BLE_ADDRESS="C4:4F:33:6A:B0:23"
pytest tests/test_meshcore_live.py -m live -v
```

## Evidence Tiers Achieved

| Tier | Sub-class           | Date | Result                                                                         |
| ---- | ------------------- | ---- | ------------------------------------------------------------------------------ |
| S    | Fake callback       | —    | Proven: simulate_inbound → codec → pipeline → fake outbound                    |
| S    | Wrapper callback    | —    | Proven: \_on_message → MeshCoreCodec.decode → pipeline routing → fake outbound |
| —    | Docker SDK-boundary | —    | Not proven (no containerized MeshCore node)                                    |
| —    | Live network/radio  | —    | Not proven                                                                     |

## Known Gaps

- No Docker setup for MeshCore. No containerized node for Docker SDK-boundary tests.
- BLE hardware validation pending (mock tests pass).
- No live hardware smoke test recorded.
- Real TCP/serial connections work via `MeshCoreSession` but have not been exercised in a full live smoke test.

## See Also

- [transport-setup/meshcore.md](../transport-setup/meshcore.md) — adapter setup, config, delivery semantics
- [diagnostics-and-evidence.md](../diagnostics-and-evidence.md) — evidence provenance and bundle collection
