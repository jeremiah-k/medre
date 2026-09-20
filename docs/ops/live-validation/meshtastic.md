# Meshtastic Live Validation

Live smoke test procedures for the Meshtastic adapter against a real radio node.

## Quick Validation

```bash
pip install -e ".[meshtastic]"

export MESHTASTIC_CONNECTION_TYPE="tcp"
export MESHTASTIC_HOST="meshtastic.local"
export MESHTASTIC_CHANNEL_INDEX="0"

pytest tests/test_meshtastic_live.py -m live -v
```

## Docker SDK-Boundary Tests

```bash
PYTHONPATH=src pytest tests/integration/test_meshtasticd_connectivity.py -m docker -v
```

Validates MeshtasticAdapter creates real `TCPInterface`, subscribes to pubsub, sends via real `sendText`, reports healthy, stops cleanly. Uses containerized meshtasticd with `-s` (simulation mode).

### What Docker Tests Prove

| Path                                 | Status     | What is proven                                                                                                                        |
| ------------------------------------ | ---------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| Docker SDK-boundary outbound         | Proven     | `deliver()` → enqueue → `send_one()` → real `sendText()` through `TCPInterface` to containerized meshtasticd. Returns real packet ID. |
| Docker SDK-boundary lifecycle        | Proven     | Adapter creates real `TCPInterface`, subscribes to pubsub, reports healthy, stops cleanly.                                            |
| Docker SDK-boundary inbound (pubsub) | Not proven | meshtasticd simulation mode may not relay packets between TCP clients. Inbound always uses `simulate_inbound`.                        |

## Serial CLI Validation

Manual CLI-driven validation using meshtastic CLI (no MEDRE adapter):

```bash
# Device discovery
ls -la /dev/ttyACM0 /dev/ttyUSB* /dev/serial/by-id/*

# Dependency checks
python3 -c "import meshtastic; print(meshtastic.__file__)"
test -w /dev/ttyACM0

# Device info
meshtastic --port /dev/ttyACM0 --info

# Node listing
meshtastic --port /dev/ttyACM0 --nodes

# Outbound test (channel 0)
meshtastic --port /dev/ttyACM0 --ch-index 0 --sendtext "MEDRE validation test"
```

## Environment Variables

| Variable                     | Required | Default | Description                             |
| ---------------------------- | -------- | ------- | --------------------------------------- |
| `MESHTASTIC_CONNECTION_TYPE` | Yes      |         | `tcp`, `serial`, or `ble`               |
| `MESHTASTIC_HOST`            | TCP      |         | Node hostname or IP                     |
| `MESHTASTIC_PORT`            | TCP      | `4403`  | TCP port                                |
| `MESHTASTIC_SERIAL_PORT`     | Serial   |         | Serial device path                      |
| `MESHTASTIC_BLE_ADDRESS`     | BLE      |         | BLE MAC address                         |
| `MESHTASTIC_CHANNEL_INDEX`   | No       | `0`     | Channel for test messages               |
| `MESHTASTIC_NODE_ID`         | No       |         | Meshtastic node ID                      |
| `MESHTASTIC_LIVE_SEND`       | TX       |         | `1` to enable RF transmission           |
| `MESHTASTIC_SOAK_CYCLES`     | No       | `10`    | Lifecycle cycles, valid range `1`–`100` |

## Physical Pair Harness (two real nodes)

```bash
export MESHTASTIC_CONNECTION_TYPE="serial"
export MESHTASTIC_SERIAL_PORT="/dev/serial/by-id/<medre-node>"
export MESHTASTIC_PEER_SERIAL_PORT="/dev/serial/by-id/<independent-peer>"
export MESHTASTIC_LIVE_SEND="1"
pytest tests/test_meshtastic_pair_live.py -m "live and hardware" -v
```

One node runs under a real in-process MEDRE runtime (real adapter, storage,
route, rendering, delivery); the second node is driven only by the pinned
mtjk SDK as an independent native peer. Proven 2026-09-19 on the private lab
mesh (US, LONG_TURBO, private primary channel, tx power 10):

| Case                 | Result | Evidence                                                                                                                                                                                                                          |
| -------------------- | ------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| N2 ingress           | PASS   | Native peer sends → durable canonical events with exact content, native sender identity and packet-id correlation (`medre inspect native-ref`).                                                                                   |
| N3 egress            | PASS   | Controlled local fake-source event → real route/plan → receipt `status="sent"` with native packet id → independent peer RF receipt of the nonce. Egress status tops out at `sent` (SDK acceptance); RF receipt is peer-side only. |
| N4 boundaries        | PASS   | Unicode/multibyte and newline payloads survive end-to-end; ~720-byte payload delivered UTF-8-safe truncated at ~227 bytes (`max_text_bytes`); normal message after boundaries succeeds.                                           |
| N5 identity/dedup    | PASS   | Identical text with distinct native packet ids → two distinct durable events. Same-second duplicates are not physically producible: the firmware drops sends spaced < ~2.2 s (see pacing note).                                   |

### Manual negative-control evidence

N6 is separate bench evidence, not a case implemented by
`tests/test_meshtastic_pair_live.py`: wrong PSK on the owned peer produced a
firmware-level drop with no canonical event; the receiver remained usable, the
PSK was restored byte-exact, and a subsequent positive delivery was admitted.

**Pacing note:** the lab pair (nRF52, LONG_TURBO) requires >= 2.2 s between
sends; at shorter spacing the sender firmware silently drops every second
message. The pair harness sets adapter `message_delay_seconds=2.5` and paces
peer sends at 2.5 s.

## Delivery Classification

Based on CLI-level serial validation:

| Aspect                      | Classification                                                |
| --------------------------- | ------------------------------------------------------------- |
| ACK reliability             | UNRELIABLE — no ACK confirmation for broadcast sends          |
| Delivery guarantee          | BEST EFFORT — fire-and-forget LoRa broadcast                  |
| Reconnect reliability (CLI) | RELIABLE — 4/4 serial connections succeeded across ~7.7 hours |
| MEDRE adapter reliability   | NOT ASSESSED                                                  |

## Known Gaps

- MEDRE adapter lifecycle via live pytest: proven (pair harness + smoke class).
- `send_one` queue path via MEDRE adapter: proven against real radio (pair harness egress).
- Encrypted channel support: private-PSK primary channel exercised; wrong-key negative control proven separately by manual firmware-level bench evidence.
- Second-node inbound reception: proven (pair harness, independent native peer).
- Session reconnect under sustained failure: partially observed (bounded stop/start cycles); no long soak.
- BLE connectivity: NOT EXECUTED.
- Docker inbound via pubsub: not proven (meshtasticd simulation mode limitation).

## Hardware Soak Harness

Physical-radio lifecycle endurance is opt-in and never runs in the default
suite. Configure the same connection variables as the live smoke tests, then:

```bash
pip install -e ".[meshtastic,dev]"
export MESHTASTIC_SOAK_CYCLES=10
pytest tests/test_meshtastic_hardware_soak.py \
  -m "hardware and live and soak and meshtastic_sdk" -v
```

The soak performs repeated MEDRE start/health/stop cycles without transmitting
RF traffic. Pytest output records lifecycle results only; it does not by itself
prove that a TCP endpoint is a physical radio.

Before classifying a run as hardware evidence, the operator verifies that the
endpoint is a physical device and archives the device identity/model, firmware
version, MEDRE commit SHA, connection type, and pytest output with the evidence
record. Runs without that independent device record remain lifecycle validation.

## See Also

- [transport-setup/meshtastic.md](../transport-setup/meshtastic.md) — adapter setup, config, delivery semantics
- [diagnostics-and-evidence.md](../diagnostics-and-evidence.md) — evidence provenance and bundle collection
