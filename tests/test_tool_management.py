# medre-test-tier: compatibility
"""Keep explicit tool changes scoped to the owning uv environment and sources."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from medre import tool_management as tm
from medre.cli.main import main


@pytest.fixture
def tool(tmp_path):
    return tm.ToolInstallation(
        "/bin/uv",
        tmp_path,
        {
            "requirements": [
                {
                    "name": "medre",
                    "git": "https://example.com/core.git?rev=stable#abcdef",
                },
                {"name": "community-radio", "extras": ["crypto"], "specifier": "==1.2"},
                {
                    "name": "other-dependency",
                    "specifier": ">=2,<3",
                    "marker": "python_version >= '3.11'",
                },
            ],
            "entrypoints": [{"name": "medre", "from": "medre"}],
        },
    )


def test_sources_use_medre_home_without_loading_runtime_config(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("invalid runtime: [")
    (tmp_path / "sources.yaml").write_text(
        "adapters:\n  matrix: custom-matrix==2\n  meshcore: null\n  radio: community-radio @ file:///tmp/radio.whl\n"
    )
    sources = tm.load_sources()
    assert sources["matrix"] == "custom-matrix==2"
    assert sources["radio"] == "community-radio @ file:///tmp/radio.whl"
    assert "meshcore" not in sources
    assert "meshtastic" in sources


def test_missing_default_sources_require_no_files(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    assert tm.load_sources() == tm.DEFAULT_SOURCES
    assert not list(tmp_path.iterdir())
    with pytest.raises(tm.ToolManagementError, match="does not exist"):
        tm.load_sources(tmp_path / "missing.yaml")


@pytest.mark.parametrize(
    "text",
    [
        "adapters: []",
        "core: medre",
        "adapters: {radio: --evil}",
        "adapters: {radio: medre==2}",
        "adapters: {radio: 42}",
        "adapters: {radio: first, radio: second}",
        "adapters: {'bad alias': package}",
        "adapters: {radio: 'git+https://example.com/radio.git'}",
    ],
)
def test_invalid_catalog_fails_before_any_process(text, tmp_path, monkeypatch):
    path = tmp_path / "sources.yaml"
    path.write_text(text)
    monkeypatch.setattr(
        tm.subprocess, "run", lambda *a, **k: pytest.fail("package manager called")
    )
    with pytest.raises(tm.ToolManagementError):
        tm.load_sources(path)


def test_install_preserves_core_ref_extras_markers_and_unrelated_packages(tool):
    command = tool.change_adapters(["matrix[e2e]"], tm.DEFAULT_SOURCES)
    assert command[-1] == "medre @ git+https://example.com/core.git@stable"
    assert "community-radio[crypto]==1.2" in command
    assert "other-dependency>=2,<3 ; python_version >= '3.11'" in command
    assert (
        "medre-adapter-matrix[e2e] @ git+https://github.com/jeremiah-k/medre-adapter-matrix.git@main"
        in command
    )
    assert command[:4] == ["/bin/uv", "tool", "install", "--python"]
    assert not any("pip" == arg for arg in command)


def test_remove_matches_provider_distribution_rather_than_transport(tool):
    command = tool.change_adapters(
        ["radio"],
        {"radio": "community-radio @ git+https://example.com/replacement.git"},
        remove=True,
    )
    assert "community-radio[crypto]==1.2" not in command
    assert "other-dependency>=2,<3 ; python_version >= '3.11'" in command
    with pytest.raises(tm.ToolManagementError, match="not an explicit dependency"):
        tool.change_adapters(["matrix"], tm.DEFAULT_SOURCES, remove=True)


def test_extra_selection_unions_source_extras():
    assert tm.selected_source(
        "matrix[e2e]", {"matrix": "provider[socks] @ file:///tmp/test.whl"}
    ) == (
        "provider",
        "provider[e2e,socks] @ file:///tmp/test.whl",
    )


@pytest.mark.parametrize(
    ("receipt", "expected"),
    [
        (
            {
                "name": "radio",
                "git": "ssh://git@example.com/radio?branch=feature%2Froute&subdirectory=python%2Fadapter#abcd",
            },
            "radio @ git+ssh://git@example.com/radio@feature/route#subdirectory=python/adapter",
        ),
        (
            {"name": "radio", "git": "https://example.com/radio?rev=abc123#abc123"},
            "radio @ git+https://example.com/radio@abc123",
        ),
        (
            {"name": "radio", "git": "https://example.com/radio#abc123"},
            "radio @ git+https://example.com/radio",
        ),
        (
            {"name": "radio", "directory": "/tmp/radio folder"},
            "radio @ file:///tmp/radio%20folder",
        ),
        (
            {
                "name": "radio",
                "url": "https://example.com/radio.zip",
                "subdirectory": "pkg",
            },
            "radio @ https://example.com/radio.zip#subdirectory=pkg",
        ),
        ({"name": "radio"}, "radio"),
        ("radio[extra]>=2", "radio[extra]>=2"),
    ],
)
def test_receipt_sources_round_trip_requested_intent(receipt, expected):
    assert tm._receipt_requirement(receipt) == expected


@pytest.mark.parametrize(
    "requirement",
    [
        {"name": "radio", "editable": "/tmp/radio"},
        {"name": "radio", "index": "https://private/simple"},
        {"name": "radio", "git": "https://example.com/radio?lfs=true"},
        {"name": "radio", "git": "https://example.com/radio?rev=first&rev=second"},
        {"name": "radio", "git": "https://example.com/radio?rev=first&branch=second"},
        {"name": "radio", "directory": "../radio"},
        {
            "name": "radio",
            "url": "https://example.com/radio.zip",
            "git": "https://example.com/radio",
        },
        {"name": "radio", "extras": "e2e"},
        {"name": "radio", "url": 123},
    ],
)
def test_unreplayable_requirements_are_not_silently_discarded(tool, requirement):
    tool.receipt["requirements"].append(requirement)
    with pytest.raises(tm.ToolManagementError):
        tool.change_adapters(["matrix"], tm.DEFAULT_SOURCES)


@pytest.mark.parametrize(
    "settings",
    [
        {"constraints": [{"name": "dependency", "specifier": "<2"}]},
        {"options": {"prerelease": "allow"}},
        {"entrypoints": [{"name": "extra", "from": "other-package"}]},
        {"unexpected": []},
    ],
)
def test_custom_receipt_settings_fail_closed_for_dependency_changes(tool, settings):
    tool.receipt.update(settings)
    with pytest.raises(tm.ToolManagementError):
        tool.change_adapters(["matrix"], tm.DEFAULT_SOURCES)
    assert tool.update() == ["/bin/uv", "tool", "upgrade", "medre"]


def test_receipt_cannot_redirect_core_to_another_package(tool):
    tool.receipt["requirements"][0] = {"name": "unrelated"}
    with pytest.raises(tm.ToolManagementError, match="Ambiguous"):
        tool.requirements()


def test_environment_detection_requires_the_actual_tool_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(tm.shutil, "which", lambda _: "/bin/uv")
    monkeypatch.setattr(tm.sys, "prefix", str(tmp_path / "project"))
    monkeypatch.setattr(
        tm.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=str(tmp_path)),
    )
    with pytest.raises(
        tm.ToolManagementError, match="not running from its persistent uv tool"
    ):
        tm.ToolInstallation.inspect()
    prefix = tmp_path / "medre"
    prefix.mkdir()
    (prefix / "uv-receipt.toml").write_text('[tool]\nrequirements = [{name="medre"}]\n')
    monkeypatch.setattr(tm.sys, "prefix", str(prefix))
    assert tm.ToolInstallation.inspect().prefix == prefix
    (prefix / "uv-receipt.toml").write_text("invalid")
    with pytest.raises(tm.ToolManagementError, match="valid MEDRE uv tool receipt"):
        tm.ToolInstallation.inspect()


def test_uv_missing_and_directory_failure_are_actionable(monkeypatch):
    monkeypatch.setattr(tm.shutil, "which", lambda _: None)
    with pytest.raises(tm.ToolManagementError, match="uv is not on PATH"):
        tm.ToolInstallation.inspect()
    monkeypatch.setattr(tm.shutil, "which", lambda _: "/bin/uv")
    monkeypatch.setattr(
        tm.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=2)
    )
    with pytest.raises(tm.ToolManagementError, match="uv tool dir"):
        tm.ToolInstallation.inspect()


def test_sources_cli_works_without_uv_or_installed_providers(
    tmp_path, monkeypatch, capsys
):
    from medre.cli import install_commands

    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    monkeypatch.setattr(
        install_commands, "load_sources", lambda path=None: tm.DEFAULT_SOURCES
    )
    monkeypatch.setattr(tm.shutil, "which", lambda _: None)
    main(["adapters", "sources"])
    assert "adapters:" in capsys.readouterr().out


@pytest.mark.parametrize(
    "args",
    [
        ["adapters", "install"],
        ["adapters", "remove"],
        ["adapters", "check", "matrix"],
        ["adapters", "sources", "--dry-run"],
        ["adapters", "--sources", "sources.yaml"],
        ["adapters", "install", "matrix", "--config", "bridge.yaml"],
    ],
)
def test_invalid_management_arguments_are_rejected(args):
    with pytest.raises(SystemExit) as exc:
        main(args)
    assert exc.value.code == 2


def test_dry_run_prints_complete_quoted_command_without_mutating(
    tool, tmp_path, monkeypatch, capsys
):
    from medre.cli import install_commands

    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    monkeypatch.setattr(tm.ToolInstallation, "inspect", lambda: tool)
    monkeypatch.setattr(
        install_commands, "load_sources", lambda path=None: tm.DEFAULT_SOURCES
    )
    monkeypatch.setattr(
        tm.subprocess, "run", lambda *a, **k: pytest.fail("mutation attempted")
    )
    main(["adapters", "install", "matrix", "--dry-run"])
    assert "community-radio[crypto]==1.2" in capsys.readouterr().out


def test_update_uses_native_uv_receipt_preserving_upgrade(tool, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(tm.ToolInstallation, "inspect", lambda: tool)
    seen = []
    monkeypatch.setattr(
        tm.subprocess,
        "run",
        lambda command, **kwargs: seen.append(command)
        or subprocess.CompletedProcess(command, 0),
    )
    main(["update"])
    assert seen == [["/bin/uv", "tool", "upgrade", "medre"]]


def test_uv_failure_is_returned_without_success_message(tool, monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(tm.ToolInstallation, "inspect", lambda: tool)
    monkeypatch.setattr(
        tm.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 7),
    )
    with pytest.raises(SystemExit) as exc:
        main(["update"])
    assert exc.value.code == 7
    assert "Installation complete" not in capsys.readouterr().out


def test_source_schema_accepts_catalog_and_disabled_defaults():
    import jsonschema

    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "docs/schemas/package-sources.schema.json"
        ).read_text()
    )
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(
        {
            "core": "medre[e2e] @ git+https://example.org/medre.git@main",
            "adapters": {
                **tm.DEFAULT_SOURCES,
                "community": "my-provider>=1",
                "meshcore": None,
            },
        },
        schema,
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"adapters": {}, "runtime": {}}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"adapters": {}, "core": "other-core"}, schema)


def test_adapter_selection_cannot_replace_core():
    with pytest.raises(tm.ToolManagementError, match="replace MEDRE core"):
        tm.selected_source("core", {"core": "MEDRE @ file:///tmp/other"})


def test_implicit_python_request_tracks_minor_version(tool):
    command = tool.change_adapters(["matrix"], tm.DEFAULT_SOURCES)
    assert (
        command[command.index("--python") + 1]
        == f"{sys.version_info.major}.{sys.version_info.minor}"
    )


@pytest.mark.parametrize(
    "args",
    [["update"], ["adapters", "install", "matrix"], ["adapters", "remove", "radio"]],
)
def test_windows_handoff_is_explicit_and_does_not_report_installation(
    tool, tmp_path, monkeypatch, capsys, args
):
    from medre.cli import install_commands

    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    monkeypatch.setattr(
        install_commands,
        "load_sources",
        lambda path=None: {**tm.DEFAULT_SOURCES, "radio": "community-radio"},
    )
    monkeypatch.setattr(tm.ToolInstallation, "inspect", lambda: tool)
    monkeypatch.setattr(install_commands.sys, "platform", "win32")
    monkeypatch.setattr(
        tm.subprocess,
        "run",
        lambda *a, **k: pytest.fail("running executable would be replaced"),
    )
    with pytest.raises(SystemExit) as exc:
        main(args)
    assert exc.value.code == 2
    output = capsys.readouterr()
    assert "No packages changed" in output.err
    assert "after medre.exe exits" in output.err
    assert "Installation complete" not in output.out
    assert "/bin/uv tool" in output.out


@pytest.mark.parametrize("text", ["", "42", "[]"])
def test_scalar_sources_are_rejected_by_strict_loader(text, tmp_path):
    source = tmp_path / "sources.yaml"
    source.write_text(text)
    with pytest.raises(tm.ToolManagementError, match="Invalid sources YAML"):
        tm.load_sources(source)


@pytest.mark.parametrize("field", ["git", "url"])
@pytest.mark.parametrize(
    "subdir", ["python/adapter&another=value", "python/#fragment", "path with spaces"]
)
def test_subdirectory_delimiters_fail_closed(tool, field, subdir):
    from urllib.parse import urlencode

    requirement = {"name": "subdir-provider"}
    if field == "git":
        requirement["git"] = "https://example.com/repo?" + urlencode(
            {"subdirectory": subdir}
        )
    else:
        requirement.update(url="https://example.com/package.zip", subdirectory=subdir)
    tool.receipt["requirements"].append(requirement)
    with pytest.raises(tm.ToolManagementError, match="subdirectory"):
        tool.change_adapters(["matrix"], tm.DEFAULT_SOURCES)


@pytest.mark.parametrize(
    "fragment", ["sha256=" + "a" * 64, "subdirectory=other", "egg=radio"]
)
def test_url_receipt_never_concatenates_two_fragments(tool, fragment):
    """A reconstructed requirement must not lose a hash or source option."""
    tool.receipt["requirements"].append(
        {
            "name": "radio",
            "url": f"https://example.com/radio.zip#{fragment}",
            "subdirectory": "python",
        }
    )
    with pytest.raises(tm.ToolManagementError, match="existing fragment"):
        tool.change_adapters(["matrix"], tm.DEFAULT_SOURCES)


def test_url_receipt_preserves_existing_fragment_without_subdirectory():
    receipt = {"name": "radio", "url": "https://example.com/radio.zip#sha256=abc"}
    assert tm._receipt_requirement(receipt) == (
        "radio @ https://example.com/radio.zip#sha256=abc"
    )


def test_python_request_survives_dependency_changes(tool):
    tool.receipt["python"] = "3.13"
    command = tool.change_adapters(["matrix"], tm.DEFAULT_SOURCES)
    assert command[command.index("--python") + 1] == "3.13"


@pytest.mark.parametrize(
    "settings",
    [
        {"requirements": []},
        {"requirements": "medre"},
        {"entrypoints": "medre"},
        {"entrypoints": [None]},
        {"python": ["3.13"]},
    ],
)
def test_malformed_receipts_fail_without_tracebacks(tool, settings):
    tool.receipt.update(settings)
    with pytest.raises(tm.ToolManagementError):
        tool.change_adapters(["matrix"], tm.DEFAULT_SOURCES)


def test_empty_inventory_explains_environment_and_installation(monkeypatch, capsys):
    from medre.cli import config_commands
    from medre.config.errors import ConfigNotFoundError
    from medre.config import loader

    monkeypatch.setattr(config_commands, "TRANSPORTS", ())

    def missing_config(*args, **kwargs):
        raise ConfigNotFoundError("missing")

    monkeypatch.setattr(loader, "load_config", missing_config)
    monkeypatch.setattr(
        tm.subprocess,
        "run",
        lambda *a, **k: pytest.fail("read-only inventory invoked uv"),
    )
    config_commands._adapters()
    output = capsys.readouterr().out
    assert "No usable adapter providers discovered in this Python environment" in output
    assert "medre adapters sources" in output
    assert "medre adapters install ALIAS" in output
    assert "before generating a sample" in output
    assert "run 'medre config sample'" not in output
