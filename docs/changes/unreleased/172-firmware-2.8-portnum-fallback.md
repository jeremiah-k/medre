# Changed

- Extend the SDK-less `_NUMERIC_PORTNUM_FALLBACK` portnum table to full
  parity with the Meshtastic protobuf `PortNum` enum at protobufs pin
  `1b4cb00` (firmware 2.8). SDK-less deployments now resolve firmware 2.8
  portnums — including `36` (`node_status`), `37` (`mesh_beacon`), `79`
  (`lora_ota`), and `112` (`groupalarm`) — to their symbolic names instead
  of degrading to numeric strings, matching the SDK-installed path exactly.
- Add classifier tests pinning the firmware 2.8 numeric resolutions, the
  fallback naming convention, and fallback/SDK-table parity whenever the
  optional `meshtastic` SDK is installed.
