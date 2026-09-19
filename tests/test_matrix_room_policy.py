"""Encrypted-room-only policy (``require_encrypted_rooms=True``) regressions.

Consumer-visible guarantees exercised through the real
:class:`~medre.adapters.matrix.adapter.MatrixAdapter` and
:class:`~medre.adapters.matrix.session.MatrixSession` code, with only the
SDK client faked:

* Egress — a policy violation must fail closed as a permanent error
  *before* any SDK ``room_send`` call; an encrypted room with active
  crypto may send; crypto unavailability never downgrades to plaintext.
* Ingress — events from rooms not established as encrypted must never
  reach durable admission (including first-sync recovered/history
  backlog), and the drop must not stall the durable sync checkpoint.
* Flag false — ordinary adapter behavior is unchanged.

These tests are red on a baseline that ignores ``require_encrypted_rooms``
at runtime: plaintext/unknown rooms would send and be admitted.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.matrix.session import MatrixSession
from medre.config.adapters.matrix import MatrixConfig
from medre.core.contracts.adapter import AdapterPermanentError
from medre.core.ingress import AdmissionResult
from medre.core.rendering.renderer import RenderingResult
from tests.helpers.matrix_adapter import (
    make_adapter_context,
    make_fake_nio_event,
    wire_mock_session,
)

_ROOM = "!room:example.com"
_ENCRYPTED_ROOM = "!enc:example.com"


def _policy_config(**overrides: Any) -> MatrixConfig:
    """Build a config with the encrypted-room-only policy enabled."""
    defaults: dict[str, Any] = {
        "adapter_id": "matrix-1",
        "homeserver": "https://matrix.example.com",
        "user_id": "@bot:example.com",
        "access_token": "tok",
        "encryption_mode": "e2ee_optional",
        "require_encrypted_rooms": True,
    }
    defaults.update(overrides)
    return MatrixConfig(**defaults)


def _make_result(room: str = _ROOM, event_id: str = "evt-1") -> RenderingResult:
    return RenderingResult(
        event_id=event_id,
        target_adapter="matrix-1",
        target_channel=room,
        payload={"msgtype": "m.text", "body": "hello"},
    )


def _send_client() -> MagicMock:
    client = MagicMock(name="mock_async_client")
    client.room_send = AsyncMock(return_value=SimpleNamespace(event_id="$sent-1"))
    client.rooms = {}
    return client


def _wire_ingress(**config_overrides: Any) -> tuple[
    MatrixAdapter,
    MatrixSession,
    MagicMock,
    list[tuple[str, str]],
    list[tuple[str, str, str]],
]:
    config = _policy_config(**config_overrides)
    adapter = MatrixAdapter(config)
    _published, ctx = make_adapter_context()
    admitted: list[tuple[str, str]] = []

    async def _admit(event: Any, provenance: str) -> AdmissionResult:
        admitted.append((event.event_id, provenance))
        return AdmissionResult(
            event_id=event.event_id,
            created=True,
            provenance=provenance,  # type: ignore[arg-type]
            work_status="pending",
        )

    ctx.admit_inbound = _admit  # type: ignore[assignment]
    adapter.ctx = ctx
    adapter._started = True

    checkpoints: list[tuple[str, str, str]] = []

    async def _commit(stream: str, cursor: str, metadata: str) -> None:
        checkpoints.append((stream, cursor, metadata))

    async def _load(stream: str) -> None:
        return None

    client = MagicMock(name="mock_async_client")
    client.rooms = {}
    client.acknowledge_classic_sync = MagicMock()
    session = MatrixSession(
        config,
        message_callback=adapter._on_room_message,
        admission_callback=adapter._on_room_message,
        checkpoint_loader=_load,
        checkpoint_committer=_commit,
    )
    session._client = client
    session._live_sync_started = True
    adapter._session = session
    return adapter, session, client, admitted, checkpoints


async def test_plaintext_room_refused_without_sdk_send() -> None:
    """A room nio reports as unencrypted must not reach room_send."""
    adapter = MatrixAdapter(_policy_config())
    client = _send_client()
    client.rooms = {_ROOM: SimpleNamespace(room_id=_ROOM, encrypted=False)}
    session = wire_mock_session(adapter, client)
    session._crypto_enabled = True

    with pytest.raises(AdapterPermanentError, match="not established as encrypted"):
        await adapter.deliver(_make_result())

    assert client.room_send.await_count == 0


async def test_unknown_room_refused_without_sdk_send() -> None:
    """A room with no known encryption state must fail closed, not guess."""
    adapter = MatrixAdapter(_policy_config())
    client = _send_client()
    session = wire_mock_session(adapter, client)
    session._crypto_enabled = True
    # Room absent from client.rooms and absent from session tracking.

    with pytest.raises(AdapterPermanentError, match="not established as encrypted"):
        await adapter.deliver(_make_result())

    assert client.room_send.await_count == 0


async def test_encrypted_room_sends_with_active_crypto() -> None:
    """A room nio reports as encrypted sends when crypto is active."""
    adapter = MatrixAdapter(_policy_config())
    client = _send_client()
    client.rooms = {
        _ENCRYPTED_ROOM: SimpleNamespace(room_id=_ENCRYPTED_ROOM, encrypted=True)
    }
    session = wire_mock_session(adapter, client)
    session._crypto_enabled = True

    delivery = await adapter.deliver(_make_result(room=_ENCRYPTED_ROOM))

    assert delivery is not None
    assert delivery.native_message_id == "$sent-1"
    assert client.room_send.await_count == 1


async def test_crypto_unavailable_fails_closed_for_encrypted_room() -> None:
    """e2ee_optional crypto fallback must not weaken the policy.

    Even a room known to be encrypted is refused when crypto is not
    active — the adapter never silently sends plaintext instead.
    """
    adapter = MatrixAdapter(_policy_config())
    client = _send_client()
    session = wire_mock_session(adapter, client)
    session._crypto_enabled = False
    session._room_states[_ENCRYPTED_ROOM] = "encrypted"

    with pytest.raises(AdapterPermanentError, match="refusing to send"):
        await adapter.deliver(_make_result(room=_ENCRYPTED_ROOM))

    assert client.room_send.await_count == 0


async def test_flag_false_plaintext_room_still_sends() -> None:
    """Flag false keeps ordinary plaintext delivery behavior."""
    adapter = MatrixAdapter(_policy_config(require_encrypted_rooms=False))
    client = _send_client()
    client.rooms = {_ROOM: SimpleNamespace(room_id=_ROOM, encrypted=False)}
    session = wire_mock_session(adapter, client)
    session._crypto_enabled = False

    delivery = await adapter.deliver(_make_result())

    assert delivery is not None
    assert client.room_send.await_count == 1


async def test_plaintext_room_event_never_admitted() -> None:
    """An event from a room nio reports as unencrypted is dropped."""
    _adapter, session, _client, admitted, _checkpoints = _wire_ingress()
    room = SimpleNamespace(room_id=_ROOM, encrypted=False)

    # Bounded wait proves the drop path cannot hang the sync loop.
    await asyncio.wait_for(
        session._on_nio_admission(room, make_fake_nio_event(), "live"),
        timeout=2.0,
    )

    assert admitted == []
    assert session._recovered_event_count == 0


async def test_unknown_encryption_room_event_never_admitted() -> None:
    """An event with no room encryption evidence is dropped."""
    _adapter, session, _client, admitted, _checkpoints = _wire_ingress()
    room = SimpleNamespace(room_id=_ROOM)  # no ``encrypted`` attribute

    await asyncio.wait_for(
        session._on_nio_admission(room, make_fake_nio_event(), "live"),
        timeout=2.0,
    )

    assert admitted == []


async def test_first_sync_backlog_plaintext_never_admitted() -> None:
    """Recovered/history backlog before the first live sync is filtered too.

    The durable admission path bypasses generic startup suppression,
    so the policy itself must gate the pre-live window.
    """
    _adapter, session, _client, admitted, _checkpoints = _wire_ingress()
    session._live_sync_started = False
    room = SimpleNamespace(room_id=_ROOM, encrypted=False)

    await asyncio.wait_for(
        session._on_nio_admission(room, make_fake_nio_event(), "history"),
        timeout=2.0,
    )

    assert admitted == []
    assert session._history_event_count == 1


async def test_encrypted_room_event_admitted() -> None:
    """Events from rooms nio reports as encrypted are admitted."""
    adapter, session, _client, admitted, _checkpoints = _wire_ingress()
    room = SimpleNamespace(room_id=_ENCRYPTED_ROOM, encrypted=True)

    await asyncio.wait_for(
        session._on_nio_admission(room, make_fake_nio_event(), "live"),
        timeout=2.0,
    )

    assert len(admitted) == 1
    assert admitted[0][1] == "live"
    assert adapter.diagnostics()["inbound_published"] == 1


async def test_session_tracked_encrypted_room_event_admitted() -> None:
    """Session m.room.encryption tracking also establishes the room.

    Covers rooms whose nio room object lacks the ``encrypted`` flag
    but which the session tracked via RoomEncryptionEvent handling.
    """
    _adapter, session, _client, admitted, _checkpoints = _wire_ingress()
    session._room_states[_ROOM] = "encrypted"
    room = SimpleNamespace(room_id=_ROOM)

    await asyncio.wait_for(
        session._on_nio_admission(room, make_fake_nio_event(), "live"),
        timeout=2.0,
    )

    assert len(admitted) == 1


async def test_flag_false_plaintext_event_still_admitted() -> None:
    """Flag false keeps ordinary durable admission behavior."""
    _adapter, session, _client, admitted, _checkpoints = _wire_ingress(
        require_encrypted_rooms=False
    )
    room = SimpleNamespace(room_id=_ROOM, encrypted=False)

    await asyncio.wait_for(
        session._on_nio_admission(room, make_fake_nio_event(), "live"),
        timeout=2.0,
    )

    assert len(admitted) == 1


async def test_policy_drop_does_not_stall_sync_checkpoint() -> None:
    """A policy-dropped event is consumed and the cursor still advances.

    The drop must be a plain return — never a deferral signal — so
    nio acknowledges the sync response and the committed checkpoint
    progresses after policy drops.
    """
    _adapter, session, client, admitted, checkpoints = _wire_ingress()
    room = SimpleNamespace(room_id=_ROOM, encrypted=False)

    await asyncio.wait_for(
        session._on_nio_admission(room, make_fake_nio_event(), "live"),
        timeout=2.0,
    )
    assert admitted == []

    response = SimpleNamespace(next_batch="batch-42")
    await asyncio.wait_for(session._on_sync_response(response), timeout=2.0)

    assert len(checkpoints) == 1
    stream, cursor, _metadata = checkpoints[0]
    assert stream == "classic_sync"
    assert cursor == "batch-42"
    client.acknowledge_classic_sync.assert_called_once_with("batch-42")
    assert session._committed_sync_token == "batch-42"
