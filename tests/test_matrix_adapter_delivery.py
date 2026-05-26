"""Tests for MatrixAdapter delivery retry, encrypted-room safety, and rate-limit paths.

Covers:
  - Lines 533-534 — encrypted room safety transient/permanent error propagation
  - Line 555 — retry loop entry (first-attempt success)
  - Lines 615-616 — rate-limit retry_after_ms message formatting

These tests use a mock session injected directly into the adapter
to exercise the real MatrixAdapter.deliver() method without network.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from medre.adapters.matrix.adapter import MatrixAdapter, _NioRateLimitError
from medre.adapters.matrix.errors import MatrixSendError
from medre.config.adapters.matrix import MatrixConfig
from medre.core.contracts.adapter import (
    AdapterContext,
    AdapterPermanentError,
    AdapterSendError,
)
from medre.core.rendering.renderer import RenderingResult
from tests.helpers.matrix_adapter import wire_mock_session as _wire_mock_session


def _make_config(**overrides) -> MatrixConfig:
    defaults = {
        "adapter_id": "matrix-test",
        "homeserver": "https://matrix.example.com",
        "user_id": "@bot:example.com",
        "access_token": "tok",
    }
    defaults.update(overrides)
    return MatrixConfig(**defaults)


def _make_ctx() -> AdapterContext:
    import asyncio
    from datetime import datetime, timezone

    return AdapterContext(
        adapter_id="matrix-test",
        event_bus=None,
        publish_inbound=AsyncMock(),
        logger=logging.getLogger("test.matrix-adapter-delivery"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


def _make_result(
    room_id: str = "!room:server",
    body: str = "hello",
) -> RenderingResult:
    return RenderingResult(
        event_id="evt-1",
        target_adapter="matrix-test",
        target_channel=room_id,
        payload={"msgtype": "m.text", "body": body},
        metadata={"renderer": "matrix"},
    )


def _make_adapter_with_session(
    config: MatrixConfig | None = None,
    mock_client: MagicMock | None = None,
) -> MatrixAdapter:
    """Create a MatrixAdapter with a pre-injected mock session.

    Uses ``wire_mock_session`` to wrap the mock client in a real
    MatrixSession, preserving the session boundary.
    """
    cfg = config or _make_config()
    adapter = MatrixAdapter(cfg)
    adapter.ctx = _make_ctx()
    _wire_mock_session(adapter, mock_client or MagicMock(), config=cfg)
    return adapter


# ===================================================================
# Encrypted room safety — lines 533-534
# ===================================================================


class TestEncryptedRoomSafetyErrorPropagation:
    """Cover lines 533-534 — MatrixSendError propagation from _check_encrypted_room_safety."""

    async def test_transient_send_error_raises_adapter_send_error(self) -> None:
        session = MagicMock()
        session.is_room_member.return_value = True
        session.room_send = AsyncMock()
        adapter = _make_adapter_with_session(mock_client=session)

        # Patch _check_encrypted_room_safety to raise transient MatrixSendError
        async def _fake_deliver(result):
            # We need to hit the try/except around _check_encrypted_room_safety
            # directly. The simplest approach: patch the method.
            pass

        with pytest.raises(AdapterSendError) as exc_info:
            with pytest.MonkeyPatch.context() as mp:
                # Make _check_encrypted_room_safety raise transient MatrixSendError
                def _raise_transient(self_inner, room_id):
                    raise MatrixSendError("encrypted room rejected", transient=True)

                mp.setattr(
                    MatrixAdapter,
                    "_check_encrypted_room_safety",
                    _raise_transient,
                )
                await adapter.deliver(_make_result())

        assert exc_info.value.transient is True
        assert "encrypted room rejected" in str(exc_info.value)

    async def test_permanent_send_error_raises_adapter_permanent_error(self) -> None:
        session = MagicMock()
        session.is_room_member.return_value = True
        session.room_send = AsyncMock()
        adapter = _make_adapter_with_session(mock_client=session)

        with pytest.raises(AdapterPermanentError) as exc_info:
            with pytest.MonkeyPatch.context() as mp:

                def _raise_permanent(self_inner, room_id):
                    raise MatrixSendError(
                        "encrypted room permanently blocked", transient=False
                    )

                mp.setattr(
                    MatrixAdapter,
                    "_check_encrypted_room_safety",
                    _raise_permanent,
                )
                await adapter.deliver(_make_result())

        assert "encrypted room permanently blocked" in str(exc_info.value)


# ===================================================================
# Retry loop first-attempt success — line 555
# ===================================================================


class TestDeliverRetryLoopSuccess:
    """Cover line 555 — retry loop runs and succeeds on first attempt."""

    async def test_first_attempt_success_returns_result(self) -> None:
        session = MagicMock()
        session.is_room_member.return_value = True
        session.room_send = AsyncMock(return_value=SimpleNamespace(event_id="$sent-1"))
        adapter = _make_adapter_with_session(mock_client=session)

        result = await adapter.deliver(_make_result())

        assert result is not None
        assert result.native_message_id == "$sent-1"
        assert result.native_channel_id == "!room:server"
        session.room_send.assert_called_once()


# ===================================================================
# Rate-limit retry_after_ms — line 615
# ===================================================================


class TestRateLimitRetryAfterMs:
    """Cover line 615 — retry_after_ms formatting in rate-limit error."""

    async def test_rate_limit_with_retry_after_ms(self) -> None:
        session = MagicMock()
        session.is_room_member.return_value = True
        # No event_id attr → triggers rate-limit check
        # _is_nio_rate_limited_response checks for specific attributes
        adapter = _make_adapter_with_session(mock_client=session)

        # Patch _is_nio_rate_limited_response to return True and
        # make room_send raise _NioRateLimitError directly
        async def _raise_rate_limit(**kwargs):
            raise _NioRateLimitError("M_LIMIT_EXCEEDED", retry_after_ms=500)

        session.room_send = AsyncMock(side_effect=_raise_rate_limit)

        with pytest.raises(AdapterSendError) as exc_info:
            await adapter.deliver(_make_result())

        assert exc_info.value.transient is True
        error_msg = str(exc_info.value)
        assert "retry_after_ms=500" in error_msg
        assert "rate-limited" in error_msg

    async def test_rate_limit_without_retry_after_ms(self) -> None:
        session = MagicMock()
        session.is_room_member.return_value = True

        async def _raise_rate_limit(**kwargs):
            raise _NioRateLimitError("M_LIMIT_EXCEEDED", retry_after_ms=None)

        session.room_send = AsyncMock(side_effect=_raise_rate_limit)
        adapter = _make_adapter_with_session(mock_client=session)

        with pytest.raises(AdapterSendError) as exc_info:
            await adapter.deliver(_make_result())

        assert exc_info.value.transient is True
        error_msg = str(exc_info.value)
        assert "retry_after_ms" not in error_msg


# ===================================================================
# _matrix_event_type validation — message_type fallback
# ===================================================================


class TestMatrixEventTypeValidation:
    """Cover _matrix_event_type None / empty / non-string fallback to m.room.message."""

    async def test_none_event_type_falls_back(self) -> None:
        """_matrix_event_type=None → message_type=m.room.message."""
        session = MagicMock()
        session.is_room_member.return_value = True
        session.room_send = AsyncMock(return_value=SimpleNamespace(event_id="$evt-1"))
        adapter = _make_adapter_with_session(mock_client=session)

        payload = {"msgtype": "m.text", "body": "hello", "_matrix_event_type": None}
        result = RenderingResult(
            event_id="evt-1",
            target_adapter="matrix-test",
            target_channel="!room:server",
            payload=payload,
        )
        await adapter.deliver(result)

        call_kwargs = session.room_send.call_args
        assert call_kwargs.kwargs.get("message_type") == "m.room.message"

    async def test_empty_string_event_type_falls_back(self) -> None:
        """_matrix_event_type='' → message_type=m.room.message."""
        session = MagicMock()
        session.is_room_member.return_value = True
        session.room_send = AsyncMock(return_value=SimpleNamespace(event_id="$evt-1"))
        adapter = _make_adapter_with_session(mock_client=session)

        payload = {"msgtype": "m.text", "body": "hello", "_matrix_event_type": ""}
        result = RenderingResult(
            event_id="evt-1",
            target_adapter="matrix-test",
            target_channel="!room:server",
            payload=payload,
        )
        await adapter.deliver(result)

        call_kwargs = session.room_send.call_args
        assert call_kwargs.kwargs.get("message_type") == "m.room.message"

    async def test_non_string_event_type_falls_back(self) -> None:
        """_matrix_event_type=42 (non-string) → message_type=m.room.message."""
        session = MagicMock()
        session.is_room_member.return_value = True
        session.room_send = AsyncMock(return_value=SimpleNamespace(event_id="$evt-1"))
        adapter = _make_adapter_with_session(mock_client=session)

        payload = {"msgtype": "m.text", "body": "hello", "_matrix_event_type": 42}
        result = RenderingResult(
            event_id="evt-1",
            target_adapter="matrix-test",
            target_channel="!room:server",
            payload=payload,
        )
        await adapter.deliver(result)

        call_kwargs = session.room_send.call_args
        assert call_kwargs.kwargs.get("message_type") == "m.room.message"

    async def test_valid_event_type_used(self) -> None:
        """_matrix_event_type='m.reaction' → message_type=m.reaction."""
        session = MagicMock()
        session.is_room_member.return_value = True
        session.room_send = AsyncMock(return_value=SimpleNamespace(event_id="$evt-1"))
        adapter = _make_adapter_with_session(mock_client=session)

        payload = {
            "msgtype": "m.text",
            "body": "👍",
            "_matrix_event_type": "m.reaction",
        }
        result = RenderingResult(
            event_id="evt-1",
            target_adapter="matrix-test",
            target_channel="!room:server",
            payload=payload,
        )
        await adapter.deliver(result)

        call_kwargs = session.room_send.call_args
        assert call_kwargs.kwargs.get("message_type") == "m.reaction"

    async def test_whitespace_event_type_falls_back(self) -> None:
        """_matrix_event_type='  ' (whitespace) → message_type=m.room.message."""
        session = MagicMock()
        session.is_room_member.return_value = True
        session.room_send = AsyncMock(return_value=SimpleNamespace(event_id="$evt-1"))
        adapter = _make_adapter_with_session(mock_client=session)

        payload = {"msgtype": "m.text", "body": "hello", "_matrix_event_type": "  "}
        result = RenderingResult(
            event_id="evt-1",
            target_adapter="matrix-test",
            target_channel="!room:server",
            payload=payload,
        )
        await adapter.deliver(result)

        call_kwargs = session.room_send.call_args
        assert call_kwargs.kwargs.get("message_type") == "m.room.message"

    async def test_event_type_not_leaked_into_content(self) -> None:
        """_matrix_event_type is popped from content before room_send."""
        session = MagicMock()
        session.is_room_member.return_value = True
        session.room_send = AsyncMock(return_value=SimpleNamespace(event_id="$evt-1"))
        adapter = _make_adapter_with_session(mock_client=session)

        payload = {
            "msgtype": "m.text",
            "body": "hello",
            "_matrix_event_type": "m.reaction",
        }
        result = RenderingResult(
            event_id="evt-1",
            target_adapter="matrix-test",
            target_channel="!room:server",
            payload=payload,
        )
        await adapter.deliver(result)

        call_kwargs = session.room_send.call_args
        sent_content = call_kwargs.kwargs.get("content", {})
        assert "_matrix_event_type" not in sent_content
