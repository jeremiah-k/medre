# Hardware Inventory

> Last updated: 2026-05-12 (hardware probe update)
> Scope: Physical LoRa radio devices available for MEDRE testing
> Status: Devices present. Serial protocols probed. **CRITICAL: Device mapping corrected — ttyACM0 is T-Beam companion (CH9102F), ttyACM1 is Meshtastic T-Beam (CH9102F), ttyUSB0 is T-LoRa V2.1-1.6 (CP2104). Previous mapping had devices swapped.** RNode on ttyUSB0 non-responsive to KISS probe. MeshCore companion on ttyACM0 uses heartbeat protocol, not MeshCore serial.

This document records the two SX1276 LoRa devices available for MEDRE development and testing. It exists because future test runs depend on knowing which physical device is which, what firmware it runs, and how to recover it.

## 1. Device Summary

|              | Device A (MeshCore)                  | Device B (RNode/Reticulum) |
| ------------ | ------------------------------------ | -------------------------- |
| **Role**     | MeshCore companion node              | RNode for Reticulum/LXMF   |
| **Board**    | LilyGO T-Beam v1.1                   | LilyGO T-LoRa V2.1-1.6     |
| **SoC**      | ESP32-D0WDQ6 rev 1.0                 | ESP32-PICO-D4 rev 1.0      |
| **Radio**    | SX1276                               | SX1276                     |
| **Flash**    | 4MB (external)                       | 4MB (embedded)             |
| **PSRAM**    | Yes                                  | No                         |
| **PMU**      | AXP192 (battery mgmt)                | None (analog GPIO 35)      |
| **GPS**      | Yes (L76K)                           | No                         |
| **Display**  | None onboard                         | SSD1306 OLED (I2C 0x3C)    |
| **MAC**      | C4:4F:33:6A:B0:21                    | 4C:75:25:D6:E3:E0          |
| **BLE MAC**  | C4:4F:33:6A:B0:23                    | N/A (no BLE advertising)   |
| **Firmware** | MeshCore v1.15.0 companion_radio_ble | RNode v1.86                |
| **BLE Name** | MeshCore-B4C6ED2C                    | N/A                        |

## 2. Stable Serial Paths

Always use `/dev/serial/by-id/` paths. Ephemeral `/dev/ttyUSB*` and `/dev/ttyACM*` may swap on reboot.

| Device                         | Stable Path                                                                                   | Ephemeral      | Notes                                                     |
| ------------------------------ | --------------------------------------------------------------------------------------------- | -------------- | --------------------------------------------------------- |
| T-Beam companion (MeshCore)    | `/dev/serial/by-id/usb-1a86_USB_Single_Serial_5435017200-if00`                                | `/dev/ttyACM0` | CH9102F, 0x27 heartbeat protocol, NOT MeshCore SDK serial |
| T-Beam Meshtastic              | `/dev/serial/by-id/usb-1a86_USB_Single_Serial_5435017226-if00`                                | `/dev/ttyACM1` | CH9102F, Meshtastic protobuf at 115200                    |
| T-LoRa V2.1-1.6 (RNode target) | `/dev/serial/by-id/usb-Silicon_Labs_CP2104_USB_to_UART_Bridge_Controller_02036439-if00-port0` | `/dev/ttyUSB0` | CP2104, KISS probe returned NO RESPONSE                   |

**⚠️ Mapping correction:** Previous version had T-Beam on ttyUSB0 and T-LoRa on ttyACM0. Corrected after physical USB inspection showed:

- CH9102F (QinHeng) devices appear as `/dev/ttyACM*` (cdc_acm driver)
- CP2104 (Silicon Labs) devices appear as `/dev/ttyUSB*` (cp210x driver)
- Serial numbers confirmed by `ls /dev/serial/by-id/`

## 3. USB Identification

| Device                      | VID:PID     | UART Chip           | Serial #     | Kernel Driver |
| --------------------------- | ----------- | ------------------- | ------------ | ------------- |
| T-Beam companion (MeshCore) | `1a86:55d4` | QinHeng CH9102F     | `5435017200` | `cdc_acm`     |
| T-Beam Meshtastic           | `1a86:55d4` | QinHeng CH9102F     | `5435017226` | `cdc_acm`     |
| T-LoRa V2.1-1.6             | `10c4:ea60` | Silicon Labs CP2104 | `02036439`   | `cp210x`      |

## 4. Firmware Details

### Device A: MeshCore v1.15.0 companion_radio_ble

- **Binary**: `Tbeam_SX1276_companion_radio_ble-v1.15.0-dee3e26-merged.bin`
- **Source**: `meshcore-dev/MeshCore` GitHub release `companion-v1.15.0`
- **Build variant**: `lilygo_tbeam_SX1276` (board: `ttgo-t-beam`)
- **Radio class**: CustomSX1276 / CustomSX1276Wrapper
- **Partition**: min_spiffs.csv (4MB)
- **Connectivity**: BLE (primary), serial at non-standard baud (not 115200 for app output)
- **Verified**: BLE advertising as `MeshCore-B4C6ED2C` confirmed via `bluetoothctl scan on`

### Device B: RNode v1.86

- **Binary**: `rnode_firmware_lora32v21.zip` (auto-downloaded by rnodeconf)
- **Source**: `markqvist/RNode_Firmware` GitHub releases
- **Product code**: `0xB1` (LilyGO LoRa32 v2.1), model `0xB9` (850-950 MHz / SX1276)
- **Frequency range**: 850.0 - 950.0 MHz
- **Max TX power**: 17 dBm
- **Modem chip**: SX1276
- **Device mode**: Normal (host-controlled)
- **Verified**: UNCONFIRMED on ttyUSB0 (T-LoRa, CP2104) — KISS probe returned no response. Note: `rnodeconf /dev/ttyACM0 --info` previously returned valid RNode device info, but ttyACM0 is the T-Beam companion (CH9102F), NOT the T-LoRa RNode target. The T-Beam companion may have had RNode firmware at some point, but it currently runs MeshCore companion_radio_ble firmware.
- **⚠️ Hardware probe finding**: KISS DETECT probe at 115200 and 57600 baud to ttyUSB0 (CP2104) returned NO RESPONSE. RNode firmware status on this device is UNCONFIRMED. May need reflash or DTR/RTS toggling.

## 4a. Full Serial Device Table (Hardware Probe Results)

| Path           | by-id                                                                       | USB Chip              | Confirmed Protocol                    | Baud Rate                  | Permissions            | MEDRE Adapter       | Next Action                                                                               |
| -------------- | --------------------------------------------------------------------------- | --------------------- | ------------------------------------- | -------------------------- | ---------------------- | ------------------- | ----------------------------------------------------------------------------------------- |
| `/dev/ttyACM0` | `usb-1a86_USB_Single_Serial_5435017200-if00`                                | CH9102F (QinHeng)     | `0x27 XX YY` heartbeat (~1s interval) | Unknown (probed at 115200) | `crw-rw----` (dialout) | MeshCore companion  | **NOT MeshCore SDK serial**. BLE is the intended path for companion_radio_ble firmware.   |
| `/dev/ttyACM1` | `usb-1a86_USB_Single_Serial_5435017226-if00`                                | CH9102F (QinHeng)     | Meshtastic protobuf                   | 115200 (confirmed)         | `crw-rw----` (dialout) | Meshtastic adapter  | **Operational.** Meshtastic live tests passed against this device.                        |
| `/dev/ttyUSB0` | `usb-Silicon_Labs_CP2104_USB_to_UART_Bridge_Controller_02036439-if00-port0` | CP2104 (Silicon Labs) | **NO RESPONSE** to KISS probe         | Probed at 115200, 57600    | `crw-rw----` (dialout) | LXMF/RNode (target) | **KISS probe failed.** Try `rnodeconf --info`, DTR/RTS toggle, or reflash RNode firmware. |

### BLE Device Table

| Adapter | State              | Target Device     | MAC                 | SDK Method              | Status                                              |
| ------- | ------------------ | ----------------- | ------------------- | ----------------------- | --------------------------------------------------- |
| `hci0`  | UP RUNNING (BlueZ) | MeshCore-B4C6ED2C | `C4:4F:33:6A:B0:23` | `MeshCore.create_ble()` | **Advertising confirmed, connection NOT ATTEMPTED** |

## 5. Flashing Commands (Recovery Reference)

### MeshCore on T-Beam (Device A)

```bash
# Download firmware
gh release download companion-v1.15.0 \
  --repo meshcore-dev/MeshCore \
  --pattern "Tbeam_SX1276_companion_radio_ble-*-merged.bin" \
  --dir /tmp/meshcore-firmware

# Erase flash (clears stale partitions from previous firmware)
esptool --port /dev/ttyACM0 erase-flash

# Flash merged binary (bootloader + partition table + app) at offset 0x0
esptool --port /dev/ttyACM0 write-flash 0x0 \
  /tmp/meshcore-firmware/Tbeam_SX1276_companion_radio_ble-v1.15.0-dee3e26-merged.bin

# Verify BLE advertising
bluetoothctl --timeout 8 scan on | grep MeshCore
```

**⚠️ Note:** MeshCore companion_radio_ble firmware uses BLE as primary transport. Serial port shows heartbeat only (0x27 XX YY). Use `MeshCore.create_ble()` for communication, NOT `create_serial()`.

### RNode on T-LoRa V2.1-1.6 (Device B)

```bash
# Interactive autoinstall (recommended)
rnodeconf --autoinstall
# Select: appropriate ttyUSB0 port, [3] LilyGO LoRa32 v2.1, Enter disclaimer, [2] 868/915/923 MHz

# Non-interactive alternative (automated stdin)
printf '\n1\n3\n\n2\ny\nyes\n\n' | rnodeconf --autoinstall

# Verify installation — use correct device path (CP2104 on ttyUSB0)
rnodeconf /dev/ttyUSB0 --info
```

**⚠️ Hardware probe status:** RNode firmware on this device is UNCONFIRMED. The `rnodeconf --info` command needs to be re-run against the correct ttyUSB0 path. Previous verification may have been against a different device mapping.

### Recover to Meshtastic (if needed)

```bash
# Both devices previously ran Meshtastic and can be restored
# T-Beam: esptool erase-flash, then flash Meshtastic tbeam firmware
# T-LoRa: esptool erase-flash, then flash Meshtastic tlora-v2-1-1_6 firmware
# Use Meshtastic flasher or esptool with firmware from meshtastic/firmware releases
```

## 6. Reticulum Configuration for RNode (Device B)

Add to `~/.reticulum/config` to use the RNode with Reticulum:

```ini
[[RNode Interface]]
  type = RNodeInterface
  # NOTE: Verify this path after RNode firmware confirmed active on ttyUSB0
  port = /dev/serial/by-id/usb-Silicon_Labs_CP2104_USB_to_UART_Bridge_Controller_02036439-if00-port0
  frequency = 915000000
  bandwidth = 125000
  txpower = 17
  spreadingfactor = 7
  codingrate = 5
```

**⚠️ Hardware probe status**: This config has NOT been validated. The KISS probe to this serial port returned no response. RNode firmware status on the T-LoRa V2.1-1.6 is UNCONFIRMED. Do not rely on this config until RNode serial path is confirmed working.

## 7. Known Issues

### MeshCore on T-Beam (ttyACM0) — Hardware Probe Findings

- **Serial protocol mismatch**: ttyACM0 speaks 3-byte heartbeat (0x27 XX YY), NOT MeshCore SDK serial protocol (expects 0x3e start marker). `MeshCore.create_serial("/dev/ttyACM0")` will fail or hang.
- **Companion_radio_ble firmware**: BLE is the primary transport for this firmware. Serial output is a heartbeat only.
- **BLE preconditions met**: hci0 UP, bleak importable, MeshCore-B4C6ED2C advertising at C4:4F:33:6A:B0:23. **BLE connection NOT YET ATTEMPTED.**
- Core dump partition checksum mismatch on first boot after fresh flash (non-fatal, clears after first successful boot).

### MeshCore on T-LoRa V2.1-1.6 (NOT USED - blocked)

- MeshCore v1.15.0 hangs on T-LoRa V2.1-1.6 in infinite I2C sensor probe retry loop (Issue #976 in meshcore-dev/MeshCore).
- This is why the T-LoRa was assigned RNode duty instead of MeshCore.
- Older MeshCore versions (v1.9.0) reportedly work on this board. TX power must be <= 17 dBm.

### RNode on T-LoRa V2.1-1.6 (ttyUSB0) — Hardware Probe Findings

- **KISS probe FAILED**: No response to DETECT command at 115200 or 57600 baud. Device is completely silent.
- **Possible causes**: (1) RNode firmware not active on device, (2) device in sleep state needing DTR/RTS, (3) wrong baud rate, (4) needs reflash.
- **Previously documented as working**: `rnodeconf /dev/ttyACM0 --info` was reported as returning valid info — but this may have been a different device mapping (see mapping correction above).
- **Action needed**: Run `rnodeconf /dev/ttyUSB0 --info` to verify. If no response, reflash with `rnodeconf --autoinstall`.

### Meshtastic on T-Beam (ttyACM1)

- Working correctly. Meshtastic protobuf at 115200 baud confirmed.
- Max TX power limited to 17 dBm by firmware (appropriate for SX1276 on this board).

## 8. Local Repositories

| Repo                | Path                                       | Relevance                      |
| ------------------- | ------------------------------------------ | ------------------------------ |
| MeshCore firmware   | `/home/jeremiah/dev/meshcore/MeshCore/`    | Board variants, build configs  |
| MeshCore Python SDK | `/home/jeremiah/dev/meshcore/meshcore_py/` | Python SDK for MeshCore        |
| Reticulum           | `/home/jeremiah/dev/Reticulum/`            | RNS stack (includes rnodeconf) |
| LXMF                | `/home/jeremiah/dev/LXMF/`                 | LXMF messaging layer           |
| NomadNet            | `/home/jeremiah/dev/NomadNet/`             | LXMF client/UI                 |
| Meshtastic          | `/home/jeremiah/dev/meshtastic/`           | Meshtastic firmware/tools      |

## 9. Tools Available

| Tool            | Version | Path                                            |
| --------------- | ------- | ----------------------------------------------- |
| esptool         | 5.2.0   | `/home/jeremiah/.local/bin/esptool`             |
| rnodeconf       | 2.5.0   | `/home/jeremiah/.platformio/penv/bin/rnodeconf` |
| rns (Reticulum) | 1.2.5   | pip (platformio penv)                           |
