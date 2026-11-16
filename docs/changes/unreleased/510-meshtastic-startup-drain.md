# Meshtastic startup packet drain

The selected Meshtastic provider drains packets for 15 seconds after its first
receive-ready connection. Set `startup_packet_drain_seconds: 0` to disable this
startup policy. The interval does not rearm on transparent reconnect.

Production examples enable the setting explicitly. Immediate-ingress test fixtures
disable it so they continue to exercise their intended routing behavior.
The current adapter configuration schema describes the optional field; released
0.1.0 schema snapshots and their freeze digests remain unchanged.
