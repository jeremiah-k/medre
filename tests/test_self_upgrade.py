"""Update intent remains separate from sources, discovery and uv ownership."""

from types import SimpleNamespace

import pytest

from medre import self_upgrade as upgrade
from medre import tool_management as tm
from medre.cli.main import main

READ_INSTALLED_ADAPTERS = upgrade.installed_adapters


@pytest.fixture
def tool(tmp_path):
    return tm.ToolInstallation(
        "/bin/uv",
        tmp_path,
        {
            "requirements": [
                {
                    "name": "medre",
                    "git": "https://example.org/core?rev=1111111111111111111111111111111111111111",
                },
                {
                    "name": "custom-matrix",
                    "extras": ["e2e"],
                    "git": "https://example.org/matrix?rev=2222222222222222222222222222222222222222",
                },
                {
                    "name": "other",
                    "specifier": "<3",
                    "marker": "python_version >= '3.11'",
                },
            ]
        },
    )


@pytest.fixture(autouse=True)
def providers(monkeypatch):
    monkeypatch.setattr(
        upgrade,
        "installed_adapters",
        lambda: {"custom-matrix": {"matrix"}, "radio": {"radio"}},
    )


def test_bulk_upgrade_preserves_receipt_options_without_replaying(tool):
    tool.receipt["options"] = {"resolution": "lowest-direct"}
    assert upgrade.plan_upgrade(tool).command == ("/bin/uv", "tool", "upgrade", "medre")


def test_core_only_retains_every_adapter_source_extra_and_marker(tool, monkeypatch):
    monkeypatch.setattr(
        upgrade,
        "installed_adapters",
        lambda: pytest.fail("core-only inspected providers"),
    )
    plan = upgrade.plan_upgrade(tool, core_only=True)
    assert plan.command[-2:] == ("--upgrade-package", "medre")
    assert (
        "custom-matrix[e2e] @ git+https://example.org/matrix@2222222222222222222222222222222222222222"
        in plan.command
    )
    assert "other<3 ; python_version >= '3.11'" in plan.command
    assert (
        "medre @ git+https://example.org/core@1111111111111111111111111111111111111111"
        in plan.command
    )
    assert "--upgrade" not in plan.command


def test_all_adapters_uses_metadata_including_transitive_providers(tool):
    plan = upgrade.plan_upgrade(tool, adapters_only=True)
    assert plan.packages == ("custom-matrix", "radio")
    assert plan.command[-4:] == (
        "--upgrade-package",
        "custom-matrix",
        "--upgrade-package",
        "radio",
    )
    assert plan.command[-1] != "medre"


def test_aliases_and_distribution_names_share_one_selection(tool):
    catalog = tm.SourceCatalog(None, {"room": "custom-matrix @ git+https://other/fork"})
    for selection in ["room", "matrix", "custom_matrix"]:
        assert upgrade.plan_upgrade(
            tool, selections=(selection,), catalog=catalog
        ).packages == ("custom-matrix",)
    assert upgrade.plan_upgrade(
        tool, selections=("room", "custom-matrix"), catalog=catalog
    ).packages == ("custom-matrix",)


def test_catalog_changes_are_explicit_and_keep_installed_extras(tool):
    catalog = tm.SourceCatalog(
        "medre @ git+https://new/core@stable",
        {"room": "custom-matrix[proxy] @ git+https://new/matrix@main"},
    )
    unchanged = upgrade.plan_upgrade(tool, selections=("room",), catalog=catalog)
    assert not any("https://new/" in arg for arg in unchanged.command)
    changed = upgrade.plan_upgrade(tool, catalog=catalog, sync_sources=True)
    assert "medre @ git+https://new/core@stable" in changed.command
    assert "custom-matrix[e2e,proxy] @ git+https://new/matrix@main" in changed.command
    assert "other<3 ; python_version >= '3.11'" in changed.command


def test_adapter_source_sync_cannot_redirect_core(tool):
    catalog = tm.SourceCatalog("medre @ git+https://new/core", {})
    plan = upgrade.plan_upgrade(
        tool, adapters_only=True, catalog=catalog, sync_sources=True
    )
    assert (
        "medre @ git+https://example.org/core@1111111111111111111111111111111111111111"
        in plan.command
    )
    assert not any("https://new/core" in arg for arg in plan.command)


def test_source_sync_does_not_install_catalog_only_providers(tool):
    catalog = tm.SourceCatalog(None, {"unused": "uninstalled @ git+https://new/repo"})
    assert not any(
        "uninstalled" in arg
        for arg in upgrade.plan_upgrade(
            tool, catalog=catalog, sync_sources=True
        ).command
    )


@pytest.mark.parametrize("selection", ["missing", "matrix[e2e]", "--evil", "medre"])
def test_invalid_or_uninstalled_selection_never_becomes_an_install(tool, selection):
    with pytest.raises(tm.ToolManagementError):
        upgrade.plan_upgrade(tool, selections=(selection,))


def test_conflicting_aliases_refuse_source_sync(tool):
    with pytest.raises(tm.ToolManagementError, match="Conflicting source"):
        upgrade.plan_upgrade(
            tool,
            sync_sources=True,
            catalog=tm.SourceCatalog(
                None, {"a": "custom-matrix==1", "b": "custom-matrix==2"}
            ),
        )


def test_selective_updates_refuse_custom_options_instead_of_losing_them(tool):
    tool.receipt["options"] = {"prerelease": "allow"}
    with pytest.raises(tm.ToolManagementError, match="custom uv settings"):
        upgrade.plan_upgrade(tool, core_only=True)


def test_no_adapters_is_a_successful_noop(tool, monkeypatch):
    monkeypatch.setattr(upgrade, "installed_adapters", lambda: {})
    assert upgrade.plan_upgrade(tool, adapters_only=True).command == ()


def test_provider_discovery_skips_core_owned_entry_points(monkeypatch):
    entries = [
        SimpleNamespace(name="smoke", dist=SimpleNamespace(metadata={"Name": "medre"})),
        SimpleNamespace(
            name="matrix", dist=SimpleNamespace(metadata={"Name": "custom.matrix"})
        ),
    ]
    monkeypatch.setattr(
        upgrade.importlib.metadata, "entry_points", lambda **kw: entries
    )
    assert READ_INSTALLED_ADAPTERS() == {"custom-matrix": {"matrix"}}
    with pytest.raises(tm.ToolManagementError, match="No installed adapter"):
        upgrade._selection(
            "medre", tm.SourceCatalog(None, {}), READ_INSTALLED_ADAPTERS()
        )


def test_provider_discovery_never_imports_a_native_sdk(monkeypatch):
    entry = SimpleNamespace(
        name="matrix",
        dist=SimpleNamespace(metadata={"Name": "custom.matrix"}),
        load=lambda: pytest.fail("SDK imported"),
    )
    monkeypatch.setattr(
        upgrade.importlib.metadata, "entry_points", lambda **kw: [entry]
    )
    assert READ_INSTALLED_ADAPTERS() == {"custom-matrix": {"matrix"}}


@pytest.mark.parametrize(
    "command",
    [
        ["update"],
        ["upgrade"],
        ["upgrade", "--core-only"],
        ["adapters", "upgrade"],
        ["adapters", "update", "matrix"],
    ],
)
def test_dry_run_has_no_mutation_and_aliases_share_planning(
    tool, tmp_path, monkeypatch, capsys, command
):
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    monkeypatch.setattr(tm.ToolInstallation, "inspect", lambda: tool)
    monkeypatch.setattr(
        tm.subprocess, "run", lambda *a, **k: pytest.fail("dry run changed packages")
    )
    main([*command, "--dry-run"])
    assert "/bin/uv tool" in capsys.readouterr().out


@pytest.mark.parametrize(
    "arguments",
    [
        ["upgrade", "--core-only", "matrix"],
        ["upgrade", "--core-only", "--adapters-only"],
        ["adapters", "sources", "--sync-sources"],
        ["upgrade", "--sources", "unused.yaml"],
    ],
)
def test_conflicting_flags_fail_before_ownership_inspection(monkeypatch, arguments):
    monkeypatch.setattr(
        tm.ToolInstallation, "inspect", lambda: pytest.fail("inspection called")
    )
    with pytest.raises(SystemExit) as error:
        main(arguments)
    assert error.value.code == 2


def test_adapter_check_rejects_selections_with_accurate_guidance(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["adapters", "check", "matrix"])
    assert exc.value.code == 2
    assert (
        "only install, remove, and upgrade accept selections" in capsys.readouterr().err
    )


def test_source_catalog_accepts_core_without_selecting_it_as_adapter(tmp_path):
    path = tmp_path / "sources.yaml"
    path.write_text("core: medre @ git+https://example.org/core@main\nadapters: {}\n")
    assert (
        tm.load_source_catalog(path).core == "medre @ git+https://example.org/core@main"
    )
    assert "medre" not in tm.load_sources(path)
    path.write_text("core: other-core\nadapters: {}\n")
    with pytest.raises(tm.ToolManagementError, match="core source"):
        tm.load_source_catalog(path)


def test_removed_alias_can_be_cleaned_up_by_distribution_name(tool):
    command = tool.change_adapters(["custom-matrix"], {}, remove=True)
    assert not any("custom-matrix" in arg for arg in command)


@pytest.mark.parametrize(
    "arguments",
    [
        ["upgrade", "--dry-run"],
        ["update", "--core-only", "--dry-run"],
        ["adapters", "sources"],
        ["adapters", "--dry-run", "upgrade"],
    ],
)
def test_package_management_does_not_load_adapter_cli_hooks(
    tool, tmp_path, monkeypatch, arguments
):
    from medre.cli import contrib

    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    monkeypatch.setattr(
        contrib,
        "register_adapter_contributors",
        lambda *a: pytest.fail("broken provider imported"),
    )
    monkeypatch.setattr(tm.ToolInstallation, "inspect", lambda: tool)
    monkeypatch.setattr(
        tm.subprocess, "run", lambda *a, **k: pytest.fail("mutation attempted")
    )
    main(arguments)


def test_bulk_repair_does_not_require_readable_provider_metadata(tool, monkeypatch):
    monkeypatch.setattr(
        upgrade,
        "installed_adapters",
        lambda: pytest.fail("metadata required to repair tool"),
    )
    assert upgrade.plan_upgrade(tool).command == tuple(tool.update())


@pytest.mark.parametrize(
    "raw",
    [
        {"name": "unselected", "git": "https://example.org/source?branch=main#abcdef"},
        {"name": "unselected", "directory": None},
        {"name": "unselected", "path": None},
        {"name": "unselected", "url": "file:///tmp/unselected.whl"},
        "unselected @ file:///tmp/unselected.whl",
        "unselected @ git+https://example.org/source@main",
    ],
)
def test_unselected_mutable_sources_refuse_scoped_changes_before_mutation(tool, raw):
    if isinstance(raw, dict):
        for source in ("directory", "path"):
            if raw.get(source, "") is None:
                raw[source] = str(tool.prefix / "source")
    tool.receipt["requirements"].append(raw)
    with pytest.raises(
        tm.ToolManagementError, match="cannot preserve the mutable source"
    ):
        upgrade.plan_upgrade(tool, core_only=True)


def test_pipx_and_project_ownership_guidance_use_interpreter_prefix(
    tmp_path, monkeypatch
):
    prefix = tmp_path / ".venv"
    prefix.mkdir()
    monkeypatch.setattr(tm.sys, "prefix", str(prefix))
    (tmp_path / "uv.lock").touch()
    assert "Detected a uv project" in tm._environment_guidance()
    (prefix / "pipx_metadata.json").touch()
    assert "pipx upgrade medre --include-injected" in tm._environment_guidance()


def test_source_inventory_includes_a_configured_core_without_installation(
    tmp_path, monkeypatch, capsys
):
    import yaml

    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    (tmp_path / "sources.yaml").write_text(
        "core: medre @ git+https://example.org/fork@main\nadapters: {}\n"
    )
    monkeypatch.setattr(
        tm.ToolInstallation,
        "inspect",
        lambda: pytest.fail("source inventory inspected installation"),
    )
    main(["adapters", "sources"])
    assert (
        yaml.safe_load(capsys.readouterr().out)["core"]
        == "medre @ git+https://example.org/fork@main"
    )
