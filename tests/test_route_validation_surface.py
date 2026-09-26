"""Runtime route-validation failures surface through operator fences.

Operator-facing surfaces re-run the startup route compilation and must
report its failures instead of passing silently: the smoke preflight
summary, the evidence config section, and (covered with its CLI siblings
in :mod:`tests.test_cli_route_commands`) ``routes validate``.  This file
pins the first two at the function seam, forcing the otherwise implausible
compilation failure via the expansion-token derivation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from medre.config.loader import load_config
from medre.runtime.evidence._config_sections import _collect_route_validation
from medre.runtime.smoke import _run_preflight

pytestmark = pytest.mark.usefixtures("isolated_config_env")

_CONFIG = """\
runtime:
  name: route-validation-surface
storage:
  backend: memory
adapters:
  matrix:
    main:
      enabled: true
      adapter_kind: fake
      homeserver: https://fake.local
      user_id: '@bot:fake.local'
      access_token: tok_main
      room_allowlist: ['!room:fake.local']
      encryption_mode: plaintext
  meshtastic:
    radio:
      enabled: true
      adapter_kind: fake
      connection_type: fake
routes:
  mapped:
    source_adapters: [radio]
    dest_adapters: [main]
    directionality: source_to_dest
    context_map:
      "0":
        dest_context: '!room0:fake.local'
      "1":
        dest_context: '!room1:fake.local'
"""


def _load_config(tmp_path: Path) -> Any:
    p = tmp_path / "config.yaml"
    p.write_text(_CONFIG)
    config, _source, _paths = load_config(str(p))
    return config


def _force_expansion_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "medre.config.route_expansion._context_map_token",
        lambda _ctx: "h_dead",
    )


def test_smoke_preflight_reports_route_expansion_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The smoke preflight summary carries the expansion failure."""
    _force_expansion_collision(monkeypatch)

    summary = _run_preflight(_load_config(tmp_path))

    assert summary["route_count"] == 1
    assert any(
        "collide on expansion token 'h_dead'" in e for e in summary["route_errors"]
    )


def test_evidence_route_section_reports_route_expansion_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The evidence route-validation section carries the expansion failure
    and surfaces as a partial section that is not valid."""
    _force_expansion_collision(monkeypatch)

    section = _collect_route_validation(_load_config(tmp_path))

    assert section["status"] == "partial"
    data = section["data"]
    assert data["route_count"] == 1
    assert any("collide on expansion token 'h_dead'" in e for e in data["route_errors"])
    assert data["valid"] is False
