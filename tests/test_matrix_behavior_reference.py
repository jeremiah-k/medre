"""Executable Matrix behavior requirements derived from the MMRelay corpus.

These tests intentionally exercise MEDRE boundaries.  MMRelay is provenance for
real-world transport behavior, not a runtime dependency and not an architecture
model for MEDRE.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.matrix.codec import MatrixCodec
from medre.adapters.matrix.errors import MatrixConnectionError
from medre.adapters.matrix.identity import MatrixCrossSigningService
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.adapters.matrix.session import MatrixSession
from medre.config.adapters.matrix import MatrixConfig
from medre.core.contracts.adapter import AdapterContext
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.rendering.renderer import RenderingContext


def _matrix_config(**overrides: Any) -> MatrixConfig:
    values: dict[str, Any] = {
        "adapter_id": "matrix-reference",
        "homeserver": "https://matrix.example.test",
        "user_id": "@relay:example.test",
        "access_token": "token",
    }
    values.update(overrides)
    return MatrixConfig(**values)


def _matrix_event(
    *,
    sender: str = "@alice:example.test",
    event_id: str = "$event",
    body: str = "hello",
    content: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event_content = content or {"msgtype": "m.text", "body": body}
    return {
        "room_id": "!room:example.test",
        "sender": sender,
        "sender_display_name": sender,
        "body": body,
        "event_id": event_id,
        "source": {
            "type": "m.room.message",
            "event_id": event_id,
            "sender": sender,
            "content": event_content,
        },
        "msgtype": event_content.get("msgtype", "m.text"),
        "server_timestamp": 1,
    }


def _context() -> AdapterContext:
    return AdapterContext(
        adapter_id="matrix-reference",
        event_bus=None,
        publish_inbound=AsyncMock(),
        logger=logging.getLogger("test.matrix.reference"),
        clock=lambda: datetime.now(UTC),
        shutdown_event=asyncio.Event(),
    )


async def test_device_discovery_accepts_matching_authenticated_identity() -> None:
    session = MatrixSession(_matrix_config())
    client = SimpleNamespace(
        access_token=None,
        whoami=AsyncMock(
            return_value=SimpleNamespace(
                user_id="@relay:example.test",
                device_id="DEVICE42",
            )
        ),
    )
    session._client = client

    assert await session._discover_device_id() == "DEVICE42"
    assert client.access_token == "token"
    client.whoami.assert_awaited_once()


async def test_device_discovery_rejects_authenticated_user_mismatch() -> None:
    session = MatrixSession(_matrix_config())
    session._client = SimpleNamespace(
        access_token=None,
        whoami=AsyncMock(
            return_value=SimpleNamespace(
                user_id="@other:example.test",
                device_id="DEVICE42",
            )
        ),
    )

    with pytest.raises(MatrixConnectionError, match="not configured user_id"):
        await session._discover_device_id()


async def test_encrypted_client_policy_combines_peer_recovery_and_medre_checkpoint() -> (
    None
):
    admission = AsyncMock()
    loader = AsyncMock(return_value=None)
    committer = AsyncMock()
    session = MatrixSession(
        _matrix_config(),
        admission_callback=admission,
        checkpoint_loader=loader,
        checkpoint_committer=committer,
    )
    captured: dict[str, Any] = {}

    def _config_factory(**kwargs: Any) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(replace_rotated_device_keys=False, **kwargs)

    nio_module = SimpleNamespace(AsyncClientConfig=_config_factory)
    client_config = session._build_client_config(
        nio_module,
        encryption_enabled=True,
    )

    assert client_config.replace_rotated_device_keys is True
    assert captured["max_timeouts"] == 3
    assert captured["backfill_limited_timelines"] is True
    assert captured["store_sync_tokens"] is False
    assert captured["backfill_persist_recovery"] is False


def test_encrypted_client_policy_supports_frozen_provider_config() -> None:
    @dataclass(frozen=True)
    class FrozenConfig:
        encryption_enabled: bool
        max_timeouts: int
        backfill_limited_timelines: bool
        store_sync_tokens: bool
        backfill_persist_recovery: bool
        replace_rotated_device_keys: bool = False

    session = MatrixSession(_matrix_config())
    nio_module = SimpleNamespace(AsyncClientConfig=FrozenConfig)

    config = session._build_client_config(nio_module, encryption_enabled=True)

    assert config.replace_rotated_device_keys is True


def test_encrypted_client_policy_tolerates_unreplaceable_provider_config() -> None:
    class ImmutableConfig:
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)
            self.__dict__["replace_rotated_device_keys"] = False

        def __setattr__(self, name: str, value: Any) -> None:
            if name == "replace_rotated_device_keys":
                raise AttributeError("immutable")
            object.__setattr__(self, name, value)

    session = MatrixSession(_matrix_config())
    nio_module = SimpleNamespace(AsyncClientConfig=ImmutableConfig)

    config = session._build_client_config(nio_module, encryption_enabled=True)

    assert config.replace_rotated_device_keys is False


async def test_runtime_cross_signing_reconciliation_never_bootstraps_missing_identity() -> (
    None
):
    response = SimpleNamespace(
        status=200,
        json=AsyncMock(return_value={"master_keys": {}, "device_keys": {}}),
    )
    ensure_cross_signing = AsyncMock()
    client = SimpleNamespace(
        ensure_cross_signing=ensure_cross_signing,
        cross_signing_identity=None,
        user_id="@relay:example.test",
        device_id="DEVICE42",
        access_token="token",
        send=AsyncMock(return_value=response),
    )
    service = MatrixCrossSigningService(client, operation_timeout_seconds=1.0)

    result = await service.reconcile(allow_bootstrap=False)

    assert result is None
    ensure_cross_signing.assert_not_awaited()
    diagnostics = service.diagnostics()
    assert diagnostics.chain_status == "missing"
    assert diagnostics.repair_required is True
    assert diagnostics.reset_required is False


async def test_live_missing_room_key_request_retries_transient_failure_then_succeeds() -> (
    None
):
    session = MatrixSession(_matrix_config(encryption_mode="e2ee_required"))
    session._crypto_enabled = True
    session._live_sync_started = True
    success = SimpleNamespace()
    to_device = AsyncMock(side_effect=[TimeoutError("temporary"), success])
    session._client = SimpleNamespace(
        device_id="DEVICE42",
        user_id="@relay:example.test",
        to_device=to_device,
        olm=None,
        store=None,
    )
    event = SimpleNamespace(
        event_id="$encrypted",
        session_id="sensitive-session-id",
        as_key_request=MagicMock(return_value={"request": "missing-key"}),
    )
    room = SimpleNamespace(room_id="!room:example.test")

    with patch("medre.adapters.matrix.session._sleep", new=AsyncMock()) as sleep:
        await session._on_megolm_event(room, event)
        tasks = list(session._room_key_request_tasks.values())
        if tasks:
            await asyncio.gather(*tasks)

    assert event.room_id == "!room:example.test"
    assert to_device.await_count == 2
    sleep.assert_awaited_once_with(2.0)
    diagnostics = session.diagnostics()
    assert diagnostics.megolm_recovery_attempts == 2
    assert diagnostics.megolm_recovery_successes == 1
    assert diagnostics.megolm_recovery_failures == 0


async def test_missing_room_key_request_is_bounded_on_explicit_provider_errors() -> (
    None
):
    session = MatrixSession(_matrix_config(encryption_mode="e2ee_required"))
    session._crypto_enabled = True
    error_type = type("ToDeviceError", (), {})
    response = error_type()
    response.errcode = "M_LIMIT_EXCEEDED"
    to_device = AsyncMock(return_value=response)
    session._client = SimpleNamespace(
        device_id="DEVICE42",
        user_id="@relay:example.test",
        to_device=to_device,
        olm=None,
        store=None,
    )
    event = SimpleNamespace(
        as_key_request=MagicMock(return_value={"request": "missing-key"}),
    )

    with patch("medre.adapters.matrix.session._sleep", new=AsyncMock()) as sleep:
        await session._request_missing_room_key(
            event=event,
            event_id="$encrypted",
            room_id="!room:example.test",
            session_id_tag="redacted",
        )

    assert to_device.await_count == 3
    assert sleep.await_args_list[0].args == (2.0,)
    assert sleep.await_args_list[1].args == (4.0,)
    diagnostics = session.diagnostics()
    assert diagnostics.megolm_recovery_attempts == 3
    assert diagnostics.megolm_recovery_successes == 0
    assert diagnostics.megolm_recovery_failures == 1


async def test_missing_room_key_request_noops_without_crypto_or_device_identity() -> (
    None
):
    session = MatrixSession(_matrix_config())
    event = SimpleNamespace(as_key_request=MagicMock())

    await session._request_missing_room_key(
        event=event,
        event_id="$encrypted",
        room_id="!room:example.test",
        session_id_tag="redacted",
    )
    event.as_key_request.assert_not_called()

    session._crypto_enabled = True
    session._client = SimpleNamespace(device_id=None, user_id="@relay:example.test")
    await session._request_missing_room_key(
        event=event,
        event_id="$encrypted",
        room_id="!room:example.test",
        session_id_tag="redacted",
    )
    event.as_key_request.assert_not_called()


async def test_missing_room_key_request_records_request_construction_failure() -> None:
    session = MatrixSession(_matrix_config())
    session._crypto_enabled = True
    session._client = SimpleNamespace(
        device_id="DEVICE42",
        user_id="@relay:example.test",
        to_device=AsyncMock(),
    )
    event = SimpleNamespace(
        as_key_request=MagicMock(side_effect=ValueError("bad event")),
    )

    await session._request_missing_room_key(
        event=event,
        event_id="$encrypted",
        room_id="!room:example.test",
        session_id_tag="redacted",
    )

    assert session._room_key_request_failures == 1
    session._client.to_device.assert_not_awaited()


async def test_missing_room_key_request_propagates_cancellation() -> None:
    session = MatrixSession(_matrix_config())
    session._crypto_enabled = True
    session._client = SimpleNamespace(
        device_id="DEVICE42",
        user_id="@relay:example.test",
        to_device=AsyncMock(side_effect=asyncio.CancelledError()),
    )
    event = SimpleNamespace(
        as_key_request=MagicMock(return_value={"request": "missing-key"}),
    )

    with pytest.raises(asyncio.CancelledError):
        await session._request_missing_room_key(
            event=event,
            event_id="$encrypted",
            room_id="!room:example.test",
            session_id_tag="redacted",
        )

    assert session._room_key_request_attempts == 1
    assert session._room_key_request_failures == 0


async def test_missing_room_key_request_does_not_retry_programming_error() -> None:
    session = MatrixSession(_matrix_config())
    session._crypto_enabled = True
    session._client = SimpleNamespace(
        device_id="DEVICE42",
        user_id="@relay:example.test",
        to_device=AsyncMock(side_effect=TypeError("provider contract changed")),
    )
    event = SimpleNamespace(
        as_key_request=MagicMock(return_value={"request": "missing-key"}),
    )

    await session._request_missing_room_key(
        event=event,
        event_id="$encrypted",
        room_id="!room:example.test",
        session_id_tag="redacted",
    )

    assert session._client.to_device.await_count == 1
    assert session._room_key_request_attempts == 1
    assert session._room_key_request_failures == 1


async def test_startup_undecryptable_event_does_not_request_historical_keys() -> None:
    session = MatrixSession(_matrix_config(encryption_mode="e2ee_required"))
    session._crypto_enabled = True
    to_device = AsyncMock()
    session._client = SimpleNamespace(
        device_id="DEVICE42",
        user_id="@relay:example.test",
        to_device=to_device,
        olm=None,
        store=None,
    )
    event = SimpleNamespace(
        event_id="$history",
        session_id="history-session",
        as_key_request=MagicMock(),
    )

    await session._on_megolm_event(
        SimpleNamespace(room_id="!room:example.test"),
        event,
    )

    to_device.assert_not_awaited()
    event.as_key_request.assert_not_called()
    assert session.diagnostics().megolm_recovery_attempts == 0


async def test_duplicate_live_undecryptable_event_requests_keys_once_per_window() -> (
    None
):
    session = MatrixSession(_matrix_config(encryption_mode="e2ee_required"))
    session._crypto_enabled = True
    session._live_sync_started = True
    to_device = AsyncMock(return_value=SimpleNamespace())
    session._client = SimpleNamespace(
        device_id="DEVICE42",
        user_id="@relay:example.test",
        to_device=to_device,
        olm=None,
        store=None,
    )
    event = SimpleNamespace(
        event_id="$encrypted",
        session_id="same-session",
        as_key_request=MagicMock(return_value={"request": "missing-key"}),
    )
    room = SimpleNamespace(room_id="!room:example.test")

    await session._on_megolm_event(room, event)
    tasks = list(session._room_key_request_tasks.values())
    if tasks:
        await asyncio.gather(*tasks)
    await session._on_megolm_event(room, event)

    to_device.assert_awaited_once()
    assert session.diagnostics().megolm_recovery_attempts == 1


async def test_stop_drains_recovery_task_registered_after_cancel_snapshot() -> None:
    """A task registered after stop()'s snapshot is still cancelled.

    Regression: stop() used to snapshot, clear, and gather once. A sync
    callback racing shutdown could register a Megolm recovery task after
    the snapshot — uncancelled, running against the closing client.
    """
    session = MatrixSession(_matrix_config(encryption_mode="e2ee_required"))
    session._crypto_enabled = True
    session._live_sync_started = True
    started = asyncio.Event()
    cancelled = asyncio.Event()
    stop_entered = asyncio.Event()

    async def _to_device(_request: object) -> object:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return SimpleNamespace()

    session._client = SimpleNamespace(
        device_id="DEVICE42",
        user_id="@relay:example.test",
        to_device=AsyncMock(side_effect=_to_device),
        olm=None,
        store=None,
        stop_sync_forever=lambda: stop_entered.set(),
    )
    event = SimpleNamespace(
        event_id="$late",
        session_id="late-session",
        as_key_request=MagicMock(return_value={"request": "missing-key"}),
    )

    # First recovery task exists before stop().
    await session._on_megolm_event(
        SimpleNamespace(room_id="!room:example.test"), event
    )
    await started.wait()

    async def _late_callback() -> None:
        # Simulates the race window: registers a second task after stop()
        # has snapshotted and cleared the registry but before the drain
        # finishes.
        await stop_entered.wait()
        late_event = SimpleNamespace(
            event_id="$later",
            session_id="later-session",
            as_key_request=MagicMock(return_value={"request": "missing-key"}),
        )
        await session._on_megolm_event(
            SimpleNamespace(room_id="!room:example.test"), late_event
        )

    late = asyncio.create_task(_late_callback())
    await session.stop(timeout=2)
    await late

    assert cancelled.is_set(), "in-flight recovery task was not cancelled"
    assert not session._room_key_request_tasks, (
        "recovery registry not fully drained at stop"
    )


async def test_no_new_recovery_task_is_created_while_stopping() -> None:
    """Task creation refuses once shutdown is signalled."""
    session = MatrixSession(_matrix_config(encryption_mode="e2ee_required"))
    session._crypto_enabled = True
    session._live_sync_started = True
    session._stop_requested = True
    to_device = AsyncMock()
    session._client = SimpleNamespace(
        device_id="DEVICE42",
        user_id="@relay:example.test",
        to_device=to_device,
        olm=None,
        store=None,
    )
    event = SimpleNamespace(
        event_id="$stopping",
        session_id="stopping-session",
        as_key_request=MagicMock(return_value={"request": "missing-key"}),
    )

    await session._on_megolm_event(
        SimpleNamespace(room_id="!room:example.test"), event
    )

    to_device.assert_not_awaited()
    event.as_key_request.assert_not_called()
    assert not session._room_key_request_tasks


async def test_megolm_callback_detaches_recovery_from_sync_processing() -> None:
    session = MatrixSession(_matrix_config(encryption_mode="e2ee_required"))
    session._crypto_enabled = True
    session._live_sync_started = True
    request_started = asyncio.Event()
    release_request = asyncio.Event()

    async def _to_device(_request: object) -> object:
        request_started.set()
        await release_request.wait()
        return SimpleNamespace()

    session._client = SimpleNamespace(
        device_id="DEVICE42",
        user_id="@relay:example.test",
        to_device=_to_device,
        olm=None,
        store=None,
    )
    event = SimpleNamespace(
        event_id="$encrypted",
        session_id="detached-session",
        as_key_request=MagicMock(return_value={"request": "missing-key"}),
    )

    await session._on_megolm_event(
        SimpleNamespace(room_id="!room:example.test"),
        event,
    )

    assert len(session._room_key_request_tasks) == 1
    await request_started.wait()
    task = next(iter(session._room_key_request_tasks.values()))
    assert task.done() is False

    release_request.set()
    await task


async def test_stop_cancels_detached_megolm_recovery() -> None:
    session = MatrixSession(_matrix_config(encryption_mode="e2ee_required"))
    session._crypto_enabled = True
    session._live_sync_started = True
    request_started = asyncio.Event()

    async def _to_device(_request: object) -> object:
        request_started.set()
        await asyncio.Event().wait()
        return SimpleNamespace()

    client = SimpleNamespace(
        device_id="DEVICE42",
        user_id="@relay:example.test",
        to_device=_to_device,
        olm=None,
        store=None,
        close=AsyncMock(),
        stop_sync_forever=MagicMock(),
    )
    session._client = client
    session._closed = False
    event = SimpleNamespace(
        event_id="$encrypted",
        session_id="shutdown-session",
        as_key_request=MagicMock(return_value={"request": "missing-key"}),
    )

    await session._on_megolm_event(
        SimpleNamespace(room_id="!room:example.test"),
        event,
    )
    await request_started.wait()
    task = next(iter(session._room_key_request_tasks.values()))

    await session.stop()

    assert task.cancelled() is True
    assert session._room_key_request_tasks == {}
    client.close.assert_awaited_once()


async def test_missing_room_key_request_stops_on_permanent_errcode() -> None:
    session = MatrixSession(_matrix_config(encryption_mode="e2ee_required"))
    session._crypto_enabled = True
    error_type = type("ToDeviceError", (), {})
    response = error_type()
    response.errcode = "M_FORBIDDEN"
    to_device = AsyncMock(return_value=response)
    session._client = SimpleNamespace(
        device_id="DEVICE42",
        user_id="@relay:example.test",
        to_device=to_device,
        olm=None,
        store=None,
    )
    event = SimpleNamespace(
        as_key_request=MagicMock(return_value={"request": "missing-key"}),
    )

    with patch("medre.adapters.matrix.session._sleep", new=AsyncMock()) as sleep:
        await session._request_missing_room_key(
            event=event,
            event_id="$encrypted",
            room_id="!room:example.test",
            session_id_tag="redacted",
        )

    to_device.assert_awaited_once()
    sleep.assert_not_awaited()
    diagnostics = session.diagnostics()
    assert diagnostics.megolm_recovery_attempts == 1
    assert diagnostics.megolm_recovery_successes == 0
    assert diagnostics.megolm_recovery_failures == 1


async def test_megolm_event_without_valid_room_id_skips_recovery_request() -> None:
    session = MatrixSession(_matrix_config(encryption_mode="e2ee_required"))
    session._crypto_enabled = True
    session._live_sync_started = True
    to_device = AsyncMock()
    session._client = SimpleNamespace(
        device_id="DEVICE42",
        user_id="@relay:example.test",
        to_device=to_device,
        olm=None,
        store=None,
    )
    event = SimpleNamespace(
        event_id="$encrypted",
        session_id="unknown-room-session",
        as_key_request=MagicMock(),
    )

    await session._on_megolm_event(None, event)

    assert session._room_key_request_tasks == {}
    event.as_key_request.assert_not_called()
    to_device.assert_not_awaited()


async def test_missing_room_key_helper_rejects_invalid_room_id() -> None:
    """The recovery helper never constructs a request for a placeholder room."""
    session = MatrixSession(_matrix_config(encryption_mode="e2ee_required"))
    session._crypto_enabled = True
    session._client = MagicMock()
    event = MagicMock()

    await session._request_missing_room_key(
        event=event,
        event_id="$encrypted",
        room_id="<unknown>",
        session_id_tag="unknown",
    )

    event.as_key_request.assert_not_called()


async def test_matrix_self_message_is_suppressed_before_canonical_publish() -> None:
    adapter = MatrixAdapter(_matrix_config())
    adapter.ctx = _context()
    adapter._started = True

    await adapter._on_room_message(
        _matrix_event(sender="@relay:example.test"),
    )

    adapter.ctx.publish_inbound.assert_not_awaited()
    assert adapter.diagnostics()["inbound_suppressed_self"] == 1


def test_matrix_reply_decode_preserves_native_target_reference() -> None:
    codec = MatrixCodec("matrix-reference", _matrix_config())
    event = codec.decode(
        _matrix_event(
            event_id="$reply",
            body="reply",
            content={
                "msgtype": "m.text",
                "body": "reply",
                "m.relates_to": {
                    "m.in_reply_to": {"event_id": "$original"},
                },
            },
        ),
        room_id="!room:example.test",
    )

    assert len(event.relations) == 1
    relation = event.relations[0]
    assert relation.relation_type == "reply"
    assert relation.target_native_ref is not None
    assert relation.target_native_ref.native_message_id == "$original"


async def test_matrix_reply_render_uses_native_matrix_event_id() -> None:
    codec = MatrixCodec("matrix-reference", _matrix_config())
    decoded = codec.decode(
        _matrix_event(
            event_id="$reply",
            body="reply",
            content={
                "msgtype": "m.text",
                "body": "reply",
                "m.relates_to": {
                    "m.in_reply_to": {"event_id": "$original"},
                },
            },
        ),
        room_id="!room:example.test",
    )
    event = CanonicalEvent(
        event_id=decoded.event_id,
        event_kind=decoded.event_kind,
        schema_version=decoded.schema_version,
        timestamp=decoded.timestamp,
        source_adapter=decoded.source_adapter,
        source_transport_id=decoded.source_transport_id,
        source_channel_id=decoded.source_channel_id,
        parent_event_id=decoded.parent_event_id,
        lineage=decoded.lineage,
        relations=decoded.relations,
        payload={"body": "outbound reply"},
        metadata=EventMetadata(),
    )

    rendered = await MatrixRenderer().render(
        event,
        RenderingContext(
            target_adapter="matrix-reference",
            target_platform="matrix",
            delivery_strategy="direct",
        ),
    )

    assert rendered.payload["m.relates_to"] == {
        "m.in_reply_to": {"event_id": "$original"}
    }


def test_encrypted_send_policy_remains_permissive_for_peer_devices() -> None:
    encrypted = MatrixAdapter(_matrix_config(encryption_mode="e2ee_required"))
    plaintext = MatrixAdapter(_matrix_config(encryption_mode="plaintext"))

    assert encrypted._should_ignore_unverified_devices() is True
    assert plaintext._should_ignore_unverified_devices() is False
