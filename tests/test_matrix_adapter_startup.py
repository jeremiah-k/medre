"""Tests for MatrixAdapter startup-history suppression (is_live boundary).

Moved from test_matrix_adapter.py to keep that file under 1500 lines.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.matrix.metadata import MatrixMetadataEnvelope
from medre.adapters.matrix.session import MatrixSession
from tests.helpers.matrix_adapter import make_adapter_context as _make_adapter_context
from tests.helpers.matrix_adapter import make_fake_nio_event as _make_fake_nio_event
from tests.helpers.matrix_adapter import (
    make_fake_reaction_event as _make_fake_reaction_event,
)
from tests.helpers.matrix_adapter import make_fake_room as _make_fake_room
from tests.helpers.matrix_adapter import make_matrix_config as _make_matrix_config


class TestStartupHistorySuppression:
    """_on_room_message suppresses events before is_live (startup backlog)."""

    async def test_not_live_text_suppressed(self) -> None:
        """RoomMessageText is suppressed when session.is_live is False."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        # Mock session with is_live = False
        mock_session = MagicMock(name="session")
        mock_session.is_live = False
        mock_session._track_room = MagicMock()
        adapter._session = mock_session

        event = _make_fake_nio_event(sender="@alice:example.com")
        room = _make_fake_room()

        await adapter._on_room_message(room, event)
        assert len(published) == 0
        assert adapter._inbound_suppressed_startup == 1

    async def test_live_text_published(self) -> None:
        """RoomMessageText publishes normally when session.is_live is True."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        # Mock session with is_live = True
        mock_session = MagicMock(name="session")
        mock_session.is_live = True
        mock_session._track_room = MagicMock()
        mock_session.crypto_enabled = False
        mock_session.room_state = MagicMock(return_value="unknown")
        adapter._session = mock_session

        event = _make_fake_nio_event(sender="@alice:example.com")
        room = _make_fake_room()

        await adapter._on_room_message(room, event)
        assert len(published) == 1
        assert adapter._inbound_suppressed_startup == 0

    async def test_not_live_reaction_suppressed(self) -> None:
        """m.reaction events are suppressed when session.is_live is False."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        mock_session = MagicMock(name="session")
        mock_session.is_live = False
        mock_session._track_room = MagicMock()
        adapter._session = mock_session

        event = _make_fake_reaction_event(sender="@alice:example.com")
        room = _make_fake_room()

        await adapter._on_room_message(room, event)
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

        mock_session = MagicMock(name="session")
        mock_session.is_live = False
        mock_session._track_room = MagicMock()
        adapter._session = mock_session

        event = _make_fake_nio_event(sender="@bot:example.com")
        room = _make_fake_room()

        await adapter._on_room_message(room, event)
        assert adapter._inbound_suppressed_startup == 1
        assert adapter._inbound_suppressed_self == 0
        assert len(published) == 0

    async def test_pre_live_event_does_not_decode(self) -> None:
        """Pre-live events do not attempt decode (no codec call side-effects)."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        mock_session = MagicMock(name="session")
        mock_session.is_live = False
        mock_session._track_room = MagicMock()
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

        await adapter._on_room_message(room, event)
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

        mock_session = MagicMock(name="session")
        mock_session.is_live = True
        mock_session._track_room = MagicMock()
        adapter._session = mock_session

        event = _make_fake_nio_event(sender="@bot:example.com")
        room = _make_fake_room()

        await adapter._on_room_message(room, event)
        assert adapter._inbound_suppressed_self == 1
        assert adapter._inbound_suppressed_startup == 0
        assert len(published) == 0

    async def test_live_normal_event_publishes(self) -> None:
        """Live normal third-party event is decoded and published."""
        config = _make_matrix_config(user_id="@bot:example.com")
        adapter = MatrixAdapter(config)
        published, ctx = _make_adapter_context()
        adapter.ctx = ctx

        mock_session = MagicMock(name="session")
        mock_session.is_live = True
        mock_session._track_room = MagicMock()
        adapter._session = mock_session

        event = _make_fake_nio_event(sender="@alice:example.com")
        room = _make_fake_room()

        await adapter._on_room_message(room, event)
        assert adapter._inbound_published == 1
        assert adapter._inbound_suppressed_startup == 0
        assert len(published) == 1
