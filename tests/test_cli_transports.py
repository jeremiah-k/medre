"""SDK availability probing for the registry-driven CLI transport inventory."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from medre.cli.transports import is_transport_installed


@pytest.fixture()
def no_sdk_imports(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the probed import hook, recording every attempted module."""
    attempted: list[str] = []

    def _fail(name: str) -> object:
        attempted.append(name)
        raise ImportError(f"no module named {name!r}")

    monkeypatch.setattr(
        "medre.cli.transports.importlib", SimpleNamespace(import_module=_fail)
    )
    return attempted


def test_unknown_transport_is_not_installed() -> None:
    assert is_transport_installed("no-such-transport") is False


def test_transport_without_import_names_needs_no_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "medre.cli.transports.TRANSPORTS", [("sidecar", None, ())]
    )
    assert is_transport_installed("sidecar") is True


def test_transport_installed_when_any_import_name_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempted: list[str] = []

    def _import(name: str) -> object:
        attempted.append(name)
        if name == "second":
            return SimpleNamespace()
        raise ImportError(f"no module named {name!r}")

    monkeypatch.setattr(
        "medre.cli.transports.importlib", SimpleNamespace(import_module=_import)
    )
    monkeypatch.setattr(
        "medre.cli.transports.TRANSPORTS",
        [("sample", "sample-dist", ("first", "second"))],
    )

    assert is_transport_installed("sample") is True
    assert attempted == ["first", "second"]


def test_registered_transport_not_installed_without_sdk(
    monkeypatch: pytest.MonkeyPatch,
    no_sdk_imports: list[str],
) -> None:
    monkeypatch.setattr(
        "medre.cli.transports.TRANSPORTS", [("sample", "sample-dist", ("a", "b"))]
    )
    assert is_transport_installed("sample") is False
    assert no_sdk_imports == ["a", "b"]
