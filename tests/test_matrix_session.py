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
import importlib
import logging
import sys
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

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
            # Five callbacks: message types + ReactionEvent +
            # MegolmEvent + RoomEncryptionEvent + InviteMemberEvent
            assert mock_nio.AsyncClient.return_value.add_event_callback.call_count == 5
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


def _no_reaction_importlib():
    """Patch importlib.import_module so importlib fallback returns nothing.

    Prevents the importlib fallback in _reaction_event_classes from
    hitting the real installed nio package during unit tests.
    """
    _empty_mod = MagicMock(name="empty_importlib_mod")
    del _empty_mod.ReactionEvent

    def _fake_import(name, *a, **kw):
        if name in ("nio.events", "nio.events.room_events"):
            return _empty_mod
        return importlib.import_module(name, *a, **kw)

    return patch("medre.adapters.matrix.session.importlib.import_module", side_effect=_fake_import)


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

        with _no_reaction_importlib():
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

        with _no_reaction_importlib():
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

        with _no_reaction_importlib():
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

        with _no_reaction_importlib():
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

        with _no_reaction_importlib():
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

        with _no_reaction_importlib():
            result = _reaction_event_classes(nio_mod)
        assert result == (cls,)

    def test_importlib_fallback_finds_reaction_event(self) -> None:
        """importlib fallback discovers ReactionEvent when getattr paths miss.

        Monkeypatch nio so ReactionEvent is absent from top-level and from
        events/room_events attributes, but importlib.import_module("nio.events")
        returns a module with ReactionEvent.
        """
        import types

        from medre.adapters.matrix.session import _reaction_event_classes

        cls = type("ReactionEvent", (), {})

        # Build an nio.events module that carries ReactionEvent
        events_mod = types.ModuleType("nio.events")
        events_mod.ReactionEvent = cls  # type: ignore[attr-defined]

        # nio.events.room_events without ReactionEvent
        room_events_mod = types.ModuleType("nio.events.room_events")

        # Build the fake nio module: no ReactionEvent anywhere via getattr
        nio_mod = MagicMock(name="nio")
        del nio_mod.ReactionEvent
        # events attribute returns a module WITHOUT ReactionEvent
        fake_events = MagicMock(name="nio.events")
        del fake_events.ReactionEvent
        fake_room_events = MagicMock(name="nio.events.room_events")
        del fake_room_events.ReactionEvent
        fake_events.room_events = fake_room_events
        nio_mod.events = fake_events

        # Inject the real modules into sys.modules so importlib finds them
        saved_nio = sys.modules.get("nio")
        saved_nio_events = sys.modules.get("nio.events")
        saved_nio_room = sys.modules.get("nio.events.room_events")
        try:
            sys.modules["nio.events"] = events_mod
            sys.modules["nio.events.room_events"] = room_events_mod

            result = _reaction_event_classes(nio_mod)
            assert result == (cls,), (
                f"Expected importlib fallback to find ReactionEvent, got {result}"
            )
        finally:
            # Restore sys.modules
            if saved_nio is None:
                sys.modules.pop("nio", None)
            else:
                sys.modules["nio"] = saved_nio
            if saved_nio_events is None:
                sys.modules.pop("nio.events", None)
            else:
                sys.modules["nio.events"] = saved_nio_events
            if saved_nio_room is None:
                sys.modules.pop("nio.events.room_events", None)
            else:
                sys.modules["nio.events.room_events"] = saved_nio_room


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


# ===================================================================
# Part D — ensure_joined / ensure_joined_rooms / invite handling
# ===================================================================


class TestEnsureJoined:
    """MatrixSession.ensure_joined behaviour."""

    async def test_returns_true_when_already_joined(self, mock_nio) -> None:
        """Room already in client.rooms → True without calling join."""
        config = make_matrix_config()
        session = MatrixSession(config)
        try:
            await session.start()
            mock_client = mock_nio.AsyncClient.return_value
            mock_client.rooms = {"!room:server": MagicMock()}
            result = await session.ensure_joined("!room:server")
            assert result is True
            mock_client.join.assert_not_called()
        finally:
            await session.stop()

    async def test_calls_join_when_not_joined(self, mock_nio) -> None:
        """Room not in client.rooms → join called, True on success."""
        config = make_matrix_config()
        session = MatrixSession(config)
        try:
            await session.start()
            mock_client = mock_nio.AsyncClient.return_value
            mock_client.rooms = {}
            result = await session.ensure_joined("!room:server")
            assert result is True
            mock_client.join.assert_called_once_with("!room:server")
        finally:
            await session.stop()

    async def test_returns_false_on_join_failure(self, mock_nio) -> None:
        """Join returns error response → False, no crash."""
        config = make_matrix_config()
        session = MatrixSession(config)
        try:
            await session.start()
            mock_client = mock_nio.AsyncClient.return_value
            mock_client.rooms = {}
            # Return a response without room_id (error-like).
            error_resp = MagicMock(name="join_error")
            del error_resp.room_id
            error_resp.__str__ = lambda self: "forbidden"
            mock_client.join = AsyncMock(return_value=error_resp)
            result = await session.ensure_joined("!room:server")
            assert result is False
        finally:
            await session.stop()

    async def test_returns_false_on_join_exception(self, mock_nio) -> None:
        """Join raises exception → False, no crash."""
        config = make_matrix_config()
        session = MatrixSession(config)
        try:
            await session.start()
            mock_client = mock_nio.AsyncClient.return_value
            mock_client.rooms = {}
            mock_client.join = AsyncMock(side_effect=RuntimeError("network"))
            result = await session.ensure_joined("!room:server")
            assert result is False
        finally:
            await session.stop()

    async def test_returns_false_when_client_none(self) -> None:
        """Client is None → warning logged, False returned."""
        config = make_matrix_config()
        session = MatrixSession(config)
        result = await session.ensure_joined("!room:server")
        assert result is False

    async def test_returns_false_for_invalid_room_id(self, mock_nio) -> None:
        """Non-string or empty room_id → False."""
        config = make_matrix_config()
        session = MatrixSession(config)
        try:
            await session.start()
            assert await session.ensure_joined("") is False
            assert await session.ensure_joined(123) is False  # type: ignore[arg-type]
        finally:
            await session.stop()


class TestEnsureJoinedRooms:
    """MatrixSession.ensure_joined_rooms batch behaviour."""

    async def test_joins_multiple_rooms(self, mock_nio) -> None:
        """Joins each room, returns dict of results."""
        config = make_matrix_config()
        session = MatrixSession(config)
        try:
            await session.start()
            mock_client = mock_nio.AsyncClient.return_value
            mock_client.rooms = {}
            results = await session.ensure_joined_rooms(
                ["!a:server", "!b:server"]
            )
            assert results == {"!a:server": True, "!b:server": True}
            assert mock_client.join.call_count == 2
        finally:
            await session.stop()

    async def test_deduplicates_rooms(self, mock_nio) -> None:
        """Duplicate room IDs are joined only once."""
        config = make_matrix_config()
        session = MatrixSession(config)
        try:
            await session.start()
            mock_client = mock_nio.AsyncClient.return_value
            mock_client.rooms = {}
            results = await session.ensure_joined_rooms(
                ["!a:server", "!a:server", "!b:server"]
            )
            assert len(results) == 2
            assert mock_client.join.call_count == 2
        finally:
            await session.stop()

    async def test_failure_does_not_prevent_others(self, mock_nio) -> None:
        """One join failure does not prevent other rooms from being attempted."""
        config = make_matrix_config()
        session = MatrixSession(config)
        try:
            await session.start()
            mock_client = mock_nio.AsyncClient.return_value
            mock_client.rooms = {}

            call_count = 0

            async def _conditional_join(rid: str) -> MagicMock:
                nonlocal call_count
                call_count += 1
                if rid == "!bad:server":
                    err = MagicMock(name="error")
                    del err.room_id
                    return err
                resp = MagicMock(name="ok")
                resp.room_id = rid
                return resp

            mock_client.join = AsyncMock(side_effect=_conditional_join)
            results = await session.ensure_joined_rooms(
                ["!bad:server", "!good:server"]
            )
            assert results["!bad:server"] is False
            assert results["!good:server"] is True
        finally:
            await session.stop()

    async def test_empty_list_returns_empty(self, mock_nio) -> None:
        """Empty iterable returns empty dict."""
        config = make_matrix_config()
        session = MatrixSession(config)
        try:
            await session.start()
            results = await session.ensure_joined_rooms([])
            assert results == {}
        finally:
            await session.stop()


class TestRegisterInviteCallback:
    """Direct tests for _register_invite_callback (session.py:625-647)."""

    def test_returns_early_when_client_none(self) -> None:
        """When _client is None, method returns without error (line 632-633)."""
        config = make_matrix_config()
        session = MatrixSession(config)
        assert session._client is None
        # Should not raise and should not attempt any registration.
        session._register_invite_callback()

    def test_catches_import_error(self, mock_nio) -> None:
        """ImportError during `import nio` is caught, no crash (line 646)."""
        config = make_matrix_config()
        session = MatrixSession(config)
        # Force a real client so we pass the None guard.
        session._client = MagicMock(name="mock_client")
        with patch.dict(sys.modules, {"nio": None}):
            session._register_invite_callback()
        # Client should not have add_event_callback called (import failed).
        session._client.add_event_callback.assert_not_called()

    def test_catches_attribute_error(self, mock_nio) -> None:
        """AttributeError during getattr is caught, no crash (line 646)."""
        config = make_matrix_config()
        session = MatrixSession(config)
        mock_client = MagicMock(name="mock_client")
        session._client = mock_client

        # Build nio where getattr on .events raises AttributeError,
        # triggering the except clause on line 646.
        failing_nio = MagicMock(name="nio")
        del failing_nio.InviteMemberEvent  # top-level getattr → None

        # Replace .events with an object that raises on *any* attribute access.
        class _Boom:
            def __getattr__(self, name: str) -> None:
                raise AttributeError(f"no attribute {name}")
        failing_nio.events = _Boom()

        with patch.dict(sys.modules, {"nio": failing_nio, "nio.events": _Boom()}):
            session._register_invite_callback()
        mock_client.add_event_callback.assert_not_called()

    def test_no_registration_when_invite_cls_none(self, mock_nio) -> None:
        """When InviteMemberEvent not found, no callback is registered (line 639-640)."""
        config = make_matrix_config()
        session = MatrixSession(config)
        session._client = MagicMock(name="mock_client")

        # Build nio without InviteMemberEvent anywhere.
        stripped_nio = MagicMock(name="nio_no_invite")
        del stripped_nio.InviteMemberEvent
        stripped_events = MagicMock(name="nio.events_no_invite")
        del stripped_events.InviteMemberEvent
        stripped_nio.events = stripped_events

        with patch.dict(sys.modules, {"nio": stripped_nio, "nio.events": stripped_events}):
            session._register_invite_callback()
        session._client.add_event_callback.assert_not_called()

    def test_registers_when_invite_cls_found_top_level(self, mock_nio) -> None:
        """When InviteMemberEvent found at nio top level, callback registered."""
        config = make_matrix_config()
        session = MatrixSession(config)
        session._client = MagicMock(name="mock_client")

        invite_cls = MagicMock(name="InviteMemberEvent")
        fake_nio = MagicMock(name="nio")
        fake_nio.InviteMemberEvent = invite_cls
        fake_events = MagicMock(name="nio.events")
        fake_nio.events = fake_events

        with patch.dict(sys.modules, {"nio": fake_nio, "nio.events": fake_events}):
            session._register_invite_callback()
        session._client.add_event_callback.assert_called_once()
        call_args = session._client.add_event_callback.call_args
        # Bound methods: compare by __func__ and __self__, not identity.
        registered_handler = call_args[0][0]
        assert registered_handler.__func__ is session._on_invite.__func__
        assert invite_cls in call_args[0][1]

    def test_registers_via_nio_events_fallback(self, mock_nio) -> None:
        """When InviteMemberEvent only on nio.events, callback registered."""
        config = make_matrix_config()
        session = MatrixSession(config)
        session._client = MagicMock(name="mock_client")

        invite_cls = MagicMock(name="InviteMemberEvent")
        fake_nio = MagicMock(name="nio")
        del fake_nio.InviteMemberEvent
        fake_events = MagicMock(name="nio.events")
        fake_events.InviteMemberEvent = invite_cls
        fake_nio.events = fake_events

        with patch.dict(sys.modules, {"nio": fake_nio, "nio.events": fake_events}):
            session._register_invite_callback()
        session._client.add_event_callback.assert_called_once()
        call_args = session._client.add_event_callback.call_args
        registered_handler = call_args[0][0]
        assert registered_handler.__func__ is session._on_invite.__func__
        assert invite_cls in call_args[0][1]


class TestJoinOncePaths:
    """Targeted tests for _join_once inner coroutine (session.py:688-701).

    All paths exercised via ensure_joined.
    """

    async def test_join_success_returns_true(self, mock_nio) -> None:
        """Response with room_id → True (line 688)."""
        config = make_matrix_config()
        session = MatrixSession(config)
        try:
            await session.start()
            mock_client = mock_nio.AsyncClient.return_value
            mock_client.rooms = {}
            resp = MagicMock(name="join_ok")
            resp.room_id = "!room:server"
            mock_client.join = AsyncMock(return_value=resp)
            assert await session.ensure_joined("!room:server") is True
        finally:
            await session.stop()

    async def test_join_failure_no_room_id_returns_false(self, mock_nio) -> None:
        """Response without room_id → False (line 693)."""
        config = make_matrix_config()
        session = MatrixSession(config)
        try:
            await session.start()
            mock_client = mock_nio.AsyncClient.return_value
            mock_client.rooms = {}
            err = MagicMock(name="join_error")
            del err.room_id
            err.__str__ = lambda self: "M_FORBIDDEN"
            mock_client.join = AsyncMock(return_value=err)
            assert await session.ensure_joined("!room:server") is False
        finally:
            await session.stop()

    async def test_join_exception_returns_false(self, mock_nio) -> None:
        """Exception from client.join → False (line 698)."""
        config = make_matrix_config()
        session = MatrixSession(config)
        try:
            await session.start()
            mock_client = mock_nio.AsyncClient.return_value
            mock_client.rooms = {}
            mock_client.join = AsyncMock(side_effect=ConnectionError("timeout"))
            assert await session.ensure_joined("!room:server") is False
        finally:
            await session.stop()

    async def test_finally_cleans_joining_rooms(self, mock_nio) -> None:
        """finally block removes room from _joining_rooms on success (line 699-701)."""
        config = make_matrix_config()
        session = MatrixSession(config)
        try:
            await session.start()
            mock_client = mock_nio.AsyncClient.return_value
            mock_client.rooms = {}
            resp = MagicMock(name="join_ok")
            resp.room_id = "!room:server"
            mock_client.join = AsyncMock(return_value=resp)
            await session.ensure_joined("!room:server")
            # After join completes, _joining_rooms should be cleaned up.
            assert "!room:server" not in session._joining_rooms
        finally:
            await session.stop()

    async def test_finally_cleans_joining_rooms_on_failure(self, mock_nio) -> None:
        """finally block removes room from _joining_rooms on failure (line 699-701)."""
        config = make_matrix_config()
        session = MatrixSession(config)
        try:
            await session.start()
            mock_client = mock_nio.AsyncClient.return_value
            mock_client.rooms = {}
            err = MagicMock(name="join_error")
            del err.room_id
            mock_client.join = AsyncMock(return_value=err)
            await session.ensure_joined("!room:server")
            assert "!room:server" not in session._joining_rooms
        finally:
            await session.stop()

    async def test_finally_cleans_joining_rooms_on_exception(self, mock_nio) -> None:
        """finally block removes room from _joining_rooms on exception (line 699-701)."""
        config = make_matrix_config()
        session = MatrixSession(config)
        try:
            await session.start()
            mock_client = mock_nio.AsyncClient.return_value
            mock_client.rooms = {}
            mock_client.join = AsyncMock(side_effect=RuntimeError("boom"))
            await session.ensure_joined("!room:server")
            assert "!room:server" not in session._joining_rooms
        finally:
            await session.stop()


class TestConcurrentJoinDeduplication:
    """_joining_rooms Future prevents duplicate concurrent joins."""

    async def test_concurrent_join_dedup(self, mock_nio) -> None:
        """Two concurrent ensure_joined calls for same room deduplicate."""
        config = make_matrix_config()
        session = MatrixSession(config)
        try:
            await session.start()
            mock_client = mock_nio.AsyncClient.return_value
            mock_client.rooms = {}

            join_count = 0

            async def _slow_join(rid: str) -> MagicMock:
                nonlocal join_count
                join_count += 1
                await asyncio.sleep(0)  # yield to allow concurrency
                # Simulate nio behaviour: room appears in client.rooms after join.
                mock_client.rooms[rid] = MagicMock(name=f"room_{rid}")
                resp = MagicMock(name="ok")
                resp.room_id = rid
                return resp

            mock_client.join = AsyncMock(side_effect=_slow_join)

            # Launch two concurrent joins for the same room.
            results = await asyncio.gather(
                session.ensure_joined("!room:server"),
                session.ensure_joined("!room:server"),
            )
            # Both should return True (one from actual join, one from dedup)
            assert all(results)
            # join should only have been called once due to dedup
            assert join_count == 1
        finally:
            await session.stop()

    async def test_concurrent_join_both_true_on_success(self, mock_nio) -> None:
        """Both callers get True when the underlying join succeeds."""
        config = make_matrix_config()
        session = MatrixSession(config)
        try:
            await session.start()
            mock_client = mock_nio.AsyncClient.return_value
            mock_client.rooms = {}

            async def _join(rid: str) -> MagicMock:
                await asyncio.sleep(0)
                resp = MagicMock(name="ok")
                resp.room_id = rid
                return resp

            mock_client.join = AsyncMock(side_effect=_join)

            results = await asyncio.gather(
                session.ensure_joined("!room:server"),
                session.ensure_joined("!room:server"),
            )
            assert results == [True, True]
        finally:
            await session.stop()

    async def test_concurrent_join_both_false_on_failure(self, mock_nio) -> None:
        """Both callers get False when the underlying join fails."""
        config = make_matrix_config()
        session = MatrixSession(config)
        try:
            await session.start()
            mock_client = mock_nio.AsyncClient.return_value
            mock_client.rooms = {}

            async def _failing_join(rid: str) -> MagicMock:
                await asyncio.sleep(0)
                err = MagicMock(name="error")
                del err.room_id
                return err

            mock_client.join = AsyncMock(side_effect=_failing_join)

            results = await asyncio.gather(
                session.ensure_joined("!room:server"),
                session.ensure_joined("!room:server"),
            )
            assert results == [False, False]
        finally:
            await session.stop()

    async def test_concurrent_join_both_false_on_exception(self, mock_nio) -> None:
        """Both callers get False when the underlying join raises."""
        config = make_matrix_config()
        session = MatrixSession(config)
        try:
            await session.start()
            mock_client = mock_nio.AsyncClient.return_value
            mock_client.rooms = {}

            async def _exception_join(rid: str) -> None:
                await asyncio.sleep(0)
                raise RuntimeError("network error")

            mock_client.join = AsyncMock(side_effect=_exception_join)

            results = await asyncio.gather(
                session.ensure_joined("!room:server"),
                session.ensure_joined("!room:server"),
            )
            assert results == [False, False]
        finally:
            await session.stop()

    async def test_already_joined_skips_join(self, mock_nio) -> None:
        """Already-joined room returns True without calling join."""
        config = make_matrix_config()
        session = MatrixSession(config)
        try:
            await session.start()
            mock_client = mock_nio.AsyncClient.return_value
            mock_client.rooms = {"!room:server": MagicMock()}
            result = await session.ensure_joined("!room:server")
            assert result is True
            mock_client.join.assert_not_called()
        finally:
            await session.stop()


class TestInviteHandling:
    """_on_invite callback behaviour."""

    async def test_invite_to_configured_room_accepted(self, mock_nio) -> None:
        """Invite to a room in auto_join_rooms triggers ensure_joined."""
        config = make_matrix_config()
        session = MatrixSession(config, auto_join_rooms=("!target:server",))
        try:
            await session.start()
            mock_client = mock_nio.AsyncClient.return_value
            mock_client.rooms = {}

            event = MagicMock(name="invite_event")
            event.room_id = "!target:server"
            room = MagicMock(name="room")

            await session._on_invite(room, event)
            mock_client.join.assert_called_once_with("!target:server")
        finally:
            await session.stop()

    async def test_invite_to_unconfigured_room_ignored(self, mock_nio) -> None:
        """Invite to room NOT in auto_join_rooms is ignored."""
        config = make_matrix_config()
        session = MatrixSession(config, auto_join_rooms=("!target:server",))
        try:
            await session.start()
            mock_client = mock_nio.AsyncClient.return_value
            mock_client.rooms = {}

            event = MagicMock(name="invite_event")
            event.room_id = "!other:server"
            room = MagicMock(name="room")

            await session._on_invite(room, event)
            mock_client.join.assert_not_called()
        finally:
            await session.stop()

    async def test_invite_callback_registered(self, mock_nio) -> None:
        """InviteMemberEvent callback is registered in _finalize_start."""
        config = make_matrix_config()
        session = MatrixSession(config, auto_join_rooms=("!room:server",))
        try:
            await session.start()
            calls = mock_nio.AsyncClient.return_value.add_event_callback.call_args_list
            invite_registered = any(
                mock_nio.InviteMemberEvent in call[0][1]
                for call in calls
                if len(call[0]) >= 2
            )
            assert invite_registered, (
                "InviteMemberEvent not found in any add_event_callback call"
            )
        finally:
            await session.stop()

    async def test_invite_no_room_id_no_crash(self, mock_nio) -> None:
        """Invite event without room_id does not crash."""
        config = make_matrix_config()
        session = MatrixSession(config, auto_join_rooms=("!room:server",))
        try:
            await session.start()
            event = MagicMock(name="invite_event")
            del event.room_id
            await session._on_invite(None, event)  # no crash
        finally:
            await session.stop()

    async def test_invite_join_failure_no_crash(self, mock_nio) -> None:
        """Invite to configured room where join fails does not crash."""
        config = make_matrix_config()
        session = MatrixSession(config, auto_join_rooms=("!target:server",))
        try:
            await session.start()
            mock_client = mock_nio.AsyncClient.return_value
            mock_client.rooms = {}
            mock_client.join = AsyncMock(side_effect=RuntimeError("fail"))

            event = MagicMock(name="invite_event")
            event.room_id = "!target:server"
            room = MagicMock(name="room")

            await session._on_invite(room, event)  # no crash
        finally:
            await session.stop()


# ===================================================================
# Cancellation safety for ensure_joined
# ===================================================================


class TestEnsureJoinedCancellationSafety:
    """Cancellation-safe ensure_joined using asyncio.Task + asyncio.shield."""

    async def test_waiter_cancel_does_not_affect_leader(self, mock_nio) -> None:
        """Cancelling a waiter does not cancel the leader's join task."""
        config = make_matrix_config()
        session = MatrixSession(config)
        try:
            await session.start()
            mock_client = mock_nio.AsyncClient.return_value
            mock_client.rooms = {}

            join_started = asyncio.Event()

            async def _slow_join(rid: str) -> MagicMock:
                join_started.set()
                await asyncio.sleep(10)  # long enough to cancel waiter
                resp = MagicMock(name="ok")
                resp.room_id = rid
                return resp

            mock_client.join = AsyncMock(side_effect=_slow_join)

            # Leader starts ensure_joined
            leader_task = asyncio.create_task(
                session.ensure_joined("!room:server")
            )
            await join_started.wait()

            # Waiter starts ensure_joined — gets the in-flight task
            waiter_task = asyncio.create_task(
                session.ensure_joined("!room:server")
            )
            await asyncio.sleep(0)  # let waiter enter shield

            # Cancel the waiter
            waiter_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter_task

            # Leader should still be running (not cancelled)
            assert not leader_task.done()

            # Clean up: cancel leader so test finishes
            leader_task.cancel()
            try:
                await leader_task
            except asyncio.CancelledError:
                pass
        finally:
            await session.stop()

    async def test_stop_cancels_outstanding_join_tasks(self, mock_nio) -> None:
        """stop() cancels outstanding join tasks without leaking."""
        config = make_matrix_config()
        session = MatrixSession(config)
        await session.start()
        mock_client = mock_nio.AsyncClient.return_value
        mock_client.rooms = {}

        join_started = asyncio.Event()

        async def _slow_join(rid: str) -> MagicMock:
            join_started.set()
            await asyncio.sleep(10)
            resp = MagicMock(name="ok")
            resp.room_id = rid
            return resp

        mock_client.join = AsyncMock(side_effect=_slow_join)

        # Start a join
        task = asyncio.create_task(session.ensure_joined("!room:server"))
        await join_started.wait()

        # Task is in-flight
        assert "!room:server" in session._joining_rooms

        # stop should cancel the join task
        await session.stop()

        # _joining_rooms should be cleared
        assert len(session._joining_rooms) == 0

        # The task should have been cancelled
        assert task.cancelled() or task.done()

    async def test_concurrent_failure_both_receive_false(self, mock_nio) -> None:
        """Two concurrent callers call join exactly once and both get False on failure."""
        config = make_matrix_config()
        session = MatrixSession(config)
        try:
            await session.start()
            mock_client = mock_nio.AsyncClient.return_value
            mock_client.rooms = {}

            join_count = 0

            async def _failing_join(rid: str) -> MagicMock:
                nonlocal join_count
                join_count += 1
                await asyncio.sleep(0)
                err = MagicMock(name="error")
                del err.room_id
                return err

            mock_client.join = AsyncMock(side_effect=_failing_join)

            results = await asyncio.gather(
                session.ensure_joined("!room:server"),
                session.ensure_joined("!room:server"),
            )
            assert results == [False, False]
            assert join_count == 1
        finally:
            await session.stop()
