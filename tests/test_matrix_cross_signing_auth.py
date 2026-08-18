"""Authenticated Matrix cross-signing bootstrap and CLI tests."""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from medre.adapters.matrix.auth import MatrixLoginResult
from medre.adapters.matrix.e2ee_bootstrap import bootstrap_login_cross_signing
from medre.adapters.matrix.errors import MatrixConnectionError
from medre.adapters.matrix.identity import MatrixCrossSigningDiagnostics
from medre.cli.contrib import register_builtin_contributors


def _login_result() -> MatrixLoginResult:
    return MatrixLoginResult(
        homeserver="https://matrix.example.com",
        user_id="@bot:example.com",
        device_id="DEVICE",
        access_token="secret-token",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    register_builtin_contributors(subparsers)
    return parser


class _FakeBootstrapClient:
    def __init__(self) -> None:
        self.logged_in = False
        self.olm = object()
        self.store = object()
        self.restore_login = MagicMock(side_effect=self._restore)
        self.close = AsyncMock()

    def _restore(self, **kwargs: str) -> None:
        self.logged_in = True
        self.user_id = kwargs["user_id"]
        self.device_id = kwargs["device_id"]
        self.access_token = kwargs["access_token"]


class _FakeNioModule:
    def __init__(self, client: _FakeBootstrapClient) -> None:
        self.client = client
        self.config_calls: list[bool] = []
        self.client_calls: list[dict[str, object]] = []

    def AsyncClientConfig(self, *, encryption_enabled: bool):
        self.config_calls.append(encryption_enabled)
        return SimpleNamespace(encryption_enabled=encryption_enabled)

    def AsyncClient(self, **kwargs: object) -> _FakeBootstrapClient:
        self.client_calls.append(kwargs)
        return self.client


async def test_bootstrap_prepares_runtime_store_and_verifies_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import medre.adapters.matrix.e2ee_bootstrap as bootstrap_mod

    client = _FakeBootstrapClient()
    nio = _FakeNioModule(client)
    store_path = tmp_path / "store"
    observed: dict[str, object] = {}

    class FakeService:
        def __init__(self, provider: object, *, logger=None) -> None:
            observed["provider"] = provider

        async def reconcile(self, **kwargs: object) -> str:
            observed["kwargs"] = kwargs
            return "uploaded_and_signed"

        def diagnostics(self) -> MatrixCrossSigningDiagnostics:
            return MatrixCrossSigningDiagnostics(
                provider_supported=True,
                local_identity_present=True,
                server_identity_present=True,
                current_device_self_signed=True,
                chain_status="valid",
            )

    monkeypatch.setattr(bootstrap_mod._compat_mod, "HAS_NIO", True)
    monkeypatch.setattr(bootstrap_mod._compat_mod, "HAS_E2EE", True)
    monkeypatch.setattr(bootstrap_mod, "matrix_store_path_for_adapter", lambda _: store_path)
    monkeypatch.setattr(bootstrap_mod, "MatrixCrossSigningService", FakeService)
    monkeypatch.setitem(sys.modules, "nio", nio)  # type: ignore[arg-type]

    result = await bootstrap_login_cross_signing(
        _login_result(),
        "fresh-password",
        adapter_id="main",
    )

    assert store_path.is_dir()
    assert nio.config_calls == [True]
    assert nio.client_calls[0]["store_path"] == str(store_path)
    assert client.restore_login.call_args.kwargs == {
        "user_id": "@bot:example.com",
        "device_id": "DEVICE",
        "access_token": "secret-token",
    }
    assert observed["kwargs"] == {
        "password": "fresh-password",
        "allow_bootstrap": True,
        "reset": False,
    }
    assert result.provider_result == "uploaded_and_signed"
    assert result.diagnostics.chain_status == "valid"
    client.close.assert_awaited_once()


async def test_bootstrap_propagates_explicit_reset_to_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import medre.adapters.matrix.e2ee_bootstrap as bootstrap_mod

    client = _FakeBootstrapClient()
    nio = _FakeNioModule(client)
    observed: dict[str, object] = {}

    class FakeService:
        def __init__(self, provider: object, *, logger=None) -> None:
            pass

        async def reconcile(self, **kwargs: object) -> str:
            observed.update(kwargs)
            return "uploaded_and_signed"

        def diagnostics(self) -> MatrixCrossSigningDiagnostics:
            return MatrixCrossSigningDiagnostics(
                provider_supported=True,
                local_identity_present=True,
                server_identity_present=True,
                current_device_self_signed=True,
                chain_status="valid",
            )

    monkeypatch.setattr(bootstrap_mod._compat_mod, "HAS_NIO", True)
    monkeypatch.setattr(bootstrap_mod._compat_mod, "HAS_E2EE", True)
    monkeypatch.setattr(
        bootstrap_mod, "matrix_store_path_for_adapter", lambda _: tmp_path / "store"
    )
    monkeypatch.setattr(bootstrap_mod, "MatrixCrossSigningService", FakeService)
    monkeypatch.setitem(sys.modules, "nio", nio)  # type: ignore[arg-type]

    await bootstrap_login_cross_signing(
        _login_result(),
        "fresh-password",
        adapter_id="main",
        reset=True,
    )

    assert observed["password"] == "fresh-password"
    assert observed["allow_bootstrap"] is True
    assert observed["reset"] is True


async def test_bootstrap_requires_e2ee_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    import medre.adapters.matrix.e2ee_bootstrap as bootstrap_mod

    monkeypatch.setattr(bootstrap_mod._compat_mod, "HAS_NIO", True)
    monkeypatch.setattr(bootstrap_mod._compat_mod, "HAS_E2EE", False)

    with pytest.raises(MatrixConnectionError, match="matrix-e2e"):
        await bootstrap_login_cross_signing(
            _login_result(),
            "fresh-password",
            adapter_id="main",
        )


async def test_bootstrap_closes_client_when_crypto_store_does_not_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import medre.adapters.matrix.e2ee_bootstrap as bootstrap_mod

    client = _FakeBootstrapClient()
    client.olm = None
    nio = _FakeNioModule(client)
    monkeypatch.setattr(bootstrap_mod._compat_mod, "HAS_NIO", True)
    monkeypatch.setattr(bootstrap_mod._compat_mod, "HAS_E2EE", True)
    monkeypatch.setattr(
        bootstrap_mod, "matrix_store_path_for_adapter", lambda _: tmp_path / "store"
    )
    monkeypatch.setitem(sys.modules, "nio", nio)  # type: ignore[arg-type]

    with pytest.raises(MatrixConnectionError, match="crypto store"):
        await bootstrap_login_cross_signing(
            _login_result(),
            "fresh-password",
            adapter_id="main",
        )

    client.close.assert_awaited_once()


def test_login_parser_accepts_cross_signing_options() -> None:
    args = _parser().parse_args(
        [
            "adapter",
            "matrix",
            "auth",
            "login",
            "--user",
            "@bot:example.com",
            "--password",
            "pw",
            "--adapter-id",
            "main",
            "--reset-cross-signing",
        ]
    )
    assert args.adapter_id == "main"
    assert args.reset_cross_signing is True


async def test_login_bootstraps_before_persisting_credentials(
    tmp_path: Path,
) -> None:
    from medre.adapters.matrix.cli import _adapter_matrix_auth_login

    args = SimpleNamespace(
        homeserver="https://matrix.example.com",
        user="@bot:example.com",
        password="fresh-password",
        password_stdin=False,
        adapter_id="main",
        reset_cross_signing=False,
    )
    result = _login_result()
    call_order: list[str] = []
    bootstrap_result = SimpleNamespace(store_path=tmp_path / "store")

    async def bootstrap(*args: object, **kwargs: object):
        call_order.append("bootstrap")
        assert args[0] == result
        assert args[1] == "fresh-password"
        assert kwargs == {"adapter_id": "main", "reset": False}
        return bootstrap_result

    def save(_result: MatrixLoginResult) -> Path:
        call_order.append("save")
        return tmp_path / "matrix.json"

    stdout = io.StringIO()
    with (
        patch("medre.adapters.matrix.auth.matrix_login", return_value=result),
        patch(
            "medre.adapters.matrix.auth.matrix_whoami",
            return_value="@bot:example.com",
        ),
        patch("medre.adapters.matrix.auth.save_credentials_json", side_effect=save),
        patch(
            "medre.adapters.matrix.e2ee_bootstrap.bootstrap_login_cross_signing",
            side_effect=bootstrap,
        ),
        patch("sys.stdout", stdout),
    ):
        await _adapter_matrix_auth_login(args)

    assert call_order == ["bootstrap", "save"]
    output = stdout.getvalue()
    assert "Cross-signing: verified" in output
    assert "fresh-password" not in output
    assert result.access_token not in output


async def test_login_does_not_persist_credentials_when_bootstrap_fails(
    tmp_path: Path,
) -> None:
    from medre.adapters.matrix.cli import _adapter_matrix_auth_login

    args = SimpleNamespace(
        homeserver="https://matrix.example.com",
        user="@bot:example.com",
        password="fresh-password",
        password_stdin=False,
        adapter_id="main",
        reset_cross_signing=False,
    )
    result = _login_result()
    save = MagicMock()

    with (
        patch("medre.adapters.matrix.auth.matrix_login", return_value=result),
        patch(
            "medre.adapters.matrix.auth.matrix_whoami",
            return_value="@bot:example.com",
        ),
        patch("medre.adapters.matrix.auth.save_credentials_json", save),
        patch(
            "medre.adapters.matrix.e2ee_bootstrap.bootstrap_login_cross_signing",
            side_effect=MatrixConnectionError("cross-signing failed"),
        ),
        pytest.raises(SystemExit) as exc_info,
    ):
        await _adapter_matrix_auth_login(args)

    assert exc_info.value.code == 1
    save.assert_not_called()


async def test_reset_flag_without_adapter_id_is_rejected_before_login() -> None:
    from medre.adapters.matrix.cli import _adapter_matrix_auth_login

    args = SimpleNamespace(
        homeserver="https://matrix.example.com",
        user="@bot:example.com",
        password="fresh-password",
        password_stdin=False,
        adapter_id=None,
        reset_cross_signing=True,
    )

    with (
        patch("medre.adapters.matrix.auth.matrix_login") as login,
        pytest.raises(SystemExit) as exc_info,
    ):
        await _adapter_matrix_auth_login(args)

    assert exc_info.value.code == 1
    login.assert_not_called()


async def test_basic_login_without_adapter_id_remains_sdk_free(tmp_path: Path) -> None:
    from medre.adapters.matrix.cli import _adapter_matrix_auth_login

    args = SimpleNamespace(
        homeserver="https://matrix.example.com",
        user="@bot:example.com",
        password="fresh-password",
        password_stdin=False,
        adapter_id=None,
        reset_cross_signing=False,
    )
    result = _login_result()
    with (
        patch("medre.adapters.matrix.auth.matrix_login", return_value=result),
        patch(
            "medre.adapters.matrix.auth.matrix_whoami",
            return_value="@bot:example.com",
        ),
        patch(
            "medre.adapters.matrix.auth.save_credentials_json",
            return_value=tmp_path / "matrix.json",
        ),
    ):
        await _adapter_matrix_auth_login(args)
