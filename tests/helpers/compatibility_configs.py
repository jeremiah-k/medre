# medre-test-tier: compatibility
"""Provider-dependent YAML fixtures for the cross-adapter compatibility tier."""

CONFIG_WITH_ROUTES = """\
runtime:
  name: test-routes
logging:
  level: INFO
storage:
  backend: sqlite
  path: '{state}/test.db'
adapters:
  matrix:
    main:
      enabled: true
      homeserver: https://matrix.test
      user_id: '@bot:test'
      access_token: tok
      room_allowlist: ['!room:test']
      encryption_mode: plaintext
  meshtastic:
    radio:
      enabled: true
      connection_type: serial
      serial_port: /dev/ttyACM0
      origin_label: TestMesh
routes:
  matrix_to_radio:
    source_adapters: [main]
    dest_adapters: [radio]
    directionality: source_to_dest
    enabled: true
  radio_to_matrix:
    source_adapters: [radio]
    dest_adapters: [main]
    directionality: source_to_dest
    enabled: false
  bidirectional_bridge:
    source_adapters: [main]
    dest_adapters: [radio]
    directionality: bidirectional
    enabled: true
    source_room: '!room:test'
    dest_channel: '1'
    policy:
      allowed_event_types: [message]
"""

CONFIG_NO_ROUTES = """\
runtime:
  name: test-no-routes
logging:
  level: INFO
storage:
  backend: sqlite
  path: '{state}/test.db'
adapters:
  matrix:
    main:
      enabled: true
      homeserver: https://matrix.test
      user_id: '@bot:test'
      access_token: tok
      room_allowlist: ['!room:test']
      encryption_mode: plaintext
"""

CONFIG_WITH_ROUTE_TARGETING = """\
runtime:
  name: test-targets
logging:
  level: INFO
storage:
  backend: sqlite
  path: '{state}/test.db'
adapters:
  matrix:
    src:
      enabled: true
      homeserver: https://matrix.test
      user_id: '@bot:test'
      access_token: tok
      room_allowlist: ['!room:test']
      encryption_mode: plaintext
    dst:
      enabled: true
      homeserver: https://matrix.test
      user_id: '@bot2:test'
      access_token: tok2
      room_allowlist: ['!room2:test']
      encryption_mode: plaintext
routes:
  targeted_route:
    source_adapters: [src]
    dest_adapters: [dst]
    directionality: bidirectional
    enabled: true
    source_room: '!room:test'
    dest_room: '!room2:test'
    policy:
      allowed_event_types: [message, reaction]
"""

CONFIG_ROUTE_UNKNOWN_ADAPTERS = """\
runtime:
  name: test-unknown
logging:
  level: INFO
storage:
  backend: sqlite
  path: '{state}/test.db'
adapters:
  matrix:
    main:
      enabled: true
      homeserver: https://matrix.test
      user_id: '@bot:test'
      access_token: tok
      room_allowlist: ['!room:test']
      encryption_mode: plaintext
routes:
  orphan_route:
    source_adapters: [nonexistent]
    dest_adapters: [also_missing]
    directionality: source_to_dest
    enabled: true
"""

CONFIG_BAD_LIMITS = """\
runtime:
  name: test-bad-limits
  limits:
    max_inflight_deliveries: -1
storage:
  backend: sqlite
  path: '{state}/test.db'
adapters:
  matrix:
    main:
      enabled: true
      homeserver: https://matrix.test
      user_id: '@bot:test'
      access_token: tok
      room_allowlist: ['!room:test']
      encryption_mode: plaintext
"""

CONFIG_DISABLED_ADAPTER_IN_ROUTE = """\
runtime:
  name: test-disabled-adapter-route
logging:
  level: INFO
storage:
  backend: sqlite
  path: '{state}/test.db'
adapters:
  matrix:
    offline:
      enabled: false
      homeserver: https://matrix.test
      user_id: '@bot:test'
      access_token: tok
      room_allowlist: ['!room:test']
      encryption_mode: plaintext
    active:
      enabled: true
      homeserver: https://matrix.test
      user_id: '@bot2:test'
      access_token: tok2
      room_allowlist: ['!room2:test']
      encryption_mode: plaintext
routes:
  uses_disabled:
    source_adapters: [active]
    dest_adapters: [offline]
    directionality: source_to_dest
    enabled: true
"""

CONFIG_DISABLED_ROUTE_UNKNOWN_REFS = """\
runtime:
  name: test-disabled-unknown
logging:
  level: INFO
storage:
  backend: sqlite
  path: '{state}/test.db'
adapters:
  matrix:
    main:
      enabled: true
      homeserver: https://matrix.test
      user_id: '@bot:test'
      access_token: tok
      room_allowlist: ['!room:test']
      encryption_mode: plaintext
routes:
  ghost_route:
    source_adapters: [phantom]
    dest_adapters: [specter]
    directionality: source_to_dest
    enabled: false
"""

CONFIG_FAKE_MULTI = """\
runtime:
  name: workflow-test
  shutdown_timeout_seconds: 5
  limits:
    max_inflight_deliveries: 50
    max_inflight_replay_events: 25
    shutdown_drain_timeout_seconds: 3
    delivery_acquire_timeout_seconds: 0.5
logging:
  level: INFO
  format: text
storage:
  backend: memory
adapters:
  matrix:
    fake_matrix:
      enabled: true
      adapter_kind: fake
      homeserver: https://fake.local
      user_id: '@bot:fake.local'
      access_token: fake_tok
      room_allowlist: ['!room:fake.local']
      encryption_mode: plaintext
  meshtastic:
    fake_mesh:
      enabled: true
      adapter_kind: fake
      connection_type: fake
      origin_label: TestMesh
routes:
  matrix_to_mesh:
    source_adapters: [fake_matrix]
    dest_adapters: [fake_mesh]
    directionality: source_to_dest
    enabled: true
    source_room: '!room:fake.local'
    dest_channel: '1'
  mesh_to_matrix:
    source_adapters: [fake_mesh]
    dest_adapters: [fake_matrix]
    directionality: source_to_dest
    enabled: false
  bidirectional_bridge:
    source_adapters: [fake_matrix]
    dest_adapters: [fake_mesh]
    directionality: bidirectional
    enabled: true
    policy:
      allowed_event_types: [message]
"""

CONFIG_SINGLE_ADAPTER = """\
runtime:
  name: single-adapter
storage:
  backend: memory
adapters:
  matrix:
    solo:
      enabled: true
      adapter_kind: fake
      homeserver: https://fake.local
      user_id: '@bot:fake.local'
      access_token: tok_single
      room_allowlist: ['!room:fake.local']
      encryption_mode: plaintext
"""
