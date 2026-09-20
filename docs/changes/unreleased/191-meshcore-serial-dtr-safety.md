# 191: Pin safe serial line state for MeshCore companion connections

## What changed

`MeshCoreSession` no longer calls `meshcore.MeshCore.create_serial` for
serial companions. The pinned SDK's factory retries once with
`dtr=not dtr` when the first handshake gets no protocol response (e.g. a
slow board boot), re-opening the port with DTR asserted — and
`SerialConnection` latches `transport.serial.dtr` in `connection_made`, so
the line is asserted before any post-hoc check could run. MEDRE instead
constructs the client itself —
`SerialConnection(port, baudrate, cx_dly=0.1, rts=False, dtr=False)` handed
to `MeshCore(connection, auto_reconnect=False)` followed by one awaited
`connect()` — so the requested deasserted line state is the only state ever
applied, and a silent companion raises `MeshCoreConnectionError` for the
normal startup classification/retry handling instead of triggering an
inverted-DTR second attempt.

The pinned `meshcore` SDK defaults `dtr=True`; on boards whose USB-UART
auto-download circuit drives IO0 from DTR (observed on LilyGO T-LoRa
V2.1-1.6, CH9102X), asserting DTR holds IO0 low, so an EN-line reset while
DTR is asserted can drop the companion radio into the ESP32 ROM bootloader,
killing the serial protocol until a clean warm boot. Deasserted is the safe
state for every board observed with the pinned SDK.

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

- `pytest tests/test_meshcore_session_startup.py -q` — serial tests assert the
  direct construction call shape, that `create_serial` is never awaited, and
  that a silent companion fails without a second (inverted-line) attempt.
- `pytest tests/test_meshcore_pinned_sdk_contract.py -m meshcore_sdk -q` —
  freezes `SerialConnection`/`MeshCore` construction shapes and executes the
  pinned `SerialConnection` against a simulated port to prove the requested
  `dtr=False, rts=False` state is latched at open (SDK-boundary proof; the
  firmware-side bootloader effect is modelled, not observed).
- Live board evidence recorded in the ops live-validation notes (deasserted
  lines, warm reset; not re-run for this change).

Serial startup now retains ownership of the SDK client before awaiting the
handshake, so a connect-time exception after opening the transport still runs
the common failed-start disconnect path and releases the serial port.
