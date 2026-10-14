"""Shared drill/smoke configuration writers.

Extracted from tests/test_drill_route_target.py so test modules can share
the four-adapter harness topology without importing other test modules.
"""

from __future__ import annotations

from pathlib import Path


def write_drill_target_config(tmp_path: Path) -> str:
    """Write a 4-adapter config whose sorted-first route source (``lxmf``)
    does not deliver to the sorted-first other adapter (``matrix``).

    Mirrors the external-adapter harness topology: ``lxmf`` is the
    alphabetically-first route source (via the reverse leg of the
    bidirectional meshcore/lxmf bridge) but its only delivery target is
    ``meshcore``.  Adapter failure drills must patch ``meshcore``, not
    ``matrix``.
    """
    cfg = tmp_path / "drill_target_route.yaml"
    cfg.write_text("""\
runtime:
  name: drill-target-route-smoke
  shutdown_timeout_seconds: 10
logging:
  level: WARNING
  format: text
storage:
  backend: memory
adapters:
  matrix:
    matrix:
      enabled: true
      adapter_kind: fake
      homeserver: https://fake.local
      user_id: "@smoke-bot:fake.local"
      access_token: fake_token_drill_target
      room_allowlist:
        - "!smoke-room:fake.local"
      encryption_mode: plaintext
  meshtastic:
    meshtastic:
      enabled: true
      adapter_kind: fake
      connection_type: fake
      origin_label: smoke-radio
  meshcore:
    meshcore:
      enabled: true
      adapter_kind: fake
      connection_type: fake
  lxmf:
    lxmf:
      enabled: true
      adapter_kind: fake
      connection_type: fake
      display_name: MEDRE Drill LXMF
routes:
  matrix_fanout:
    source_adapters:
      - matrix
    dest_adapters:
      - meshtastic
      - meshcore
      - lxmf
    directionality: source_to_dest
    enabled: true
    policy:
      allowed_event_types:
        - message.text
  meshtastic_to_matrix:
    source_adapters:
      - meshtastic
    dest_adapters:
      - matrix
    directionality: source_to_dest
    enabled: true
  meshcore_lxmf_bridge:
    source_adapters:
      - meshcore
    dest_adapters:
      - lxmf
    directionality: bidirectional
    enabled: true
""")
    return str(cfg)
