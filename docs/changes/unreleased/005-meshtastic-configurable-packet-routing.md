# Meshtastic Configurable Packet Routing

Add configurable packet classification policy to the Meshtastic adapter,
borrowing concepts from mmrelay's `chat_portnums`, `disabled_portnums`,
`encrypted_action`, and `detection_sensor` config model.

## Changed

- `src/medre/config/adapters/meshtastic.py` — added `encrypted_action`, `chat_portnums`, `disabled_portnums`, `detection_sensor_relay` fields with validation
- `src/medre/adapters/meshtastic/packet_classifier.py` — classifier now reads config for encrypted disposition, portnum blacklist, portnum promotion, and detection sensor handling; `config=None` produces identical behavior to old hardcoded logic

## Added

- `tests/test_meshtastic_classifier_metadata.py` — 16 new tests: configurable encrypted action (3), disabled portnums (4), chat portnum promotion (4), detection sensor config (4), plus 1 symbolic normalization test
