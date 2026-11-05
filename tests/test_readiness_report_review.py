"""Adversarial offline readiness contracts: stable categories and no data leaks."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from medre import adapter_registry, dev_environment
from medre.adapter_registry import AdapterDiscoveryFailure, AdapterTypeRegistry
from medre.cli.readiness_commands import _routes_line
from medre.config.errors import (
    ConfigError,
    ConfigFileError,
    ConfigNotFoundError,
    ConfigValidationError,
)
from medre.config.loader import ConfigSource
from medre.runtime import readiness_report
from medre.tool_management import ToolManagementError


@pytest.fixture(autouse=True)
def isolated_config_discovery(monkeypatch, tmp_path: Path, isolated_config_env) -> None:
    """Keep real CLI calls away from developer and working-directory configs."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path / "private-state"))
    monkeypatch.chdir(tmp_path)


def _entry(**changes: Any) -> dict[str, Any]:
    value = {
        "transport": "matrix",
        "repository": "example/medre-adapter-matrix",
        "ref": "a" * 40,
        "distribution": "medre-adapter-matrix",
    }
    value.update(changes)
    return value


def test_missing_checkout_fallback_is_not_found(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(readiness_report, "_PACKAGE_ROOT", tmp_path)
    monkeypatch.setattr(dev_environment, "checkout_root", lambda: tmp_path)
    assert readiness_report._locate_manifest() is None
    status, providers = readiness_report._load_manifest()
    assert status == "not_found"
    assert providers == []


@pytest.mark.parametrize(
    "reason, expected",
    [
        ("ImportError: /secret/path", "ImportError"),
        ("module.CustomError: credentials", "module.CustomError"),
        ("/secret/private/path", "provider_error"),
        ("bad prefix: secret", "provider_error"),
        ("../../Bad: secret", "provider_error"),
        ("", "provider_error"),
    ],
)
def test_quarantined_provider_category_is_sanitized(
    monkeypatch, reason: str, expected: str
) -> None:
    monkeypatch.setattr(
        adapter_registry,
        "ADAPTER_REGISTRY",
        AdapterTypeRegistry(
            (), failures=(AdapterDiscoveryFailure("matrix", "provider:load", reason),)
        ),
    )
    assert readiness_report._registration_error_category("matrix") == expected


@pytest.mark.parametrize(
    "error, category",
    [
        (ConfigNotFoundError("/secret/path"), "config_not_found"),
        (ConfigFileError("/secret/path"), "config_file_error"),
        (ConfigError("/secret/path"), "config_error"),
    ],
)
def test_loaded_config_uses_the_same_closed_error_categories(
    monkeypatch, tmp_path: Path, error: ConfigError, category: str
) -> None:
    monkeypatch.setattr(
        readiness_report, "find_config", lambda _: (tmp_path / "bridge.yaml", None)
    )

    def broken_load(_: object) -> None:
        raise error

    monkeypatch.setattr(readiness_report, "load_config", broken_load)
    result, config = readiness_report._load_effective_config(None)
    assert config is None
    assert result["status"] == category
    assert result["error_category"] == category
    assert "/secret/path" not in json.dumps(result)


def test_route_plan_exception_has_closed_category_and_no_fabricated_counts(
    monkeypatch,
) -> None:
    def broken_plan(_: object) -> None:
        raise RuntimeError("/secret/native-id: bad route")

    monkeypatch.setattr(readiness_report, "build_route_plan", broken_plan)
    result = readiness_report._routes_section(SimpleNamespace())
    assert result == {
        "status": "blocked",
        "error_category": "route_expansion_error",
        "total": None,
        "enabled": None,
        "expansion_failures": None,
        "conflicts": None,
    }
    assert "counts unavailable" in _routes_line(result)
    assert "/secret" not in json.dumps(result)


@pytest.mark.parametrize(
    "entries",
    [
        [],
        [_entry(transport="BAD NAME")],
        [_entry(ref="abc")],
        [_entry(repository="missing-slash")],
        [_entry(distribution="foo bar")],
        [_entry(), _entry()],
        [_entry(extras=["e2e"])],
        [_entry(python_requires=313)],
        [_entry(local_integration="false")],
        [_entry(), None],
        {"unexpected": "mapping"},
    ],
)
def test_malformed_manifest_rejected_by_both_authorities(
    monkeypatch, tmp_path: Path, entries: Any
) -> None:
    path = tmp_path / ".github" / "adapter-compatibility.json"
    path.parent.mkdir()
    path.write_text(json.dumps(entries), encoding="utf-8")
    monkeypatch.setattr(readiness_report, "_locate_manifest", lambda: path)
    assert readiness_report._load_manifest() == ("malformed", [])
    with pytest.raises(ToolManagementError):
        dev_environment._snapshot_sources(tmp_path)


def test_manifest_does_not_borrow_pins_from_unrelated_checkout(
    monkeypatch, tmp_path: Path
) -> None:
    installed = tmp_path / "installed"
    foreign = tmp_path / "foreign"
    snapshot = foreign / ".github" / "adapter-compatibility.json"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(json.dumps([_entry()]), encoding="utf-8")
    monkeypatch.setattr(readiness_report, "_PACKAGE_ROOT", installed)
    monkeypatch.setattr(dev_environment, "checkout_root", lambda: foreign)
    assert readiness_report._load_manifest() == ("not_found", [])


def test_manifest_removed_after_lookup_is_not_found(
    monkeypatch, tmp_path: Path
) -> None:
    absent = tmp_path / ".github" / "adapter-compatibility.json"
    monkeypatch.setattr(readiness_report, "_locate_manifest", lambda: absent)
    assert readiness_report._load_manifest() == ("not_found", [])


def test_config_source_survives_second_explicit_lookup(
    monkeypatch, tmp_path: Path
) -> None:
    selected = tmp_path / "bridge.yaml"
    monkeypatch.setattr(
        readiness_report,
        "find_config",
        lambda _: (selected, ConfigSource.MEDRE_CONFIG),
    )
    monkeypatch.setattr(
        readiness_report,
        "load_config",
        lambda path: (SimpleNamespace(), ConfigSource.EXPLICIT, object()),
    )
    monkeypatch.setattr(
        readiness_report, "apply_env_overrides", lambda config, paths: config
    )
    section, _config = readiness_report._load_effective_config(None)
    assert section["status"] == "valid"
    assert section["source"] == ConfigSource.MEDRE_CONFIG.value


@pytest.mark.parametrize(
    "unsafe_path, expected",
    [
        ("adapters.matrix.secret/identity.value", "adapters.matrix.instance"),
        ("adapters.matrix.agent@example.com", "adapters.matrix.instance"),
        ("adapters.private_token_123.instance", "adapters"),
        ("routes./secret/private", "routes.route"),
        ("logging.overrides.private.key", "logging.overrides"),
        ("/secret/native/identity", "section"),
    ],
)
def test_validation_location_never_echoes_operator_key(
    monkeypatch, unsafe_path: str, expected: str
) -> None:
    monkeypatch.setattr(
        readiness_report,
        "get_adapter_spec",
        lambda transport: object() if transport == "matrix" else None,
    )

    def broken_find(_: object) -> None:
        raise ConfigValidationError("private text", section_path=unsafe_path)

    monkeypatch.setattr(readiness_report, "find_config", broken_find)
    section, config = readiness_report._load_effective_config(None)
    assert config is None
    assert section["section_path"] == expected
    assert "secret" not in json.dumps(section)
    assert "private" not in json.dumps(section)


def test_guidance_reports_quarantined_provider_without_a_blocked_adapter() -> None:
    manifest = {
        "status": "available",
        "providers": [
            {
                "registration": "broken",
                "distribution": "medre-adapter-matrix",
                "distribution_status": "installed",
            }
        ],
    }
    steps = readiness_report._guidance(manifest, {"status": "valid"}, [], None)
    assert any("adapters check" in step for step in steps)
    assert not any("looks complete" in step for step in steps)


def test_guidance_directs_missing_sdk_and_pure_preparation_separately() -> None:
    manifest = {"status": "available", "providers": []}
    rows = [
        {"preflight": "blocked", "blocked_category": "sdk_distribution_missing"},
        {"preflight": "blocked", "blocked_category": "preparation_failed"},
    ]
    steps = readiness_report._guidance(manifest, {"status": "valid"}, rows, None)
    assert any("SDK distributions" in step for step in steps)
    assert any("provider-owned configuration preparation" in step for step in steps)
    assert not any("adapters check" in step for step in steps)


def test_readiness_parser_skips_provider_cli_loading(monkeypatch, capsys) -> None:
    from importlib import import_module

    main_module = import_module("medre.cli.main")

    original = main_module._build_parser
    seen = []

    def recorded_parser(*, include_adapter_cli: bool = True):
        seen.append(include_adapter_cli)
        return original(include_adapter_cli=include_adapter_cli)

    monkeypatch.setattr(main_module, "_build_parser", recorded_parser)
    main_module.main(["readiness", "--json"])
    assert seen == [False]
    assert json.loads(capsys.readouterr().out)["boundary"]["offline"] is True


def test_readiness_never_executes_provider_cli_contributors(
    monkeypatch, capsys
) -> None:
    from importlib import import_module

    main_module = import_module("medre.cli.main")
    contributors = import_module("medre.cli.contrib")

    def forbidden_registration(_subparsers: object) -> None:
        raise AssertionError("readiness loaded provider CLI contributions")

    monkeypatch.setattr(
        contributors, "register_adapter_contributors", forbidden_registration
    )
    main_module.main(["readiness", "--json"])
    assert json.loads(capsys.readouterr().out)["boundary"]["offline"] is True


def test_installed_unregistered_provider_prevents_complete_guidance() -> None:
    manifest = {
        "status": "available",
        "providers": [
            {
                "registration": "unregistered",
                "distribution": "medre-adapter-matrix",
                "distribution_status": "installed",
            }
        ],
    }
    steps = readiness_report._guidance(manifest, {"status": "valid"}, [], None)
    assert any("no registered adapter entry point" in step for step in steps)
    assert not any("looks complete" in step for step in steps)
