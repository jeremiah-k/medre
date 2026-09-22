"""Installed mindroom-nio Classic Sync recovery contract guard."""

from __future__ import annotations

import inspect

import pytest

from tests.helpers.sdk_contract import assert_installed_extra_matches_declared_pins


@pytest.mark.matrix_sdk
def test_mindroom_nio_exposes_application_owned_classic_sync_contract() -> None:
    import nio
    from nio import event_provenance as provenance

    # Import name alone cannot distinguish mindroom-nio from upstream matrix-nio.
    assert_installed_extra_matches_declared_pins("matrix", ("mindroom-nio",))

    config_params = inspect.signature(nio.AsyncClientConfig).parameters
    for name in (
        "max_limit_exceeded",
        "max_timeouts",
        "backfill_limited_timelines",
        "store_sync_tokens",
        "backfill_persist_recovery",
        "replace_rotated_device_keys",
    ):
        assert name in config_params

    config = nio.AsyncClientConfig()
    assert config.max_limit_exceeded is None

    send_source = inspect.getsource(nio.AsyncClient._send)
    callback_dispatch = send_source.index("await self.run_response_callbacks([resp])")
    rate_limit_sleep = send_source.index("await asyncio.sleep(retry_after_ms / 1000)")
    assert callback_dispatch < rate_limit_sleep
    response_source = inspect.getsource(nio.AsyncClient.create_matrix_response)
    assert "resp.transport_response = transport_response" in response_source
    assert callable(getattr(nio, "RoomSendError", None))

    from medre.adapters.matrix.errors import (
        is_nio_rate_limited_response,
        retry_after_seconds_from_ms,
    )

    room_send_error = nio.RoomSendError.from_dict(
        {
            "errcode": "M_LIMIT_EXCEEDED",
            "error": "Too many requests",
            "retry_after_ms": 4000,
        },
        "!room:example.test",
    )
    assert room_send_error.status_code == "M_LIMIT_EXCEEDED"
    assert is_nio_rate_limited_response(room_send_error)
    assert retry_after_seconds_from_ms(room_send_error.retry_after_ms) == 4.0

    for name in (
        "add_event_admission_callback",
        "acknowledge_classic_sync",
        "acknowledge_unrecovered_rooms",
        "reset_classic_sync_state",
        "sync_forever",
        "stop_sync_forever",
        "to_device",
    ):
        assert callable(getattr(nio.AsyncClient, name, None))

    assert isinstance(
        getattr(nio.AsyncClient, "has_uncommitted_classic_sync_state", None),
        property,
    )
    admission_params = inspect.signature(
        nio.AsyncClient.add_event_admission_callback
    ).parameters
    assert len(admission_params) == 3  # self + callback + event classes
    assert callable(getattr(nio.events.MegolmEvent, "as_key_request", None))
    assert list(provenance.TimelineEventProvenance) == [
        provenance.TimelineEventProvenance.LIVE,
        provenance.TimelineEventProvenance.RECOVERED,
        provenance.TimelineEventProvenance.HISTORY,
    ]
    assert [item.value for item in provenance.TimelineEventProvenance] == [
        "live",
        "recovered",
        "history",
    ]
