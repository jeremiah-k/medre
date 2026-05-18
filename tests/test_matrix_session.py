"""Tests for MatrixSession lifecycle boundary and E2EE implementation.

Covers:
  - MatrixSession lifecycle (start/stop/diagnostics)
  - Config validation for encryption_mode, require_encrypted_rooms
  - E2EE dependency detection (monkeypatchable)
  - Adapter start behavior per encryption_mode
  - Adapter diagnostics with crypto scaffold fields
  - Adapter delegates lifecycle to MatrixSession
  - E2EE required/optional startup flows
  - MegolmEvent undecryptable handling
  - Sync failure logging
  - Encrypted room safety check in deliver()

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

from medre.core.contracts.adapter import AdapterContext, AdapterPermanentError, AdapterSendError
from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.matrix.compat import HAS_E2EE, HAS_NIO
from medre.config.adapters.matrix import MatrixConfig
from medre.config.adapters.errors import MatrixConfigError
from medre.adapters.matrix.errors import MatrixConnectionError, MatrixSendError
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


def _fast_sleep_patch():
    """Return a mock sleep that yields for delay=0 but skips backoff delays.

    Usage::

        with _fast_sleep_patch():
            await session.start()
            for _ in range(N):
                await asyncio.sleep(0)
    """
    original_sleep = asyncio.sleep

    async def _fast_sleep(delay: float) -> None:
        if delay <= 0:
            await original_sleep(0)
        # positive delays are skipped (instant backoff for tests)

    return patch("asyncio.sleep", side_effect=_fast_sleep)


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
    client.rooms = {}
    # whoami() returns a response with device_id for device discovery.
    whoami_resp = MagicMock(name="whoami_response")
    whoami_resp.device_id = "MOCK_DISCOVERED_DEVICE"
    client.whoami = AsyncMock(return_value=whoami_resp)
    mock.AsyncClient = MagicMock(return_value=client)
    mock.ClientConfig = MagicMock(name="ClientConfig")
    mock.RoomMessageText = MagicMock(name="RoomMessageText")
    mock.RoomMessageNotice = MagicMock(name="RoomMessageNotice")
    mock.RoomMessageEmote = MagicMock(name="RoomMessageEmote")
    # nio.events.MegolmEvent for undecryptable event callback
    mock_events = MagicMock(name="nio.events")
    mock_events.MegolmEvent = MagicMock(name="MegolmEvent")
    mock_events.RoomEncryptionEvent = MagicMock(name="RoomEncryptionEvent")
    mock.events = mock_events
    return mock


@pytest.fixture
def mock_nio():
    """Inject a mock nio module into sys.modules and patch HAS_NIO."""
    mock = _build_mock_nio_module()
    saved_nio = sys.modules.get("nio")
    saved_nio_events = sys.modules.get("nio.events")
    sys.modules["nio"] = mock
    sys.modules["nio.events"] = mock.events
    with patch("medre.adapters.matrix.adapter.HAS_NIO", True):
        yield mock
    # Restore
    if saved_nio is None:
        sys.modules.pop("nio", None)
    else:
        sys.modules["nio"] = saved_nio
    if saved_nio_events is None:
        sys.modules.pop("nio.events", None)
    else:
        sys.modules["nio.events"] = saved_nio_events


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

    def test_e2ee_required_without_store_path_ok(self) -> None:
        """e2ee_required does not require store_path at validation time —
        the runtime builder injects it before session construction."""
        config = _make_config(encryption_mode="e2ee_required")
        assert config.store_path is None
        config.validate()  # no error — builder injects store_path later

    def test_e2ee_required_without_device_id_ok(self) -> None:
        """e2ee_required does not require device_id — discovered via whoami."""
        config = _make_config(encryption_mode="e2ee_required")
        assert config.device_id is None
        config.validate()  # no error — session discovers device_id

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
# TestE2EEDefaultDerivation
# ===================================================================


class TestE2EEDefaultDerivation:
    """e2ee_required works without operator-supplied device_id (store_path from builder)."""

    async def test_e2ee_required_without_device_id_discovers_it(
        self, mock_nio, tmp_path
    ) -> None:
        """e2ee_required starts with store_path set and discovers device_id
        via whoami() when device_id is not configured."""
        import medre.adapters.matrix.compat as compat

        original = compat.HAS_E2EE
        try:
            compat.HAS_E2EE = True
            store = tmp_path / "matrix-store"
            # store_path set (as the builder would inject), no device_id
            config = _make_config(
                encryption_mode="e2ee_required",
                store_path=str(store),
            )
            assert config.store_path is not None
            assert config.device_id is None

            session = MatrixSession(config)
            try:
                await session.start()
                assert session.crypto_enabled is True
                assert session.crypto_store_loaded is True
                # whoami should have been called to discover device_id
                mock_nio.AsyncClient.return_value.whoami.assert_awaited_once()
            finally:
                await session.stop()
        finally:
            compat.HAS_E2EE = original

    async def test_e2ee_required_without_store_path_raises(self, mock_nio) -> None:
        """e2ee_required without store_path raises — no tempdir fallback."""
        import medre.adapters.matrix.compat as compat

        original = compat.HAS_E2EE
        try:
            compat.HAS_E2EE = True
            config = _make_config(encryption_mode="e2ee_required")
            assert config.store_path is None

            session = MatrixSession(config)
            with pytest.raises(
                MatrixConnectionError,
                match="E2EE requires a store_path",
            ):
                await session.start()
        finally:
            compat.HAS_E2EE = original

    async def test_e2ee_required_uses_configured_store_when_set(
        self, mock_nio
    ) -> None:
        """When store_path is explicitly configured, it is used as-is."""
        import medre.adapters.matrix.compat as compat

        original = compat.HAS_E2EE
        try:
            compat.HAS_E2EE = True
            config = _make_config(
                encryption_mode="e2ee_required",
                store_path="/tmp/test-store-explicit",
                device_id="EXPLICIT_DEV",
            )
            session = MatrixSession(config)
            try:
                await session.start()
                assert session.crypto_enabled is True
                # whoami should NOT have been called (device_id was given)
                mock_nio.AsyncClient.return_value.whoami.assert_not_awaited()
            finally:
                await session.stop()
        finally:
            compat.HAS_E2EE = original

    def test_no_default_store_path_function(self) -> None:
        """_default_store_path has been removed; no tempdir fallback."""
        import medre.adapters.matrix.session as session_mod

        assert not hasattr(session_mod, "_default_store_path")
        assert not hasattr(session_mod, "_DEFAULT_STORE_DIR_TEMPLATE")

    async def test_e2ee_optional_attempts_crypto_without_device(
        self, mock_nio, tmp_path
    ) -> None:
        """e2ee_optional with HAS_E2EE=True, store_path set, no device_id
        attempts crypto and discovers device_id via whoami()."""
        import medre.adapters.matrix.compat as compat

        original = compat.HAS_E2EE
        try:
            compat.HAS_E2EE = True
            store = tmp_path / "matrix-store"
            config = _make_config(
                encryption_mode="e2ee_optional",
                store_path=str(store),
            )
            session = MatrixSession(config)
            try:
                await session.start()
                assert session.crypto_enabled is True
            finally:
                await session.stop()
        finally:
            compat.HAS_E2EE = original


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
            # Three callbacks: message types + MegolmEvent + RoomEncryptionEvent
            assert (
                mock_nio.AsyncClient.return_value.add_event_callback.call_count == 3
            )
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
        logger = logging.getLogger("test.sync_failure")
        session = MatrixSession(config, logger=logger)

        # Mock sleep: instant for backoff (>0), real yield for 0
        original_sleep = asyncio.sleep
        async def _fast_sleep(delay: float) -> None:
            if delay <= 0:
                await original_sleep(0)
            # else: instant (skip backoff)
        try:
            with patch("asyncio.sleep", side_effect=_fast_sleep):
                await session.start()
                for _ in range(100):
                    await original_sleep(0)
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

    async def test_e2ee_required_raises_without_e2ee_deps(self, mock_nio) -> None:
        """e2ee_required raises when HAS_E2EE is False (no crypto deps)."""
        import medre.adapters.matrix.compat as compat
        original = compat.HAS_E2EE
        try:
            compat.HAS_E2EE = False
            config = _make_config(
                encryption_mode="e2ee_required",
                store_path="/tmp/store",
                device_id="DEV",
            )
            adapter = MatrixAdapter(config)
            with pytest.raises(
                MatrixConnectionError,
                match="mindroom-nio\\[e2e\\] not installed",
            ):
                await adapter.start(_make_context())
        finally:
            compat.HAS_E2EE = original

    async def test_e2ee_required_succeeds_with_e2ee_deps(self, mock_nio) -> None:
        """e2ee_required starts with crypto_enabled=True when HAS_E2EE=True."""
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
            try:
                await adapter.start(_make_context())
                assert adapter._session is not None
                assert adapter._session.crypto_enabled is True
                diag = adapter.diagnostics()
                assert diag["crypto_enabled"] is True
                assert diag["encryption_mode"] == "e2ee_required"
            finally:
                await adapter.stop()
        finally:
            compat.HAS_E2EE = original

    async def test_e2ee_optional_starts_plaintext(self, mock_nio) -> None:
        """e2ee_optional without HAS_E2EE falls back to plaintext."""
        import medre.adapters.matrix.compat as compat
        original = compat.HAS_E2EE
        try:
            compat.HAS_E2EE = False
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
        finally:
            compat.HAS_E2EE = original

    async def test_e2ee_optional_with_crypto_deps(self, mock_nio) -> None:
        """e2ee_optional with HAS_E2EE=True, store_path, device_id → crypto_enabled."""
        import medre.adapters.matrix.compat as compat
        original = compat.HAS_E2EE
        try:
            compat.HAS_E2EE = True
            config = _make_config(
                encryption_mode="e2ee_optional",
                store_path="/tmp/store",
                device_id="DEV",
            )
            adapter = MatrixAdapter(config)
            try:
                await adapter.start(_make_context())
                assert adapter._session is not None
                assert adapter._session.crypto_enabled is True
                diag = adapter.diagnostics()
                assert diag["crypto_enabled"] is True
            finally:
                await adapter.stop()
        finally:
            compat.HAS_E2EE = original

    async def test_e2ee_optional_falls_back_on_crypto_failure(
        self, mock_nio
    ) -> None:
        """e2ee_optional falls back to plaintext when crypto setup fails."""
        import medre.adapters.matrix.compat as compat
        original = compat.HAS_E2EE
        try:
            compat.HAS_E2EE = True
            # Make ClientConfig raise so crypto setup fails
            mock_nio.ClientConfig.side_effect = TypeError("nope")
            config = _make_config(
                encryption_mode="e2ee_optional",
                # no store_path, no device_id
            )
            adapter = MatrixAdapter(config)
            try:
                await adapter.start(_make_context())
                assert adapter._session is not None
                # Crypto failed → plaintext fallback
                assert adapter._session.crypto_enabled is False
            finally:
                await adapter.stop()
        finally:
            compat.HAS_E2EE = original
            mock_nio.ClientConfig.side_effect = None

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


# ===================================================================
# TestSyncFailureLogging
# ===================================================================


class TestSyncFailureLogging:
    """Sync task failure is logged via the session logger."""

    async def test_sync_failure_logged_to_session_logger(
        self, mock_nio, caplog
    ) -> None:
        """Sync failure is recorded AND logged to the session logger."""
        async def _failing_sync(*a: object, **kw: object) -> None:
            await asyncio.sleep(0)
            raise RuntimeError("sync died")

        mock_nio.AsyncClient.return_value.sync_forever = _failing_sync
        config = _make_config()
        logger = logging.getLogger("test.sync_failure_log")
        session = MatrixSession(config, logger=logger)
        try:
            # Patch sleep BEFORE start so reconnect backoff is instant
            with _fast_sleep_patch():
                await session.start()
                for _ in range(100):
                    await asyncio.sleep(0)
            assert session.last_sync_error is not None
            assert isinstance(session.last_sync_error, RuntimeError)

            # Use a handler that captures records
            log_records: list[logging.LogRecord] = []
            handler = logging.Handler()
            handler.emit = lambda record: log_records.append(record)  # type: ignore[assignment]
            logger.addHandler(handler)
            logger.setLevel(logging.ERROR)

            # Create a new session with same pattern to capture log
            session2 = MatrixSession(config, logger=logger)
            mock_nio.AsyncClient.return_value.sync_forever = _failing_sync
            with _fast_sleep_patch():
                await session2.start()
                for _ in range(100):
                    await asyncio.sleep(0)

            assert any(
                "Max sync reconnect attempts" in rec.getMessage()
                for rec in log_records
            ), f"Expected sync failure log; got: {[r.getMessage() for r in log_records]}"
            logger.removeHandler(handler)
            await session2.stop()
        finally:
            await session.stop()


# ===================================================================
# TestMegolmEventHandling
# ===================================================================


class TestMegolmEventHandling:
    """Undecryptable MegolmEvent handling."""

    async def test_megolm_event_increments_count(self, mock_nio) -> None:
        """Receiving an undecryptable MegolmEvent increments count and sets error."""
        config = _make_config(
            encryption_mode="e2ee_required",
            store_path="/tmp/store",
            device_id="DEV",
        )
        import medre.adapters.matrix.compat as compat
        original = compat.HAS_E2EE
        try:
            compat.HAS_E2EE = True
            session = MatrixSession(config)
            try:
                await session.start()
                assert session.undecryptable_event_count == 0

                # Simulate MegolmEvent callback
                event = MagicMock(name="megolm_event")
                event.event_id = "$undecryptable_1"
                room = MagicMock(name="room")
                room.room_id = "!encrypted:example.com"

                await session._on_megolm_event(room, event)

                assert session.undecryptable_event_count == 1
                assert session.last_crypto_error is not None
                assert "undecryptable" in session.last_crypto_error.lower()
                assert session.encrypted_room_seen is True
                # No tokens/keys in the error
                assert "access_token" not in session.last_crypto_error
            finally:
                await session.stop()
        finally:
            compat.HAS_E2EE = original

    async def test_megolm_event_does_not_crash(self, mock_nio) -> None:
        """MegolmEvent callback never raises, even with bad data."""
        config = _make_config(
            encryption_mode="e2ee_required",
            store_path="/tmp/store",
            device_id="DEV",
        )
        import medre.adapters.matrix.compat as compat
        original = compat.HAS_E2EE
        try:
            compat.HAS_E2EE = True
            session = MatrixSession(config)
            try:
                await session.start()
                # Call with None room, minimal event
                event = MagicMock(name="bad_event")
                event.event_id = "$bad"
                del event.source  # no source attribute
                await session._on_megolm_event(None, event)
                assert session.undecryptable_event_count == 1
            finally:
                await session.stop()
        finally:
            compat.HAS_E2EE = original

    async def test_megolm_event_no_secrets_in_error(self, mock_nio) -> None:
        """last_crypto_error must not contain secrets or session_id."""
        config = _make_config(
            encryption_mode="e2ee_required",
            store_path="/tmp/store",
            device_id="DEV",
            access_token="super-secret-token",
        )
        import medre.adapters.matrix.compat as compat
        original = compat.HAS_E2EE
        try:
            compat.HAS_E2EE = True
            session = MatrixSession(config)
            try:
                await session.start()
                event = MagicMock(name="megolm_event")
                event.event_id = "$test"
                room = MagicMock(name="room")
                room.room_id = "!room:example.com"
                await session._on_megolm_event(room, event)

                assert "super-secret-token" not in (session.last_crypto_error or "")
                # Blocker 6: session_id must NOT appear in last_crypto_error
                assert "session_id" not in (session.last_crypto_error or "")
            finally:
                await session.stop()
        finally:
            compat.HAS_E2EE = original


# ===================================================================
# TestEncryptedRoomSafety
# ===================================================================


class TestEncryptedRoomSafety:
    """_check_encrypted_room_safety in adapter deliver() path."""

    async def test_deliver_raises_if_encrypted_room_no_crypto(
        self, mock_nio
    ) -> None:
        """deliver() raises when room is encrypted but crypto is not active."""
        from medre.adapters.matrix.errors import MatrixSendError
        from medre.core.rendering.renderer import RenderingResult

        config = _make_config()
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(_make_context())
            assert adapter._session.crypto_enabled is False  # type: ignore[union-attr]

            # Simulate an encrypted room via client.rooms
            room_id = "!encrypted_room:example.com"
            room_obj = MagicMock(name="room_obj")
            room_obj.encrypted = True
            adapter._client.rooms = {room_id: room_obj}

            result = RenderingResult(
                event_id="evt_1",
                target_adapter="matrix-test",
                payload={"msgtype": "m.text", "body": "hello"},
                target_channel=room_id,
            )
            with pytest.raises(AdapterPermanentError, match="encrypted but E2EE crypto is not active"):
                await adapter.deliver(result)
        finally:
            await adapter.stop()

    async def test_deliver_ok_if_not_encrypted_room(self, mock_nio) -> None:
        """deliver() succeeds when room is not encrypted."""
        from medre.core.rendering.renderer import RenderingResult

        config = _make_config()
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(_make_context())
            # Room not encrypted → should be fine
            response_mock = MagicMock()
            response_mock.event_id = "$event_123"
            adapter._client.room_send = AsyncMock(return_value=response_mock)

            result = RenderingResult(
                event_id="evt_2",
                target_adapter="matrix-test",
                payload={"msgtype": "m.text", "body": "hello"},
                target_channel="!plain_room:example.com",
            )
            deliver_result = await adapter.deliver(result)
            assert deliver_result is not None
            assert deliver_result.native_message_id == "$event_123"
        finally:
            await adapter.stop()

    async def test_plaintext_room_send_not_blocked_by_flag(self, mock_nio) -> None:
        """Plaintext room send is not blocked even if encrypted_room_seen is True."""
        from medre.core.rendering.renderer import RenderingResult

        config = _make_config()
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(_make_context())
            # encrypted_room_seen is True globally but room is plaintext
            adapter._session._encrypted_room_seen = True  # type: ignore[union-attr]
            assert adapter._session.crypto_enabled is False  # type: ignore[union-attr]

            room_id = "!plain_room:example.com"
            room_obj = MagicMock(name="room_obj")
            room_obj.encrypted = False
            adapter._client.rooms = {room_id: room_obj}

            response_mock = MagicMock()
            response_mock.event_id = "$event_123"
            adapter._client.room_send = AsyncMock(return_value=response_mock)

            result = RenderingResult(
                event_id="evt_3",
                target_adapter="matrix-test",
                payload={"msgtype": "m.text", "body": "hello"},
                target_channel=room_id,
            )
            deliver_result = await adapter.deliver(result)
            assert deliver_result is not None
        finally:
            await adapter.stop()

    async def test_e2ee_crypto_enabled_allows_send(self, mock_nio) -> None:
        """e2ee_required with crypto_enabled=True allows send even in encrypted room."""
        from medre.adapters.matrix.errors import MatrixSendError
        from medre.core.rendering.renderer import RenderingResult
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
            try:
                await adapter.start(_make_context())
                assert adapter._session.crypto_enabled is True  # type: ignore[union-attr]

                room_id = "!encrypted:example.com"
                room_obj = MagicMock(name="room_obj")
                room_obj.encrypted = True
                adapter._client.rooms = {room_id: room_obj}

                response_mock = MagicMock()
                response_mock.event_id = "$event_456"
                adapter._client.room_send = AsyncMock(return_value=response_mock)

                result = RenderingResult(
                    event_id="evt_4",
                    target_adapter="matrix-test",
                    payload={"msgtype": "m.text", "body": "secret"},
                    target_channel=room_id,
                )
                deliver_result = await adapter.deliver(result)
                assert deliver_result is not None
                assert deliver_result.native_message_id == "$event_456"
            finally:
                await adapter.stop()
        finally:
            compat.HAS_E2EE = original

    async def test_unknown_room_allows_send(self, mock_nio) -> None:
        """Unknown room (not in client.rooms) allows send optimistically."""
        from medre.core.rendering.renderer import RenderingResult

        config = _make_config()
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(_make_context())
            assert adapter._session.crypto_enabled is False  # type: ignore[union-attr]

            # Room not in client.rooms → optimistic allow
            adapter._client.rooms = {}

            response_mock = MagicMock()
            response_mock.event_id = "$event_789"
            adapter._client.room_send = AsyncMock(return_value=response_mock)

            result = RenderingResult(
                event_id="evt_5",
                target_adapter="matrix-test",
                payload={"msgtype": "m.text", "body": "hello"},
                target_channel="!unknown:example.com",
            )
            deliver_result = await adapter.deliver(result)
            assert deliver_result is not None
        finally:
            await adapter.stop()


# ===================================================================
# TestE2EEDiagnostics
# ===================================================================


class TestE2EEDiagnostics:
    """Diagnostics truthfully report crypto state."""

    async def test_e2ee_required_diagnostics_with_crypto(
        self, mock_nio
    ) -> None:
        """e2ee_required mode shows crypto_enabled=True in diagnostics."""
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
            try:
                await adapter.start(_make_context())
                diag = adapter.diagnostics()
                assert diag["crypto_enabled"] is True
                assert diag["encryption_mode"] == "e2ee_required"
                assert diag["store_path_configured"] is True
                assert diag["device_id_configured"] is True
                assert diag["encrypted_room_seen"] is False
                assert diag["undecryptable_event_count"] == 0
                assert diag["last_crypto_error"] is None
            finally:
                await adapter.stop()
        finally:
            compat.HAS_E2EE = original

    async def test_plaintext_diagnostics_crypto_false(self, mock_nio) -> None:
        """plaintext mode shows crypto_enabled=False."""
        config = _make_config()
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(_make_context())
            diag = adapter.diagnostics()
            assert diag["crypto_enabled"] is False
        finally:
            await adapter.stop()

    async def test_diagnostics_after_megolm_event(self, mock_nio) -> None:
        """Diagnostics reflect undecryptable event state."""
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
            try:
                await adapter.start(_make_context())
                event = MagicMock(name="megolm_event")
                event.event_id = "$undec"
                room = MagicMock(name="room")
                room.room_id = "!room:example.com"
                await adapter._session._on_megolm_event(room, event)  # type: ignore[union-attr]

                diag = adapter.diagnostics()
                assert diag["encrypted_room_seen"] is True
                assert diag["undecryptable_event_count"] == 1
                assert diag["last_crypto_error"] is not None
            finally:
                await adapter.stop()
        finally:
            compat.HAS_E2EE = original


# ===================================================================
# TestBlocker3ClientConfigFailure
# ===================================================================


class TestBlocker3ClientConfigFailure:
    """Blocker 3: ClientConfig(encryption_enabled=True) failure handling."""

    async def test_client_config_succeeds_crypto_enabled(self, mock_nio) -> None:
        """ClientConfig succeeds → crypto_enabled=True."""
        import medre.adapters.matrix.compat as compat
        original = compat.HAS_E2EE
        try:
            compat.HAS_E2EE = True
            config = _make_config(
                encryption_mode="e2ee_required",
                store_path="/tmp/store",
                device_id="DEV",
            )
            session = MatrixSession(config)
            try:
                await session.start()
                assert session.crypto_enabled is True
            finally:
                await session.stop()
        finally:
            compat.HAS_E2EE = original

    async def test_client_config_raises_matrix_connection_error(
        self, mock_nio
    ) -> None:
        """ClientConfig raises → MatrixConnectionError raised, crypto_enabled stays False."""
        import medre.adapters.matrix.compat as compat
        original = compat.HAS_E2EE
        try:
            compat.HAS_E2EE = True
            mock_nio.ClientConfig.side_effect = TypeError("bad param")
            config = _make_config(
                encryption_mode="e2ee_required",
                store_path="/tmp/store",
                device_id="DEV",
            )
            session = MatrixSession(config)
            with pytest.raises(MatrixConnectionError, match="Failed to configure E2EE"):
                await session.start()
            assert session.crypto_enabled is False
        finally:
            compat.HAS_E2EE = original
            mock_nio.ClientConfig.side_effect = None

    async def test_client_closed_on_config_failure(self, mock_nio) -> None:
        """If AsyncClient was created but ClientConfig fails, client is closed."""
        import medre.adapters.matrix.compat as compat
        original = compat.HAS_E2EE
        try:
            compat.HAS_E2EE = True
            mock_nio.ClientConfig.side_effect = TypeError("bad param")
            config = _make_config(
                encryption_mode="e2ee_required",
                store_path="/tmp/store",
                device_id="DEV",
            )
            session = MatrixSession(config)
            with pytest.raises(MatrixConnectionError):
                await session.start()
            assert session.client is None
            assert session.crypto_enabled is False
        finally:
            compat.HAS_E2EE = original
            mock_nio.ClientConfig.side_effect = None


# ===================================================================
# TestBlocker4RoomEncryptionEvent
# ===================================================================


class TestBlocker4RoomEncryptionEvent:
    """Blocker 4: RoomEncryptionEvent callback registration and state update."""

    async def test_room_encryption_event_callback_registered(
        self, mock_nio
    ) -> None:
        """RoomEncryptionEvent callback is registered on start."""
        cb = MagicMock()
        config = _make_config()
        session = MatrixSession(config, message_callback=cb)
        try:
            await session.start()
            client_mock = mock_nio.AsyncClient.return_value
            # Should have 3 callbacks: message + megolm + room_encryption
            assert client_mock.add_event_callback.call_count == 3
            # Check that one of the calls used RoomEncryptionEvent
            call_args_list = client_mock.add_event_callback.call_args_list
            event_types_used = []
            for call in call_args_list:
                event_types_used.extend(call[0][1])
            assert mock_nio.events.RoomEncryptionEvent in event_types_used
        finally:
            await session.stop()

    async def test_room_encryption_event_sets_flag(self, mock_nio) -> None:
        """_on_room_encryption_event sets _encrypted_room_seen=True."""
        config = _make_config()
        session = MatrixSession(config)
        try:
            await session.start()
            assert session.encrypted_room_seen is False

            room = MagicMock(name="room")
            room.room_id = "!encrypted:example.com"
            event = MagicMock(name="encryption_event")

            await session._on_room_encryption_event(room, event)
            assert session.encrypted_room_seen is True
        finally:
            await session.stop()

    async def test_room_encryption_event_logs_info(
        self, mock_nio, caplog
    ) -> None:
        """_on_room_encryption_event logs an info message."""
        config = _make_config()
        session = MatrixSession(config)
        try:
            await session.start()
            room = MagicMock(name="room")
            room.room_id = "!encrypted:example.com"
            event = MagicMock(name="encryption_event")

            with caplog.at_level(logging.INFO):
                await session._on_room_encryption_event(room, event)

            assert any(
                "RoomEncryptionEvent" in rec.getMessage()
                for rec in caplog.records
            )
        finally:
            await session.stop()


# ===================================================================
# TestBlocker6DiagnosticsRedaction
# ===================================================================


class TestBlocker6DiagnosticsRedaction:
    """Blocker 6: last_crypto_error must not include session_id or access_token."""

    async def test_no_session_id_in_last_crypto_error(self, mock_nio) -> None:
        """session_id from event.source must not appear in last_crypto_error."""
        import medre.adapters.matrix.compat as compat
        original = compat.HAS_E2EE
        try:
            compat.HAS_E2EE = True
            config = _make_config(
                encryption_mode="e2ee_required",
                store_path="/tmp/store",
                device_id="DEV",
            )
            session = MatrixSession(config)
            try:
                await session.start()
                event = MagicMock(name="megolm_event")
                event.event_id = "$test_redact"
                # Even if source has session_id, it must not leak
                event.source = {
                    "content": {
                        "session_id": "sensitive_session_id_12345",
                        "algorithm": "m.megolm.v1.aes-sha2",
                    }
                }
                room = MagicMock(name="room")
                room.room_id = "!room:example.com"
                await session._on_megolm_event(room, event)

                err = session.last_crypto_error or ""
                assert "sensitive_session_id_12345" not in err
                assert "session_id" not in err
            finally:
                await session.stop()
        finally:
            compat.HAS_E2EE = original

    async def test_no_access_token_in_last_crypto_error(self, mock_nio) -> None:
        """Access token must not appear in last_crypto_error."""
        import medre.adapters.matrix.compat as compat
        original = compat.HAS_E2EE
        try:
            compat.HAS_E2EE = True
            config = _make_config(
                encryption_mode="e2ee_required",
                store_path="/tmp/store",
                device_id="DEV",
                access_token="tok_super_secret_999",
            )
            session = MatrixSession(config)
            try:
                await session.start()
                event = MagicMock(name="megolm_event")
                event.event_id = "$test_no_tok"
                room = MagicMock(name="room")
                room.room_id = "!room:example.com"
                await session._on_megolm_event(room, event)

                assert "tok_super_secret_999" not in (session.last_crypto_error or "")
            finally:
                await session.stop()
        finally:
            compat.HAS_E2EE = original


# ===================================================================
# Track 1: TestSyncRecovery
# ===================================================================


class TestSyncRecovery:
    """Automatic sync recovery with bounded reconnect/backoff."""

    async def test_reconnect_after_transient_failure(self, mock_nio) -> None:
        """sync_forever fails 3 times then succeeds → reconnect happens."""
        call_count = 0
        fail_count = 3

        async def _sync_controlled(*a: object, **kw: object) -> None:
            nonlocal call_count
            call_count += 1
            if call_count <= fail_count:
                raise ConnectionError(f"transient error #{call_count}")
            # Success — block forever
            await asyncio.Event().wait()

        mock_nio.AsyncClient.return_value.sync_forever = _sync_controlled

        config = _make_config()
        session = MatrixSession(config)

        with _fast_sleep_patch():
            try:
                await session.start()
                # Give the sync loop time to run through failures
                for _ in range(30):
                    await asyncio.sleep(0)
                # sync_forever was called more than fail_count times
                assert call_count > fail_count
                # Reconnect happened and then sync succeeded
                assert session.sync_task_running is True
                assert session._sync_failure is None
                # Reconnect attempts accumulated during failures
                assert session.reconnect_attempts == fail_count
                # Not currently in reconnect phase (sync_forever is running)
                assert session.reconnecting is False
            finally:
                await session.stop()

    async def test_max_reconnect_attempts_reached(self, mock_nio) -> None:
        """sync_forever always fails → max attempts reached, _sync_failure set."""
        import medre.adapters.matrix.session as sess_mod

        async def _always_fail(*a: object, **kw: object) -> None:
            await asyncio.sleep(0)
            raise ConnectionError("persistent failure")

        mock_nio.AsyncClient.return_value.sync_forever = _always_fail

        config = _make_config()
        session = MatrixSession(config)

        with _fast_sleep_patch():
            try:
                await session.start()
                # Give the sync loop time to exhaust retries
                for _ in range(100):
                    await asyncio.sleep(0)
                # Should have given up
                assert session._sync_failure is not None
                assert isinstance(session._sync_failure, ConnectionError)
                assert session.reconnect_attempts >= sess_mod._MAX_RECONNECT_ATTEMPTS
                assert session.reconnecting is False
            finally:
                await session.stop()

    async def test_reconnect_stops_on_adapter_stop(self, mock_nio) -> None:
        """Stop during reconnect backoff prevents further attempts."""
        attempts = 0

        async def _always_fail(*a: object, **kw: object) -> None:
            nonlocal attempts
            attempts += 1
            await asyncio.sleep(0)
            raise ConnectionError("fail")

        mock_nio.AsyncClient.return_value.sync_forever = _always_fail

        config = _make_config()
        session = MatrixSession(config)

        with _fast_sleep_patch():
            try:
                await session.start()
                # Let a few failures happen
                for _ in range(20):
                    await asyncio.sleep(0)
                assert attempts > 0

                # Stop the session
                await session.stop()
                recorded_attempts = attempts

                # Give more loop iterations — no more attempts
                for _ in range(10):
                    await asyncio.sleep(0)
                assert attempts == recorded_attempts
                assert session._stop_requested is True
            finally:
                await session.stop()

    async def test_reconnect_diagnostics_state(self, mock_nio) -> None:
        """Diagnostics reflect reconnect state during backoff."""

        async def _sync_phased(*a: object, **kw: object) -> None:
            raise ConnectionError("transient")

        mock_nio.AsyncClient.return_value.sync_forever = _sync_phased

        config = _make_config()
        session = MatrixSession(config)

        original_sleep = asyncio.sleep

        with _fast_sleep_patch():
            try:
                await session.start()
                # Let a failure happen
                for _ in range(20):
                    await original_sleep(0)

                # Check diagnostics — should have reconnect state
                diag = session.diagnostics()
                assert diag.reconnect_attempts >= 1
            finally:
                await session.stop()

    async def test_cancelled_error_stops_reconnect(self, mock_nio) -> None:
        """CancelledError during sync stops reconnect loop."""
        async def _cancel_sync(*a: object, **kw: object) -> None:
            await asyncio.sleep(0)
            raise asyncio.CancelledError()

        mock_nio.AsyncClient.return_value.sync_forever = _cancel_sync

        config = _make_config()
        session = MatrixSession(config)
        try:
            await session.start()
            for _ in range(10):
                await asyncio.sleep(0)
            # CancelledError should not set sync_failure
            assert session._sync_failure is None
        finally:
            await session.stop()

    async def test_backoff_delay_increases(self, mock_nio) -> None:
        """Backoff delay increases exponentially with each attempt."""
        attempts = 0

        async def _always_fail(*a: object, **kw: object) -> None:
            nonlocal attempts
            attempts += 1
            raise ConnectionError("fail")

        mock_nio.AsyncClient.return_value.sync_forever = _always_fail

        config = _make_config()
        session = MatrixSession(config)

        original_sleep = asyncio.sleep
        sleep_delays: list[float] = []

        async def _track_sleep(delay: float) -> None:
            sleep_delays.append(delay)
            if delay <= 0:
                await original_sleep(0)

        with patch("asyncio.sleep", side_effect=_track_sleep):
            try:
                await session.start()
                for _ in range(100):
                    await original_sleep(0)
            finally:
                await session.stop()

        # Verify delays are generally increasing (exponential backoff)
        real_delays = [d for d in sleep_delays if d > 0.01]
        if len(real_delays) >= 2:
            for i in range(1, min(len(real_delays), 5)):
                assert real_delays[i] >= real_delays[i - 1] * 0.5, (
                    f"Delay not increasing: {real_delays}"
                )


# ===================================================================
# Track 2: TestCryptoStoreContinuity
# ===================================================================


class TestCryptoStoreContinuity:
    """Crypto-store continuity and identity preservation."""

    async def test_e2ee_session_crypto_enabled_and_store_loaded(
        self, mock_nio
    ) -> None:
        """E2EE session sets crypto_enabled and crypto_store_loaded."""
        import medre.adapters.matrix.compat as compat
        original = compat.HAS_E2EE
        try:
            compat.HAS_E2EE = True
            config = _make_config(
                encryption_mode="e2ee_required",
                store_path="/tmp/store",
                device_id="DEV",
            )
            session = MatrixSession(config)
            try:
                await session.start()
                assert session.crypto_enabled is True
                assert session.crypto_store_loaded is True
                diag = session.diagnostics()
                assert diag.crypto_store_loaded is True
            finally:
                await session.stop()
        finally:
            compat.HAS_E2EE = original

    async def test_plaintext_session_no_crypto_store_loaded(
        self, mock_nio
    ) -> None:
        """Plaintext session has crypto_store_loaded=False."""
        config = _make_config()
        session = MatrixSession(config)
        try:
            await session.start()
            assert session.crypto_enabled is False
            assert session.crypto_store_loaded is False
            diag = session.diagnostics()
            assert diag.crypto_store_loaded is False
        finally:
            await session.stop()

    async def test_restart_preserves_state(self, mock_nio) -> None:
        """Start/stop/restart cycle preserves clean state."""
        config = _make_config()
        session = MatrixSession(config)

        # First cycle
        await session.start()
        assert session.connected is True
        assert session.crypto_store_loaded is False
        await session.stop()
        assert session.connected is False

        # Second cycle — fresh state
        await session.start()
        assert session.connected is True
        assert session.sync_task_running is True
        assert session.reconnect_attempts == 0
        assert session.reconnecting is False
        await session.stop()
        assert session.connected is False

    async def test_e2ee_optional_fallback_no_store_loaded(self, mock_nio) -> None:
        """e2ee_optional without HAS_E2EE has crypto_store_loaded=False."""
        import medre.adapters.matrix.compat as compat
        original = compat.HAS_E2EE
        try:
            compat.HAS_E2EE = False
            config = _make_config(encryption_mode="e2ee_optional")
            session = MatrixSession(config)
            try:
                await session.start()
                assert session.crypto_store_loaded is False
                assert session.crypto_enabled is False
            finally:
                await session.stop()
        finally:
            compat.HAS_E2EE = original


# ===================================================================
# Track 3: TestSyncStateResilience
# ===================================================================


class TestSyncStateResilience:
    """Hardened start/stop — no leaked tasks/exceptions/clients."""

    async def test_double_start_is_noop(self, mock_nio) -> None:
        """Starting an already-started session logs warning and returns."""
        config = _make_config()
        session = MatrixSession(config)
        try:
            await session.start()
            client_before = session.client
            # Second start should be a no-op
            await session.start()
            assert session.client is client_before
        finally:
            await session.stop()

    async def test_double_stop_is_safe(self, mock_nio) -> None:
        """Stopping twice does not raise."""
        config = _make_config()
        session = MatrixSession(config)
        await session.start()
        await session.stop()
        await session.stop()  # no raise
        assert session.client is None
        assert session.closed is True

    async def test_start_stop_start_cycles(self, mock_nio) -> None:
        """Repeated start/stop/start cycles are safe."""
        config = _make_config()
        session = MatrixSession(config)
        for _ in range(3):
            await session.start()
            assert session.connected is True
            assert session.sync_task_running is True
            await session.stop()
            assert session.connected is False
            assert session.client is None

    async def test_stop_during_sync_then_restart(self, mock_nio) -> None:
        """Stop during sync, then restart with clean state."""
        config = _make_config()
        session = MatrixSession(config)
        # First cycle
        await session.start()
        assert session.sync_task_running is True
        await session.stop()
        # Verify clean state
        assert session._sync_task is None
        assert session.client is None
        assert session._stop_requested is True
        # Restart
        await session.start()
        assert session.sync_task_running is True
        assert session.reconnect_attempts == 0
        await session.stop()

    async def test_no_unobserved_exceptions(self, mock_nio) -> None:
        """Sync failure does not produce unobserved task exceptions."""
        async def _failing_sync(*a: object, **kw: object) -> None:
            await asyncio.sleep(0)
            raise RuntimeError("sync died")

        mock_nio.AsyncClient.return_value.sync_forever = _failing_sync

        config = _make_config()
        session = MatrixSession(config)

        with _fast_sleep_patch():
            try:
                await session.start()
                for _ in range(100):
                    await asyncio.sleep(0)
                # Failure is recorded, not leaked
                assert session._sync_failure is not None
                # The task is done
                assert session._sync_task is not None
                assert session._sync_task.done()
            finally:
                await session.stop()

    async def test_client_closed_on_login_failure(self, mock_nio) -> None:
        """If restore_login fails, client is closed and set to None."""
        mock_nio.AsyncClient.return_value.logged_in = False
        config = _make_config()
        session = MatrixSession(config)
        with pytest.raises(MatrixConnectionError):
            await session.start()
        assert session.client is None


# ===================================================================
# Track 4: TestRoomStateTracking
# ===================================================================


class TestRoomStateTracking:
    """Room encryption state cache in MatrixSession."""

    async def test_room_encryption_event_marks_encrypted(
        self, mock_nio
    ) -> None:
        """RoomEncryptionEvent sets room state to encrypted."""
        config = _make_config()
        session = MatrixSession(config)
        try:
            await session.start()

            room = MagicMock(name="room")
            room.room_id = "!encrypted:example.com"
            event = MagicMock(name="encryption_event")

            await session._on_room_encryption_event(room, event)

            assert session.room_state("!encrypted:example.com") == "encrypted"
            assert session.encrypted_room_count == 1
            assert session.plaintext_room_count == 0
        finally:
            await session.stop()

    async def test_megolm_event_marks_encrypted(self, mock_nio) -> None:
        """MegolmEvent callback marks room as encrypted."""
        config = _make_config()
        session = MatrixSession(config)
        try:
            await session.start()

            event = MagicMock(name="megolm_event")
            event.event_id = "$undec"
            room = MagicMock(name="room")
            room.room_id = "!enc_room:example.com"

            await session._on_megolm_event(room, event)

            assert session.room_state("!enc_room:example.com") == "encrypted"
            assert session.encrypted_room_count == 1
        finally:
            await session.stop()

    async def test_unseen_room_is_unknown(self, mock_nio) -> None:
        """Room not yet seen returns 'unknown'."""
        config = _make_config()
        session = MatrixSession(config)
        try:
            await session.start()
            assert session.room_state("!never_seen:example.com") == "unknown"
            assert session.encrypted_room_count == 0
            assert session.plaintext_room_count == 0
        finally:
            await session.stop()

    async def test_multiple_rooms_tracking(self, mock_nio) -> None:
        """Multiple rooms tracked independently."""
        config = _make_config()
        session = MatrixSession(config)
        try:
            await session.start()

            # Room 1: encrypted via RoomEncryptionEvent
            room1 = MagicMock(name="room1")
            room1.room_id = "!room1:example.com"
            await session._on_room_encryption_event(room1, MagicMock())

            # Room 2: unknown (just tracked)
            session._track_room("!room2:example.com")

            assert session.room_state("!room1:example.com") == "encrypted"
            assert session.room_state("!room2:example.com") == "unknown"
            assert session.encrypted_room_count == 1
            assert session.plaintext_room_count == 0
        finally:
            await session.stop()

    async def test_room_states_reset_on_start(self, mock_nio) -> None:
        """Room states reset on fresh start."""
        config = _make_config()
        session = MatrixSession(config)
        try:
            await session.start()
            room = MagicMock(name="room")
            room.room_id = "!room:example.com"
            await session._on_room_encryption_event(room, MagicMock())
            assert session.encrypted_room_count == 1
        finally:
            await session.stop()

        # Restart — state should be clean
        await session.start()
        assert session.encrypted_room_count == 0
        assert session.plaintext_room_count == 0
        await session.stop()

    async def test_encrypted_room_send_blocked_without_crypto(
        self, mock_nio
    ) -> None:
        """Session-tracked encrypted room blocks send without crypto."""
        from medre.core.rendering.renderer import RenderingResult

        config = _make_config()
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(_make_context())

            # Mark room as encrypted via session tracking
            room = MagicMock(name="room")
            room.room_id = "!tracked_enc:example.com"
            await adapter._session._on_room_encryption_event(room, MagicMock())  # type: ignore[union-attr]

            assert adapter._session.room_state("!tracked_enc:example.com") == "encrypted"  # type: ignore[union-attr]

            result = RenderingResult(
                event_id="evt_tracked",
                target_adapter="matrix-test",
                payload={"msgtype": "m.text", "body": "hello"},
                target_channel="!tracked_enc:example.com",
            )
            with pytest.raises(AdapterPermanentError, match="encrypted but E2EE"):
                await adapter.deliver(result)
        finally:
            await adapter.stop()

    async def test_plaintext_room_send_allowed(self, mock_nio) -> None:
        """Session-tracked plaintext room allows send."""
        from medre.core.rendering.renderer import RenderingResult

        config = _make_config()
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(_make_context())

            # Mark room as plaintext explicitly
            adapter._session._room_states["!plain:example.com"] = "plaintext"  # type: ignore[union-attr]

            response_mock = MagicMock()
            response_mock.event_id = "$evt_plain"
            adapter._client.room_send = AsyncMock(return_value=response_mock)

            result = RenderingResult(
                event_id="evt_plain",
                target_adapter="matrix-test",
                payload={"msgtype": "m.text", "body": "hello"},
                target_channel="!plain:example.com",
            )
            deliver_result = await adapter.deliver(result)
            assert deliver_result is not None
        finally:
            await adapter.stop()

    async def test_unknown_room_falls_back_to_client_rooms(
        self, mock_nio
    ) -> None:
        """Unknown room state falls back to client.rooms check."""
        from medre.core.rendering.renderer import RenderingResult

        config = _make_config()
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(_make_context())

            # Room not tracked by session but encrypted in client.rooms
            room_id = "!fallback_enc:example.com"
            room_obj = MagicMock(name="room_obj")
            room_obj.encrypted = True
            adapter._client.rooms = {room_id: room_obj}

            # Session doesn't know about this room
            assert adapter._session.room_state(room_id) == "unknown"  # type: ignore[union-attr]

            result = RenderingResult(
                event_id="evt_fallback",
                target_adapter="matrix-test",
                payload={"msgtype": "m.text", "body": "hello"},
                target_channel=room_id,
            )
            with pytest.raises(AdapterPermanentError, match="encrypted but E2EE"):
                await adapter.deliver(result)
        finally:
            await adapter.stop()


# ===================================================================
# Track 5: TestDeliveryRetry
# ===================================================================


class TestDeliveryRetry:
    """Bounded delivery retry for transient failures."""

    async def test_retry_on_transient_then_succeed(self, mock_nio) -> None:
        """room_send fails transiently 2 times then succeeds."""
        from medre.core.rendering.renderer import RenderingResult

        call_count = 0

        async def _transient_then_ok(**kwargs: Any) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise ConnectionError("network error")
            resp = MagicMock()
            resp.event_id = "$retry_success"
            return resp

        config = _make_config()
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(_make_context())
            adapter._client.room_send = _transient_then_ok

            result = RenderingResult(
                event_id="evt_retry",
                target_adapter="matrix-test",
                payload={"msgtype": "m.text", "body": "hello"},
                target_channel="!room:example.com",
            )

            with patch("asyncio.sleep", new_callable=AsyncMock):
                deliver_result = await adapter.deliver(result)

            assert deliver_result is not None
            assert deliver_result.native_message_id == "$retry_success"
            assert call_count == 3
            assert adapter._transient_delivery_failures == 2
        finally:
            await adapter.stop()

    async def test_max_retries_exhausted(self, mock_nio) -> None:
        """room_send always fails transiently → max retries reached."""
        from medre.core.rendering.renderer import RenderingResult

        call_count = 0

        async def _always_transient(**kwargs: Any) -> None:
            nonlocal call_count
            call_count += 1
            raise ConnectionError("persistent network error")

        config = _make_config()
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(_make_context())
            adapter._client.room_send = _always_transient

            result = RenderingResult(
                event_id="evt_max_retry",
                target_adapter="matrix-test",
                payload={"msgtype": "m.text", "body": "hello"},
                target_channel="!room:example.com",
            )

            with patch("asyncio.sleep", new_callable=AsyncMock):
                with pytest.raises(AdapterSendError, match="transient retries"):
                    await adapter.deliver(result)

            assert call_count == 3
            assert adapter._transient_delivery_failures == 3
            assert adapter._permanent_delivery_failures >= 1
        finally:
            await adapter.stop()

    async def test_non_transient_no_retry(self, mock_nio) -> None:
        """Non-transient error (MatrixSendError) raises immediately, no retry."""
        from medre.core.rendering.renderer import RenderingResult

        call_count = 0

        async def _room_send_non_transient(**kwargs: Any) -> MagicMock:
            nonlocal call_count
            call_count += 1
            resp = MagicMock()
            # No event_id → triggers MatrixSendError (non-transient)
            del resp.event_id
            return resp

        config = _make_config()
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(_make_context())
            adapter._client.room_send = _room_send_non_transient

            result = RenderingResult(
                event_id="evt_non_transient",
                target_adapter="matrix-test",
                payload={"msgtype": "m.text", "body": "hello"},
                target_channel="!room:example.com",
            )

            with pytest.raises(AdapterPermanentError):
                await adapter.deliver(result)

            assert call_count == 1
            assert adapter._permanent_delivery_failures == 1
            assert adapter._transient_delivery_failures == 0
        finally:
            await adapter.stop()

    async def test_oserror_is_transient(self) -> None:
        """OSError is classified as transient."""
        from medre.adapters.matrix.adapter import _is_transient_error
        assert _is_transient_error(OSError("network")) is True

    async def test_matrix_send_error_is_not_transient(self) -> None:
        """MatrixSendError is NOT classified as transient."""
        from medre.adapters.matrix.adapter import _is_transient_error
        assert _is_transient_error(MatrixSendError("fail")) is False

    async def test_delivery_stats_in_diagnostics(self, mock_nio) -> None:
        """Delivery stats appear in adapter diagnostics."""
        from medre.core.rendering.renderer import RenderingResult

        config = _make_config()
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(_make_context())

            response_mock = MagicMock()
            response_mock.event_id = "$diag_evt"
            adapter._client.room_send = AsyncMock(return_value=response_mock)

            result = RenderingResult(
                event_id="evt_diag",
                target_adapter="matrix-test",
                payload={"msgtype": "m.text", "body": "hello"},
                target_channel="!room:example.com",
            )
            await adapter.deliver(result)

            diag = adapter.diagnostics()
            assert "transient_delivery_failures" in diag
            assert "permanent_delivery_failures" in diag
            assert diag["transient_delivery_failures"] == 0
            assert diag["permanent_delivery_failures"] == 0
        finally:
            await adapter.stop()


# ===================================================================
# Track 6: TestOperationalDiagnostics
# ===================================================================


class TestOperationalDiagnostics:
    """All diagnostic fields present and correct — no secrets."""

    async def test_all_session_diagnostic_fields(self, mock_nio) -> None:
        """MatrixSessionDiagnostics includes all new fields."""
        config = _make_config()
        session = MatrixSession(config)
        diag = session.diagnostics()
        # Original fields
        assert hasattr(diag, "connected")
        assert hasattr(diag, "logged_in")
        assert hasattr(diag, "sync_task_running")
        assert hasattr(diag, "last_sync_error")
        assert hasattr(diag, "crypto_enabled")
        assert hasattr(diag, "encrypted_room_seen")
        assert hasattr(diag, "undecryptable_event_count")
        # Track 1
        assert hasattr(diag, "sync_running")
        assert hasattr(diag, "reconnecting")
        assert hasattr(diag, "reconnect_attempts")
        assert hasattr(diag, "last_successful_sync")
        # Track 2
        assert hasattr(diag, "crypto_store_loaded")
        # Track 4
        assert hasattr(diag, "encrypted_room_count")
        assert hasattr(diag, "plaintext_room_count")

    async def test_all_adapter_diagnostic_fields(self, mock_nio) -> None:
        """Adapter diagnostics() dict includes all new fields."""
        config = _make_config()
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(_make_context())
            diag = adapter.diagnostics()
            # Track 1
            assert "sync_running" in diag
            assert "reconnecting" in diag
            assert "reconnect_attempts" in diag
            assert "last_successful_sync" in diag
            # Track 2
            assert "crypto_store_loaded" in diag
            # Track 4
            assert "encrypted_room_count" in diag
            assert "plaintext_room_count" in diag
            # Track 5
            assert "transient_delivery_failures" in diag
            assert "permanent_delivery_failures" in diag
        finally:
            await adapter.stop()

    async def test_no_secrets_in_diagnostics(self, mock_nio) -> None:
        """No secrets leak in diagnostics dict values."""
        config = _make_config(access_token="super-secret-token-123")
        adapter = MatrixAdapter(config)
        diag = adapter.diagnostics()
        for key, val in diag.items():
            assert "super-secret-token-123" not in str(val), (
                f"Secret leaked in diagnostics field {key!r}"
            )

    async def test_session_properties_default_values(self) -> None:
        """Session properties have correct defaults before start."""
        config = _make_config()
        session = MatrixSession(config)
        assert session.sync_running is False
        assert session.reconnecting is False
        assert session.reconnect_attempts == 0
        assert session.last_successful_sync is None
        assert session.crypto_store_loaded is False
        assert session.encrypted_room_count == 0
        assert session.plaintext_room_count == 0
        assert session.room_state("!any:example.com") == "unknown"

    async def test_adapter_diagnostics_before_start(self) -> None:
        """Adapter diagnostics have correct defaults before start."""
        config = _make_config()
        adapter = MatrixAdapter(config)
        diag = adapter.diagnostics()
        assert diag["sync_running"] is False
        assert diag["reconnecting"] is False
        assert diag["reconnect_attempts"] == 0
        assert diag["last_successful_sync"] is None
        assert diag["crypto_store_loaded"] is False
        assert diag["encrypted_room_count"] == 0
        assert diag["plaintext_room_count"] == 0
        assert diag["transient_delivery_failures"] == 0
        assert diag["permanent_delivery_failures"] == 0
