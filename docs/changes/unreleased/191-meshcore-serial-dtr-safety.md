# 191: Pin safe serial line state for MeshCore companion connections

## What changed

`MeshCoreSession` now passes `dtr=False, rts=False` to
`meshcore.MeshCore.create_serial`. The pinned SDK (`meshcore==2.3.11`) defaults
`dtr=True`; on boards whose USB-UART auto-download circuit drives IO0 from DTR
(observed on LilyGO T-LoRa V2.1-1.6, CH9102X), asserting DTR holds IO0 low,
which knocks the companion radio into the ESP32 ROM bootloader or MeshCore
"CLI rescue" mode, killing the serial protocol until a clean warm boot.
Deasserted is the safe state for every board observed with the pinned SDK.

## Why

Hardware campaign evidence (2026-09-19): with SDK defaults, serial connects to
the T-LoRa companion failed (`create_serial` returned `None` or commands
returned `no_event_received` while the device echoed a `CLI Rescue` banner);
with explicit `dtr=False, rts=False` the same board ran APPSTART, radio
configuration and readback reliably.

## Verification

- `PYTHONPATH=src pytest tests/test_meshcore_session_startup.py -q` — 35 passed.
- Live board evidence recorded in the ops live-validation notes.
