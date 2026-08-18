"""Tests for E2EE event handling, encrypted room safety, E2EE diagnostics,
RoomEncryptionEvent callbacks, diagnostics redaction, initial full-state
sync semantics, and E2EE key-management ownership.

No test requires mindroom-nio[e2e].
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.matrix.session import MatrixSession
from medre.core.contracts.adapter import AdapterPermanentError
from tests.helpers.async_utils import wait_until
from tests.helpers.matrix_session import (
    fast_sleep_patch,
    make_matrix_config,
    make_matrix_context,
)
from tests.helpers.matrix_session import mock_nio as _mock_nio  # noqa: F401

# ===================================================================
# TestMegolmEventHandling
# ===================================================================


class TestMegolmEventHandling:
    """Undecryptable MegolmEvent handling."""

    async def test_megolm_event_increments_count(self, mock_nio) -> None:
        """Receiving an undecryptable MegolmEvent increments count and sets error."""
        config = make_matrix_config(
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
        config = make_matrix_config(
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
        config = make_matrix_config(
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

    async def test_deliver_raises_if_encrypted_room_no_crypto(self, mock_nio) -> None:
        """deliver() raises when room is encrypted but crypto is not active."""
        from medre.core.rendering.renderer import RenderingResult

        config = make_matrix_config()
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(make_matrix_context())
            assert adapter._session.crypto_enabled is False  # type: ignore[union-attr]

            # Simulate an encrypted room via client.rooms
            room_id = "!encrypted_room:example.com"
            room_obj = MagicMock(name="room_obj")
            room_obj.encrypted = True
            adapter._session._client.rooms = {room_id: room_obj}

            result = RenderingResult(
                event_id="evt_1",
                target_adapter="matrix-test",
                payload={"msgtype": "m.text", "body": "hello"},
                target_channel=room_id,
            )
            with pytest.raises(
                AdapterPermanentError, match="encrypted but E2EE crypto is not active"
            ):
                await adapter.deliver(result)
        finally:
            await adapter.stop()

    async def test_deliver_ok_if_not_encrypted_room(self, mock_nio) -> None:
        """deliver() succeeds when room is not encrypted."""
        from medre.core.rendering.renderer import RenderingResult

        config = make_matrix_config()
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(make_matrix_context())
            # Room not encrypted → should be fine
            response_mock = MagicMock()
            response_mock.event_id = "$event_123"
            adapter._session._client.room_send = AsyncMock(return_value=response_mock)

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

        config = make_matrix_config()
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(make_matrix_context())
            # encrypted_room_seen is True globally but room is plaintext
            adapter._session._encrypted_room_seen = True  # type: ignore[union-attr]
            assert adapter._session.crypto_enabled is False  # type: ignore[union-attr]

            room_id = "!plain_room:example.com"
            room_obj = MagicMock(name="room_obj")
            room_obj.encrypted = False
            adapter._session._client.rooms = {room_id: room_obj}

            response_mock = MagicMock()
            response_mock.event_id = "$event_123"
            adapter._session._client.room_send = AsyncMock(return_value=response_mock)

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
        import medre.adapters.matrix.compat as compat
        from medre.core.rendering.renderer import RenderingResult

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
                assert adapter._session.crypto_enabled is True  # type: ignore[union-attr]

                room_id = "!encrypted:example.com"
                room_obj = MagicMock(name="room_obj")
                room_obj.encrypted = True
                adapter._session._client.rooms = {room_id: room_obj}

                response_mock = MagicMock()
                response_mock.event_id = "$event_456"
                adapter._session._client.room_send = AsyncMock(
                    return_value=response_mock
                )

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

        config = make_matrix_config()
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(make_matrix_context())
            assert adapter._session.crypto_enabled is False  # type: ignore[union-attr]

            # Room not in client.rooms → optimistic allow
            adapter._session._client.rooms = {}

            response_mock = MagicMock()
            response_mock.event_id = "$event_789"
            adapter._session._client.room_send = AsyncMock(return_value=response_mock)

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

    async def test_e2ee_required_diagnostics_with_crypto(self, mock_nio) -> None:
        """e2ee_required mode shows crypto_enabled=True in diagnostics."""
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
        config = make_matrix_config()
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(make_matrix_context())
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
            config = make_matrix_config(
                encryption_mode="e2ee_required",
                store_path="/tmp/store",
                device_id="DEV",
            )
            adapter = MatrixAdapter(config)
            try:
                await adapter.start(make_matrix_context())
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
# TestBlocker4RoomEncryptionEvent
# ===================================================================


class TestBlocker4RoomEncryptionEvent:
    """Blocker 4: RoomEncryptionEvent callback registration and state update."""

    async def test_room_encryption_event_callback_registered(self, mock_nio) -> None:
        """RoomEncryptionEvent callback is registered on start."""
        cb = MagicMock()
        config = make_matrix_config()
        session = MatrixSession(config, message_callback=cb)
        try:
            await session.start()
            client_mock = mock_nio.AsyncClient.return_value
            # Should have 4 callbacks: message + megolm + room_encryption + reaction
            assert client_mock.add_event_callback.call_count == 5
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
        config = make_matrix_config()
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

    async def test_room_encryption_event_logs_info(self, mock_nio, caplog) -> None:
        """_on_room_encryption_event logs a debug message."""
        config = make_matrix_config()
        session = MatrixSession(config)
        try:
            await session.start()
            room = MagicMock(name="room")
            room.room_id = "!encrypted:example.com"
            event = MagicMock(name="encryption_event")

            with caplog.at_level(logging.DEBUG):
                await session._on_room_encryption_event(room, event)

            assert any(
                "RoomEncryptionEvent" in rec.getMessage() for rec in caplog.records
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
            config = make_matrix_config(
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
            config = make_matrix_config(
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
# Helpers for sync-loop tests
# ===================================================================


def _make_sync_response(next_batch: str | None = "batch_1") -> MagicMock:
    """Build a mock SyncResponse with or without next_batch."""
    resp = MagicMock(name="SyncResponse")
    resp.next_batch = next_batch
    return resp


async def _run_session_ticks(session: MatrixSession, ticks: int = 5) -> None:
    """Yield the event loop *ticks* times to let the sync task progress."""
    for _ in range(ticks):
        await asyncio.sleep(0)


# ===================================================================
# TestInitialSyncDoneBehavior
# ===================================================================


class TestInitialSyncDoneBehavior:
    """Verify _initial_sync_done and full_state retry semantics."""

    async def test_first_sync_raises_retry_uses_full_state(self, mock_nio) -> None:
        """First sync raises ConnectionError, retry still uses full_state=True."""
        client = mock_nio.AsyncClient.return_value
        sync_mock = AsyncMock(
            side_effect=[
                ConnectionError("network down"),
                _make_sync_response("batch_1"),
                _make_sync_response("batch_2"),
            ]
        )
        client.sync = sync_mock

        config = make_matrix_config()
        session = MatrixSession(config)
        with fast_sleep_patch():
            try:
                await session.start()
                await _run_session_ticks(session, ticks=8)
            finally:
                await session.stop()

        calls = sync_mock.call_args_list
        assert len(calls) >= 2
        # First call: full_state=True
        assert calls[0].kwargs.get("full_state") is True
        # Second call (retry): also full_state=True
        assert calls[1].kwargs.get("full_state") is True
        # Third call (after success): no full_state
        if len(calls) >= 3:
            assert calls[2].kwargs.get("full_state") is None

    async def test_first_sync_no_next_batch_retry_uses_full_state(
        self, mock_nio
    ) -> None:
        """Sync returns response without next_batch, retry still uses full_state=True."""
        client = mock_nio.AsyncClient.return_value
        sync_mock = AsyncMock(
            side_effect=[
                _make_sync_response(None),  # no next_batch → treated as error
                _make_sync_response("batch_1"),
                _make_sync_response("batch_2"),
            ]
        )
        client.sync = sync_mock

        config = make_matrix_config()
        session = MatrixSession(config)
        with fast_sleep_patch():
            try:
                await session.start()
                await _run_session_ticks(session, ticks=8)
            finally:
                await session.stop()

        calls = sync_mock.call_args_list
        assert len(calls) >= 2
        assert calls[0].kwargs.get("full_state") is True
        assert calls[1].kwargs.get("full_state") is True

    async def test_after_success_full_state_omitted(self, mock_nio) -> None:
        """After a successful initial sync, subsequent syncs omit full_state."""
        client = mock_nio.AsyncClient.return_value
        sync_mock = AsyncMock(
            side_effect=[
                _make_sync_response("batch_1"),
                _make_sync_response("batch_2"),
                _make_sync_response("batch_3"),
            ]
        )
        client.sync = sync_mock

        config = make_matrix_config()
        session = MatrixSession(config)
        with fast_sleep_patch():
            try:
                await session.start()
                await _run_session_ticks(session, ticks=8)
            finally:
                await session.stop()

        calls = sync_mock.call_args_list
        assert len(calls) >= 2
        # First call: full_state=True (initial sync)
        assert calls[0].kwargs.get("full_state") is True
        # Second call: no full_state
        assert calls[1].kwargs.get("full_state") is None

    async def test_diagnostics_initial_sync_completed(self, mock_nio) -> None:
        """initial_sync_completed is False before and True after successful sync."""
        client = mock_nio.AsyncClient.return_value

        # Phase 1: sync fails, check diagnostics
        fail_count = 0

        async def _fail_then_succeed(*args, **kwargs):
            nonlocal fail_count
            fail_count += 1
            if fail_count <= 2:
                raise ConnectionError("down")
            return _make_sync_response("batch_ok")

        client.sync = AsyncMock(side_effect=_fail_then_succeed)

        config = make_matrix_config()
        session = MatrixSession(config)
        with fast_sleep_patch():
            try:
                await session.start()
                # Before successful sync: initial_sync_completed is False
                assert session.diagnostics().initial_sync_completed is False

                # Let the sync loop recover and succeed
                await _run_session_ticks(session, ticks=12)

                # After successful sync: initial_sync_completed is True
                assert session.diagnostics().initial_sync_completed is True
            finally:
                await session.stop()


# ===================================================================
# TestE2EEKeyManagement
# ===================================================================


def _install_key_operation_mocks(client: MagicMock) -> None:
    """Install nio-owned E2EE operation mocks on a fake client."""
    client.olm = MagicMock(name="olm")
    client.store = MagicMock(name="store")
    client.should_upload_keys = True
    client.should_query_keys = True
    client.should_claim_keys = True
    client.get_users_for_key_claiming = MagicMock(return_value=["@alice:example.com"])
    client.keys_upload = AsyncMock()
    client.keys_query = AsyncMock()
    client.keys_claim = AsyncMock()
    client.send_to_device_messages = AsyncMock()


async def test_e2ee_session_does_not_duplicate_nio_key_operations(mock_nio) -> None:
    """MEDRE leaves E2EE key sequencing to mindroom-nio's sync loop."""
    import medre.adapters.matrix.compat as compat

    client = mock_nio.AsyncClient.return_value
    _install_key_operation_mocks(client)
    client.sync = AsyncMock(wraps=client.sync)
    original = compat.HAS_E2EE
    try:
        compat.HAS_E2EE = True
        session = MatrixSession(
            make_matrix_config(
                encryption_mode="e2ee_required",
                store_path="/tmp/test_e2ee_store",
                device_id="DEV1",
            )
        )
        with fast_sleep_patch():
            try:
                await session.start()
                assert await wait_until(
                    lambda: client.sync.await_count > 0,
                    timeout=1,
                    interval=0,
                )
            finally:
                await session.stop()
    finally:
        compat.HAS_E2EE = original

    client.keys_upload.assert_not_awaited()
    client.keys_query.assert_not_awaited()
    client.keys_claim.assert_not_awaited()
    client.send_to_device_messages.assert_not_awaited()


async def test_plaintext_session_does_not_run_key_operations(mock_nio) -> None:
    """Plaintext sessions do not invoke E2EE key operations from MEDRE."""
    client = mock_nio.AsyncClient.return_value
    _install_key_operation_mocks(client)
    client.sync = AsyncMock(wraps=client.sync)
    session = MatrixSession(make_matrix_config())
    with fast_sleep_patch():
        try:
            await session.start()
            assert await wait_until(
                lambda: client.sync.await_count > 0,
                timeout=1,
                interval=0,
            )
        finally:
            await session.stop()

    client.keys_upload.assert_not_awaited()
    client.keys_query.assert_not_awaited()
    client.keys_claim.assert_not_awaited()
    client.send_to_device_messages.assert_not_awaited()


# ===================================================================
# TestE2EERequiredFailClosed
# ===================================================================


class TestE2EERequiredFailClosed:
    """e2ee_required mode must fail-closed when olm/store are None."""

    async def test_e2ee_required_olm_none_raises(self, mock_nio) -> None:
        """e2ee_required + olm is None → startup raises MatrixConnectionError."""
        import medre.adapters.matrix.compat as compat
        from medre.adapters.matrix.errors import MatrixConnectionError

        original = compat.HAS_E2EE
        try:
            compat.HAS_E2EE = True
            client = mock_nio.AsyncClient.return_value
            client.olm = None
            client.store = MagicMock(name="store")

            config = make_matrix_config(
                encryption_mode="e2ee_required",
                store_path="/tmp/store",
                device_id="DEV",
            )
            session = MatrixSession(config)
            with pytest.raises(MatrixConnectionError, match="Olm subsystem"):
                await session.start()

            # After failure, client should be cleaned up
            assert session._client is None
            assert session.crypto_enabled is False
            assert session.crypto_store_loaded is False
        finally:
            compat.HAS_E2EE = original

    async def test_e2ee_required_store_none_raises(self, mock_nio) -> None:
        """e2ee_required + store is None → startup raises MatrixConnectionError."""
        import medre.adapters.matrix.compat as compat
        from medre.adapters.matrix.errors import MatrixConnectionError

        original = compat.HAS_E2EE
        try:
            compat.HAS_E2EE = True
            client = mock_nio.AsyncClient.return_value
            client.olm = MagicMock(name="olm")
            client.store = None

            config = make_matrix_config(
                encryption_mode="e2ee_required",
                store_path="/tmp/store",
                device_id="DEV",
            )
            session = MatrixSession(config)
            with pytest.raises(MatrixConnectionError, match="crypto store"):
                await session.start()

            assert session._client is None
            assert session.crypto_enabled is False
            assert session.crypto_store_loaded is False
        finally:
            compat.HAS_E2EE = original

    async def test_e2ee_optional_olm_none_continues(self, mock_nio) -> None:
        """e2ee_optional + olm is None → startup continues, crypto_enabled=False."""
        import medre.adapters.matrix.compat as compat

        original = compat.HAS_E2EE
        try:
            compat.HAS_E2EE = True
            client = mock_nio.AsyncClient.return_value
            client.olm = None
            client.store = MagicMock(name="store")

            config = make_matrix_config(
                encryption_mode="e2ee_optional",
                store_path="/tmp/store",
                device_id="DEV",
            )
            session = MatrixSession(config)
            try:
                await session.start()
                # Should fall back to plaintext, not raise
                assert session.crypto_enabled is False
                assert session.crypto_store_loaded is False
            finally:
                await session.stop()
        finally:
            compat.HAS_E2EE = original

    async def test_e2ee_optional_store_none_continues(self, mock_nio) -> None:
        """e2ee_optional + store is None → startup continues, crypto_enabled=False."""
        import medre.adapters.matrix.compat as compat

        original = compat.HAS_E2EE
        try:
            compat.HAS_E2EE = True
            client = mock_nio.AsyncClient.return_value
            client.olm = MagicMock(name="olm")
            client.store = None

            config = make_matrix_config(
                encryption_mode="e2ee_optional",
                store_path="/tmp/store",
                device_id="DEV",
            )
            session = MatrixSession(config)
            try:
                await session.start()
                assert session.crypto_enabled is False
                assert session.crypto_store_loaded is False
            finally:
                await session.stop()
        finally:
            compat.HAS_E2EE = original

    async def test_e2ee_required_failure_closes_client(self, mock_nio) -> None:
        """After e2ee_required failure, client.close() was called."""
        import medre.adapters.matrix.compat as compat
        from medre.adapters.matrix.errors import MatrixConnectionError

        original = compat.HAS_E2EE
        try:
            compat.HAS_E2EE = True
            client = mock_nio.AsyncClient.return_value
            client.olm = None
            client.store = MagicMock(name="store")

            config = make_matrix_config(
                encryption_mode="e2ee_required",
                store_path="/tmp/store",
                device_id="DEV",
            )
            session = MatrixSession(config)
            with pytest.raises(MatrixConnectionError):
                await session.start()

            # Verify close was called on the nio client
            client.close.assert_awaited()
            assert session._client is None
        finally:
            compat.HAS_E2EE = original
