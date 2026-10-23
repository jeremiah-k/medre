# Qualify Meshtastic peer connection modes

- Allow an independent Meshtastic campaign peer over BLE alongside serial and
  TCP, and reject ambiguous connection selection before constructing a client.
- Document mixed connection topologies and distinguish forwarding evidence from
  native construction cancellation and process retirement.
- Share gateway/peer serial, BLE and TCP selection across Docker hardware
  inspection, MEDRE provisioning and MMRelay qualification, with scoped Bluetooth
  access for the runner.
- Validate observer settings before opening a native radio connection.
