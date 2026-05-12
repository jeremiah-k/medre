# Hardware Inventory

> Last updated: 2026-05-12
> Scope: Physical LoRa radio devices available for MEDRE testing
> Status: Live. Both devices flashed and verified operational.

This document records the two SX1276 LoRa devices available for MEDRE development and testing. It exists because future test runs depend on knowing which physical device is which, what firmware it runs, and how to recover it.


## 1. Device Summary

| | Device A (MeshCore) | Device B (RNode/Reticulum) |
|---|---|---|
| **Role** | MeshCore companion node | RNode for Reticulum/LXMF |
| **Board** | LilyGO T-Beam v1.1 | LilyGO T-LoRa V2.1-1.6 |
| **SoC** | ESP32-D0WDQ6 rev 1.0 | ESP32-PICO-D4 rev 1.0 |
| **Radio** | SX1276 | SX1276 |
| **Flash** | 4MB (external) | 4MB (embedded) |
| **PSRAM** | Yes | No |
| **PMU** | AXP192 (battery mgmt) | None (analog GPIO 35) |
| **GPS** | Yes (L76K) | No |
| **Display** | None onboard | SSD1306 OLED (I2C 0x3C) |
| **MAC** | C4:4F:33:6A:B0:21 | 4C:75:25:D6:E3:E0 |
| **BLE MAC** | C4:4F:33:6A:B0:23 | N/A (no BLE advertising) |
| **Firmware** | MeshCore v1.15.0 companion_radio_ble | RNode v1.86 |
| **BLE Name** | MeshCore-B4C6ED2C | N/A |


## 2. Stable Serial Paths

Always use `/dev/serial/by-id/` paths. Ephemeral `/dev/ttyUSB*` and `/dev/ttyACM*` may swap on reboot.

| Device | Stable Path | Ephemeral |
|---|---|---|
| T-Beam (MeshCore) | `/dev/serial/by-id/usb-Silicon_Labs_CP2104_USB_to_UART_Bridge_Controller_02036439-if00-port0` | `/dev/ttyUSB0` |
| T-LoRa (RNode) | `/dev/serial/by-id/usb-1a86_USB_Single_Serial_5435017200-if00` | `/dev/ttyACM0` |


## 3. USB Identification

| Device | VID:PID | UART Chip | Serial # | Kernel Driver |
|---|---|---|---|---|
| T-Beam | `10c4:ea60` | Silicon Labs CP2104 | `02036439` | `cp210x` |
| T-LoRa | `1a86:55d4` | QinHeng CH9102F | `5435017200` | `cdc_acm` |


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
- **Verified**: `rnodeconf /dev/ttyACM0 --info` returns valid device info with correct signature


## 5. Flashing Commands (Recovery Reference)

### MeshCore on T-Beam (Device A)

```bash
# Download firmware
gh release download companion-v1.15.0 \
  --repo meshcore-dev/MeshCore \
  --pattern "Tbeam_SX1276_companion_radio_ble-*-merged.bin" \
  --dir /tmp/meshcore-firmware

# Erase flash (clears stale partitions from previous firmware)
esptool --port /dev/ttyUSB0 erase-flash

# Flash merged binary (bootloader + partition table + app) at offset 0x0
esptool --port /dev/ttyUSB0 write-flash 0x0 \
  /tmp/meshcore-firmware/Tbeam_SX1276_companion_radio_ble-v1.15.0-dee3e26-merged.bin

# Verify BLE advertising
bluetoothctl --timeout 8 scan on | grep MeshCore
```

### RNode on T-LoRa (Device B)

```bash
# Interactive autoinstall (recommended)
rnodeconf --autoinstall
# Select: [1] ttyACM0, [3] LilyGO LoRa32 v2.1, Enter disclaimer, [2] 868/915/923 MHz

# Non-interactive alternative (automated stdin)
printf '\n1\n3\n\n2\ny\nyes\n\n' | rnodeconf --autoinstall

# Verify installation
rnodeconf /dev/ttyACM0 --info
```

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
  port = /dev/serial/by-id/usb-1a86_USB_Single_Serial_5435017200-if00
  frequency = 915000000
  bandwidth = 125000
  txpower = 17
  spreadingfactor = 7
  codingrate = 5
```


## 7. Known Issues

### MeshCore on T-Beam

- No serial console output at 115200 baud after bootloader. MeshCore companion_radio_ble uses BLE as primary transport; serial output may be at non-standard baud or disabled in release builds. Monitor via BLE instead.
- Core dump partition checksum mismatch on first boot after fresh flash (non-fatal, clears after first successful boot).

### MeshCore on T-LoRa V2.1-1.6 (NOT USED - blocked)

- MeshCore v1.15.0 hangs on T-LoRa V2.1-1.6 in infinite I2C sensor probe retry loop (Issue #976 in meshcore-dev/MeshCore).
- This is why the T-LoRa was assigned RNode duty instead of MeshCore.
- Older MeshCore versions (v1.9.0) reportedly work on this board. TX power must be <= 17 dBm.

### RNode on T-LoRa V2.1-1.6

- Works correctly. RNode firmware v1.86 stable on this board.
- Max TX power limited to 17 dBm by firmware (appropriate for SX1276 on this board).


## 8. Local Repositories

| Repo | Path | Relevance |
|---|---|---|
| MeshCore firmware | `/home/jeremiah/dev/meshcore/MeshCore/` | Board variants, build configs |
| MeshCore Python SDK | `/home/jeremiah/dev/meshcore/meshcore_py/` | Python SDK for MeshCore |
| Reticulum | `/home/jeremiah/dev/Reticulum/` | RNS stack (includes rnodeconf) |
| LXMF | `/home/jeremiah/dev/LXMF/` | LXMF messaging layer |
| NomadNet | `/home/jeremiah/dev/NomadNet/` | LXMF client/UI |
| Meshtastic | `/home/jeremiah/dev/meshtastic/` | Meshtastic firmware/tools |


## 9. Tools Available

| Tool | Version | Path |
|---|---|---|
| esptool | 5.2.0 | `/home/jeremiah/.local/bin/esptool` |
| rnodeconf | 2.5.0 | `/home/jeremiah/.platformio/penv/bin/rnodeconf` |
| rns (Reticulum) | 1.2.5 | pip (platformio penv) |
