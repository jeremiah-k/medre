"""Tests for MatrixSession sync boundary, logging, and encryption-event dedup.

Covers:
  - Sync boundary (is_live) state transitions
  - Undecryptable MegolmEvent history suppression (backlog vs live)
  - Live undecryptable dedup (rate-limited warnings)
  - RoomEncryptionEvent logging: deduped per room, DEBUG only, no INFO
  - Backlog undecryptable summary logged at DEBUG (not INFO)

No test requires mindroom-nio[e2e].
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from medre.adapters.matrix.session import MatrixSession
from tests.helpers.matrix_session import (
    make_matrix_config,
)
from tests.helpers.matrix_session import mock_nio as _mock_nio  # noqa: F401


# ===================================================================
# Shared helpers
# ===================================================================


def _make_megolm_event(
    event_id: str = "$mega-001",
    room_id: str = "!room:server",
    session_id: str = "sess-abc",
) -> MagicMock:
    """Build a minimal fake MegolmEvent for undecryptable event tests."""
    event = MagicMock(name="MegolmEvent")
    event.event_id = event_id
    event.session_id = session_id
    return event


def _make_room(room_id: str = "!room:server") -> MagicMock:
    """Build a minimal fake Room object."""
    room = MagicMock(name="Room")
    room.room_id = room_id
    return room


# ===================================================================
# TestSyncBoundaryIsLive
# ===================================================================


class TestSyncBoundaryIsLive:
    """MatrixSession.is_live reflects sync boundary state."""

    async def test_is_live_false_before_start(self) -> None:
        config = make_matrix_config()
        session = MatrixSession(config)
        assert session.is_live is False

    async def test_is_live_true_after_first_sync(self, mock_nio) -> None:
        """After the first successful sync, is_live becomes True."""
        config = make_matrix_config()
        session = MatrixSession(config)
        try:
            await session.start()
            # The mock sync returns immediately with next_batch.
            # Give the sync task a few event-loop ticks to complete.
            for _ in range(20):
                if session.is_live:
                    break
                await asyncio.sleep(0)
            assert session.is_live is True
        finally:
            await session.stop()

    async def test_is_live_reset_on_restart(self, mock_nio) -> None:
        """is_live resets to False when session restarts."""
        config = make_matrix_config()
        session = MatrixSession(config)
        await session.start()
        for _ in range(20):
            if session.is_live:
                break
            await asyncio.sleep(0)
        assert session.is_live is True
        await session.stop()
        # After stop, session is no longer live
        assert session.is_live is False
        # Re-start: is_live should begin False again
        await session.start()
        assert session.is_live is False
        for _ in range(20):
            if session.is_live:
                break
            await asyncio.sleep(0)
        assert session.is_live is True
        await session.stop()


# ===================================================================
# TestMegolmEventHistorySuppression
# ===================================================================


class TestMegolmEventHistorySuppression:
    """Undecryptable MegolmEvent handling with sync boundary."""

    async def test_not_live_undecryptable_no_warning(self, mock_nio) -> None:
        """Before is_live, undecryptable MegolmEvents log DEBUG, not WARNING."""
        config = make_matrix_config()
        logger = logging.getLogger("test.history_suppression")
        session = MatrixSession(config, logger=logger)
        # Don't start — is_live stays False

        event = _make_megolm_event()
        room = _make_room()

        with patch.object(logger, "warning") as mock_warning, \
             patch.object(logger, "debug") as mock_debug:
            await session._on_megolm_event(room, event)
            mock_warning.assert_not_called()
            mock_debug.assert_called()

        assert session._suppressed_backlog_undecryptable == 1
        assert session.undecryptable_event_count == 1

    async def test_live_undecryptable_logs_warning(self, mock_nio) -> None:
        """After is_live, undecryptable MegolmEvents log WARNING."""
        config = make_matrix_config()
        logger = logging.getLogger("test.live_megolm")
        session = MatrixSession(config, logger=logger)
        try:
            await session.start()
            for _ in range(20):
                if session.is_live:
                    break
                await asyncio.sleep(0)
            assert session.is_live is True

            event = _make_megolm_event()
            room = _make_room()

            with patch.object(logger, "warning") as mock_warning:
                await session._on_megolm_event(room, event)
                mock_warning.assert_called_once()
        finally:
            await session.stop()


# ===================================================================
# TestLiveUndecryptableDedup
# ===================================================================


class TestLiveUndecryptableDedup:
    """Live undecryptable dedup: same key within 60s -> DEBUG only."""

    async def test_repeated_same_key_one_warning_rest_debug(
        self, mock_nio
    ) -> None:
        """Repeated live events with same room:session_id produce one WARNING."""
        config = make_matrix_config()
        logger = logging.getLogger("test.dedup")
        session = MatrixSession(config, logger=logger)
        try:
            await session.start()
            for _ in range(20):
                if session.is_live:
                    break
                await asyncio.sleep(0)
            assert session.is_live is True

            event1 = _make_megolm_event(
                event_id="$mega-a", room_id="!room:server", session_id="sess-1"
            )
            event2 = _make_megolm_event(
                event_id="$mega-b", room_id="!room:server", session_id="sess-1"
            )
            room = _make_room(room_id="!room:server")

            with patch.object(logger, "warning") as mock_warning, \
                 patch.object(logger, "debug") as mock_debug:
                await session._on_megolm_event(room, event1)
                await session._on_megolm_event(room, event2)
                # Only first should produce WARNING
                assert mock_warning.call_count == 1
                # Second should produce DEBUG (rate-limited)
                debug_calls = [
                    c for c in mock_debug.call_args_list
                    if "Rate-limited" in str(c)
                ]
                assert len(debug_calls) == 1
        finally:
            await session.stop()

    async def test_different_rooms_warn_separately(self, mock_nio) -> None:
        """Different rooms produce separate WARNINGs."""
        config = make_matrix_config()
        logger = logging.getLogger("test.dedup_rooms")
        session = MatrixSession(config, logger=logger)
        try:
            await session.start()
            for _ in range(20):
                if session.is_live:
                    break
                await asyncio.sleep(0)
            assert session.is_live is True

            event1 = _make_megolm_event(
                event_id="$mega-a", room_id="!room1:server", session_id="sess-1"
            )
            event2 = _make_megolm_event(
                event_id="$mega-b", room_id="!room2:server", session_id="sess-1"
            )
            room1 = _make_room(room_id="!room1:server")
            room2 = _make_room(room_id="!room2:server")

            with patch.object(logger, "warning") as mock_warning:
                await session._on_megolm_event(room1, event1)
                await session._on_megolm_event(room2, event2)
                assert mock_warning.call_count == 2
        finally:
            await session.stop()


# ===================================================================
# TestRoomEncryptionEventLogging
# ===================================================================


class TestRoomEncryptionEventLogging:
    """RoomEncryptionEvent logging: deduped per room, DEBUG only, no INFO.

    Production requirement:
    - Repeated events for the same room emit at most once at DEBUG.
    - No INFO record is emitted by default.
    - _encrypted_room_seen and _track_room_encrypted behavior unchanged.
    """

    async def test_first_event_logs_debug_not_info(self, mock_nio) -> None:
        """First RoomEncryptionEvent for a room logs DEBUG, not INFO."""
        config = make_matrix_config()
        logger = logging.getLogger("test.encryption_event")
        session = MatrixSession(config, logger=logger)

        room = _make_room(room_id="!enc:server")
        event = MagicMock(name="RoomEncryptionEvent")

        with patch.object(logger, "info") as mock_info, \
             patch.object(logger, "debug") as mock_debug:
            await session._on_room_encryption_event(room, event)
            mock_info.assert_not_called()
            mock_debug.assert_called_once()

        # State tracking unchanged
        assert session._encrypted_room_seen is True
        assert session.room_state("!enc:server") == "encrypted"

    async def test_repeated_event_for_same_room_silent(self, mock_nio) -> None:
        """Second event for the same room produces no log at all."""
        config = make_matrix_config()
        logger = logging.getLogger("test.encryption_event_dedup")
        session = MatrixSession(config, logger=logger)

        room = _make_room(room_id="!enc:server")
        event = MagicMock(name="RoomEncryptionEvent")

        with patch.object(logger, "debug") as mock_debug:
            await session._on_room_encryption_event(room, event)
            first_debug_count = mock_debug.call_count

            await session._on_room_encryption_event(room, event)
            # No additional debug call for the same room
            assert mock_debug.call_count == first_debug_count

        # State still reflects encrypted room
        assert session._encrypted_room_seen is True
        assert session.room_state("!enc:server") == "encrypted"

    async def test_different_rooms_each_get_debug(self, mock_nio) -> None:
        """Different rooms each get exactly one DEBUG log."""
        config = make_matrix_config()
        logger = logging.getLogger("test.encryption_event_multiroom")
        session = MatrixSession(config, logger=logger)

        room1 = _make_room(room_id="!room1:server")
        room2 = _make_room(room_id="!room2:server")
        event = MagicMock(name="RoomEncryptionEvent")

        with patch.object(logger, "info") as mock_info, \
             patch.object(logger, "debug") as mock_debug:
            await session._on_room_encryption_event(room1, event)
            await session._on_room_encryption_event(room2, event)
            mock_info.assert_not_called()
            assert mock_debug.call_count == 2

        assert session.room_state("!room1:server") == "encrypted"
        assert session.room_state("!room2:server") == "encrypted"

    async def test_no_info_even_across_many_events(self, mock_nio) -> None:
        """Even with many repeated events, zero INFO logs are emitted."""
        config = make_matrix_config()
        logger = logging.getLogger("test.encryption_event_no_info")
        session = MatrixSession(config, logger=logger)

        room = _make_room(room_id="!noisy:server")
        event = MagicMock(name="RoomEncryptionEvent")

        with patch.object(logger, "info") as mock_info:
            for _ in range(20):
                await session._on_room_encryption_event(room, event)
            mock_info.assert_not_called()


# ===================================================================
# TestBacklogSummaryLogLevel
# ===================================================================


class TestBacklogSummaryLogLevel:
    """Backlog undecryptable summary is logged at DEBUG, not INFO."""

    async def test_backlog_summary_is_debug_not_info(self, mock_nio) -> None:
        """When the sync boundary is reached with suppressed backlog events,
        the summary is logged at DEBUG, not INFO.

        The summary is emitted inside _sync_with_reconnect when the first
        sync response arrives and _suppressed_backlog_undecryptable > 0.
        We simulate this by injecting suppressed events AFTER start()
        (which resets counters) but BEFORE the sync task processes its
        first response, using a sync mock that delays once.
        """
        config = make_matrix_config()
        logger = logging.getLogger("test.backlog_summary")
        session = MatrixSession(config, logger=logger)

        # Create a sync mock that waits for a gate on the first call
        # (so we can inject backlog events before the response is processed),
        # then blocks indefinitely on subsequent calls (yielding to the
        # event loop so stop() can cancel the sync task cleanly).
        sync_gate = asyncio.Event()
        _sync_call_count = 0

        async def _gated_sync(*a: object, **kw: object) -> MagicMock:
            nonlocal _sync_call_count
            _sync_call_count += 1
            if _sync_call_count == 1:
                await sync_gate.wait()
                resp = MagicMock(name="SyncResponse")
                resp.next_batch = "batch_token_123"
                return resp
            # Subsequent calls block forever but yield to event loop,
            # so stop() can cancel this task.
            await asyncio.Event().wait()
            raise AssertionError("unreachable")  # pragma: no cover

        mock_client = mock_nio.AsyncClient.return_value
        mock_client.sync = AsyncMock(side_effect=_gated_sync)

        try:
            with patch.object(logger, "debug") as mock_debug:
                await session.start()

                # Session started but sync hasn't completed yet.
                # Inject backlog events (is_live is still False).
                for i in range(3):
                    event = _make_megolm_event(event_id=f"$backlog-{i}")
                    room = _make_room()
                    await session._on_megolm_event(room, event)

                assert session._suppressed_backlog_undecryptable == 3

                # Release the sync gate so the sync task processes the response.
                sync_gate.set()

                # Bounded wait for the sync task to set is_live.
                for _ in range(50):
                    if session.is_live:
                        break
                    await asyncio.sleep(0)

                assert session.is_live, "Sync task did not set is_live in time"

                # Check that a debug message containing the backlog summary was emitted
                debug_messages = [
                    str(call) for call in mock_debug.call_args_list
                ]
                summary_found = any(
                    "suppressed" in msg and "undecryptable" in msg
                    for msg in debug_messages
                )
                assert summary_found, (
                    f"Expected DEBUG summary log about suppressed undecryptable "
                    f"backlog events. Debug calls: {debug_messages}"
                )
        finally:
            await session.stop()
