# MeshCore Live Validation

Live smoke test procedures for the MeshCore adapter against a real radio node.

## Quick Validation

```bash
pip install -e ".[meshcore]"

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
    print("Connected: create_* returned a client")
    # self_info is populated after appstart(); availability depends on SDK version
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

Mock-based BLE validation tests exist in `tests/test_meshcore_live.py::TestMeshCoreBLEValidation` and pass without hardware. Live BLE validation was completed June 2026 against a real MeshCore BLE node on Linux BlueZ. This was the first live 3-way bridge across Matrix, Meshtastic, and MeshCore BLE.

Results:

- Matrix ↔ MeshCore: bidirectional routing observed.
- Meshtastic ↔ MeshCore: bidirectional routing observed.
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

| Tier       | Sub-class           | Date       | Result                                                                                                                                                                                           |
| ---------- | ------------------- | ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| synthetic  | Fake callback       | —          | Proven: simulate_inbound → codec → pipeline → fake outbound                                                                                                                                      |
| synthetic  | Wrapper callback    | —          | Proven: \_on_message → MeshCoreCodec.decode → pipeline routing → fake outbound                                                                                                                   |
| local-int  | Pinned loopback     | current    | Proven in CI: real pinned SDK against the local companion endpoint — framing/APPSTART, inbound dispatch, outbound MSG_SENT, reconnect, cancellation. No RF claim                                 |
| —          | Docker SDK-boundary | —          | Not proven (no containerized MeshCore node)                                                                                                                                                      |
| historical | Live network/radio  | 2026-06-11 | Historical record only, not a current-tree validation claim: first live 3-way bridge (Matrix + Meshtastic + MeshCore BLE); bidirectional routing observed with connection/reconnect bugs present |

## Physical Pair Validation (2026-09-19, campaign `buildout/hardware-readiness`)

Two LilyGo T-Beam v1.0 (SX1276 + AXP192) companions on official
`Tbeam_SX1276_companion_radio_ble` v1.17.1, one owned by MEDRE over BLE, the
other an independent native SDK peer. Commissioned to the US lab preset
(910.525 MHz / SF7 / BW 62.5 / CR5) on a private group channel; host pairing is
a one-time `bluetoothctl` bond — test runs connect pin-less over the existing
bond and never re-pair.

Opt-in harness (ordinary suite stays deselected; full output piped to the
private lab evidence directory):

```bash
MESHCORE_PAIR=1 \
MESHCORE_MEDRE_BLE_ADDRESS="<owned-board-a>" \
MESHCORE_PEER_BLE_ADDRESS="<owned-board-b>" \
pytest tests/test_meshcore_pair_live.py -m "live and hardware" \
  -p no:unraisableexception
```

Roles are reversible via the same env keys plus `MESHCORE_MEDRE_NODE_NAME` /
`MESHCORE_PEER_NODE_NAME`; both boards were validated under MEDRE (each
allocation passed the full module). `MEDRE_LIVE_QUICK=1` runs a core-evidence
subset for fast iteration; full mode is the proof gate. Coverage:

- N1 healthy lifecycle + bounded quiet window (no stale admission).
- N2 native ingress: exact content, Unicode, newline, durable admission; the
  firmware prepends the sender node name on the wire, so canonical bodies
  carry `"<sender-name>: <text>"` and no per-packet sender pubkey exists to
  assert.
- N5 controlled sender-timestamp cases through the supported SDK argument:
  wire-identical same-second text dedups to one event; distinct timestamps
  admit separately.
- N6 wrong-key channel probe is not admitted; channel restored and a positive
  delivery still admitted.
- N3 routed egress observed by the independent peer on the expected channel,
  correlated with durable receipts (channel sends are local-acceptance only —
  `PACKET_OK` is not RF delivery).
- N4 Unicode/newline passthrough and the documented radio cap: the firmware
  limits the whole group text to 160 chars _including_ its sender-name prefix.

The fully-native cross-transport bridge and fault cases (B1 both directions,
B3 negative + restored positive, B4 bounded echo, F1 BLE stop/restart, F6
scoped isolation) live in `tests/test_meshcore_meshtastic_bridge_live.py`
(`MEDRE_MC_BRIDGE=1` plus MT serial and MC BLE env endpoints).

Operational firmware truths proven on this hardware:

- The T-Beam RTC does not survive power loss; MeshCore group-message replay
  protection silently drops stale sender timestamps, so boards must be
  time-synced through the companion command after any power/reset before
  group messaging works.
- Each board accepts exactly one BLE central connection; a leftover peer or
  app holding the slot fails MEDRE's connect with async errors, not a busy
  signal. Release stale links with a targeted `bluetoothctl disconnect`.
- A channel readback keeps a fixed 16-byte secret slot; the empty-channel
  default reads back as 16 zero bytes (not an absent field).

## Hardware Bring-Up Notes (2026-09-19, campaign `buildout/hardware-readiness`)

- Pinned SDK `meshcore==2.3.11` defaults `dtr=True` on `create_serial`; boards
  with a USB-UART auto-download circuit on IO0 (observed: LilyGO T-LoRa
  V2.1-1.6) must use `dtr=False, rts=False` (fixed in `MeshCoreSession`).
- Official companion v1.17.1 ships USB-serial builds for `lilygo_tlora_v2_1`
  but not for the SX1276 T-Beam; the T-Beam companion is BLE-only.
- US preset per docs.meshcore.io FAQ 2.3: 910.525 MHz, SF7, BW 62.5, CR5.

## Known Gaps

- No Docker setup for MeshCore. No containerized node for Docker SDK-boundary tests.
- Live hardware smoke test recorded (BLE, June 2026) reported BLE instability
  with pre-scan/stale-BlueZ workarounds. The 2026-09-19 pair campaign refined
  that picture: over a one-time host bond, pin-less BLE sessions ran stably
  across repeated connects; the instability was tied to stale bonds and
  pairing-time churn rather than bonded sessions. Transport choice should
  follow the board: on ESP32 auto-download wiring, serial carries a reset
  hazard that BLE avoids, while TCP suits networked nodes.
- Real TCP/serial connections work via `MeshCoreSession` but have not been
  exercised in a full live smoke test.

## Serial-First Three-Transport Bridge

For a serial-first bring-up procedure that wires Matrix, Meshtastic, and
MeshCore together with four additional one-way MeshCore routes, see
[matrix-meshtastic-meshcore.md](matrix-meshtastic-meshcore.md).

## Deterministic Local Integration

Before hardware validation, run the real pinned MeshCore SDK against MEDRE's
local companion-protocol endpoint:

```bash
pip install -e ".[meshcore,dev]"
pytest tests/integration/test_meshcore_local_integration.py \
  -m "local_integration and meshcore_sdk and not soak" -v
```

This layer proves SDK framing/APPSTART, inbound dispatch, outbound MSG_SENT,
disconnect/reconnect, cancellation, and send serialization without claiming RF
behavior. A manual repeated-cycle variant is available with
`-m "local_integration and meshcore_sdk and soak"`.

## See Also

- [matrix-meshtastic-meshcore.md](matrix-meshtastic-meshcore.md) -- serial-first 3-way bridge bring-up (Matrix + Meshtastic + MeshCore)
- [transport-setup/meshcore.md](../transport-setup/meshcore.md) — adapter setup, config, delivery semantics
- [diagnostics-and-evidence.md](../diagnostics-and-evidence.md) — evidence provenance and bundle collection
