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
from tests.helpers.drill_configs import write_drill_target_config


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
        config_path=write_drill_target_config(tmp_path),
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
        config_path=write_drill_target_config(tmp_path),
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
