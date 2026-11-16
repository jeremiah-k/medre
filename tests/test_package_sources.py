"""Requested sources and resolved provenance remain distinct and uv-owned."""

import json
from types import SimpleNamespace

import pytest

from medre import package_sources as ps
from medre import tool_management as tm
from medre.cli.main import main


@pytest.fixture
def installation(tmp_path):
    return tm.ToolInstallation(
        "/bin/uv",
        tmp_path,
        {
            "requirements": [
                {"name": "medre", "specifier": "==0.1.0"},
                {
                    "name": "medre-adapter-matrix",
                    "extras": ["e2e"],
                    "specifier": "==0.1.0",
                },
                {
                    "name": "unrelated",
                    "specifier": ">=2",
                    "marker": "python_version >= '3.11'",
                },
            ]
        },
    )


@pytest.fixture
def catalog():
    return tm.SourceCatalog(None, tm.DEFAULT_SOURCES)


def test_defaults_are_index_requirements_and_legacy_git_reset_is_explicit(catalog):
    assert catalog.adapters["matrix"] == "medre-adapter-matrix"
    assert tm.GIT_SOURCES["matrix"].endswith("@main")
    assert tm.DEFAULT_CORE_SOURCE == "medre"


@pytest.mark.parametrize(
    "selection,expected",
    [
        ("matrix[e2e]==0.1.0", "medre-adapter-matrix[e2e]==0.1.0"),
        ("matrix>=0.1,<0.2", "medre-adapter-matrix>=0.1,<0.2"),
        ("third-party-provider==2.0", "third-party-provider==2.0"),
    ],
)
def test_inline_versions_select_index_requirements(selection, expected):
    _, raw = tm.selected_source(selection, tm.GIT_SOURCES)
    assert raw == expected


def test_switch_to_git_retains_extras_python_and_unrelated_request(
    installation, catalog
):
    plan = ps.plan_source_switch(
        installation, catalog, ("matrix",), source="git", ref="feature/replies"
    )
    assert (
        "medre-adapter-matrix[e2e] @ git+https://github.com/jeremiah-k/medre-adapter-matrix.git@feature/replies"
        in plan.command
    )
    assert "medre==0.1.0" in plan.command
    assert "unrelated>=2 ; python_version >= '3.11'" in plan.command
    assert plan.packages == ("medre-adapter-matrix",)


def test_custom_git_uses_remote_head_or_explicit_immutable_commit(
    installation, catalog
):
    for ref in [None, "a" * 40]:
        plan = ps.plan_source_switch(
            installation,
            catalog,
            ("core",),
            source="git",
            git_url="https://example.org/fork.git",
            ref=ref,
        )
        expected = "medre @ git+https://example.org/fork.git" + (
            "@" + ref if ref else ""
        )
        assert expected in plan.command


def test_all_switches_only_existing_first_party_requests(installation, catalog):
    plan = ps.plan_source_switch(
        installation, catalog, all_packages=True, source="pypi"
    )
    assert plan.packages == ("medre", "medre-adapter-matrix")
    assert "medre" in plan.command and "medre-adapter-matrix[e2e]" in plan.command
    assert not any("meshtastic" in value for value in plan.command)
    assert "unrelated>=2 ; python_version >= '3.11'" in plan.command


def test_switch_to_index_version_retains_features(installation, catalog):
    plan = ps.plan_source_switch(
        installation, catalog, ("matrix",), source="index", version="0.1.1"
    )
    assert "medre-adapter-matrix[e2e]==0.1.1" in plan.command


def test_explicit_requirement_can_select_a_release_range_or_archive(
    installation, catalog
):
    for raw in [
        "medre-adapter-matrix>=0.1,<0.2",
        "medre-adapter-matrix @ https://example.org/provider.whl",
    ]:
        plan = ps.plan_source_switch(
            installation, catalog, ("matrix",), requirement=raw
        )
        assert (
            raw.replace("medre-adapter-matrix", "medre-adapter-matrix[e2e]")
            in plan.command
        )


def test_local_editable_and_snapshot_switches_preserve_mode(
    installation, catalog, tmp_path
):
    project = tmp_path / "fork with spaces"
    project.mkdir()
    plan = ps.plan_source_switch(
        installation,
        catalog,
        ("matrix",),
        source="local",
        path=str(project),
        editable=True,
    )
    assert "--with-editable" in plan.command
    assert "medre-adapter-matrix[e2e] @ " + project.as_uri() in plan.command
    installation.receipt["requirements"][1] = {
        "name": "medre-adapter-matrix",
        "extras": ["e2e"],
        "editable": str(project),
    }
    assert "--with-editable" in installation.install_request(
        installation.requirements()
    )
    with_features = installation.requirements()
    with_features[1] = with_features[1].replace("[e2e]", "[e2e,proxy]")
    assert "--with-editable" in installation.install_request(with_features)
    snapshot = ps.plan_source_switch(
        installation, catalog, ("matrix",), source="local", path=str(project)
    )
    assert "--with-editable" not in snapshot.command
    to_index = ps.plan_source_switch(installation, catalog, ("matrix",), source="index")
    assert "--with-editable" not in to_index.command


def test_core_local_editable_uses_primary_uv_option(installation, catalog, tmp_path):
    plan = ps.plan_source_switch(
        installation,
        catalog,
        ("core",),
        source="local",
        path=str(tmp_path),
        editable=True,
    )
    assert "--editable" in plan.command
    assert "--with-editable" not in plan.command


def test_scoped_switch_refuses_unselected_moving_source(installation, catalog):
    installation.receipt["requirements"][0] = {
        "name": "medre",
        "git": "https://example.org/core.git?branch=main#abc",
    }
    with pytest.raises(
        tm.ToolManagementError, match="cannot preserve the mutable source"
    ):
        ps.plan_source_switch(installation, catalog, ("matrix",), source="index")
    assert ps.plan_source_switch(
        installation, catalog, all_packages=True, source="index"
    )


@pytest.mark.parametrize(
    "selections,options",
    [
        ((), {"source": "git"}),
        (("core", "core"), {"source": "index"}),
        (("meshtastic",), {"source": "index"}),
        (("core",), {"source": "index", "ref": "main"}),
        (("core",), {"source": "index", "version": "1 --evil"}),
        (("core",), {"source": "git", "path": "/tmp"}),
        (
            ("core",),
            {"source": "git", "git_url": "https://user:secret@example.org/repo"},
        ),
        (("core",), {"source": "git", "ref": "main\n--evil"}),
        (("core",), {"source": "local", "path": "/path/that/does/not/exist"}),
        (("core",), {"requirement": "other-package==1"}),
        (("core",), {"source": "git", "requirement": "medre"}),
        (("core", "matrix"), {"source": "index", "version": "1.0"}),
    ],
)
def test_invalid_switches_do_not_invoke_uv(
    installation, catalog, selections, options, monkeypatch
):
    monkeypatch.setattr(
        tm.subprocess, "run", lambda *a, **kw: pytest.fail("uv invoked")
    )
    with pytest.raises(tm.ToolManagementError):
        ps.plan_source_switch(installation, catalog, selections, **options)


def test_conditional_requirement_marker_survives_source_replacement(
    installation, catalog
):
    installation.receipt["requirements"][1]["marker"] = "python_version >= '3.12'"
    plan = ps.plan_source_switch(installation, catalog, ("matrix",), source="git")
    assert any(raw.endswith(" ; python_version >= '3.12'") for raw in plan.command)


def test_dry_run_leaves_catalog_and_packages_unchanged(
    installation, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    target = tmp_path / "sources.yaml"
    content = "# deliberate catalog\nadapters: {}\n"
    target.write_text(content)
    monkeypatch.setattr(tm.ToolInstallation, "inspect", lambda: installation)
    monkeypatch.setattr(
        tm.subprocess, "run", lambda *a, **kw: pytest.fail("uv invoked")
    )
    main(["sources", "switch", "matrix", "--from", "git", "--dry-run"])
    assert target.read_text() == content
    assert "uv tool install" in capsys.readouterr().out


def test_inventory_distinguishes_request_from_resolved_commit(
    installation, catalog, monkeypatch
):
    monkeypatch.setattr(tm.ToolInstallation, "inspect", lambda: installation)
    monkeypatch.setattr(
        ps.importlib.metadata,
        "distribution",
        lambda name: SimpleNamespace(
            version="0.1.0",
            read_text=lambda _: json.dumps(
                {
                    "url": "https://user:secret@example.org/fork.git?token=hidden",
                    "vcs_info": {"commit_id": "a" * 40},
                }
            ),
        ),
    )
    report = ps.source_inventory(catalog)
    core = next(row for row in report["packages"] if row["package"] == "medre")
    assert core["configured"] == ["medre"]
    assert core["requested"] == "medre==0.1.0"
    assert core["source"]["commit"] == "a" * 40
    assert "secret" not in json.dumps(report) and "hidden" not in json.dumps(report)


def test_inventory_works_without_managed_environment(catalog, monkeypatch):
    def unmanaged():
        raise tm.ToolManagementError("not a managed tool")

    monkeypatch.setattr(tm.ToolInstallation, "inspect", unmanaged)
    report = ps.source_inventory(catalog)
    assert report["receipt_error"] == "not a managed tool"
    assert len(report["packages"]) == 8


def test_unselected_moving_sources_require_explicit_refresh_permission(
    installation, catalog
):
    installation.receipt["requirements"][0] = {
        "name": "medre",
        "git": "https://example.org/core.git?branch=main",
    }
    plan = ps.plan_source_switch(
        installation, catalog, ("matrix",), source="index", allow_refresh=True
    )
    assert "medre @ git+https://example.org/core.git@main" in plan.command
    assert any("--allow-refresh" in note for note in plan.notes)
    installation.receipt["options"] = {"constraints": ["private.txt"]}
    with pytest.raises(tm.ToolManagementError, match="custom uv settings"):
        ps.plan_source_switch(
            installation, catalog, ("matrix",), source="index", allow_refresh=True
        )


def test_core_extra_selection_and_explicit_markers_are_preserved(installation, catalog):
    installation.receipt["requirements"][0]["marker"] = "python_version >= '3.11'"
    plan = ps.plan_source_switch(
        installation, catalog, ("core[dev]",), requirement="medre==0.1.0"
    )
    assert "medre[dev]==0.1.0 ; python_version >= '3.11'" in plan.command


def test_inventory_retains_conflicting_alias_choices(installation, monkeypatch):
    monkeypatch.setattr(tm.ToolInstallation, "inspect", lambda: installation)
    report = ps.source_inventory(
        tm.SourceCatalog(None, {"a": "radio==1", "b": "radio==2"})
    )
    row = next(row for row in report["packages"] if row["package"] == "radio")
    assert row["configured"] == ["radio==1", "radio==2"]


@pytest.mark.parametrize(
    "args",
    [
        ["sources", "switch", "core", "--from", "index"],
        ["sources", "switch", "matrix", "--from", "git"],
    ],
)
def test_switch_cli_returns_uv_failure_without_reporting_success(
    installation, tmp_path, monkeypatch, capsys, args
):
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    monkeypatch.setattr(tm.ToolInstallation, "inspect", lambda: installation)
    monkeypatch.setattr(tm.sys, "platform", "linux")
    monkeypatch.setattr(
        tm.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=7)
    )
    with pytest.raises(SystemExit) as exc:
        main(args)
    assert exc.value.code == 7
    assert "installation complete" not in capsys.readouterr().out.lower()


def test_switch_windows_prints_command_without_executing(
    installation, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    monkeypatch.setattr(tm.ToolInstallation, "inspect", lambda: installation)
    monkeypatch.setattr(tm.sys, "platform", "win32")
    monkeypatch.setattr(
        tm.subprocess, "run", lambda *a, **kw: pytest.fail("Windows mutation")
    )
    with pytest.raises(SystemExit) as exc:
        main(["sources", "switch", "core", "--from", "index"])
    assert exc.value.code == 2
    assert "after medre.exe exits" in capsys.readouterr().err


def test_inline_adapter_versions_cannot_replace_core():
    with pytest.raises(tm.ToolManagementError, match="replace MEDRE core"):
        tm.selected_source("medre==0.1.0", tm.DEFAULT_SOURCES)


@pytest.mark.parametrize(
    "options",
    [
        {"source": "git", "git_url": "https://[invalid/repo"},
        {"source": "local", "path": "invalid\x00path"},
    ],
)
def test_bad_source_locations_fail_with_management_errors(
    installation, catalog, options
):
    with pytest.raises(tm.ToolManagementError):
        ps.plan_source_switch(installation, catalog, ("core",), **options)
