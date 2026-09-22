"""Matrix room-send rate-limit ownership at the session boundary."""

from __future__ import annotations

from types import SimpleNamespace

from medre.adapters.matrix.session import MatrixSession
from tests.helpers.matrix_session import make_matrix_config
from tests.helpers.matrix_session import mock_nio as _mock_nio  # noqa: F401


async def test_session_registers_room_send_rate_limit_interceptor(mock_nio) -> None:
    session = MatrixSession(make_matrix_config())
    try:
        await session.start()
        calls = mock_nio.AsyncClient.return_value.add_response_callback.call_args_list
        assert any(
            call.args[0].__name__ == "_on_room_send_error_response"
            and call.args[1] is mock_nio.RoomSendError
            for call in calls
            if len(call.args) >= 2
        )
    finally:
        await session.stop()


async def test_room_send_surfaces_first_explicit_rate_limit_before_provider_retry() -> None:
    session = MatrixSession(make_matrix_config())
    response = SimpleNamespace(
        status_code="M_LIMIT_EXCEEDED",
        retry_after_ms=4000,
    )
    calls = 0

    async def room_send(**_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        await session._on_room_send_error_response(response)
        raise AssertionError("provider retry should have been interrupted")

    session._client = SimpleNamespace(room_send=room_send)

    result = await session.room_send(
        room_id="!room:example.test",
        message_type="m.room.message",
        content={"msgtype": "m.text", "body": "hello"},
        tx_id="txn-1",
    )

    assert result is response
    assert calls == 1


async def test_room_send_rate_limit_without_usable_hint_still_surfaces() -> None:
    session = MatrixSession(make_matrix_config())

    for retry_after_ms in (None, "4000", -1, float("inf"), 10**400):
        response = SimpleNamespace(
            status_code="M_LIMIT_EXCEEDED",
            retry_after_ms=retry_after_ms,
        )

        async def room_send(**_kwargs: object) -> object:
            await session._on_room_send_error_response(response)
            raise AssertionError("provider retry should have been interrupted")

        session._client = SimpleNamespace(room_send=room_send)
        result = await session.room_send(
            room_id="!room:example.test",
            message_type="m.room.message",
            content={"msgtype": "m.text", "body": "hello"},
            tx_id="txn-invalid-hint",
        )
        assert result is response


async def test_room_send_raw_http_429_without_errcode_still_surfaces() -> None:
    session = MatrixSession(make_matrix_config())
    response = SimpleNamespace(
        status_code=None,
        retry_after_ms=None,
        transport_response=SimpleNamespace(status=429),
    )

    async def room_send(**_kwargs: object) -> object:
        await session._on_room_send_error_response(response)
        raise AssertionError("provider retry should have been interrupted")

    session._client = SimpleNamespace(room_send=room_send)
    result = await session.room_send(
        room_id="!room:example.test",
        message_type="m.room.message",
        content={"msgtype": "m.text", "body": "hello"},
        tx_id="txn-http-429",
    )

    assert result is response


async def test_room_send_interceptor_ignores_non_rate_limit_errors() -> None:
    session = MatrixSession(make_matrix_config())
    response = SimpleNamespace(
        status_code="M_FORBIDDEN",
        retry_after_ms=4000,
        transport_response=SimpleNamespace(status=403),
    )

    await session._on_room_send_error_response(response)
