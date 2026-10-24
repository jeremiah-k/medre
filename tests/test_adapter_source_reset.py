"""Explicit source reset preserves requests and never guesses community defaults."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from medre import self_upgrade as upgrade
from medre.cli.main import main
from medre.tool_management import DEFAULT_SOURCES, ToolInstallation, ToolManagementError


@pytest.fixture
def installation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ToolInstallation:
    """Model a mixed development installation without importing any provider SDK."""
    monkeypatch.setattr(
        upgrade,
        "installed_adapters",
        lambda: {
            "medre-adapter-matrix": {"matrix"},
            "medre-adapter-meshtastic": {"meshtastic"},
            "community-radio": {"radio"},
        },
    )
    return ToolInstallation(
        "uv",
        tmp_path,
        {
            "python": "3.12",
            "requirements": [
                "medre @ git+https://github.com/jeremiah-k/medre.git@main",
                "medre-adapter-matrix[e2e,integration] @ git+https://github.com/jeremiah-k/medre-adapter-matrix.git@integration/continuity",
                "medre-adapter-meshtastic @ git+https://github.com/jeremiah-k/medre-adapter-meshtastic.git@candidate",
                "community-radio[crypto]==1.2",
                "unrelated-package>=2 ; python_version >= '3.12'",
            ],
            "entrypoints": [{"name": "medre", "from": "medre"}],
        },
    )


def test_selected_reset_retains_features_core_and_other_sources(installation):
    """A Matrix reset drops its candidate extra and keeps every unrelated request."""
    plan = upgrade.plan_adapter_reset(installation, selections=("matrix",))
    assert plan.packages == ("medre-adapter-matrix",)
    assert (
        "medre-adapter-matrix[e2e] @ git+https://github.com/jeremiah-k/medre-adapter-matrix.git@main"
        in plan.command
    )
    for requirement in installation.requirements():
        if not requirement.startswith("medre-adapter-matrix"):
            assert requirement in plan.command
    assert plan.command[-2:] == ("--upgrade-package", "medre-adapter-matrix")
    assert "integration/continuity" not in " ".join(plan.command)


def test_reset_all_only_selects_installed_first_party_providers(installation):
    """Defaults are a source catalog, not a request to install every provider."""
    plan = upgrade.plan_adapter_reset(installation)
    assert set(plan.packages) == {
        "medre-adapter-matrix",
        "medre-adapter-meshtastic",
    }
    assert DEFAULT_SOURCES["meshtastic"] in plan.command
    assert "community-radio[crypto]==1.2" in plan.command
    assert not any("medre-adapter-meshcore" in part for part in plan.command)
    assert "Community adapter sources remain unchanged." in plan.notes


@pytest.mark.parametrize("selection", ["radio", "missing", "matrix[integration]"])
def test_unknown_community_or_extra_selection_fails_closed(installation, selection):
    """Reset cannot invent a main repository or silently install a new adapter."""
    with pytest.raises(ToolManagementError):
        upgrade.plan_adapter_reset(installation, selections=(selection,))


def test_distribution_selection_and_duplicates_are_deterministic(installation):
    """Aliases and distribution names refer to one installed provider."""
    plan = upgrade.plan_adapter_reset(
        installation, selections=("matrix", "medre-adapter-matrix", "matrix")
    )
    assert plan.packages == ("medre-adapter-matrix",)


def test_no_first_party_providers_is_a_noop(installation, monkeypatch):
    """A community-only installation must not install the first-party defaults."""
    monkeypatch.setattr(upgrade, "installed_adapters", lambda: {"custom": {"matrix"}})
    plan = upgrade.plan_adapter_reset(installation)
    assert plan.command == ()
    with pytest.raises(ToolManagementError, match="No first-party default"):
        upgrade.plan_adapter_reset(installation, selections=("matrix",))


def test_dry_run_ignores_catalog_overrides_and_never_invokes_uv(
    installation, tmp_path, monkeypatch, capsys
):
    """Returning to defaults remains possible with a broken saved source catalog."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    source = tmp_path / "sources.yaml"
    source.write_text("invalid catalog: [")
    monkeypatch.setattr(ToolInstallation, "inspect", lambda: installation)
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: pytest.fail("dry-run changed packages")
    )
    main(["adapters", "reset", "matrix", "--dry-run"])
    output = capsys.readouterr().out
    assert "Reset adapter sources: medre-adapter-matrix" in output
    assert "integration/continuity" not in output
    assert "Source catalog overrides remain saved" in output
    assert source.read_text() == "invalid catalog: ["


def test_reset_retains_windows_executable_handoff(installation, monkeypatch, capsys):
    """Windows prints the complete source change for execution after MEDRE exits."""
    import medre.cli.install_commands as commands

    monkeypatch.setattr(ToolInstallation, "inspect", lambda: installation)
    monkeypatch.setattr(commands.sys, "platform", "win32")
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: pytest.fail("running executable replaced")
    )
    with pytest.raises(SystemExit) as caught:
        main(["adapters", "reset", "matrix"])
    assert caught.value.code == 2
    assert "No packages changed" in capsys.readouterr().err


@pytest.mark.parametrize("option", [["--sources", "custom.yaml"], ["--sync-sources"]])
def test_reset_rejects_conflicting_source_options(option, capsys):
    """Reset always selects host defaults, never an operator override."""
    with pytest.raises(SystemExit) as caught:
        main(["adapters", "reset", *option])
    assert caught.value.code == 2
    assert capsys.readouterr().err
