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

| Path | Status | What is proven |
|------|--------|---------------|
| Docker SDK-boundary outbound | Proven | `deliver()` → enqueue → `send_one()` → real `sendText()` through `TCPInterface` to containerized meshtasticd. Returns real packet ID. |
| Docker SDK-boundary lifecycle | Proven | Adapter creates real `TCPInterface`, subscribes to pubsub, reports healthy, stops cleanly. |
| Docker SDK-boundary inbound (pubsub) | Not proven | meshtasticd simulation mode may not relay packets between TCP clients. Inbound always uses `simulate_inbound`. |

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

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MESHTASTIC_CONNECTION_TYPE` | Yes | | `tcp`, `serial`, or `ble` |
| `MESHTASTIC_HOST` | TCP | | Node hostname or IP |
| `MESHTASTIC_PORT` | TCP | `4403` | TCP port |
| `MESHTASTIC_SERIAL_PORT` | Serial | | Serial device path |
| `MESHTASTIC_BLE_ADDRESS` | BLE | | BLE MAC address |
| `MESHTASTIC_CHANNEL_INDEX` | No | `0` | Channel for test messages |
| `MESHTASTIC_NODE_ID` | No | | Meshtastic node ID |
| `MESHTASTIC_LIVE_SEND` | TX | | `1` to enable RF transmission |

## Evidence Tiers Achieved

| Tier | Sub-class | Date | Result |
|------|-----------|------|--------|
| R | Hardware (serial CLI) | 2026-05-12 | Device discovery, hardware/firmware capture, 1 outbound on ch0, 3 reconnect cycles. CLI-level only — not MEDRE adapter lifecycle. |
| R | Docker SDK-boundary | — | Outbound + lifecycle proven. Inbound via pubsub not proven. |
| — | MEDRE adapter live | — | NOT EXECUTED (mtjk not in project venv during validation session). |

## Delivery Classification

Based on CLI-level serial validation:

| Aspect | Classification |
|--------|---------------|
| ACK reliability | UNRELIABLE — no ACK confirmation for broadcast sends |
| Delivery guarantee | BEST EFFORT — fire-and-forget LoRa broadcast |
| Reconnect reliability (CLI) | RELIABLE — 4/4 serial connections succeeded across ~7.7 hours |
| MEDRE adapter reliability | NOT ASSESSED |

## Known Gaps

- MEDRE adapter lifecycle (start/stop/health) via live pytest tests: NOT EXECUTED.
- `send_one` path via MEDRE adapter: NOT EXECUTED.
- MEDRE session reconnect with exponential backoff: NOT EXECUTED.
- Soak test (sustained runtime): NOT EXECUTED.
- Second-node inbound reception: NOT EXECUTED.
- Encrypted channel support: NOT EXECUTED.
- BLE connectivity: NOT EXECUTED.
- Docker inbound via pubsub: not proven (meshtasticd simulation mode limitation).

## See Also

- [transport-setup/meshtastic.md](../transport-setup/meshtastic.md) — adapter setup, config, delivery semantics
- [diagnostics-and-evidence.md](../diagnostics-and-evidence.md) — evidence provenance and bundle collection
