# Release board-held BLE links before MeshCore reconnects

- The MeshCore session's stale-link cleanup issues a bounded system-level
  `bluetoothctl disconnect` for the configured address after the per-client
  cleanup. A board's companion firmware can hold its single BLE connection
  slot from a previous session until the host terminates the link — a
  connection the runtime never owned, which no per-client disconnect can
  release. This closes the gap observed in sustained mixed campaigns, where
  the 3-transport bridge connected after the pair harness had held the same
  board and the runtime's BLE start exhausted its retries against the
  slot-held board.
- The release runs in both cleanup positions (before the first attempt and
  between retries), is hard-bounded at 5 seconds, kills an unresponsive
  `bluetoothctl`, and is a silent no-op on hosts without the binary.
- Post-kill subprocess reaping tolerates an already-exited pid so a wedged
  release can never escape the best-effort cleanup boundary.

__zcode_status=$?
if [ "$__zcode_status" -eq 0 ]; then pwd -P > '/tmp/zcode-6a581ad3-96e4-49bd-9ed6-8ae12609d717-cwd'; fi
exit "$__zcode_status"
