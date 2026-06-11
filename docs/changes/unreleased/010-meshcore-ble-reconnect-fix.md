# MeshCore BLE Reconnect Fix

Fix BLE connection failures on Linux BlueZ stacks where
le-connection-abort-by-local errors abort the initial connect, and
stale BlueZ state prevents reconnect.

## Changed

- `src/medre/adapters/meshcore/session.py`: initial BLE connection now
  pre-scans for a `BLEDevice` via `BleakScanner.find_device_by_filter()`
  before calling `MeshCore.create_ble(device=...)`, avoiding
  le-connection-abort-by-local on BlueZ stacks that reject unnamed
  address-based LE connections.
- `src/medre/adapters/meshcore/session.py`: reconnect path now clears any
  stale BlueZ connection via `client.disconnect()` before retrying, matching
  the cleanup pattern from mmrelay.
- `src/medre/adapters/meshcore/session.py`: initial BLE connection retries
  up to 3 attempts with a fresh `BleakScanner` re-scan on each failure,
  recovering from transient BlueZ adapter resets.

## Configuration

Affects adapters with `connection_type = "ble"` and a configured
`ble_address` in the MeshCore adapter block. No config changes required.
