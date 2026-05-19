"""Tests for MatrixSession lifecycle boundary — core lifecycle and adapter behavior.

Covers:
  - MatrixSession lifecycle (start/stop/diagnostics)
  - Adapter start behavior per encryption_mode
  - Adapter diagnostics with crypto scaffold fields
  - Adapter delegates lifecycle to MatrixSession

No test requires mindroom-nio[e2e].
"""

from __future__ import annotations

import asyncio
import logging
import sys
from unittest.mock import MagicMock, patch

import pytest

from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.matrix.errors import MatrixConnectionError
from medre.adapters.matrix.session import MatrixSession, MatrixSessionDiagnostics
from tests.helpers.matrix_session import (
    make_matrix_config,
    make_matrix_context,
)
from tests.helpers.matrix_session import mock_nio as _mock_nio  # noqa: F401

# ===================================================================
# TestMatrixSessionLifecycle
# ===================================================================


class TestMatrixSessionLifecycle:
    """MatrixSession start/stop/diagnostics."""

    async def test_session_start_creates_client(self, mock_nio) -> None:
        config = make_matrix_config()
        session = MatrixSession(config)
        try:
            await session.start()
            assert session.client is not None
            assert session.connected is True
            assert session.logged_in is True
        finally:
            await session.stop()

    async def test_session_stop_clears_client(self, mock_nio) -> None:
        config = make_matrix_config()
        session = MatrixSession(config)
        await session.start()
        await session.stop()
        assert session.client is None
        assert session.connected is False

    async def test_session_stop_before_start(self) -> None:
        config = make_matrix_config()
        session = MatrixSession(config)
        await session.stop()  # no raise

    async def test_session_login_failure(self, mock_nio) -> None:
        mock_nio.AsyncClient.return_value.logged_in = False
        config = make_matrix_config()
        session = MatrixSession(config)
        with pytest.raises(MatrixConnectionError, match="failed to authenticate"):
            await session.start()

    async def test_session_no_nio_raises(self) -> None:
        """Session raises ImportError when nio is not available."""
        config = make_matrix_config()
        session = MatrixSession(config)
        with patch.dict(sys.modules, {"nio": None}):
            with pytest.raises(ImportError):
                await session.start()

    async def test_session_registers_callback(self, mock_nio) -> None:
        cb = MagicMock()
        config = make_matrix_config()
        session = MatrixSession(config, message_callback=cb)
        try:
            await session.start()
            # Four callbacks: message types + ReactionEvent +
            # MegolmEvent + RoomEncryptionEvent
            assert mock_nio.AsyncClient.return_value.add_event_callback.call_count == 4
        finally:
            await session.stop()

    async def test_session_sync_task_running(self, mock_nio) -> None:
        config = make_matrix_config()
        session = MatrixSession(config)
        try:
            await session.start()
            assert session.sync_task_running is True
        finally:
            await session.stop()

    async def test_session_sync_task_not_running_after_stop(self, mock_nio) -> None:
        config = make_matrix_config()
        session = MatrixSession(config)
        await session.start()
        await session.stop()
        assert session.sync_task_running is False

    async def test_session_sync_failure_recorded(self, mock_nio) -> None:
        async def _failing_sync(*a: object, **kw: object) -> None:
            await asyncio.sleep(0)
            raise RuntimeError("sync died")

        mock_nio.AsyncClient.return_value.sync_forever = _failing_sync
        config = make_matrix_config()
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
        config = make_matrix_config()
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
        config = make_matrix_config(store_path="/tmp/store", device_id="DEV")
        session = MatrixSession(config)
        diag = session.diagnostics()
        assert diag.store_path_configured is True
        assert diag.device_id_configured is True

    def test_diagnostics_encryption_mode_e2ee_optional(self) -> None:
        config = make_matrix_config(encryption_mode="e2ee_optional")
        session = MatrixSession(config)
        diag = session.diagnostics()
        assert diag.encryption_mode == "e2ee_optional"
        assert diag.crypto_enabled is False

    async def test_diagnostics_after_start(self, mock_nio) -> None:
        config = make_matrix_config()
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
        config = make_matrix_config(access_token="super-secret-token-123")
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
                assert "super-secret-token-123" not in str(
                    val
                ), f"Secret leaked in diagnostics field {key!r}"
        finally:
            await session.stop()


# ===================================================================
# TestAdapterStartBehavior
# ===================================================================


class TestAdapterStartBehavior:
    """Adapter start behavior per encryption_mode."""

    async def test_plaintext_starts_normally(self, mock_nio) -> None:
        config = make_matrix_config()
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(make_matrix_context())
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
            config = make_matrix_config(
                encryption_mode="e2ee_required",
                store_path="/tmp/store",
                device_id="DEV",
            )
            adapter = MatrixAdapter(config)
            with pytest.raises(
                MatrixConnectionError,
                match="mindroom-nio\\[e2e\\] not installed",
            ):
                await adapter.start(make_matrix_context())
        finally:
            compat.HAS_E2EE = original

    async def test_e2ee_required_succeeds_with_e2ee_deps(self, mock_nio) -> None:
        """e2ee_required starts with crypto_enabled=True when HAS_E2EE=True."""
        import medre.adapters.matrix.compat as compat

        original = compat.HAS_E2EE
        try:
            compat.HAS_E2EE = True
            config = make_matrix_config(
                encryption_mode="e2ee_required",
                store_path="/tmp/store",
                device_id="DEV",
            )
            adapter = MatrixAdapter(config)
            try:
                await adapter.start(make_matrix_context())
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
            config = make_matrix_config(encryption_mode="e2ee_optional")
            adapter = MatrixAdapter(config)
            try:
                await adapter.start(make_matrix_context())
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
            config = make_matrix_config(
                encryption_mode="e2ee_optional",
                store_path="/tmp/store",
                device_id="DEV",
            )
            adapter = MatrixAdapter(config)
            try:
                await adapter.start(make_matrix_context())
                assert adapter._session is not None
                assert adapter._session.crypto_enabled is True
                diag = adapter.diagnostics()
                assert diag["crypto_enabled"] is True
            finally:
                await adapter.stop()
        finally:
            compat.HAS_E2EE = original

    async def test_e2ee_optional_falls_back_on_crypto_failure(self, mock_nio) -> None:
        """e2ee_optional falls back to plaintext when crypto setup fails."""
        import medre.adapters.matrix.compat as compat

        original = compat.HAS_E2EE
        try:
            compat.HAS_E2EE = True
            # Make ClientConfig raise so crypto setup fails
            mock_nio.ClientConfig.side_effect = TypeError("nope")
            config = make_matrix_config(
                encryption_mode="e2ee_optional",
                # no store_path, no device_id
            )
            adapter = MatrixAdapter(config)
            try:
                await adapter.start(make_matrix_context())
                assert adapter._session is not None
                # Crypto failed → plaintext fallback
                assert adapter._session.crypto_enabled is False
            finally:
                await adapter.stop()
        finally:
            compat.HAS_E2EE = original
            mock_nio.ClientConfig.side_effect = None

    async def test_plaintext_no_nio_raises(self) -> None:
        config = make_matrix_config()
        adapter = MatrixAdapter(config)
        with patch("medre.adapters.matrix.adapter.HAS_NIO", False):
            with pytest.raises(
                MatrixConnectionError, match="mindroom-nio not installed"
            ):
                await adapter.start(make_matrix_context())


# ===================================================================
# TestAdapterDiagnostics
# ===================================================================


class TestAdapterDiagnostics:
    """Adapter diagnostics expose crypto scaffold fields."""

    def test_diagnostics_before_start(self) -> None:
        config = make_matrix_config(encryption_mode="e2ee_optional")
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
        config = make_matrix_config()
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(make_matrix_context())
            diag = adapter.diagnostics()
            assert diag["connected"] is True
            assert diag["logged_in"] is True
            assert diag["sync_task_running"] is True
            assert diag["crypto_enabled"] is False
        finally:
            await adapter.stop()

    def test_diagnostics_no_secrets_in_dict(self) -> None:
        config = make_matrix_config(access_token="super-secret-token-123")
        adapter = MatrixAdapter(config)
        diag = adapter.diagnostics()
        for key, val in diag.items():
            assert "super-secret-token-123" not in str(
                val
            ), f"Secret leaked in diagnostics field {key!r}"


# ===================================================================
# TestAdapterDelegatesToSession
# ===================================================================


class TestAdapterDelegatesToSession:
    """Adapter delegates lifecycle to MatrixSession."""

    async def test_adapter_uses_session_for_client(self, mock_nio) -> None:
        config = make_matrix_config()
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(make_matrix_context())
            assert adapter._session is not None
            assert adapter._client is adapter._session.client
        finally:
            await adapter.stop()

    async def test_adapter_stop_destroys_session(self, mock_nio) -> None:
        config = make_matrix_config()
        adapter = MatrixAdapter(config)
        await adapter.start(make_matrix_context())
        await adapter.stop()
        assert adapter._session is None
        assert adapter._client is None

    async def test_adapter_health_check_delegates(self, mock_nio) -> None:
        config = make_matrix_config()
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(make_matrix_context())
            info = await adapter.health_check()
            assert info.health == "healthy"
        finally:
            await adapter.stop()

    async def test_adapter_health_unknown_after_stop(self, mock_nio) -> None:
        config = make_matrix_config()
        adapter = MatrixAdapter(config)
        await adapter.start(make_matrix_context())
        await adapter.stop()
        info = await adapter.health_check()
        assert info.health == "unknown"

    async def test_adapter_health_unknown_before_start(self) -> None:
        config = make_matrix_config()
        adapter = MatrixAdapter(config)
        info = await adapter.health_check()
        assert info.health == "unknown"

    async def test_adapter_double_stop_idempotent(self, mock_nio) -> None:
        config = make_matrix_config()
        adapter = MatrixAdapter(config)
        await adapter.start(make_matrix_context())
        await adapter.stop()
        await adapter.stop()
        assert adapter._session is None


# ===================================================================
# Reaction callback registration
# ===================================================================


class TestReactionCallbackRegistration:
    """Verify ReactionEvent is registered as an event callback."""

    async def test_reaction_callback_registered(self, mock_nio) -> None:
        """MatrixSession registers a callback for nio.ReactionEvent."""
        cb = MagicMock()
        config = make_matrix_config()
        session = MatrixSession(config, message_callback=cb)
        try:
            await session.start()
            calls = mock_nio.AsyncClient.return_value.add_event_callback.call_args_list
            # Check that one of the calls includes ReactionEvent
            reaction_registered = any(
                mock_nio.ReactionEvent in call[0][1]
                for call in calls
                if len(call[0]) >= 2
            )
            assert (
                reaction_registered
            ), "ReactionEvent not found in any add_event_callback call"
        finally:
            await session.stop()

    async def test_reaction_callback_graceful_without_reaction_event(
        self, mock_nio
    ) -> None:
        """If nio lacks ReactionEvent, start still succeeds (no crash)."""
        del mock_nio.ReactionEvent
        cb = MagicMock()
        config = make_matrix_config()
        session = MatrixSession(config, message_callback=cb)
        try:
            await session.start()
            # Should not raise; reaction callback simply skipped
            assert session.connected is True
        finally:
            await session.stop()

    async def test_reaction_callback_uses_same_handler(self, mock_nio) -> None:
        """The reaction callback is the same function as message callback."""
        cb = MagicMock()
        config = make_matrix_config()
        session = MatrixSession(config, message_callback=cb)
        try:
            await session.start()
            calls = mock_nio.AsyncClient.return_value.add_event_callback.call_args_list
            # Find the call that includes ReactionEvent
            for call in calls:
                if len(call[0]) >= 2 and mock_nio.ReactionEvent in call[0][1]:
                    assert call[0][0] is cb
                    break
            else:
                pytest.fail("No ReactionEvent callback found")
        finally:
            await session.stop()


# ===================================================================
# _reaction_event_classes helper
# ===================================================================


class TestReactionEventClassesHelper:
    """Tests for the module-level _reaction_event_classes helper."""

    def test_finds_top_level_reaction_event(self) -> None:
        """Finds ReactionEvent at nio top level."""
        from medre.adapters.matrix.session import _reaction_event_classes

        nio_mod = MagicMock(name="nio")
        cls = type("ReactionEvent", (), {})
        nio_mod.ReactionEvent = cls
        events = MagicMock(name="nio.events")
        del events.ReactionEvent
        room_events = MagicMock(name="nio.events.room_events")
        del room_events.ReactionEvent
        events.room_events = room_events
        nio_mod.events = events

        result = _reaction_event_classes(nio_mod)
        assert result == (cls,)

    def test_finds_events_reaction_event_when_top_level_absent(self) -> None:
        """Falls back to nio.events.ReactionEvent when top-level missing."""
        from medre.adapters.matrix.session import _reaction_event_classes

        nio_mod = MagicMock(name="nio")
        # No top-level ReactionEvent
        del nio_mod.ReactionEvent
        events = MagicMock(name="nio.events")
        cls = type("ReactionEvent", (), {})
        events.ReactionEvent = cls
        room_events = MagicMock(name="nio.events.room_events")
        del room_events.ReactionEvent
        events.room_events = room_events
        nio_mod.events = events

        result = _reaction_event_classes(nio_mod)
        assert result == (cls,)

    def test_finds_room_events_reaction_event(self) -> None:
        """Falls back to nio.events.room_events.ReactionEvent."""
        from medre.adapters.matrix.session import _reaction_event_classes

        nio_mod = MagicMock(name="nio")
        del nio_mod.ReactionEvent
        events = MagicMock(name="nio.events")
        del events.ReactionEvent
        room_events = MagicMock(name="nio.events.room_events")
        cls = type("ReactionEvent", (), {})
        room_events.ReactionEvent = cls
        events.room_events = room_events
        nio_mod.events = events

        result = _reaction_event_classes(nio_mod)
        assert result == (cls,)

    def test_returns_empty_tuple_when_no_class(self) -> None:
        """Returns empty tuple when no ReactionEvent exists anywhere."""
        from medre.adapters.matrix.session import _reaction_event_classes

        nio_mod = MagicMock(name="nio")
        del nio_mod.ReactionEvent
        events = MagicMock(name="nio.events")
        del events.ReactionEvent
        room_events = MagicMock(name="nio.events.room_events")
        del room_events.ReactionEvent
        events.room_events = room_events
        nio_mod.events = events

        result = _reaction_event_classes(nio_mod)
        assert result == ()

    def test_deduplicates_while_preserving_order(self) -> None:
        """De-duplicates classes that appear in multiple locations."""
        from medre.adapters.matrix.session import _reaction_event_classes

        nio_mod = MagicMock(name="nio")
        cls = type("ReactionEvent", (), {})
        nio_mod.ReactionEvent = cls
        events = MagicMock(name="nio.events")
        events.ReactionEvent = cls  # Same class object
        room_events = MagicMock(name="nio.events.room_events")
        room_events.ReactionEvent = cls  # Same class again
        events.room_events = room_events
        nio_mod.events = events

        result = _reaction_event_classes(nio_mod)
        assert result == (cls,)  # Only one instance

    def test_no_events_module_returns_top_level_only(self) -> None:
        """Gracefully handles nio without events submodule."""
        from medre.adapters.matrix.session import _reaction_event_classes

        nio_mod = MagicMock(name="nio")
        cls = type("ReactionEvent", (), {})
        nio_mod.ReactionEvent = cls
        # Make getattr(nio_mod, "events", None) return None
        del nio_mod.events

        result = _reaction_event_classes(nio_mod)
        assert result == (cls,)


class TestReactionCallbackMultiClass:
    """Verify callback registration for each discovered reaction class."""

    async def test_callback_registered_for_each_discovered_class(
        self, mock_nio
    ) -> None:
        """When multiple locations expose the same class, only one registration."""
        cb = MagicMock()
        config = make_matrix_config()
        session = MatrixSession(config, message_callback=cb)
        try:
            await session.start()
            calls = mock_nio.AsyncClient.return_value.add_event_callback.call_args_list
            # Find the call that includes ReactionEvent
            reaction_calls = [
                call
                for call in calls
                if len(call[0]) >= 2 and mock_nio.ReactionEvent in call[0][1]
            ]
            assert len(reaction_calls) == 1, (
                f"Expected exactly 1 ReactionEvent callback registration, "
                f"got {len(reaction_calls)}"
            )
        finally:
            await session.stop()

    async def test_no_reaction_event_logs_debug(self, mock_nio) -> None:
        """When no ReactionEvent found, start succeeds with debug log."""
        del mock_nio.ReactionEvent
        # Also ensure nio.events doesn't have it
        del mock_nio.events.ReactionEvent

        cb = MagicMock()
        config = make_matrix_config()
        logger = logging.getLogger("test.no_reaction")
        session = MatrixSession(config, message_callback=cb, logger=logger)
        try:
            await session.start()
            assert session.connected is True
        finally:
            await session.stop()
