"""Tests for MatrixSession methods with uncovered lines.

Covers:
  - is_room_member (lines 354-364)
  - is_room_encrypted (lines 366-386)
  - _normalize_event (lines 684-700)
  - _resolve_display_name (lines 702-749)
  - _on_nio_event (lines 751-765)
  - room_send (lines 1412-1420)

Follows patterns from tests/test_matrix_session.py and
tests/helpers/matrix_session.py.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from medre.adapters.matrix.errors import MatrixConnectionError
from medre.adapters.matrix.session import MatrixSession
from tests.helpers.matrix_session import make_matrix_config
from tests.helpers.matrix_session import mock_nio as _mock_nio  # noqa: F401

# ===================================================================
# is_room_member
# ===================================================================


class TestIsRoomMember:
    """Cover lines 354-364 — is_room_member method."""

    def test_client_is_none_returns_false(self) -> None:
        config = make_matrix_config()
        session = MatrixSession(config)
        assert session._client is None
        assert session.is_room_member("!room:server") is False

    def test_client_has_no_rooms_attr_returns_false(self) -> None:
        config = make_matrix_config()
        session = MatrixSession(config)
        client = SimpleNamespace()
        # No 'rooms' attribute at all
        session._client = client
        assert session.is_room_member("!room:server") is False

    def test_rooms_is_not_dict_returns_false(self) -> None:
        config = make_matrix_config()
        session = MatrixSession(config)
        client = SimpleNamespace(rooms=["!room:server"])  # list, not dict
        session._client = client
        assert session.is_room_member("!room:server") is False

    def test_rooms_is_none_returns_false(self) -> None:
        config = make_matrix_config()
        session = MatrixSession(config)
        client = SimpleNamespace(rooms=None)
        session._client = client
        assert session.is_room_member("!room:server") is False

    def test_room_id_present_returns_true(self) -> None:
        config = make_matrix_config()
        session = MatrixSession(config)
        client = SimpleNamespace(rooms={"!room:server": object()})
        session._client = client
        assert session.is_room_member("!room:server") is True

    def test_room_id_absent_returns_false(self) -> None:
        config = make_matrix_config()
        session = MatrixSession(config)
        client = SimpleNamespace(rooms={"!other:server": object()})
        session._client = client
        assert session.is_room_member("!room:server") is False


# ===================================================================
# is_room_encrypted
# ===================================================================


class TestIsRoomEncrypted:
    """Cover lines 366-386 — is_room_encrypted method."""

    def test_state_encrypted_returns_true(self) -> None:
        config = make_matrix_config()
        session = MatrixSession(config)
        session._room_states["!room:server"] = "encrypted"
        assert session.is_room_encrypted("!room:server") is True

    def test_state_plaintext_returns_false(self) -> None:
        config = make_matrix_config()
        session = MatrixSession(config)
        session._room_states["!room:server"] = "plaintext"
        assert session.is_room_encrypted("!room:server") is False

    def test_state_unknown_client_none_returns_false(self) -> None:
        config = make_matrix_config()
        session = MatrixSession(config)
        assert session._client is None
        # room_state returns "unknown" for untracked rooms
        assert session.is_room_encrypted("!room:server") is False

    def test_state_unknown_room_obj_encrypted_true(self) -> None:
        config = make_matrix_config()
        session = MatrixSession(config)
        room_obj = SimpleNamespace(encrypted=True)
        client = SimpleNamespace(rooms={"!room:server": room_obj})
        session._client = client
        assert session.is_room_encrypted("!room:server") is True

    def test_state_unknown_room_obj_encrypted_false(self) -> None:
        config = make_matrix_config()
        session = MatrixSession(config)
        room_obj = SimpleNamespace(encrypted=False)
        client = SimpleNamespace(rooms={"!room:server": room_obj})
        session._client = client
        assert session.is_room_encrypted("!room:server") is False

    def test_state_unknown_room_obj_none(self) -> None:
        config = make_matrix_config()
        session = MatrixSession(config)
        client = SimpleNamespace(rooms={})
        session._client = client
        assert session.is_room_encrypted("!room:server") is False


# ===================================================================
# _normalize_event
# ===================================================================


class TestNormalizeEvent:
    """Cover lines 684-700 — _normalize_event method."""

    def test_source_is_not_dict_uses_empty_content(self) -> None:
        config = make_matrix_config()
        session = MatrixSession(config)
        room = SimpleNamespace(room_id="!room:server", users={})
        event = SimpleNamespace(
            sender="@alice:server",
            body="hello",
            event_id="$evt1",
            source="not_a_dict",
            msgtype="m.text",
            server_timestamp=12345,
        )
        result = session._normalize_event(room, event)
        assert result["source"] == {}
        # No content_from_source key expected — source is not a dict
        # content is derived from source which is not a dict → empty
        # msgtype falls back to event.msgtype
        assert result["msgtype"] == "m.text"

    def test_msgtype_from_content(self) -> None:
        config = make_matrix_config()
        session = MatrixSession(config)
        room = SimpleNamespace(room_id="!room:server", users={})
        event = SimpleNamespace(
            sender="@alice:server",
            body="hello",
            event_id="$evt1",
            source={"content": {"msgtype": "m.notice", "body": "hello"}},
            server_timestamp=12345,
        )
        result = session._normalize_event(room, event)
        assert result["msgtype"] == "m.notice"

    def test_msgtype_from_event_attr_when_content_missing(self) -> None:
        config = make_matrix_config()
        session = MatrixSession(config)
        room = SimpleNamespace(room_id="!room:server", users={})
        event = SimpleNamespace(
            sender="@alice:server",
            body="hello",
            event_id="$evt1",
            source={"content": {}},
            msgtype="m.emote",
            server_timestamp=12345,
        )
        result = session._normalize_event(room, event)
        assert result["msgtype"] == "m.emote"

    def test_server_timestamp_from_origin_server_ts_fallback(self) -> None:
        config = make_matrix_config()
        session = MatrixSession(config)
        room = SimpleNamespace(room_id="!room:server", users={})
        event = SimpleNamespace(
            sender="@alice:server",
            body="hello",
            event_id="$evt1",
            source={"content": {"msgtype": "m.text"}},
            origin_server_ts=99999,
        )
        result = session._normalize_event(room, event)
        assert result["server_timestamp"] == 99999

    def test_server_timestamp_prefers_server_timestamp_attr(self) -> None:
        config = make_matrix_config()
        session = MatrixSession(config)
        room = SimpleNamespace(room_id="!room:server", users={})
        event = SimpleNamespace(
            sender="@alice:server",
            body="hello",
            event_id="$evt1",
            source={"content": {"msgtype": "m.text"}},
            server_timestamp=11111,
            origin_server_ts=99999,
        )
        result = session._normalize_event(room, event)
        assert result["server_timestamp"] == 11111

    def test_all_fields_populated(self) -> None:
        config = make_matrix_config()
        session = MatrixSession(config)
        room = SimpleNamespace(room_id="!room:server", users={})
        event = SimpleNamespace(
            sender="@bob:server",
            body="world",
            event_id="$evt2",
            source={
                "content": {"msgtype": "m.text", "body": "world"},
                "event_id": "$evt2",
            },
            server_timestamp=55555,
        )
        result = session._normalize_event(room, event)
        assert result["room_id"] == "!room:server"
        assert result["sender"] == "@bob:server"
        assert result["body"] == "world"
        assert result["event_id"] == "$evt2"
        assert result["source"]["content"]["msgtype"] == "m.text"
        assert result["msgtype"] == "m.text"
        assert result["server_timestamp"] == 55555
        assert result["sender_display_name"] == "@bob:server"

    def test_msgtype_non_string_returns_none(self) -> None:
        config = make_matrix_config()
        session = MatrixSession(config)
        room = SimpleNamespace(room_id="!room:server", users={})
        event = SimpleNamespace(
            sender="@alice:server",
            body="hello",
            event_id="$evt1",
            source={"content": {"msgtype": 42}},
            server_timestamp=12345,
        )
        result = session._normalize_event(room, event)
        assert result["msgtype"] is None


# ===================================================================
# _resolve_display_name
# ===================================================================


class TestResolveDisplayName:
    """Cover lines 702-749 — _resolve_display_name static method."""

    def test_user_name_callable_returns_valid_name(self) -> None:
        room = SimpleNamespace(
            user_name=lambda sender: "Alice",
            users={},
        )
        result = MatrixSession._resolve_display_name(room, "@alice:server")
        assert result == "Alice"

    def test_user_name_callable_raises_returns_fallback(self) -> None:
        def bad_name(sender: str) -> str:
            raise ValueError("nope")

        room = SimpleNamespace(user_name=bad_name, users={})
        result = MatrixSession._resolve_display_name(room, "@alice:server")
        assert result == "@alice:server"

    def test_user_name_callable_returns_none_falls_through(self) -> None:
        room = SimpleNamespace(
            user_name=lambda sender: None,
            users={"@alice:server": {"display_name": "Bob"}},
        )
        result = MatrixSession._resolve_display_name(room, "@alice:server")
        assert result == "Bob"

    def test_users_dict_display_name_key(self) -> None:
        room = SimpleNamespace(
            users={"@alice:server": {"display_name": "Alice Display"}},
        )
        result = MatrixSession._resolve_display_name(room, "@alice:server")
        assert result == "Alice Display"

    def test_users_dict_displayname_key(self) -> None:
        room = SimpleNamespace(
            users={"@alice:server": {"displayname": "Alice DN"}},
        )
        result = MatrixSession._resolve_display_name(room, "@alice:server")
        assert result == "Alice DN"

    def test_users_dict_name_key(self) -> None:
        room = SimpleNamespace(
            users={"@alice:server": {"name": "Alice Name"}},
        )
        result = MatrixSession._resolve_display_name(room, "@alice:server")
        assert result == "Alice Name"

    def test_user_info_object_attribute_path(self) -> None:
        user_info = SimpleNamespace(
            display_name="ObjAlice", displayname="ObjDN", name="ObjN"
        )
        room = SimpleNamespace(users={"@alice:server": user_info})
        result = MatrixSession._resolve_display_name(room, "@alice:server")
        assert result == "ObjAlice"

    def test_user_info_object_displayname_attr(self) -> None:
        user_info = SimpleNamespace(displayname="ObjDN")
        room = SimpleNamespace(users={"@alice:server": user_info})
        result = MatrixSession._resolve_display_name(room, "@alice:server")
        assert result == "ObjDN"

    def test_user_info_object_name_attr(self) -> None:
        user_info = SimpleNamespace(name="ObjN")
        room = SimpleNamespace(users={"@alice:server": user_info})
        result = MatrixSession._resolve_display_name(room, "@alice:server")
        assert result == "ObjN"

    def test_users_none_returns_sender(self) -> None:
        room = SimpleNamespace(users=None)
        result = MatrixSession._resolve_display_name(room, "@alice:server")
        assert result == "@alice:server"

    def test_user_info_none_returns_sender(self) -> None:
        room = SimpleNamespace(users={"@alice:server": None})
        result = MatrixSession._resolve_display_name(room, "@alice:server")
        assert result == "@alice:server"

    def test_empty_display_name_in_dict_returns_sender(self) -> None:
        room = SimpleNamespace(
            users={"@alice:server": {"display_name": "   "}},
        )
        result = MatrixSession._resolve_display_name(room, "@alice:server")
        assert result == "@alice:server"

    def test_sender_not_in_users_returns_sender(self) -> None:
        room = SimpleNamespace(users={"@other:server": {"display_name": "Other"}})
        result = MatrixSession._resolve_display_name(room, "@alice:server")
        assert result == "@alice:server"


# ===================================================================
# _on_nio_event
# ===================================================================


class TestOnNioEvent:
    """Cover lines 751-765 — _on_nio_event method."""

    async def test_no_callback_returns_early(self) -> None:
        config = make_matrix_config()
        session = MatrixSession(config)
        assert session._message_callback is None
        room = SimpleNamespace(room_id="!room:server", users={})
        event = SimpleNamespace(
            sender="@alice:server",
            body="hello",
            event_id="$evt1",
            source={"content": {"msgtype": "m.text"}},
            server_timestamp=12345,
        )
        # Should not raise and should return None
        await session._on_nio_event(room, event)

    async def test_tracks_room_and_calls_callback(self) -> None:
        config = make_matrix_config()
        callback = AsyncMock()
        session = MatrixSession(config, message_callback=callback)
        room = SimpleNamespace(room_id="!room:server", users={})
        event = SimpleNamespace(
            sender="@alice:server",
            body="hello",
            event_id="$evt1",
            source={"content": {"msgtype": "m.text"}},
            server_timestamp=12345,
        )
        await session._on_nio_event(room, event)
        # Should have tracked the room
        assert "!room:server" in session._room_states
        # Should have called the callback with normalized dict
        callback.assert_called_once()
        normalized = callback.call_args[0][0]
        assert normalized["room_id"] == "!room:server"
        assert normalized["sender"] == "@alice:server"
        assert normalized["body"] == "hello"

    async def test_empty_room_id_not_tracked(self) -> None:
        config = make_matrix_config()
        callback = AsyncMock()
        session = MatrixSession(config, message_callback=callback)
        room = SimpleNamespace(room_id="", users={})
        event = SimpleNamespace(
            sender="@alice:server",
            body="hello",
            event_id="$evt1",
            source={"content": {"msgtype": "m.text"}},
            server_timestamp=12345,
        )
        await session._on_nio_event(room, event)
        # Empty room_id should not be tracked
        assert len(session._room_states) == 0
        callback.assert_called_once()


# ===================================================================
# room_send
# ===================================================================


class TestRoomSend:
    """Cover lines 1412-1420 — room_send method."""

    async def test_client_none_raises_connection_error(self) -> None:
        config = make_matrix_config()
        session = MatrixSession(config)
        assert session._client is None
        with pytest.raises(MatrixConnectionError, match="cannot send"):
            await session.room_send(
                room_id="!room:server",
                message_type="m.room.message",
                content={"msgtype": "m.text", "body": "hello"},
            )

    async def test_client_delegates_room_send(self) -> None:
        config = make_matrix_config()
        session = MatrixSession(config)
        mock_client = MagicMock()
        mock_client.room_send = AsyncMock(
            return_value=SimpleNamespace(event_id="$sent1")
        )
        session._client = mock_client

        result = await session.room_send(
            room_id="!room:server",
            message_type="m.room.message",
            content={"msgtype": "m.text", "body": "hello"},
            ignore_unverified_devices=True,
            tx_id="txn123",
        )
        mock_client.room_send.assert_called_once_with(
            room_id="!room:server",
            message_type="m.room.message",
            content={"msgtype": "m.text", "body": "hello"},
            ignore_unverified_devices=True,
            tx_id="txn123",
        )
        assert result.event_id == "$sent1"
