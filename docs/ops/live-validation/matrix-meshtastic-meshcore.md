# Matrix + Meshtastic + MeshCore Live Bridge Validation

Serial-first bring-up procedure for a three-transport bridge between Matrix,
Meshtastic, and MeshCore. This guide wires three real adapters with serial as
the preferred connection method for MeshCore, validates all six routing
directions, and records evidence at each step.

## Prerequisites

| Requirement          | Details                                                                 |
| -------------------- | ----------------------------------------------------------------------- |
| Matrix homeserver    | Synapse or Conduit reachable over the network                           |
| Matrix bot account   | Dedicated user with a valid access token                                |
| Meshtastic radio     | TLora or similar, connected via USB-serial                              |
| MeshCore device(s)   | One or two MeshCore nodes connected via USB-serial                      |
| Python               | 3.11 or later                                                           |
| Package install      | `pip install -e ".[matrix,meshtastic]"` plus `pip install meshcore`     |
| Serial port access   | User in `dialout` group (Linux) or equivalent read/write on tty devices |
| Working 2-way config | Existing `medre.yaml` with Matrix + Meshtastic already validated        |

BLE is available as a fallback connection method for MeshCore. See
[Bluetooth fallback](#bluetooth-fallback) below.

## Step 1: Copy the existing config

Do not modify the working config in place. Create a separate copy so the
existing Matrix + Meshtastic bridge stays untouched.

```bash
cp /path/to/medre.yaml /path/to/medre-3way.yaml
```

All subsequent edits go into `medre-3way.yaml`.

## Step 2: Identify serial ports

Plug in the Meshtastic radio and MeshCore device(s). Wait a few seconds for
udev to create device nodes, then list stable serial paths:

```bash
ls -l /dev/serial/by-id/
```

Typical output:

```text
usb-Meshtastic_T-Beam-supreme_12345678 -> ../../ttyACM0
usb-MeshCore_CDC_AABBCCDD -> ../../ttyACM1
```

Record the `/dev/serial/by-id/...` paths. These are stable across reboots,
unlike `/dev/ttyACM*` which can reorder when devices are replugged.

If `by-id` entries do not appear, check:

- The USB cable carries data (some charge-only cables omit data lines).
- The user has permissions on the device node (`ls -l /dev/ttyACM*`).
- `udevadm monitor --property` while plugging the device in to watch events.

## Step 3: Configure MeshCore serial

Add a MeshCore adapter section to `medre-3way.yaml`. Use `connection_type: serial`
and the stable path from Step 2.

```yaml
adapters:
  meshcore:
    meshcore:
      enabled: true
      adapter_kind: real
      adapter_id: meshcore
      connection_type: serial
      serial_port: /dev/serial/by-id/usb-MeshCore_CDC_AABBCCDD
      serial_baudrate: 115200
      origin_label: MEDRE
      default_channel: 0
```

### MeshCore channel index

The MeshCore public/default channel is expected to be channel index 0 unless
diagnostics prove otherwise. To verify channel mapping after the runtime starts,
check the MeshCore self-info output in the diagnostics report:

```bash
medre diagnostics --refresh-health --config /path/to/medre-3way.yaml
```

If the diagnostics show a different default channel, update `default_channel`
accordingly.

### Bluetooth fallback

If serial is not viable (no USB port, device does not enumerate a serial
device, or cable issues), MeshCore can connect over BLE:

```yaml
adapters:
  meshcore:
    meshcore:
      enabled: true
      adapter_kind: real
      adapter_id: meshcore
      connection_type: ble
      ble_address: "AA:BB:CC:DD:EE:FF"
      origin_label: MEDRE
      default_channel: 0
```

BLE requires a pre-scan and may need stale BlueZ device cleanup before
connecting. See the [MeshCore live validation](meshcore.md) page for BLE setup
details and known issues.

## Step 4: Preserve Matrix auth

Do not change the existing Matrix adapter section. The homeserver URL, user ID,
access token, room allowlist, and encryption mode should remain exactly as they
are in the working 2-way config. The Matrix adapter continues to use the same
credentials and rooms.

Verify the Matrix section in `medre-3way.yaml` is unchanged from the original:

```yaml
adapters:
  matrix:
    matrix:
      enabled: true
      homeserver: "" # existing value — do not change
      user_id: "" # existing value — do not change
      access_token: "" # existing value — do not change
      room_allowlist:
        - "!exampleRoom1:example.org"
        - "!exampleRoom2:example.org"
      encryption_mode: e2ee_optional
```

## Step 5: Verify Meshtastic adapter

The Meshtastic adapter section should also be unchanged. Confirm the serial
port matches the device identified in Step 2:

```yaml
adapters:
  meshtastic:
    radio:
      enabled: true
      connection_type: serial
      serial_port: /dev/ttyACM0
      origin_label: MEDRE
      mmrelay_compatibility: true
```

If the Meshtastic device path shifted (due to adding the MeshCore device),
update `serial_port` to use the stable `/dev/serial/by-id/...` path instead of
`/dev/ttyACM0`.

## Step 6: Define routes

Preserve the existing Matrix↔Meshtastic route already present in the copied
config. Below it, add four one-way MeshCore routes. This gives clear visibility
into each MeshCore leg and makes it easy to isolate failures without disrupting
the known-good Matrix↔Meshtastic bridge.

```yaml
# --- Preserved: existing Matrix ↔ Meshtastic route (do not remove) -----------
# The bidirectional (or paired one-way) Matrix↔Meshtastic route from the
# original 2-way config is already present above. Keep it unchanged.

routes:
  # Matrix room → MeshCore channel 0
  matrix_to_meshcore:
    source_adapters:
      - matrix
    dest_adapters:
      - meshcore
    directionality: source_to_dest
    enabled: true
    source_room: "!exampleRoom1:example.org"
    dest_channel: "0"

  # MeshCore channel 0 → Matrix room
  meshcore_to_matrix:
    source_adapters:
      - meshcore
    dest_adapters:
      - matrix
    directionality: source_to_dest
    enabled: true
    source_channel: "0"
    dest_room: "!exampleRoom1:example.org"

  # Meshtastic channel → MeshCore channel 0
  meshtastic_to_meshcore:
    source_adapters:
      - radio
    dest_adapters:
      - meshcore
    directionality: source_to_dest
    enabled: true
    source_channel: "0"
    dest_channel: "0"

  # MeshCore channel 0 → Meshtastic channel
  meshcore_to_meshtastic:
    source_adapters:
      - meshcore
    dest_adapters:
      - radio
    directionality: source_to_dest
    enabled: true
    source_channel: "0"
    dest_channel: "0"
```

Adjust room IDs and channel indices to match your actual setup.

### Route ordering

Routes are independent. Ordering in the config does not affect processing. All
enabled routes run concurrently.

## Step 7: Marker messages

Send unique marker messages for each routing direction. Use a consistent prefix
and a timestamp suffix.

```bash
# Generate markers
TS=$(date +%Y%m%d-%H%M%S)
echo "Matrix → Meshtastic:   MEDRE-LIVE-MATRIX-TO-MESHTASTIC-$TS"
echo "Meshtastic → Matrix:   MEDRE-LIVE-MESHTASTIC-TO-MATRIX-$TS"
echo "Matrix → MeshCore:     MEDRE-LIVE-MATRIX-TO-MESHCORE-$TS"
echo "MeshCore → Matrix:     MEDRE-LIVE-MESHCORE-TO-MATRIX-$TS"
echo "Meshtastic → MeshCore: MEDRE-LIVE-MESHTASTIC-TO-MESHCORE-$TS"
echo "MeshCore → Meshtastic: MEDRE-LIVE-MESHCORE-TO-MESHTASTIC-$TS"
```

Post or send each marker from the matching source:

1. **Matrix to Meshtastic** (preserved route): Post
   `MEDRE-LIVE-MATRIX-TO-MESHTASTIC-<ts>` in the Matrix room. Watch the
   Meshtastic radio for the message.
2. **Meshtastic to Matrix** (preserved route): Send
   `MEDRE-LIVE-MESHTASTIC-TO-MATRIX-<ts>` from the Meshtastic radio. Watch the
   Matrix room for the message.
3. **Matrix to MeshCore**: Post `MEDRE-LIVE-MATRIX-TO-MESHCORE-<ts>` in the
   Matrix room. Watch the MeshCore device for the message.
4. **MeshCore to Matrix**: Send `MEDRE-LIVE-MESHCORE-TO-MATRIX-<ts>` from the
   MeshCore device. Watch the Matrix room for the message.
5. **Meshtastic to MeshCore**: Send
   `MEDRE-LIVE-MESHTASTIC-TO-MESHCORE-<ts>` from the Meshtastic radio. Watch
   the MeshCore device for the message.
6. **MeshCore to Meshtastic**: Send
   `MEDRE-LIVE-MESHCORE-TO-MESHTASTIC-<ts>` from the MeshCore device. Watch
   the Meshtastic radio for the message.

The unique markers make it easy to grep logs for a specific direction and
timestamp, and to correlate across log files and storage records.

## Step 8: Record evidence

After sending markers and observing receipt on the destination side, collect
evidence from three sources.

### Runtime logs

Run the bridge with debug logging to capture full adapter activity:

```bash
medre run --config /path/to/medre-3way.yaml
```

Search the log output for marker strings to confirm each routing direction
processed the message. Look for lines containing the adapter name and the
marker prefix.

### Storage receipts

```bash
medre inspect receipts
```

Check that each marker message appears with a `sent` or `success` status.
Remember that `sent`/`success` confirms the local adapter or radio accepted the
packet, not that a remote node received it over the air.

To drill into a specific event:

```bash
medre inspect event <event-id>
```

### Diagnostics snapshot

```bash
medre diagnostics --refresh-health --config /path/to/medre-3way.yaml
```

This probes each adapter's health endpoint and prints a summary table. All
three adapters should report healthy.

For a shutdown snapshot:

```bash
medre run --config /path/to/medre-3way.yaml --snapshot-on-shutdown /tmp/medre-3way-snapshot.json
```

## What Success Looks Like

A complete serial-first 3-way bridge bring-up produces:

1. **Config** - `medre-3way.yaml` exists alongside the original `medre.yaml`,
   original untouched.
2. **Serial discovery** - Stable `/dev/serial/by-id/` paths identified for
   Meshtastic and MeshCore devices.
3. **Startup** - `medre run --config medre-3way.yaml` starts without errors,
   all three adapters report healthy.
4. **Matrix to Meshtastic** (preserved) - Marker message posted in Matrix room
   appears on the Meshtastic radio.
5. **Meshtastic to Matrix** (preserved) - Marker sent from the Meshtastic radio
   appears in the Matrix room.
6. **Matrix to MeshCore** - Marker message posted in Matrix room appears on
   MeshCore device.
7. **MeshCore to Matrix** - Marker sent from MeshCore appears in Matrix room.
8. **Meshtastic to MeshCore** - Marker sent from Meshtastic radio appears on
   MeshCore device.
9. **MeshCore to Meshtastic** - Marker sent from MeshCore appears on
   Meshtastic radio.
10. **Evidence** - `medre inspect receipts` shows records for all six markers
    with `sent`/`success` status.

### Sent/success caveats

A `sent` or `success` status means the local adapter or radio accepted the
packet. It does not confirm RF delivery to a remote node. MeshCore local SDK
acceptance alone does not prove over-the-air delivery. Corroborate with a
second MeshCore device or a receiving radio if possible.

## Bluetooth Fallback

If serial is not viable, BLE is the fallback path:

1. Scan for the MeshCore device: `bluetoothctl scan on`.
2. Record the MAC address.
3. Clear stale BlueZ entries if previous connections left debris:
   `bluetoothctl remove <MAC>`.
4. Set `connection_type = "ble"` and `ble_address` in the MeshCore adapter
   section.
5. Optionally set `ble_pin` if the device requires pairing.

BLE live validation was completed June 2026 against a MeshCore node
on Linux BlueZ. See [meshcore.md](meshcore.md) for full BLE validation details
and known issues.

## Troubleshooting

| Symptom                               | Check                                                                       |
| ------------------------------------- | --------------------------------------------------------------------------- |
| MeshCore adapter fails to start       | Verify serial port path, baudrate, user permissions on the device           |
| MeshCore serial port not found        | `ls -l /dev/serial/by-id/`, try different USB port, check cable             |
| Messages not appearing on MeshCore    | Check `default_channel` matches actual channel, verify route `dest_channel` |
| MeshCore messages not reaching Matrix | Check route `dest_room` matches room in Matrix `room_allowlist`             |
| Duplicate messages on MeshCore        | Review all six routes for overlapping source/dest pairs                     |
| BLE connection failures               | Pre-scan, clear stale BlueZ entries, verify MAC address                     |

## See Also

- [matrix-meshtastic.md](matrix-meshtastic.md) -- two-transport Matrix + Meshtastic bridge bring-up
- [meshcore.md](meshcore.md) -- single-adapter MeshCore live validation, BLE results, environment variables
- [transport-setup/meshcore.md](../transport-setup/meshcore.md) -- adapter setup, config, delivery semantics
- [diagnostics-and-evidence.md](../diagnostics-and-evidence.md) -- evidence provenance and bundle collection
