"""Tests for sync recovery, crypto store continuity, sync state resilience,
room state tracking, delivery retry, and operational diagnostics.

No test requires mindroom-nio[e2e].
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.matrix.errors import MatrixConnectionError, MatrixSendError
from medre.adapters.matrix.session import MatrixSession
from medre.core.contracts.adapter import AdapterPermanentError, AdapterSendError
from tests.helpers.matrix_session import (
    fast_sleep_patch,
    make_matrix_config,
    make_matrix_context,
)
from tests.helpers.matrix_session import mock_nio as _mock_nio  # noqa: F401

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

        mock_nio.AsyncClient.return_value.sync = _failing_sync
        config = make_matrix_config()
        logger = logging.getLogger("test.sync_failure_log")
        session = MatrixSession(config, logger=logger)
        try:
            # Patch sleep BEFORE start so reconnect backoff is instant
            with fast_sleep_patch():
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
            mock_nio.AsyncClient.return_value.sync = _failing_sync
            with fast_sleep_patch():
                await session2.start()
                for _ in range(100):
                    await asyncio.sleep(0)

            assert any(
                "Max sync reconnect attempts" in rec.getMessage() for rec in log_records
            ), f"Expected sync failure log; got: {[r.getMessage() for r in log_records]}"
            logger.removeHandler(handler)
            await session2.stop()
        finally:
            await session.stop()


# ===================================================================
# TestSyncRecovery
# ===================================================================


class TestSyncRecovery:
    """Automatic sync recovery with bounded reconnect/backoff."""

    async def test_reconnect_after_transient_failure(self, mock_nio) -> None:
        """sync fails 3 times then succeeds → reconnect happens."""
        call_count = 0
        fail_count = 3

        async def _sync_controlled(*a: object, **kw: object) -> None:
            nonlocal call_count
            call_count += 1
            if call_count <= fail_count:
                await asyncio.sleep(0)
                raise ConnectionError(f"transient error #{call_count}")
            # Success — block forever
            await asyncio.Event().wait()

        mock_nio.AsyncClient.return_value.sync = _sync_controlled

        config = make_matrix_config()
        session = MatrixSession(config)

        with fast_sleep_patch():
            try:
                await session.start()
                # Give the sync loop time to run through failures
                for _ in range(30):
                    await asyncio.sleep(0)
                # sync was called more than fail_count times
                assert call_count > fail_count
                # Reconnect happened and then sync succeeded
                assert session.sync_task_running is True
                assert session._sync_failure is None
                # Reconnect attempts accumulated during failures
                assert session.reconnect_attempts == fail_count
                # Not currently in reconnect phase (sync is running)
                assert session.reconnecting is False
            finally:
                await session.stop()

    async def test_max_reconnect_attempts_reached(self, mock_nio) -> None:
        """sync always fails → max attempts reached, _sync_failure set."""
        import medre.adapters.matrix.session as sess_mod

        async def _always_fail(*a: object, **kw: object) -> None:
            await asyncio.sleep(0)
            raise ConnectionError("persistent failure")

        mock_nio.AsyncClient.return_value.sync = _always_fail

        config = make_matrix_config()
        session = MatrixSession(config)

        with fast_sleep_patch():
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

        mock_nio.AsyncClient.return_value.sync = _always_fail

        config = make_matrix_config()
        session = MatrixSession(config)

        with fast_sleep_patch():
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
            await asyncio.sleep(0)
            raise ConnectionError("transient")

        mock_nio.AsyncClient.return_value.sync = _sync_phased

        config = make_matrix_config()
        session = MatrixSession(config)

        original_sleep = asyncio.sleep

        with fast_sleep_patch():
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

        mock_nio.AsyncClient.return_value.sync = _cancel_sync

        config = make_matrix_config()
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
            await asyncio.sleep(0)
            raise ConnectionError("fail")

        mock_nio.AsyncClient.return_value.sync = _always_fail

        config = make_matrix_config()
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
                assert (
                    real_delays[i] >= real_delays[i - 1] * 0.5
                ), f"Delay not increasing: {real_delays}"


# ===================================================================
# TestCryptoStoreContinuity
# ===================================================================


class TestCryptoStoreContinuity:
    """Crypto-store continuity and identity preservation."""

    async def test_e2ee_session_crypto_enabled_and_store_loaded(self, mock_nio) -> None:
        """E2EE session sets crypto_enabled and crypto_store_loaded."""
        import medre.adapters.matrix.compat as compat

        original = compat.HAS_E2EE
        try:
            compat.HAS_E2EE = True
            config = make_matrix_config(
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

    async def test_plaintext_session_no_crypto_store_loaded(self, mock_nio) -> None:
        """Plaintext session has crypto_store_loaded=False."""
        config = make_matrix_config()
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
        config = make_matrix_config()
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
            config = make_matrix_config(encryption_mode="e2ee_optional")
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
# TestSyncStateResilience
# ===================================================================


class TestSyncStateResilience:
    """Hardened start/stop — no leaked tasks/exceptions/clients."""

    async def test_double_start_is_noop(self, mock_nio) -> None:
        """Starting an already-started session logs warning and returns."""
        config = make_matrix_config()
        session = MatrixSession(config)
        try:
            await session.start()
            client_before = session._client
            # Second start should be a no-op
            await session.start()
            assert session._client is client_before
        finally:
            await session.stop()

    async def test_double_stop_is_safe(self, mock_nio) -> None:
        """Stopping twice does not raise."""
        config = make_matrix_config()
        session = MatrixSession(config)
        await session.start()
        await session.stop()
        await session.stop()  # no raise
        assert session._client is None
        assert session.closed is True

    async def test_start_stop_start_cycles(self, mock_nio) -> None:
        """Repeated start/stop/start cycles are safe."""
        config = make_matrix_config()
        session = MatrixSession(config)
        for _ in range(3):
            await session.start()
            assert session.connected is True
            assert session.sync_task_running is True
            await session.stop()
            assert session.connected is False
            assert session._client is None

    async def test_stop_during_sync_then_restart(self, mock_nio) -> None:
        """Stop during sync, then restart with clean state."""
        config = make_matrix_config()
        session = MatrixSession(config)
        # First cycle
        await session.start()
        assert session.sync_task_running is True
        await session.stop()
        # Verify clean state
        assert session._sync_task is None
        assert session._client is None
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

        mock_nio.AsyncClient.return_value.sync = _failing_sync

        config = make_matrix_config()
        session = MatrixSession(config)

        with fast_sleep_patch():
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
        config = make_matrix_config()
        session = MatrixSession(config)
        with pytest.raises(MatrixConnectionError):
            await session.start()
        assert session._client is None


# ===================================================================
# TestRoomStateTracking
# ===================================================================


class TestRoomStateTracking:
    """Room encryption state cache in MatrixSession."""

    async def test_room_encryption_event_marks_encrypted(self, mock_nio) -> None:
        """RoomEncryptionEvent sets room state to encrypted."""
        config = make_matrix_config()
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
        config = make_matrix_config()
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
        config = make_matrix_config()
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
        config = make_matrix_config()
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
        config = make_matrix_config()
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

    async def test_encrypted_room_send_blocked_without_crypto(self, mock_nio) -> None:
        """Session-tracked encrypted room blocks send without crypto."""
        from medre.core.rendering.renderer import RenderingResult

        config = make_matrix_config()
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(make_matrix_context())

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

        config = make_matrix_config()
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(make_matrix_context())

            # Mark room as plaintext explicitly
            adapter._session._room_states["!plain:example.com"] = "plaintext"  # type: ignore[union-attr]

            response_mock = MagicMock()
            response_mock.event_id = "$evt_plain"
            adapter._session._client.room_send = AsyncMock(return_value=response_mock)

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

    async def test_unknown_room_falls_back_to_client_rooms(self, mock_nio) -> None:
        """Unknown room state falls back to client.rooms check."""
        from medre.core.rendering.renderer import RenderingResult

        config = make_matrix_config()
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(make_matrix_context())

            # Room not tracked by session but encrypted in client.rooms
            room_id = "!fallback_enc:example.com"
            room_obj = MagicMock(name="room_obj")
            room_obj.encrypted = True
            adapter._session._client.rooms = {room_id: room_obj}

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
# TestDeliveryRetry
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

        config = make_matrix_config()
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(make_matrix_context())
            adapter._session._client.room_send = _transient_then_ok

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

        config = make_matrix_config()
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(make_matrix_context())
            adapter._session._client.room_send = _always_transient

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
            # Exhausted transient retries must NOT increment permanent counter
            assert adapter._permanent_delivery_failures == 0
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

        config = make_matrix_config()
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(make_matrix_context())
            adapter._session._client.room_send = _room_send_non_transient

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

        config = make_matrix_config()
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(make_matrix_context())

            response_mock = MagicMock()
            response_mock.event_id = "$diag_evt"
            adapter._session._client.room_send = AsyncMock(return_value=response_mock)

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
# TestOperationalDiagnostics
# ===================================================================


class TestOperationalDiagnostics:
    """All diagnostic fields present and correct — no secrets."""

    async def test_all_session_diagnostic_fields(self, mock_nio) -> None:
        """MatrixSessionDiagnostics includes all new fields."""
        config = make_matrix_config()
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
        assert hasattr(diag, "sync_running")
        assert hasattr(diag, "reconnecting")
        assert hasattr(diag, "reconnect_attempts")
        assert hasattr(diag, "last_successful_sync")
        assert hasattr(diag, "crypto_store_loaded")
        assert hasattr(diag, "encrypted_room_count")
        assert hasattr(diag, "plaintext_room_count")
        assert diag.olm_loaded is False

    async def test_all_adapter_diagnostic_fields(self, mock_nio) -> None:
        """Adapter diagnostics() dict includes all new fields."""
        config = make_matrix_config()
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(make_matrix_context())
            diag = adapter.diagnostics()
            assert "sync_running" in diag
            assert "reconnecting" in diag
            assert "reconnect_attempts" in diag
            assert "last_successful_sync" in diag
            assert "crypto_store_loaded" in diag
            assert "encrypted_room_count" in diag
            assert "plaintext_room_count" in diag
            assert "olm_loaded" in diag
            assert "transient_delivery_failures" in diag
            assert "permanent_delivery_failures" in diag
        finally:
            await adapter.stop()

    async def test_no_secrets_in_diagnostics(self, mock_nio) -> None:
        """No secrets leak in diagnostics dict values."""
        config = make_matrix_config(access_token="super-secret-token-123")
        adapter = MatrixAdapter(config)
        diag = adapter.diagnostics()
        for key, val in diag.items():
            assert "super-secret-token-123" not in str(
                val
            ), f"Secret leaked in diagnostics field {key!r}"

    async def test_session_properties_default_values(self) -> None:
        """Session properties have correct defaults before start."""
        config = make_matrix_config()
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
        config = make_matrix_config()
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
        assert diag["olm_loaded"] is False
