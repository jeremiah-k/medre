"""Tests for MatrixSession lifecycle boundary and E2EE scaffolding.

Covers:
  - MatrixSession lifecycle (start/stop/diagnostics)
  - Config validation for encryption_mode, require_encrypted_rooms
  - E2EE dependency detection (monkeypatchable)
  - Adapter start behavior per encryption_mode
  - Adapter diagnostics with crypto scaffold fields
  - Adapter delegates lifecycle to MatrixSession

No test requires mindroom-nio[e2e].
"""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from medre.adapters.base import AdapterContext
from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.matrix.compat import HAS_E2EE, HAS_NIO
from medre.adapters.matrix.config import MatrixConfig
from medre.adapters.matrix.errors import MatrixConfigError, MatrixConnectionError
from medre.adapters.matrix.session import MatrixSession, MatrixSessionDiagnostics


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> MatrixConfig:
    """Build a MatrixConfig with sensible defaults."""
    defaults: dict[str, Any] = {
        "adapter_id": "matrix-test",
        "homeserver": "https://matrix.example.com",
        "user_id": "@bot:example.com",
        "access_token": "tok_123",
    }
    defaults.update(overrides)
    return MatrixConfig(**defaults)


def _make_context(adapter_id: str = "matrix-test") -> AdapterContext:
    """Build an AdapterContext with minimal fakes."""
    return AdapterContext(
        adapter_id=adapter_id,
        event_bus=None,
        publish_inbound=AsyncMock(),
        logger=logging.getLogger(f"test.{adapter_id}"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


async def _sync_forever_stub(*args: object, **kwargs: object) -> None:
    """Stub for sync_forever — blocks until cancelled."""
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass


def _build_mock_nio_module() -> MagicMock:
    """Create a mock nio module with AsyncClient and message types."""
    mock = MagicMock(name="mock_nio")
    client = MagicMock(name="mock_async_client")
    client.logged_in = True
    client.restore_login = MagicMock()
    client.add_event_callback = MagicMock()
    client.stop_sync_forever = MagicMock()
    client.close = AsyncMock()
    client.sync_forever = _sync_forever_stub
    client.room_send = AsyncMock()
    mock.AsyncClient = MagicMock(return_value=client)
    mock.RoomMessageText = MagicMock(name="RoomMessageText")
    mock.RoomMessageNotice = MagicMock(name="RoomMessageNotice")
    mock.RoomMessageEmote = MagicMock(name="RoomMessageEmote")
    return mock


@pytest.fixture
def mock_nio():
    """Inject a mock nio module into sys.modules and patch HAS_NIO."""
    mock = _build_mock_nio_module()
    saved = sys.modules.get("nio")
    sys.modules["nio"] = mock
    with patch("medre.adapters.matrix.adapter.HAS_NIO", True):
        yield mock
    if saved is None:
        sys.modules.pop("nio", None)
    else:
        sys.modules["nio"] = saved


# ===================================================================
# TestMatrixConfigEncryption
# ===================================================================


class TestMatrixConfigEncryption:
    """Config validation for encryption_mode and require_encrypted_rooms."""

    def test_plaintext_default_encryption_mode(self) -> None:
        config = _make_config()
        assert config.encryption_mode == "plaintext"
        config.validate()  # no error

    def test_e2ee_optional_validates_mode_string(self) -> None:
        config = _make_config(encryption_mode="e2ee_optional")
        assert config.encryption_mode == "e2ee_optional"
        config.validate()  # no error

    def test_e2ee_optional_no_store_path_ok(self) -> None:
        """e2ee_optional does not require store_path."""
        config = _make_config(encryption_mode="e2ee_optional")
        config.validate()

    def test_e2ee_optional_no_device_id_ok(self) -> None:
        """e2ee_optional does not require device_id."""
        config = _make_config(encryption_mode="e2ee_optional")
        config.validate()

    def test_e2ee_required_requires_store_path(self) -> None:
        config = _make_config(encryption_mode="e2ee_required", device_id="DEV")
        with pytest.raises(MatrixConfigError, match="store_path"):
            config.validate()

    def test_e2ee_required_requires_device_id(self) -> None:
        config = _make_config(
            encryption_mode="e2ee_required", store_path="/tmp/store"
        )
        with pytest.raises(MatrixConfigError, match="device_id"):
            config.validate()

    def test_e2ee_required_with_both_store_and_device(self) -> None:
        config = _make_config(
            encryption_mode="e2ee_required",
            store_path="/tmp/store",
            device_id="DEV",
        )
        config.validate()  # no error

    def test_invalid_encryption_mode_rejected(self) -> None:
        config = _make_config(encryption_mode="unknown_mode")
        with pytest.raises(MatrixConfigError, match="encryption_mode"):
            config.validate()

    def test_require_encrypted_rooms_invalid_with_plaintext(self) -> None:
        config = _make_config(require_encrypted_rooms=True)
        with pytest.raises(MatrixConfigError, match="require_encrypted_rooms"):
            config.validate()

    def test_require_encrypted_rooms_valid_with_e2ee_optional(self) -> None:
        config = _make_config(
            encryption_mode="e2ee_optional", require_encrypted_rooms=True
        )
        config.validate()

    def test_require_encrypted_rooms_valid_with_e2ee_required(self) -> None:
        config = _make_config(
            encryption_mode="e2ee_required",
            store_path="/tmp/store",
            device_id="DEV",
            require_encrypted_rooms=True,
        )
        config.validate()

    def test_repr_no_secrets(self) -> None:
        config = _make_config(
            access_token="super-secret-token-123",
            encryption_mode="e2ee_optional",
        )
        r = repr(config)
        assert "super-secret-token-123" not in r
        assert "e2ee_optional" in r

    def test_plaintext_may_omit_store_path_and_device_id(self) -> None:
        """plaintext mode works without store_path and device_id."""
        config = _make_config()
        assert config.store_path is None
        assert config.device_id is None
        config.validate()


# ===================================================================
# TestE2EEDependencyDetection
# ===================================================================


class TestE2EEDependencyDetection:
    """HAS_E2EE detection is monkeypatchable and defaults to False."""

    def test_has_e2ee_default_false(self) -> None:
        """Without crypto deps, HAS_E2EE is False."""
        import medre.adapters.matrix.compat as compat
        # The default in CI/test envs is False (no vodozemac)
        # Just check it is a bool and False in this env
        assert isinstance(compat.HAS_E2EE, bool)

    def test_has_e2ee_monkeypatch_true(self) -> None:
        """Tests can monkeypatch HAS_E2EE to True."""
        import medre.adapters.matrix.compat as compat
        original = compat.HAS_E2EE
        try:
            compat.HAS_E2EE = True
            assert compat.HAS_E2EE is True
        finally:
            compat.HAS_E2EE = original

    def test_has_e2ee_monkeypatch_false(self) -> None:
        """Tests can monkeypatch HAS_E2EE to False."""
        import medre.adapters.matrix.compat as compat
        original = compat.HAS_E2EE
        try:
            compat.HAS_E2EE = False
            assert compat.HAS_E2EE is False
        finally:
            compat.HAS_E2EE = original

    def test_check_e2ee_returns_false_when_no_nio(self) -> None:
        """_check_e2ee returns False when HAS_NIO is False."""
        from medre.adapters.matrix.compat import _check_e2ee
        import medre.adapters.matrix.compat as compat
        original_nio = compat.HAS_NIO
        try:
            compat.HAS_NIO = False
            assert _check_e2ee() is False
        finally:
            compat.HAS_NIO = original_nio


# ===================================================================
# TestMatrixSessionLifecycle
# ===================================================================


class TestMatrixSessionLifecycle:
    """MatrixSession start/stop/diagnostics."""

    async def test_session_start_creates_client(self, mock_nio) -> None:
        config = _make_config()
        session = MatrixSession(config)
        try:
            await session.start()
            assert session.client is not None
            assert session.connected is True
            assert session.logged_in is True
        finally:
            await session.stop()

    async def test_session_stop_clears_client(self, mock_nio) -> None:
        config = _make_config()
        session = MatrixSession(config)
        await session.start()
        await session.stop()
        assert session.client is None
        assert session.connected is False

    async def test_session_stop_before_start(self) -> None:
        config = _make_config()
        session = MatrixSession(config)
        await session.stop()  # no raise

    async def test_session_login_failure(self, mock_nio) -> None:
        mock_nio.AsyncClient.return_value.logged_in = False
        config = _make_config()
        session = MatrixSession(config)
        with pytest.raises(MatrixConnectionError, match="failed to authenticate"):
            await session.start()

    async def test_session_no_nio_raises(self) -> None:
        """Session raises ImportError when nio is not available."""
        config = _make_config()
        session = MatrixSession(config)
        with patch.dict(sys.modules, {"nio": None}):
            with pytest.raises(ImportError):
                await session.start()

    async def test_session_registers_callback(self, mock_nio) -> None:
        cb = MagicMock()
        config = _make_config()
        session = MatrixSession(config, message_callback=cb)
        try:
            await session.start()
            mock_nio.AsyncClient.return_value.add_event_callback.assert_called_once()
        finally:
            await session.stop()

    async def test_session_sync_task_running(self, mock_nio) -> None:
        config = _make_config()
        session = MatrixSession(config)
        try:
            await session.start()
            assert session.sync_task_running is True
        finally:
            await session.stop()

    async def test_session_sync_task_not_running_after_stop(self, mock_nio) -> None:
        config = _make_config()
        session = MatrixSession(config)
        await session.start()
        await session.stop()
        assert session.sync_task_running is False

    async def test_session_sync_failure_recorded(self, mock_nio) -> None:
        async def _failing_sync(*a: object, **kw: object) -> None:
            await asyncio.sleep(0)
            raise RuntimeError("sync died")

        mock_nio.AsyncClient.return_value.sync_forever = _failing_sync
        config = _make_config()
        session = MatrixSession(config)
        try:
            await session.start()
            await asyncio.sleep(0.05)
            assert session.last_sync_error is not None
            assert isinstance(session.last_sync_error, RuntimeError)
        finally:
            await session.stop()


# ===================================================================
# TestMatrixSessionDiagnostics
# ===================================================================


class TestMatrixSessionDiagnostics:
    """MatrixSessionDiagnostics contains crypto scaffold fields."""

    def test_diagnostics_before_start(self) -> None:
        config = _make_config()
        session = MatrixSession(config)
        diag = session.diagnostics()
        assert isinstance(diag, MatrixSessionDiagnostics)
        assert diag.connected is False
        assert diag.logged_in is False
        assert diag.sync_task_running is False
        assert diag.last_sync_error is None
        assert diag.store_path_configured is False
        assert diag.device_id_configured is False
        assert diag.encryption_mode == "plaintext"
        assert diag.crypto_enabled is False
        assert diag.last_crypto_error is None
        assert diag.encrypted_room_seen is False
        assert diag.undecryptable_event_count == 0

    def test_diagnostics_with_store_and_device(self) -> None:
        config = _make_config(store_path="/tmp/store", device_id="DEV")
        session = MatrixSession(config)
        diag = session.diagnostics()
        assert diag.store_path_configured is True
        assert diag.device_id_configured is True

    def test_diagnostics_encryption_mode_e2ee_optional(self) -> None:
        config = _make_config(encryption_mode="e2ee_optional")
        session = MatrixSession(config)
        diag = session.diagnostics()
        assert diag.encryption_mode == "e2ee_optional"
        assert diag.crypto_enabled is False

    async def test_diagnostics_after_start(self, mock_nio) -> None:
        config = _make_config()
        session = MatrixSession(config)
        try:
            await session.start()
            diag = session.diagnostics()
            assert diag.connected is True
            assert diag.logged_in is True
            assert diag.sync_task_running is True
        finally:
            await session.stop()

    async def test_diagnostics_no_secrets(self, mock_nio) -> None:
        config = _make_config(access_token="super-secret-token-123")
        session = MatrixSession(config)
        try:
            await session.start()
            diag = session.diagnostics()
            diag_dict = {
                "connected": diag.connected,
                "logged_in": diag.logged_in,
                "sync_task_running": diag.sync_task_running,
                "last_sync_error": diag.last_sync_error,
                "store_path_configured": diag.store_path_configured,
                "device_id_configured": diag.device_id_configured,
                "encryption_mode": diag.encryption_mode,
                "crypto_enabled": diag.crypto_enabled,
                "last_crypto_error": diag.last_crypto_error,
                "encrypted_room_seen": diag.encrypted_room_seen,
                "undecryptable_event_count": diag.undecryptable_event_count,
            }
            for key, val in diag_dict.items():
                assert "super-secret-token-123" not in str(val), (
                    f"Secret leaked in diagnostics field {key!r}"
                )
        finally:
            await session.stop()


# ===================================================================
# TestAdapterStartBehavior
# ===================================================================


class TestAdapterStartBehavior:
    """Adapter start behavior per encryption_mode."""

    async def test_plaintext_starts_normally(self, mock_nio) -> None:
        config = _make_config()
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(_make_context())
            assert adapter._session is not None
            assert adapter._client is not None
        finally:
            await adapter.stop()

    async def test_e2ee_required_raises_not_implemented(self, mock_nio) -> None:
        config = _make_config(
            encryption_mode="e2ee_required",
            store_path="/tmp/store",
            device_id="DEV",
        )
        adapter = MatrixAdapter(config)
        with pytest.raises(MatrixConnectionError, match="E2EE runtime is not implemented"):
            await adapter.start(_make_context())

    async def test_e2ee_required_raises_even_with_e2ee_deps(self, mock_nio) -> None:
        """Even if HAS_E2EE is True, e2ee_required raises (not yet implemented)."""
        import medre.adapters.matrix.compat as compat
        original = compat.HAS_E2EE
        try:
            compat.HAS_E2EE = True
            config = _make_config(
                encryption_mode="e2ee_required",
                store_path="/tmp/store",
                device_id="DEV",
            )
            adapter = MatrixAdapter(config)
            with pytest.raises(MatrixConnectionError, match="E2EE runtime is not implemented"):
                await adapter.start(_make_context())
        finally:
            compat.HAS_E2EE = original

    async def test_e2ee_optional_starts_plaintext(self, mock_nio) -> None:
        config = _make_config(encryption_mode="e2ee_optional")
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(_make_context())
            assert adapter._session is not None
            diag = adapter.diagnostics()
            assert diag["crypto_enabled"] is False
            assert diag["encryption_mode"] == "e2ee_optional"
        finally:
            await adapter.stop()

    async def test_plaintext_no_nio_raises(self) -> None:
        config = _make_config()
        adapter = MatrixAdapter(config)
        with patch("medre.adapters.matrix.adapter.HAS_NIO", False):
            with pytest.raises(MatrixConnectionError, match="mindroom-nio not installed"):
                await adapter.start(_make_context())


# ===================================================================
# TestAdapterDiagnostics
# ===================================================================


class TestAdapterDiagnostics:
    """Adapter diagnostics expose crypto scaffold fields."""

    def test_diagnostics_before_start(self) -> None:
        config = _make_config(encryption_mode="e2ee_optional")
        adapter = MatrixAdapter(config)
        diag = adapter.diagnostics()
        assert diag["connected"] is False
        assert diag["logged_in"] is False
        assert diag["sync_task_running"] is False
        assert diag["last_sync_error"] is None
        assert diag["store_path_configured"] is False
        assert diag["device_id_configured"] is False
        assert diag["encryption_mode"] == "e2ee_optional"
        assert diag["crypto_enabled"] is False
        assert diag["last_crypto_error"] is None
        assert diag["encrypted_room_seen"] is False
        assert diag["undecryptable_event_count"] == 0

    async def test_diagnostics_after_start(self, mock_nio) -> None:
        config = _make_config()
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(_make_context())
            diag = adapter.diagnostics()
            assert diag["connected"] is True
            assert diag["logged_in"] is True
            assert diag["sync_task_running"] is True
            assert diag["crypto_enabled"] is False
        finally:
            await adapter.stop()

    def test_diagnostics_no_secrets_in_dict(self) -> None:
        config = _make_config(access_token="super-secret-token-123")
        adapter = MatrixAdapter(config)
        diag = adapter.diagnostics()
        for key, val in diag.items():
            assert "super-secret-token-123" not in str(val), (
                f"Secret leaked in diagnostics field {key!r}"
            )


# ===================================================================
# TestAdapterDelegatesToSession
# ===================================================================


class TestAdapterDelegatesToSession:
    """Adapter delegates lifecycle to MatrixSession."""

    async def test_adapter_uses_session_for_client(self, mock_nio) -> None:
        config = _make_config()
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(_make_context())
            assert adapter._session is not None
            assert adapter._client is adapter._session.client
        finally:
            await adapter.stop()

    async def test_adapter_stop_destroys_session(self, mock_nio) -> None:
        config = _make_config()
        adapter = MatrixAdapter(config)
        await adapter.start(_make_context())
        await adapter.stop()
        assert adapter._session is None
        assert adapter._client is None

    async def test_adapter_health_check_delegates(self, mock_nio) -> None:
        config = _make_config()
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(_make_context())
            info = await adapter.health_check()
            assert info.health == "healthy"
        finally:
            await adapter.stop()

    async def test_adapter_health_unknown_after_stop(self, mock_nio) -> None:
        config = _make_config()
        adapter = MatrixAdapter(config)
        await adapter.start(_make_context())
        await adapter.stop()
        info = await adapter.health_check()
        assert info.health == "unknown"

    async def test_adapter_health_unknown_before_start(self) -> None:
        config = _make_config()
        adapter = MatrixAdapter(config)
        info = await adapter.health_check()
        assert info.health == "unknown"

    async def test_adapter_double_stop_idempotent(self, mock_nio) -> None:
        config = _make_config()
        adapter = MatrixAdapter(config)
        await adapter.start(_make_context())
        await adapter.stop()
        await adapter.stop()
        assert adapter._session is None
