"""Requested sources and resolved provenance remain distinct and uv-owned."""

import importlib.metadata
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


@pytest.mark.parametrize("json_output", [False, True])
def test_inventory_redacts_uppercase_catalog_and_receipt_urls(
    installation, tmp_path, monkeypatch, capsys, json_output
):
    raw = "medre @ HTTPS://user:secret@example.org/core.whl?token=hidden"
    installation.receipt["requirements"][0] = raw
    catalog_path = tmp_path / "sources.yaml"
    catalog_path.write_text(f"core: {raw}\nadapters: {{}}\n")
    monkeypatch.setattr(tm.ToolInstallation, "inspect", lambda: installation)
    args = ["sources", "show", "--sources", str(catalog_path)]
    main(args + (["--json"] if json_output else []))
    output = capsys.readouterr().out
    assert "secret" not in output and "hidden" not in output
    assert "example.org/core.whl?redacted" in output


@pytest.mark.parametrize("mode", ["dry-run", "linux", "win32"])
def test_switch_preview_redacts_both_sources_but_execution_keeps_arguments(
    installation, tmp_path, monkeypatch, capsys, mode
):
    previous = "medre @ HTTPS://user:oldsecret@example.org/old.whl?token=oldtoken"
    target = "medre @ https://user:newsecret@example.org/new.whl?token=newtoken"
    installation.receipt["requirements"][0] = previous
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    monkeypatch.setattr(tm.ToolInstallation, "inspect", lambda: installation)
    monkeypatch.setattr(tm.sys, "platform", "linux" if mode == "dry-run" else mode)
    commands = []
    monkeypatch.setattr(
        tm.subprocess,
        "run",
        lambda command, **kwargs: commands.append(command)
        or SimpleNamespace(returncode=0),
    )
    args = ["sources", "switch", "core", "--requirement", target]
    if mode == "win32":
        with pytest.raises(SystemExit) as exc:
            main(args)
        assert exc.value.code == 2
    else:
        main(args + (["--dry-run"] if mode == "dry-run" else []))
    output = capsys.readouterr()
    combined = output.out + output.err
    for secret in ("oldsecret", "oldtoken", "newsecret", "newtoken"):
        assert secret not in combined
    assert "not a copyable command" in combined
    assert "old.whl?redacted" in combined and "new.whl?redacted" in combined
    if mode == "linux":
        assert len(commands) == 1 and target in commands[0]
    else:
        assert not commands


def test_inventory_includes_uncataloged_provider_without_receipt(catalog, monkeypatch):
    def unmanaged():
        raise tm.ToolManagementError("not a managed tool")

    monkeypatch.setattr(tm.ToolInstallation, "inspect", unmanaged)
    monkeypatch.setattr(
        ps, "installed_adapters", lambda: {"community-radio": {"radio"}}
    )
    monkeypatch.setattr(
        ps.importlib.metadata,
        "distribution",
        lambda name: SimpleNamespace(version="2.0", read_text=lambda _: None),
    )
    row = next(
        row
        for row in ps.source_inventory(catalog)["packages"]
        if row["package"] == "community-radio"
    )
    assert row["configured"] == [] and row["requested"] is None
    assert row["installed_version"] == "2.0"
    assert row["source_label"] == "pypi"


@pytest.mark.parametrize("separator", [" ; ", " ;", "  ;  "])
def test_editable_replay_ignores_marker_separator_spacing(
    installation, tmp_path, separator
):
    installation.receipt["requirements"][1] = {
        "name": "medre-adapter-matrix",
        "editable": str(tmp_path),
        "marker": "python_version >= '3.11'",
    }
    raw = "medre-adapter-matrix[e2e] @ " + tmp_path.as_uri() + separator
    raw += "python_version >= '3.11'"
    command = installation.change_adapters(["matrix"], {"matrix": raw})
    assert command[command.index(raw) - 1] == "--with-editable"


def test_inline_version_preserves_catalog_marker_and_url_semicolon():
    name, raw = tm.selected_source(
        "matrix==1.0",
        {"matrix": "radio @ https://example.org/a;b.whl ;os_name == 'nt'"},
    )
    assert name == "radio"
    assert raw == "radio==1.0 ; os_name == 'nt'"


@pytest.mark.parametrize(
    "raw",
    [
        "unrelated @ GIT+HTTPS://example.org/provider@main",
        "unrelated @ git+https://example.org/" + "a" * 40,
    ],
)
def test_scoped_switch_refuses_case_variant_and_unpinned_hash_path(
    installation, catalog, raw
):
    installation.receipt["requirements"][2] = raw
    with pytest.raises(
        tm.ToolManagementError, match="cannot preserve the mutable source"
    ):
        ps.plan_source_switch(installation, catalog, ("core",), source="index")


def test_distribution_selection_takes_precedence_over_alias(installation):
    catalog = tm.SourceCatalog(None, {"medre-adapter-matrix": "unrelated"})
    plan = ps.plan_source_switch(
        installation, catalog, ("medre-adapter-matrix",), source="index"
    )
    assert plan.packages == ("medre-adapter-matrix",)
    assert "unrelated>=2 ; python_version >= '3.11'" in plan.command


def test_git_url_cannot_hide_ref_in_repository_option(installation, catalog):
    with pytest.raises(tm.ToolManagementError, match="select its revision"):
        ps.plan_source_switch(
            installation,
            catalog,
            ("core",),
            source="git",
            git_url="https://example.org/repo.git@main",
        )


def test_inventory_survives_broken_provider_metadata(
    installation, catalog, monkeypatch
):
    def broken():
        raise tm.ToolManagementError("Cannot read installed adapter metadata")

    monkeypatch.setattr(tm.ToolInstallation, "inspect", lambda: installation)
    monkeypatch.setattr(ps, "installed_adapters", broken)
    report = ps.source_inventory(catalog)
    assert report["provider_error"] == "Cannot read installed adapter metadata"
    assert any(row["package"] == "unrelated" for row in report["packages"])


@pytest.mark.parametrize(
    "error", [OSError("exec failed"), tm.subprocess.TimeoutExpired("uv", 15)]
)
def test_inventory_survives_unusable_uv(catalog, monkeypatch, error):
    monkeypatch.setattr(tm.shutil, "which", lambda _: "/bin/uv")

    def broken(*args, **kwargs):
        raise error

    monkeypatch.setattr(tm.subprocess, "run", broken)
    report = ps.source_inventory(catalog)
    assert "Cannot locate uv's tool directory" in report["receipt_error"]
    assert report["packages"]


def test_catalog_display_redacts_without_editing_source(tmp_path, capsys):
    path = tmp_path / "sources.yaml"
    raw = (
        "adapters:\n"
        "  radio: radio @ HTTPS://user:secret@example.org/radio.whl?token=hidden\n"
    )
    path.write_text(raw)
    main(["adapters", "sources", "--sources", str(path)])
    output = capsys.readouterr().out
    assert "secret" not in output and "hidden" not in output
    assert "edit the original catalog" in output
    assert path.read_text() == raw


def test_package_management_imports_do_not_discover_providers():
    script = """
import importlib.metadata
import sys
from types import SimpleNamespace

def load():
    raise AssertionError('loaded a provider')

importlib.metadata.entry_points = lambda **kw: [SimpleNamespace(
    name='radio', value='radio:factory', load=load,
    dist=SimpleNamespace(metadata={'Name': 'community-radio'}),
)]
from medre import package_sources as ps
from medre import tool_management as tm
from medre.cli import install_commands, command_display

def unavailable():
    raise tm.ToolManagementError('unmanaged')

tm.ToolInstallation.inspect = unavailable
assert 'medre.adapter_registry' not in sys.modules
assert any(row['package'] == 'community-radio' for row in
           ps.source_inventory(tm.SourceCatalog(None, {}))['packages'])
assert 'medre.adapter_registry' not in sys.modules
"""
    result = tm.subprocess.run(
        [tm.sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_switch_plan_redacts_source_notes_without_changing_request(
    installation, catalog
):
    previous = "medre @ HTTPS://user:oldsecret@example.org/old.whl?token=oldtoken"
    target = "medre @ https://user:newsecret@example.org/new.whl?token=newtoken"
    installation.receipt["requirements"][0] = previous
    plan = ps.plan_source_switch(installation, catalog, ("core",), requirement=target)
    assert target in plan.command
    notes = "\n".join(plan.notes)
    for secret in ("oldsecret", "oldtoken", "newsecret", "newtoken"):
        assert secret not in notes


def test_blank_direct_source_metadata_is_not_an_index_installation(
    installation, catalog, monkeypatch
):
    monkeypatch.setattr(tm.ToolInstallation, "inspect", lambda: installation)
    monkeypatch.setattr(
        ps.importlib.metadata,
        "distribution",
        lambda name: SimpleNamespace(version="2.0", read_text=lambda _: ""),
    )
    report = ps.source_inventory(catalog)
    core = next(row for row in report["packages"] if row["package"] == "medre")
    assert core["source"] is None
    assert core["source_label"] is None
    assert core["provenance_error"] == "Installed source metadata is unreadable."


def test_show_renders_installed_sources_with_shared_labels(
    installation, tmp_path, monkeypatch, capsys
):
    direct = {
        "url": "https://github.com/jeremiah-k/medre-adapter-matrix.git",
        "vcs_info": {
            "vcs": "git",
            "commit_id": "d2619ed8efa621984591d2a50466e4c32a952de6",
        },
    }
    dist = SimpleNamespace(
        version="0.1.1",
        read_text=lambda name: json.dumps(direct)
        if name == "direct_url.json"
        else None,
    )
    monkeypatch.setattr(ps.importlib.metadata, "distribution", lambda name: dist)
    monkeypatch.setattr(tm.ToolInstallation, "inspect", lambda: installation)
    catalog_path = tmp_path / "sources.yaml"
    catalog_path.write_text("core: medre\nadapters: {}\n")
    main(["sources", "show", "--sources", str(catalog_path)])
    out = capsys.readouterr().out
    assert (
        "installed source: github:jeremiah-k/medre-adapter-matrix@d2619ed8" in out
    )
    assert '"kind"' not in out and '"commit"' not in out

    main(["sources", "show", "--sources", str(catalog_path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    row = payload["packages"][0]
    assert row["source_label"] == "github:jeremiah-k/medre-adapter-matrix@d2619ed8"
    assert row["source"]["kind"] == "git"


def test_show_hides_installed_source_line_for_uninstalled_packages(
    installation, tmp_path, monkeypatch, capsys
):
    def missing(name: str) -> object:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(ps.importlib.metadata, "distribution", missing)
    monkeypatch.setattr(tm.ToolInstallation, "inspect", lambda: installation)
    catalog_path = tmp_path / "sources.yaml"
    catalog_path.write_text("core: medre\nadapters: {}\n")
    main(["sources", "show", "--sources", str(catalog_path)])
    out = capsys.readouterr().out
    assert "installed source:" not in out
    assert "not installed" in out


@pytest.mark.parametrize("mode", ["dry-run", "linux", "win32"])
@pytest.mark.parametrize("location", ["receipt", "target"])
def test_whitespace_credentials_are_rejected_before_output_or_execution(
    installation, tmp_path, monkeypatch, capsys, mode, location
):
    malformed = "medre @ https://user:two words@example.org/core.whl"
    if location == "receipt":
        installation.receipt["requirements"][0] = malformed
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    monkeypatch.setattr(tm.ToolInstallation, "inspect", lambda: installation)
    monkeypatch.setattr(tm.sys, "platform", "linux" if mode == "dry-run" else mode)
    monkeypatch.setattr(
        tm.subprocess, "run", lambda *a, **kw: pytest.fail("invalid URL reached uv")
    )
    args = [
        "sources",
        "switch",
        "core",
        "--requirement",
        malformed if location == "target" else "medre==0.1.0",
    ]
    with pytest.raises(SystemExit) as exc:
        main(args + (["--dry-run"] if mode == "dry-run" else []))
    assert exc.value.code == 2
    output = capsys.readouterr()
    assert "whitespace" in output.err
    assert "two words" not in output.out + output.err
    assert "user:" not in output.out + output.err


def test_redaction_hides_whole_malformed_url_even_without_requirement_validation():
    value = "medre @ HTTPS://user:two words@example.org/core.whl?token=secret"
    assert tm.redact_source(value) == "medre @ (invalid source URL)"


def test_valid_url_markers_and_escaped_path_spaces_remain_supported():
    value = "medre @ file:///tmp/source%20tree ; python_version >= '3.11'"
    assert tm.package_name(value) == "medre"
    assert tm.redact_source(value) == value
