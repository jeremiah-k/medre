# medre-test-tier: compatibility
"""Source-agnostic external adapter installation preflight."""

from __future__ import annotations

from pathlib import Path

import pytest

from medre.adapter_installation import inspect_adapter_installation
from medre.cli.main import main
from medre.config.errors import ConfigFileError, ConfigValidationError


def _config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "bridge.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_preflight_reports_uninstalled_community_transport(tmp_path: Path) -> None:
    path = _config(
        tmp_path, "adapters:\n  future_radio:\n    node:\n      enabled: true\n"
    )
    report = inspect_adapter_installation(path)
    assert report.required == ("future_radio",)
    assert report.missing == ("future_radio",)


def test_preflight_reports_available_entry_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "medre.adapter_installation.registered_transports", lambda: ("matrix",)
    )
    path = _config(tmp_path, "adapters:\n  matrix:\n    bot:\n      enabled: true\n")
    report = inspect_adapter_installation(path)
    assert "matrix" in report.installed
    assert report.missing == ()


def test_preflight_includes_disabled_transport(tmp_path: Path) -> None:
    path = _config(
        tmp_path, "adapters:\n  custom_unused:\n    node:\n      enabled: false\n"
    )
    assert inspect_adapter_installation(path).missing == ("custom_unused",)


def test_preflight_rejects_invalid_type_without_leaking_values(tmp_path: Path) -> None:
    path = _config(tmp_path, "adapters:\n  ../../private:\n    token: secret-value\n")
    with pytest.raises(
        ConfigValidationError, match="invalid adapter transport name"
    ) as error:
        inspect_adapter_installation(path)
    assert "secret-value" not in str(error.value)


def test_cli_check_fails_actionably_when_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _config(tmp_path, "adapters:\n  new_transport:\n    node: {}\n")
    with pytest.raises(SystemExit) as error:
        main(["adapters", "check", "--config", str(path)])
    assert error.value.code != 0
    out = capsys.readouterr()
    assert "new_transport: missing" in out.out
    assert "PyPI, Git, or local paths" in out.err


def test_cli_check_succeeds_when_installed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "medre.adapter_installation.registered_transports", lambda: ("matrix",)
    )
    path = _config(tmp_path, "adapters:\n  matrix:\n    bot: {}\n")
    main(["adapters", "check", "--config", str(path)])
    assert "matrix: installed" in capsys.readouterr().out


@pytest.mark.parametrize("value", ["[]", "''", "false", "0"])
def test_preflight_rejects_empty_nonmapping_adapter_section(
    tmp_path: Path, value: str
) -> None:
    path = _config(tmp_path, f"adapters: {value}\n")
    with pytest.raises(ConfigValidationError, match="adapters must be a mapping"):
        inspect_adapter_installation(path)


def test_preflight_distinguishes_broken_from_missing(tmp_path, monkeypatch, capsys):
    from medre import adapter_registry
    from medre.adapter_registry import AdapterDiscoveryFailure, AdapterTypeRegistry

    failure = AdapterDiscoveryFailure(
        "private_chat", "broken_plugin:spec", "ImportError: unavailable"
    )
    monkeypatch.setattr(
        adapter_registry,
        "ADAPTER_REGISTRY",
        AdapterTypeRegistry((), failures=(failure,)),
    )
    path = _config(tmp_path, "adapters:\n  private_chat:\n    node: {}\n")
    report = inspect_adapter_installation(path)
    assert report.missing == ()
    assert report.broken == (failure,)
    with pytest.raises(SystemExit) as error:
        main(["adapters", "check", "--config", str(path)])
    assert error.value.code == 2
    assert "private_chat: broken" in capsys.readouterr().out
    with pytest.raises(SystemExit) as error:
        main(["--help"])
    assert error.value.code == 0


def test_preflight_treats_null_adapter_section_as_empty(tmp_path: Path) -> None:
    report = inspect_adapter_installation(_config(tmp_path, "adapters: null\n"))
    assert report.required == ()
    assert report.missing == ()


def test_preflight_rejects_nonmapping_instance_section(tmp_path: Path) -> None:
    with pytest.raises(ConfigValidationError, match="instances must be a mapping"):
        inspect_adapter_installation(_config(tmp_path, "adapters:\n  matrix: []\n"))


def test_preflight_does_not_reveal_unreadable_config_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _config(tmp_path, "adapters: {}\n")
    original = Path.read_text

    def unreadable(self: Path, *args: object, **kwargs: object) -> str:
        if self == path:
            raise PermissionError("access to private-token-string denied")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable)
    with pytest.raises(ConfigFileError, match="PermissionError") as error:
        inspect_adapter_installation(path)
    assert "private-token-string" not in str(error.value)
