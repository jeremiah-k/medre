# 202: MeshCore stop drops the BlueZ central link; harness waits are hard-bounded

Two bounded-failure fixes:

1. **MeshCore BLE stop-leak.** The pinned SDK's `disconnect()` can return
   while BlueZ still holds the BLE central link — observed twice in the
   live campaign, where a clean runtime stop left the board `Connected`
   for ~35s+ until an explicit `bluetoothctl disconnect`, plausibly
   feeding the historical intermittent `Failed to connect` on the next
   start. `MeshCoreSession.stop()` now performs the bounded best-effort
   BlueZ disconnect for the configured address after the SDK disconnect
   (BLE connections only; failures are suppressed and never fail the
   stop path itself). The extra Bleak/D-Bus disconnect is itself hard-bounded;
   a wedged cleanup task is cancelled, detached with terminal-result ownership,
   and cannot stall adapter shutdown indefinitely.

2. **`bounded()` is a hard deadline.** The live-harness helper used
   `asyncio.wait_for`, which waits for the inner task to _acknowledge_
   cancellation — cancellation-resistant SDK code (a callback that
   swallows `CancelledError` and keeps hanging) defeated the bound and
   deferred discovery to some much larger outer timeout (the campaign's
   "15-minute hang" failure class). `bounded()` now races a timer, cancels
   best-effort on expiry, and returns control to the caller immediately;
   rude stragglers stay pending for the process boundary to reap. A new
   shared `launch_bounded()` helper captures the live suites' build+start
   pattern. It cancels and settles failed/timed-out startup before invoking
   `stop()`, so cleanup never races a still-mutating `start()`. If startup
   ignores cancellation beyond the cleanup budget, `stop()` is deferred until
   that start task eventually settles. Cleanup failures never mask
   the primary startup failure. This is pinned deterministically by
   `tests/test_live_fault_bounds.py` together with the rude-work deadline proof.
