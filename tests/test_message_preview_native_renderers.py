# medre-test-tier: compatibility
"""Installed renderer distributions answer previews with their pure legs."""

from __future__ import annotations

import json
import sys

import pytest

from medre.config.env import apply_env_overrides
from medre.config.loader import load_config
from medre.runtime.message_preview import build_message_preview


async def test_installed_matrix_and_xmpp_use_real_pure_renderers(tmp_path):
    pytest.importorskip("medre.adapters.matrix.renderer")
    pytest.importorskip("medre.adapters.xmpp.renderer")
    # A small SDK-free authored config, not a real login or native session.
    config_path = tmp_path / "matrix-xmpp.yaml"
    config_path.write_text("""adapters:
  matrix:
    matrix_bot:
      adapter_kind: fake
      homeserver: https://matrix.example.org
      user_id: '@bot:example.org'
      access_token: synthetic-secret-never-display
      room_allowlist: ['!room:example.org']
      encryption_mode: plaintext
  xmpp:
    xmpp_bot:
      adapter_kind: fake
      jid: bot@example.org/bridge
      rooms: ['room@conference.example.org']
routes:
  bridge:
    source_adapters: [matrix_bot]
    dest_adapters: [xmpp_bot]
    directionality: bidirectional
    context_map:
      '!room:example.org': {dest_context: 'room@conference.example.org'}
    policy:
      allowed_event_types: ['message.created', 'message.text']
""")
    config, _, paths = load_config(config_path)
    before = set(sys.modules)
    report = await build_message_preview(
        apply_env_overrides(config, paths), route_id="bridge", relation="reply"
    )
    assert report["status"] == "rendered", report
    assert {item["renderer"] for item in report["legs"]} == {"xmpp", "matrix"}
    assert all(
        item["fallback_applied"] == "strategy_fallback_text" for item in report["legs"]
    )
    assert "synthetic-secret-never-display" not in json.dumps(report)
    sdk_roots = {"nio", "slixmpp"}
    assert not (set(sys.modules) - before) & sdk_roots
