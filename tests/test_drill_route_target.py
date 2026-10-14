"""Tests for route-aware adapter failure drill target selection.

Proves that the adapter failure drills patch an adapter the injected
event can actually reach: the first sorted non-source adapter that is a
delivery target of an enabled route leg from the chosen source.

No Docker, network, or SDK dependencies.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from medre.runtime.drill import run_drill


@pytest.fixture(autouse=True)
def _clean_path_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "MEDRE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
    ):
        monkeypatch.delenv(var, raising=False)


def _write_drill_target_config(tmp_path: Path) -> str:
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


@pytest.mark.asyncio
async def test_adapter_permanent_failure_patches_route_target(
    tmp_path: Path,
) -> None:
    """Permanent-failure drill patches a route-reachable adapter.

    With the sorted-first source ``lxmf`` routing only to ``meshcore``,
    the drill must patch ``meshcore`` (not the sorted-first non-source
    adapter ``matrix``) so the injected failure is actually observed.
    """
    report = await run_drill(
        "adapter_permanent_failure",
        config_path=_write_drill_target_config(tmp_path),
    )
    assert report["status"] == "passed", report.get("fail_reasons", [])
    assert report["source_adapter"] == "lxmf"
    assert report["target_adapters"] == ["meshcore"]

    patch_step = next(
        (s for s in report["drill_steps"] if s["step"] == "patch_adapter"),
        None,
    )
    assert patch_step is not None
    assert patch_step["target_adapter"] == "meshcore"

    verify_step = next(
        (s for s in report["drill_steps"] if s["step"] == "verify_permanent_failure"),
        None,
    )
    assert verify_step is not None
    assert verify_step["observed"] == "permanent_failure"


@pytest.mark.asyncio
async def test_adapter_transient_failure_patches_route_target(
    tmp_path: Path,
) -> None:
    """Transient-failure drill patches a route-reachable adapter.

    Same topology as the permanent-failure regression: the patched
    adapter must be ``meshcore``, the sole delivery target of the
    sorted-first source ``lxmf``.
    """
    report = await run_drill(
        "adapter_transient_failure",
        config_path=_write_drill_target_config(tmp_path),
    )
    assert report["status"] == "passed", report.get("fail_reasons", [])
    assert report["source_adapter"] == "lxmf"
    assert report["target_adapters"] == ["meshcore"]

    patch_step = next(
        (s for s in report["drill_steps"] if s["step"] == "patch_adapter"),
        None,
    )
    assert patch_step is not None
    assert patch_step["target_adapter"] == "meshcore"

    verify_step = next(
        (s for s in report["drill_steps"] if s["step"] == "verify_transient_failure"),
        None,
    )
    assert verify_step is not None
    assert verify_step["observed"] == "transient_failure"
    assert report["recovery_path"]["recovery_simulated"] is True
