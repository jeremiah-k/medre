# medre-test-tier: compatibility
"""Availability contracts independent of optional extras installed on the host."""

from __future__ import annotations

import builtins
import runpy
from pathlib import Path
from types import ModuleType

import pytest


@pytest.mark.parametrize("installed", [False, True])
def test_meshcore_guard_handles_sdk_availability(
    monkeypatch: pytest.MonkeyPatch,
    installed: bool,
) -> None:
    from medre.adapters.meshcore import compat

    original_import = builtins.__import__
    original_guard = compat.HAS_MESHCORE

    def import_sdk(name: str, *args, **kwargs):
        if name == "meshcore":
            if not installed:
                raise ModuleNotFoundError("meshcore unavailable", name=name)
            return ModuleType(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_sdk)
    # Execute an isolated module namespace: reloading the real guard would
    # leak synthetic availability into later adapter tests.
    namespace = runpy.run_path(str(Path(compat.__file__)))
    assert namespace["HAS_MESHCORE"] is installed
    assert compat.HAS_MESHCORE is original_guard


@pytest.mark.parametrize("installed", [False, True])
def test_adapters_command_reports_optional_sdk_availability(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    installed: bool,
) -> None:
    import medre.cli.config_commands as config_commands
    from medre.cli import main
    from medre.cli.config_commands import TRANSPORTS

    monkeypatch.setattr(
        config_commands, "is_transport_installed", lambda _name: installed
    )
    for name in ("MEDRE_HOME", "MEDRE_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    for name in (
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
    ):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))

    # Simulate dependency-probe outcomes without loading an SDK.
    try:
        main(["adapters"])
    except SystemExit as exc:
        assert exc.code in (None, 0)
    lines = capsys.readouterr().out.splitlines()
    for transport, distribution, names in TRANSPORTS:
        if names:
            status = "installed" if installed else "not installed"
            assert (
                f"  {transport:14s} SDK ({distribution or 'external'}): {status}"
                in lines
            )
        else:
            assert f"  {transport:14s} Python SDK: not required" in lines
