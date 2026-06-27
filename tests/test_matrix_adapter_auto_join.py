"""Tests for MatrixAdapter auto-join integration.

Covers start() auto-join of configured rooms and deliver() auto-join for
target channels not yet joined.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from medre.adapters.matrix.adapter import MatrixAdapter
from medre.core.contracts.adapter import AdapterPermanentError
from medre.core.rendering.renderer import RenderingResult
from tests.helpers.matrix_session import (
    make_matrix_config,
    make_matrix_context,
)

# ===================================================================
# auto-join integration
# ===================================================================


class TestAdapterStartAutoJoin:
    """MatrixAdapter.start() calls ensure_joined_rooms with configured auto_join_rooms."""

    async def test_start_calls_ensure_joined_rooms(self, mock_nio) -> None:
        """When auto_join_rooms is configured, start calls ensure_joined_rooms."""
        config = make_matrix_config(
            auto_join_rooms=("!room1:server", "!room2:server"),
        )
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(make_matrix_context())
            mock_client = mock_nio.AsyncClient.return_value
            # join should have been called for each room
            assert mock_client.join.call_count == 2
        finally:
            await adapter.stop()

    async def test_start_no_auto_join_when_empty(self, mock_nio) -> None:
        """When auto_join_rooms is empty, no join calls."""
        config = make_matrix_config()
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(make_matrix_context())
            mock_client = mock_nio.AsyncClient.return_value
            mock_client.join.assert_not_called()
        finally:
            await adapter.stop()

    async def test_start_does_not_fail_on_join_failure(self, mock_nio) -> None:
        """Adapter start succeeds even if auto-join fails for some rooms."""
        config = make_matrix_config(
            auto_join_rooms=("!bad:server",),
        )
        adapter = MatrixAdapter(config)
        mock_client = mock_nio.AsyncClient.return_value

        async def _failing_join(rid: str) -> MagicMock:
            err = MagicMock(name="error")
            del err.room_id
            return err

        mock_client.join = AsyncMock(side_effect=_failing_join)
        try:
            await adapter.start(make_matrix_context())
            assert adapter._session is not None
        finally:
            await adapter.stop()


class TestAdapterDeliverAutoJoin:
    """MatrixAdapter.deliver() auto-join for configured target rooms."""

    async def test_deliver_auto_joins_configured_room(self, mock_nio) -> None:
        """deliver() auto-joins a configured room not yet joined."""
        config = make_matrix_config(
            auto_join_rooms=("!target:server",),
        )
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(make_matrix_context())
            mock_client = mock_nio.AsyncClient.return_value
            mock_client.rooms = {}

            # Reset join call count from startup auto-join.
            mock_client.join.reset_mock()

            result = RenderingResult(
                event_id="evt-1",
                target_adapter="matrix-test",
                target_channel="!target:server",
                payload={"msgtype": "m.text", "body": "hello"},
                metadata={},
            )
            # Set up room_send to return a valid response.
            send_resp = MagicMock(name="send_resp")
            send_resp.event_id = "$evt-123"
            mock_client.room_send = AsyncMock(return_value=send_resp)

            await adapter.deliver(result)
            mock_client.join.assert_called_once_with("!target:server")
        finally:
            await adapter.stop()

    async def test_deliver_raises_on_join_failure(self, mock_nio) -> None:
        """deliver() raises AdapterPermanentError when auto-join fails."""
        config = make_matrix_config(
            auto_join_rooms=("!target:server",),
        )
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(make_matrix_context())
            mock_client = mock_nio.AsyncClient.return_value
            mock_client.rooms = {}

            # Make join fail.
            err_resp = MagicMock(name="error")
            del err_resp.room_id
            mock_client.join = AsyncMock(return_value=err_resp)

            result = RenderingResult(
                event_id="evt-1",
                target_adapter="matrix-test",
                target_channel="!target:server",
                payload={"msgtype": "m.text", "body": "hello"},
                metadata={},
            )
            with pytest.raises(AdapterPermanentError, match="auto-join"):
                await adapter.deliver(result)
        finally:
            await adapter.stop()

    async def test_deliver_does_not_auto_join_unconfigured_room(self, mock_nio) -> None:
        """deliver() does NOT auto-join a room not in auto_join_rooms."""
        config = make_matrix_config(
            auto_join_rooms=("!configured:server",),
        )
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(make_matrix_context())
            mock_client = mock_nio.AsyncClient.return_value
            # Set up rooms with the target room already joined (plaintext).
            mock_client.rooms = {}

            mock_client.join.reset_mock()

            # Set up room_send to return a valid response.
            send_resp = MagicMock(name="send_resp")
            send_resp.event_id = "$evt-123"
            mock_client.room_send = AsyncMock(return_value=send_resp)

            result = RenderingResult(
                event_id="evt-1",
                target_adapter="matrix-test",
                target_channel="!unconfigured:server",
                payload={"msgtype": "m.text", "body": "hello"},
                metadata={},
            )
            await adapter.deliver(result)
            # join should NOT have been called for the unconfigured room
            mock_client.join.assert_not_called()
        finally:
            await adapter.stop()

    async def test_deliver_skips_join_when_already_joined(self, mock_nio) -> None:
        """deliver() skips auto-join when already in client.rooms."""
        config = make_matrix_config(
            auto_join_rooms=("!target:server",),
        )
        adapter = MatrixAdapter(config)
        try:
            await adapter.start(make_matrix_context())
            mock_client = mock_nio.AsyncClient.return_value
            mock_client.rooms = {"!target:server": SimpleNamespace(encrypted=False)}

            mock_client.join.reset_mock()

            send_resp = MagicMock(name="send_resp")
            send_resp.event_id = "$evt-123"
            mock_client.room_send = AsyncMock(return_value=send_resp)

            result = RenderingResult(
                event_id="evt-1",
                target_adapter="matrix-test",
                target_channel="!target:server",
                payload={"msgtype": "m.text", "body": "hello"},
                metadata={},
            )
            await adapter.deliver(result)
            mock_client.join.assert_not_called()
        finally:
            await adapter.stop()
