"""Tests for MatrixAdapter startup-history suppression (is_live boundary).

Moved from test_matrix_adapter.py to keep that file under 1500 lines.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.matrix.errors import MatrixConnectionError
from medre.adapters.matrix.metadata import MatrixMetadataEnvelope
from medre.adapters.matrix.session import MatrixSession
from tests.helpers.matrix_adapter import make_adapter_context as _make_adapter_context
from tests.helpers.matrix_adapter import make_fake_nio_event as _make_fake_nio_event
from tests.helpers.matrix_adapter import (
    make_fake_reaction_event as _make_fake_reaction_event,
)
from tests.helpers.matrix_adapter import make_fake_room as _make_fake_room
from tests.helpers.matrix_adapter import make_matrix_config as _make_matrix_config
from tests.helpers.matrix_adapter import to_event_dict as _to_event_dict


class TestStartupHistorySuppression:
    """_on_room_message suppresses events before is_live (startup backlog)."""

    async def test_not_live_text_suppressed(self) -> None:
        """RoomMessageText is suppressed when session.is_live is False."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx
        adapter._started = True

        # Mock session with is_live = False
        mock_session = MagicMock(name="session")
        mock_session.is_live = False
        adapter._session = mock_session

        event = _make_fake_nio_event(sender="@alice:example.com")
        room = _make_fake_room()

        await adapter._on_room_message(_to_event_dict(room, event))
        assert len(published) == 0
        assert adapter._inbound_suppressed_startup == 1

    async def test_live_text_published(self) -> None:
        """RoomMessageText publishes normally when session.is_live is True."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx
        adapter._started = True

        # Mock session with is_live = True
        mock_session = MagicMock(name="session")
        mock_session.is_live = True
        mock_session.crypto_enabled = False
        mock_session.room_state = MagicMock(return_value="unknown")
        adapter._session = mock_session

        event = _make_fake_nio_event(sender="@alice:example.com")
        room = _make_fake_room()

        await adapter._on_room_message(_to_event_dict(room, event))
        assert len(published) == 1
        assert adapter._inbound_suppressed_startup == 0

    async def test_not_live_reaction_suppressed(self) -> None:
        """m.reaction events are suppressed when session.is_live is False."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx
        adapter._started = True

        mock_session = MagicMock(name="session")
        mock_session.is_live = False
        adapter._session = mock_session

        event = _make_fake_reaction_event(sender="@alice:example.com")
        room = _make_fake_room()

        await adapter._on_room_message(_to_event_dict(room, event))
        assert len(published) == 0
        assert adapter._inbound_suppressed_startup == 1

    async def test_invite_handled_regardless_of_is_live(self, mock_nio) -> None:
        """InviteMemberEvent is handled even when is_live is False."""
        from tests.helpers.matrix_session import make_matrix_config as _cfg

        config = _cfg()
        session = MatrixSession(config, auto_join_rooms=("!target:server",))
        try:
            await session.start()
            # is_live may be True or False depending on timing;
            # either way, invite should work
            mock_client = mock_nio.AsyncClient.return_value
            mock_client.rooms = {}

            event = MagicMock(name="invite_event")
            event.room_id = "!target:server"
            room = MagicMock(name="room")

            await session._on_invite(room, event)
            mock_client.join.assert_called_once_with("!target:server")
        finally:
            await session.stop()

    async def test_diagnostics_exposes_startup_suppressed(self) -> None:
        """diagnostics() dict includes inbound_suppressed_startup."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)
        adapter._inbound_suppressed_startup = 5
        diag = adapter.diagnostics()
        assert diag["inbound_suppressed_startup"] == 5

    async def test_pre_live_self_message_increments_startup_not_self(self) -> None:
        """Pre-live self-message increments startup suppression, not self."""
        config = _make_matrix_config(user_id="@bot:example.com")
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx
        adapter._started = True

        mock_session = MagicMock(name="session")
        mock_session.is_live = False
        adapter._session = mock_session

        event = _make_fake_nio_event(sender="@bot:example.com")
        room = _make_fake_room()

        await adapter._on_room_message(_to_event_dict(room, event))
        assert adapter._inbound_suppressed_startup == 1
        assert adapter._inbound_suppressed_self == 0
        assert len(published) == 0

    async def test_pre_live_event_does_not_decode(self) -> None:
        """Pre-live events do not attempt decode (no codec call side-effects)."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx
        adapter._started = True

        mock_session = MagicMock(name="session")
        mock_session.is_live = False
        adapter._session = mock_session

        # Use an event with content that would cause a MEDRE envelope
        # decode -- if decode happened, envelope suppression would fire.
        envelope = MatrixMetadataEnvelope(
            source_adapter="matrix-1",
            canonical_event_id="evt-orig",
        )
        content = {
            "msgtype": "m.text",
            "body": "backlog",
            **envelope.to_content(),
        }
        event = _make_fake_nio_event(
            sender="@alice:example.com",
            content=content,
        )
        room = _make_fake_room()

        await adapter._on_room_message(_to_event_dict(room, event))
        # Startup suppression fires first -- no decode, no envelope check
        assert adapter._inbound_suppressed_startup == 1
        assert adapter._inbound_suppressed_envelope == 0
        assert len(published) == 0

    async def test_live_self_message_counts_self_suppression(self) -> None:
        """Live self-message increments self suppression, not startup."""
        config = _make_matrix_config(user_id="@bot:example.com")
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx
        adapter._started = True

        mock_session = MagicMock(name="session")
        mock_session.is_live = True
        adapter._session = mock_session

        event = _make_fake_nio_event(sender="@bot:example.com")
        room = _make_fake_room()

        await adapter._on_room_message(_to_event_dict(room, event))
        assert adapter._inbound_suppressed_self == 1
        assert adapter._inbound_suppressed_startup == 0
        assert len(published) == 0

    async def test_live_normal_event_publishes(self) -> None:
        """Live normal third-party event is decoded and published."""
        config = _make_matrix_config(user_id="@bot:example.com")
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx
        adapter._started = True

        mock_session = MagicMock(name="session")
        mock_session.is_live = True
        adapter._session = mock_session

        event = _make_fake_nio_event(sender="@alice:example.com")
        room = _make_fake_room()

        await adapter._on_room_message(_to_event_dict(room, event))
        assert adapter._inbound_published == 1
        assert adapter._inbound_suppressed_startup == 0
        assert len(published) == 1


class TestStartLifecycleCleanup:
    """start() properly rolls back lifecycle state on failure."""

    async def test_has_nio_false_leaves_no_stale_ctx(self) -> None:
        """HAS_NIO=False → start() raises, ctx is None, _started is False."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()

        with patch("medre.adapters.matrix.adapter.HAS_NIO", False):
            with pytest.raises(MatrixConnectionError, match="mindroom-nio"):
                await adapter.start(ctx)

        assert adapter.ctx is None
        assert adapter._started is False
        assert adapter._start_time is None

        # _on_room_message drops events without publishing
        event = _make_fake_nio_event()
        room = _make_fake_room()
        await adapter._on_room_message(_to_event_dict(room, event))
        assert len(published) == 0

    async def test_session_start_raises_clears_state(self) -> None:
        """session.start() failure clears _session, _started, and ctx."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()

        mock_session = MagicMock(name="session")
        mock_session.start = AsyncMock(side_effect=RuntimeError("boom"))
        mock_session.stop = AsyncMock()

        with (
            patch("medre.adapters.matrix.adapter.HAS_NIO", True),
            patch(
                "medre.adapters.matrix.adapter.MatrixSession",
                return_value=mock_session,
            ),
        ):
            with pytest.raises(RuntimeError, match="boom"):
                await adapter.start(ctx)

        assert adapter._started is False
        assert adapter._session is None
        assert adapter.ctx is None
        assert adapter._start_time is None

        # _on_room_message drops events without publishing
        event = _make_fake_nio_event()
        room = _make_fake_room()
        await adapter._on_room_message(_to_event_dict(room, event))
        assert len(published) == 0

    async def test_auto_join_raises_clears_state(self) -> None:
        """auto-join failure stops session and clears all lifecycle state."""
        config = _make_matrix_config(
            auto_join_rooms=("!room1:server", "!room2:server"),
        )
        adapter = MatrixAdapter(config)
        _published, ctx = _make_adapter_context()

        mock_session = MagicMock(name="session")
        mock_session.start = AsyncMock()
        mock_session.stop = AsyncMock()
        mock_session.ensure_joined_rooms = AsyncMock(
            side_effect=RuntimeError("join failed"),
        )

        with (
            patch("medre.adapters.matrix.adapter.HAS_NIO", True),
            patch(
                "medre.adapters.matrix.adapter.MatrixSession",
                return_value=mock_session,
            ),
        ):
            with pytest.raises(RuntimeError, match="join failed"):
                await adapter.start(ctx)

        mock_session.stop.assert_called_once()
        assert adapter._session is None
        assert adapter._started is False
        assert adapter.ctx is None
        assert adapter._start_time is None

    async def test_auto_join_failure_no_started_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """'started' log must NOT be emitted when auto-join fails."""
        config = _make_matrix_config(
            auto_join_rooms=("!room1:server",),
        )
        adapter = MatrixAdapter(config)
        _, ctx = _make_adapter_context()

        mock_session = MagicMock(name="session")
        mock_session.start = AsyncMock()
        mock_session.stop = AsyncMock()
        mock_session.ensure_joined_rooms = AsyncMock(
            side_effect=RuntimeError("join failed"),
        )

        with (
            patch("medre.adapters.matrix.adapter.HAS_NIO", True),
            patch(
                "medre.adapters.matrix.adapter.MatrixSession",
                return_value=mock_session,
            ),
            caplog.at_level(logging.INFO),
        ):
            with pytest.raises(RuntimeError):
                await adapter.start(ctx)

        started_logs = [
            r
            for r in caplog.records
            if "MatrixAdapter" in r.message and "started" in r.message
        ]
        assert len(started_logs) == 0

    async def test_successful_start_marks_started_after_all_steps(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Successful start sets _started, calls _mark_started, logs after
        auto-join."""
        config = _make_matrix_config(
            auto_join_rooms=("!room1:server",),
        )
        adapter = MatrixAdapter(config)
        _, ctx = _make_adapter_context()

        mock_session = MagicMock(name="session")
        mock_session.start = AsyncMock()
        mock_session.stop = AsyncMock()
        mock_session.ensure_joined_rooms = AsyncMock(
            return_value={"!room1:server": True},
        )

        with (
            patch("medre.adapters.matrix.adapter.HAS_NIO", True),
            patch(
                "medre.adapters.matrix.adapter.MatrixSession",
                return_value=mock_session,
            ),
            caplog.at_level(logging.DEBUG),
        ):
            await adapter.start(ctx)

        assert adapter._started is True
        assert adapter.ctx is ctx
        assert adapter._start_time is not None

        # Verify log ordering: debug "joining" before info "started"
        debug_join_idx = None
        info_started_idx = None
        for i, record in enumerate(caplog.records):
            if "Matrix session connected; joining configured rooms" in record.message:
                debug_join_idx = i
            if "MatrixAdapter matrix-1 started" in record.message:
                info_started_idx = i

        assert debug_join_idx is not None, "debug 'joining' log not found"
        assert info_started_idx is not None, "info 'started' log not found"
        assert (
            debug_join_idx < info_started_idx
        ), "'joining' log should appear before 'started' log"

    async def test_post_failed_start_on_room_message_does_not_publish(self) -> None:
        """After a failed start, _on_room_message drops events (guard fires)."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()

        # Simulate a failed start: ctx=None, _started=False
        with patch("medre.adapters.matrix.adapter.HAS_NIO", False):
            with pytest.raises(MatrixConnectionError):
                await adapter.start(ctx)

        assert adapter.ctx is None
        assert adapter._started is False

        # Now try to deliver a message — should be dropped silently
        event = _make_fake_nio_event(sender="@alice:example.com")
        room = _make_fake_room()
        await adapter._on_room_message(_to_event_dict(room, event))
        assert len(published) == 0

    async def test_start_time_only_set_on_full_success(self) -> None:
        """_mark_started (and thus _start_time) is only set when start()
        completes fully — not on partial failure paths."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)

        # Verify initial state
        assert adapter._start_time is None

        # HAS_NIO=False path — _mark_started not called
        _, ctx = _make_adapter_context()
        with patch("medre.adapters.matrix.adapter.HAS_NIO", False):
            with pytest.raises(MatrixConnectionError):
                await adapter.start(ctx)
        assert adapter._start_time is None

        # session.start() failure path — _mark_started not called
        adapter2 = MatrixAdapter(_make_matrix_config())
        mock_session = MagicMock(name="session")
        mock_session.start = AsyncMock(side_effect=RuntimeError("fail"))
        mock_session.stop = AsyncMock()
        _, ctx2 = _make_adapter_context()
        with (
            patch("medre.adapters.matrix.adapter.HAS_NIO", True),
            patch(
                "medre.adapters.matrix.adapter.MatrixSession",
                return_value=mock_session,
            ),
        ):
            with pytest.raises(RuntimeError):
                await adapter2.start(ctx2)
        assert adapter2._start_time is None

        # Successful path — _mark_started IS called
        adapter3 = MatrixAdapter(_make_matrix_config())
        mock_session3 = MagicMock(name="session")
        mock_session3.start = AsyncMock()
        mock_session3.stop = AsyncMock()
        _, ctx3 = _make_adapter_context()
        with (
            patch("medre.adapters.matrix.adapter.HAS_NIO", True),
            patch(
                "medre.adapters.matrix.adapter.MatrixSession",
                return_value=mock_session3,
            ),
        ):
            await adapter3.start(ctx3)
        assert adapter3._start_time is not None

    async def test_stop_clears_start_time(self) -> None:
        """stop() clears _start_time back to None."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)
        _, ctx = _make_adapter_context()

        mock_session = MagicMock(name="session")
        mock_session.start = AsyncMock()
        mock_session.stop = AsyncMock()
        mock_session.last_sync_error = None

        with (
            patch("medre.adapters.matrix.adapter.HAS_NIO", True),
            patch(
                "medre.adapters.matrix.adapter.MatrixSession",
                return_value=mock_session,
            ),
        ):
            await adapter.start(ctx)

        assert adapter._start_time is not None

        await adapter.stop()

        assert adapter._start_time is None
        assert adapter._started is False

    async def test_successful_start_stop_failed_restart_leaves_start_time_none(
        self,
    ) -> None:
        """After successful start→stop, a failed restart leaves _start_time None."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)
        _, ctx = _make_adapter_context()

        # Phase 1: successful start
        mock_session1 = MagicMock(name="session")
        mock_session1.start = AsyncMock()
        mock_session1.stop = AsyncMock()
        mock_session1.last_sync_error = None
        mock_session1.closed = True

        with (
            patch("medre.adapters.matrix.adapter.HAS_NIO", True),
            patch(
                "medre.adapters.matrix.adapter.MatrixSession",
                return_value=mock_session1,
            ),
        ):
            await adapter.start(ctx)
        assert adapter._start_time is not None

        # Phase 2: stop
        await adapter.stop()
        assert adapter._start_time is None

        # Phase 3: failed restart (HAS_NIO=False)
        with patch("medre.adapters.matrix.adapter.HAS_NIO", False):
            with pytest.raises(MatrixConnectionError):
                await adapter.start(ctx)
        assert adapter._start_time is None
