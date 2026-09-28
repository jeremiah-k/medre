# Record physical validation campaign in release readiness

- Promote the Meshtastic and MeshCore readiness rows earned by the
  2026-09-27/28 physical validation campaign (about two hours of wall-clock
  runtime including harness repair rounds): 22 live/hardware
  executions green across the single-node live, physical-pair, MeshCore
  BLE-pair, 3-transport bridge, and 40-cycle lifecycle-soak harnesses,
  plus a peer-reboot recovery rerun.
- Record the campaign's tree, devices, and operational conditions
  (MeshCore clock re-sync after power cycle, BLE-only ESP32 firmware,
  BLE pre-scan/stale-link hygiene) under the readiness evidence section.
- Remove the two now-executed rows from the not-executed gates table.
  Delivery-reliability measurement, non-text inbound processing, LXMF
  live validation, and external Matrix validation remain open gates.
