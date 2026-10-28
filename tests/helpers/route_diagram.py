"""Synthetic four-adapter topology shared by route presentation tests."""

FOURWAY_CONFIG = """\
runtime:
  name: topology-preview
storage:
  backend: memory
adapters:
  matrix:
    matrix:
      adapter_kind: fake
      homeserver: https://example.invalid
      user_id: '@test:example.invalid'
      access_token: this-token-must-not-appear
      encryption_mode: plaintext
  meshtastic:
    FXLF:
      adapter_kind: fake
      connection_type: fake
    FXLT:
      adapter_kind: fake
      connection_type: fake
  meshcore:
    meshcore:
      adapter_kind: fake
      connection_type: fake
routes:
  all_radios:
    source_adapters: [matrix]
    dest_adapters: [FXLF, FXLT, meshcore]
    directionality: bidirectional
    source_room: '!long-private-room:example.invalid'
    dest_channel: '0'
  different_channels:
    source_adapters: [FXLF]
    dest_adapters: [FXLT]
    directionality: bidirectional
    context_map:
      '3': {dest_context: '2'}
      '4': {dest_context: '1'}
  paused:
    enabled: false
    source_adapters: [FXLT]
    dest_adapters: [matrix]
"""
