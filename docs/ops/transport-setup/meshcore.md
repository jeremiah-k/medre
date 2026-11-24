# MeshCore Transport Setup

Install the adapter in the same environment as MEDRE using the
[installation guide](../install.md). Activate a project environment before
invoking CLI tools; persistent uv tools already expose `medre` on PATH.
The adapter supplies both its SDK-free fake and its native SDK dependencies.

Setting up and running the MEDRE MeshCore adapter against a real radio node.

Fake mode is the default and recommended path for all development and testing. Real connectivity modes are opt-in for live validation only.

## Before you begin

### Prerequisites

| Requirement          | Details                                                                                                                    |
| -------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| MeshCore node        | A MeshCore companion radio node accessible via TCP, serial, or BLE                                                         |
| Python               | 3.11 or later                                                                                                              |
| Package install      | Install the adapter in the MEDRE environment; see [installation](../install.md) (its pinned MeshCore SDK installs with it) |
| Network access (TCP) | Your machine can reach the node's IP address on port 4000                                                                  |
| Serial access        | USB cable connecting the node; user in `dialout` group on Linux                                                            |
| BLE access           | BLE-capable hardware and BlueZ on Linux (optional)                                                                         |

### Node Setup

#### TCP Connectivity (recommended for testing)

Most MeshCore nodes expose a TCP interface. SDK default port: 4000.

1. Power on the node and connect it to your network.
2. Find the node's IP address.
3. Verify TCP connectivity:

```bash
nc -zv 192.168.1.100 4000
```

4. Optionally verify with the MeshCore SDK directly:

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

**Note:** `create_tcp` can return `None` if the transport connects but `appstart()` fails. Always check for `None`.

#### Serial Connectivity

```bash
# Find the serial port
ls /dev/ttyUSB* /dev/ttyACM*

# Ensure serial port access
sudo usermod -aG dialout $USER
# Log out and back in
```

#### BLE Connectivity

BLE connects to a MeshCore node over Bluetooth Low Energy. This mode is useful
when the node is not on a shared network and no USB cable is available.

##### Requirements

| Requirement       | Details                                                                        |
| ----------------- | ------------------------------------------------------------------------------ |
| Bluetooth adapter | BLE-capable hardware (most built-in laptop adapters work)                      |
| BlueZ             | Linux Bluetooth stack. Verify with `bluetoothctl --version`                    |
| `bleak`           | Python BLE library. Installed automatically with `meshcore`                    |
| Permissions       | User must be in the `bluetooth` group, or run with capabilities for BLE access |

```bash
# Verify BlueZ is running
bluetoothctl --version

# Ensure BLE access permissions
sudo usermod -aG bluetooth $USER
# Log out and back in for group changes to take effect

# The adapter distribution installs the pinned meshcore SDK (includes bleak)
```

##### Pair the device first

The MeshCore node must be paired with your host before MEDRE tries to connect.
Unpaired BLE connections will fail or behave unpredictably.

```bash
bluetoothctl
[bluetoothctl] scan on
# Find your MeshCore node in the scan results and note its MAC address
[bluetoothctl] pair AA:BB:CC:DD:EE:FF
[bluetoothctl] trust AA:BB:CC:DD:EE:FF
[bluetoothctl] scan off
[bluetoothctl] exit
```

##### Firmware Compatibility

The adapter distribution pins its MeshCore SDK directly in its package
metadata; the installed adapter's declared pin is the version authority.
Verify the installed SDK surface in the environment containing MEDRE:

```bash
# Activated deployment project:
python -c "import meshcore; print(meshcore.__all__)"
# Persistent uv tool (POSIX):
"$(uv tool dir)/medre/bin/python" -c "import meshcore; print(meshcore.__all__)"
```

For the equivalent Windows PowerShell invocation see
[installation](../install.md#run-adapter-sdk-tools-in-the-same-environment).
The SDK should expose `MeshCore`, `BLEConnection`, `SerialConnection`,
`TCPConnection`, `ConnectionManager`, `EventType` and `BinaryReqType`. Run
the optional TCP connectivity probe above with the same interpreter.

## Set up

Install the adapter:

```bash
medre adapters install meshcore
```

Save a private `connections.yaml` with one real TCP instance. The common case
needs the connection type and the node host; the default TCP port is shown in
[Options](#options):

```yaml
adapters:
  meshcore:
    meshcore-alpha:
      adapter_kind: real
      connection_type: tcp
      host: 192.168.1.100
routes: {}
```

For BLE, set `connection_type: ble` and `ble_address` to the MAC address
you recorded during pairing (either in YAML or through the environment
override below). Add another adapter instance, then build routes with
[guided configuration](../guided-configuration.md).

### Environment Overrides

```bash
export MEDRE_ADAPTER__MESHCORE_ALPHA__TRANSPORT=meshcore
export MEDRE_ADAPTER__MESHCORE_ALPHA__CONNECTION_TYPE=ble
export MEDRE_ADAPTER__MESHCORE_ALPHA__BLE_ADDRESS=AA:BB:CC:DD:EE:FF
# Optional: BLE pairing PIN for programmatic pairing
# export MEDRE_ADAPTER__MESHCORE_ALPHA__BLE_PIN=123456
```

`MEDRE_MESHCORE_*` runtime config variables are unsupported. Use
`MEDRE_ADAPTER__<TOKEN>__<FIELD>` instead; see
[configuration](../configuration.md#environment-variable-overrides) for the
complete override contract.

## Verify

Check and run the bridge configuration saved by guided configuration:

```bash
medre config check --config bridge.yaml
medre routes plan --config bridge.yaml
medre run --config bridge.yaml
```

A healthy start logs `MeshCoreAdapter meshcore-alpha started (mode=tcp)` and
health settles at `healthy`. Configurations missing the fields required for
their connection type are rejected at load — see
[Configuration Validation](#configuration-validation).

### Health States

| State     | Meaning                                      |
| --------- | -------------------------------------------- |
| `unknown` | Adapter has not started, or has been stopped |
| `healthy` | Adapter started successfully                 |
| `failed`  | Client exists but start did not complete     |

Intermediate state: reconnecting. The session enters a reconnecting state during exponential backoff after unexpected disconnects.

## Options

| Key                     | Default                    | What it changes                                                                | When to touch it                                            |
| ----------------------- | -------------------------- | ------------------------------------------------------------------------------ | ----------------------------------------------------------- |
| `port`                  | SDK (4000)                 | TCP port of the node interface                                                 | Non-default node port                                       |
| `serial_port`           | unset                      | Serial device path (required for `serial`)                                     | Serial connections                                          |
| `ble_address`           | unset                      | BLE MAC address (required for `ble`)                                           | BLE connections                                             |
| `ble_pin`               | unset                      | BLE pairing PIN for programmatic pairing; sensitive                            | Headless hosts where `bluetoothctl` pairing is not feasible |
| `identity`              | unset                      | MeshCore node identity string (e.g. a node name)                               | When the node distinguishes identities                      |
| `pubkey`                | unset                      | Expected node public key as a hex string; validated, not yet enforced as a pin | Recording the expected companion node key                   |
| `node_config`           | empty                      | Opaque node-specific settings; secret keys are rejected                        | Node-specific tuning only                                   |
| `default_channel`       | `0`                        | Channel used for outbound channel messages                                     | Routing on a non-zero channel                               |
| `message_delay_seconds` | `0.5`                      | Minimum spacing between outbound messages                                      | Congested networks                                          |
| `max_text_bytes`        | MeshCore group-text budget | UTF-8 byte budget for rendered outbound text                                   | Longer payloads                                             |
| `max_text_parts`        | `1`                        | Number of text parts a rendered message may use                                | Multi-part delivery                                         |
| `origin_label`          | empty                      | Prefix identifying this origin on the other side                               | Labeling in mixed bridges                                   |
| `relay_prefix`          | unset                      | Custom rendering template for relayed text                                     | Custom presentation                                         |

### Configuration Validation

`MeshCoreConfig.validate()` enforces:

| Connection type | Required fields             |
| --------------- | --------------------------- |
| `fake`          | `adapter_id` only           |
| `tcp`           | `adapter_id`, `host`        |
| `serial`        | `adapter_id`, `serial_port` |
| `ble`           | `adapter_id`, `ble_address` |

Additional rules:

- `ble_pin` (if provided) must be a non-empty string. Sensitive — never
  exposed in diagnostics.
- `identity` (if provided) must be a non-empty string.
- `pubkey` (if provided) must be a non-empty hex string.
- `node_config` must not contain keys named `private_key`, `secret`, or `password`.
- `message_delay_seconds` must be finite and >= 0.
- `default_channel` must be >= 0.

## Troubleshooting

A healthy adapter proves only local node acceptance, never mesh delivery or
RF confirmation; see [Known Limitations](#known-limitations).

### `le-connection-abort-by-local` on every connection attempt

This is the most common BLE issue on Linux. It means BlueZ has a stale
connection entry for the device address. MEDRE handles this automatically on
reconnect, but if you hit it on a fresh start:

```bash
# Force-disconnect the device in BlueZ
bluetoothctl disconnect AA:BB:CC:DD:EE:FF

# Remove and re-pair if disconnect alone is not enough
bluetoothctl remove AA:BB:CC:DD:EE:FF
bluetoothctl scan on
bluetoothctl pair AA:BB:CC:DD:EE:FF
bluetoothctl trust AA:BB:CC:DD:EE:FF
```

Then restart MEDRE.

### Connection works but drops after a few minutes

BLE connections are sensitive to distance and interference. Try these steps:

1. Move closer to the MeshCore node. BLE range is typically 10 to 30 meters
   depending on antenna and environment.
2. Power cycle the node. Some BLE firmware accumulates state that a simple
   disconnect does not clear.
3. Check for 2.4 GHz interference (WiFi, microwaves, USB 3.0 devices).
4. If the problem persists, switch to TCP or serial. BLE is the least reliable
   transport mode and should only be used when wired or network connectivity is
   not available.

### `bleak` import error

The pinned `meshcore` SDK depends on `bleak`; it installs with the adapter
distribution. Check the interpreter in the
[MEDRE environment](../install.md#run-adapter-sdk-tools-in-the-same-environment)
first. If the import fails there, check whether the adapter and its native SDK
are installed correctly; an unrelated shell interpreter proves nothing.

### `bluetoothctl scan on` does not show the node

1. Confirm the node is powered on and in BLE advertising mode.
2. Check that your Bluetooth adapter is not blocked:

```bash
rfkill list bluetooth
# If blocked:
sudo rfkill unblock bluetooth
```

3. Restart the Bluetooth service:

```bash
sudo systemctl restart bluetooth
```

### Known Limitations

1. **No Docker setup for MeshCore.** No containerized MeshCore node for Docker SDK-boundary tests.
2. **BLE is the least reliable transport.** Connection quality depends heavily on distance, interference, and BlueZ behavior. Prefer TCP or serial for stable deployments. See BLE troubleshooting above.
3. **Fire-and-forget delivery.** `sent` means local node acceptance. No remote receipt confirmation.
4. **Auto-reconnect.** On unexpected disconnect, the session attempts bounded exponential backoff reconnection (1 s → 2 s → 4 s, capped at 30 s, ±25 % jitter, max 10 attempts).
5. **Contact-based sender resolution is scaffold.** `source_transport_id` carries the raw pubkey prefix.
6. **message_delay_seconds is enforced.** Outbound sends are serialized with a lock and spaced by the configured delay.

## Reference

- BLE session handling, connection mode construction, manual adapter wiring,
  startup and shutdown sequences, and outbound delivery paths:
  [MeshCore transport internals](../../dev/transport-internals/meshcore.md)
- [guided configuration](../guided-configuration.md) — the route builder used
  to connect two adapters

### See Also

- [live-validation/meshcore.md](../../dev/live-validation/meshcore.md) — live smoke test procedures
- [diagnostics-and-evidence.md](../diagnostics-and-evidence.md) — evidence provenance and bundle collection
- [recovery-and-replay.md](../recovery-and-replay.md) — crash recovery and replay
