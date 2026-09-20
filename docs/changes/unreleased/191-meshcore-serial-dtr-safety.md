# 191: Pin safe serial line state for MeshCore companion connections

## What changed

`MeshCoreSession` now passes `dtr=False, rts=False` to
`meshcore.MeshCore.create_serial`. The pinned `meshcore` SDK defaults
`dtr=True`; on boards whose USB-UART auto-download circuit drives IO0 from DTR
(observed on LilyGO T-LoRa V2.1-1.6, CH9102X), asserting DTR holds IO0 low, so
an EN-line reset while DTR is asserted can drop the companion radio into the
ESP32 ROM bootloader, killing the serial protocol until a clean warm boot.
Deasserted is the safe state for every board observed with the pinned SDK.

Later bench evidence (2026-09-19) refined the failure attribution: the MeshCore
"CLI rescue" banner on these boards is triggered by the board's own floating
GPIO0 user-button input (`PIN_USER_BTN`, `INPUT`, no internal pull), which can
phantom-press within seconds of a power cycle — a board quirk independent of
MEDRE's line state. It recovers with a warm reset while the port is held at
`dtr=False, rts=False`. The SDK default-line fix remains correct and necessary:
MEDRE must never assert IO0/EN itself, and deasserted lines are also the
recovery posture for the board quirk.

## Why

Hardware campaign evidence (2026-09-19): with SDK defaults, serial connects to
the T-LoRa companion failed (`create_serial` returned `None` or commands
returned `no_event_received` while the device echoed a `CLI Rescue` banner);
with explicit `dtr=False, rts=False` the same board ran APPSTART, radio
configuration and readback reliably.

## Verification

- `PYTHONPATH=src pytest tests/test_meshcore_session_startup.py -q` — 35 passed.
- Live board evidence recorded in the ops live-validation notes.
