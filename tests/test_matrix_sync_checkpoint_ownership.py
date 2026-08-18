"""Matrix Classic Sync checkpoint-ownership tests.

These tests exercise MEDRE's side of the mindroom-nio 0.40 application-owned
checkpoint contract without requiring a homeserver.
"""

from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from medre.adapters.matrix.session import MatrixSession
from medre.core.ingress.types import AdapterCheckpoint
from tests.helpers.matrix_session import make_matrix_config


def _durable_session(**overrides: object) -> MatrixSession:
    async def admit(_event: dict[str, object], _provenance: str) -> None:
        return None

    async def load(_stream: str) -> AdapterCheckpoint | None:
        return None

    async def commit(_stream: str, _cursor: str, _metadata: str) -> None:
        return None

    params: dict[str, object] = {
        "admission_callback": admit,
        "checkpoint_loader": load,
        "checkpoint_committer": commit,
    }
    params.update(overrides)
    return MatrixSession(make_matrix_config(), **params)


def test_durable_client_config_selects_application_owned_classic_state() -> None:
    captured: dict[str, object] = {}

    def config_factory(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    session = _durable_session()
    nio = SimpleNamespace(AsyncClientConfig=config_factory)
    session._build_client_config(nio, encryption_enabled=False)

    assert captured == {
        "encryption_enabled": False,
        "max_timeouts": 3,
        "backfill_limited_timelines": True,
        "store_sync_tokens": False,
        "backfill_persist_recovery": False,
    }


async def test_admission_failure_rejects_event_for_nio_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CallbackNotAcceptedError(Exception):
        pass

    async def reject(_event: dict[str, object], _provenance: str) -> None:
        raise RuntimeError("sqlite write failed")

    session = _durable_session(admission_callback=reject)
    fake_nio = SimpleNamespace(CallbackNotAcceptedError=CallbackNotAcceptedError)
    monkeypatch.setitem(sys.modules, "nio", fake_nio)
    room = SimpleNamespace(room_id="!room:example.org")
    event = SimpleNamespace(
        sender="@alice:example.org",
        event_id="$event",
        body="hello",
        source={
            "event_id": "$event",
            "sender": "@alice:example.org",
            "type": "m.room.message",
            "content": {"msgtype": "m.text", "body": "hello"},
        },
    )

    with pytest.raises(CallbackNotAcceptedError, match="durable ingress"):
        await session._on_nio_admission(
            room, event, SimpleNamespace(value="recovered")
        )
    assert session._recovered_event_count == 1


async def test_admission_failure_preserves_original_error_when_rejection_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = RuntimeError("sqlite write failed")

    async def reject(_event: dict[str, object], _provenance: str) -> None:
        raise original

    session = _durable_session(admission_callback=reject)
    monkeypatch.setitem(sys.modules, "nio", SimpleNamespace())
    monkeypatch.delitem(sys.modules, "nio.exceptions", raising=False)

    room = SimpleNamespace(room_id="!room:example.org")
    event = SimpleNamespace(
        sender="@alice:example.org",
        event_id="$event",
        body="hello",
        source={
            "event_id": "$event",
            "sender": "@alice:example.org",
            "type": "m.room.message",
            "content": {"msgtype": "m.text", "body": "hello"},
        },
    )

    with pytest.raises(RuntimeError, match="sqlite write failed") as caught:
        await session._on_nio_admission(
            room, event, SimpleNamespace(value="live")
        )

    assert caught.value is original


async def test_sync_response_commits_checkpoint_before_nio_ack() -> None:
    calls: list[tuple[str, str]] = []

    async def commit(stream: str, cursor: str, _metadata: str) -> None:
        calls.append(("commit", f"{stream}:{cursor}"))

    session = _durable_session(checkpoint_committer=commit)
    client = MagicMock()
    client.acknowledge_classic_sync.side_effect = lambda cursor: calls.append(
        ("ack", cursor)
    )
    session._client = client
    response = SimpleNamespace(next_batch="s42", abandoned_rooms={})

    await session._on_sync_response(response)

    assert calls == [("commit", "classic_sync:s42"), ("ack", "s42")]
    assert session._committed_sync_token == "s42"
    assert session.diagnostics().committed_checkpoint_present is True


async def test_checkpoint_failure_does_not_acknowledge_nio() -> None:
    async def commit(_stream: str, _cursor: str, _metadata: str) -> None:
        raise OSError("disk full")

    session = _durable_session(checkpoint_committer=commit)
    client = MagicMock()
    session._client = client

    with pytest.raises(OSError, match="disk full"):
        await session._on_sync_response(
            SimpleNamespace(next_batch="s43", abandoned_rooms={})
        )

    client.acknowledge_classic_sync.assert_not_called()
    assert session._committed_sync_token is None


async def test_stopping_session_does_not_commit_or_ack_sync_response() -> None:
    commits = AsyncMock()
    session = _durable_session(checkpoint_committer=commits)
    client = MagicMock()
    session._client = client
    session._stop_requested = True

    await session._on_sync_response(
        SimpleNamespace(next_batch="s-stop", abandoned_rooms={})
    )

    commits.assert_not_awaited()
    client.acknowledge_classic_sync.assert_not_called()


async def test_load_checkpoint_restores_committed_cursor_and_clears_stale_recovery() -> None:
    checkpoint = AdapterCheckpoint(
        adapter_id="matrix-test",
        stream="classic_sync",
        cursor="s41",
        metadata_json='{"abandoned_rooms":{"!lost:example.org":["fetch_failed"]}}',
        updated_at="2026-08-18T00:00:00Z",
    )

    async def load(stream: str) -> AdapterCheckpoint | None:
        assert stream == "classic_sync"
        return checkpoint

    session = _durable_session(checkpoint_loader=load)
    client = MagicMock()
    client.store = object()
    client.clear_persisted_sync_recovery = MagicMock()
    session._client = client

    await session._load_classic_checkpoint()

    assert session._committed_sync_token == "s41"
    assert session._recovery_abandoned_rooms == {
        "!lost:example.org": ("fetch_failed",)
    }
    assert json.loads(session._recovery_last_abandonment or "{}") == {
        "causes": {"fetch_failed": 1},
        "room_count": 1,
    }
    client.clear_persisted_sync_recovery.assert_called_once_with()


async def test_sync_failure_resets_uncommitted_state_before_retry() -> None:
    session = _durable_session()
    session._committed_sync_token = "committed"
    session._config = make_matrix_config()
    client = MagicMock()
    client.has_uncommitted_classic_sync_state = True
    client.reset_classic_sync_state = AsyncMock()
    client.next_batch = "staged"
    attempts = 0

    async def sync_forever(**_kwargs: object) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("lost connection")
        session._stop_requested = True

    client.sync_forever = sync_forever
    session._client = client

    original_sleep = asyncio.sleep

    async def no_delay(_delay: float) -> None:
        await original_sleep(0)

    from unittest.mock import patch

    with patch("asyncio.sleep", side_effect=no_delay):
        await session._sync_with_reconnect()

    client.reset_classic_sync_state.assert_awaited_once_with()
    assert client.next_batch == "committed"
    assert attempts == 2


async def test_recovery_abandonment_is_persisted_before_cursor_advances() -> None:
    calls: list[str] = []
    committed_metadata = ""

    async def commit(_stream: str, _cursor: str, metadata: str) -> None:
        nonlocal committed_metadata
        committed_metadata = metadata
        calls.append("commit")

    session = _durable_session(checkpoint_committer=commit)
    client = MagicMock()
    client.acknowledge_classic_sync.side_effect = lambda _cursor: calls.append("ack")
    client.acknowledge_unrecovered_rooms.side_effect = lambda _rooms: calls.append(
        "settle"
    )
    session._client = client
    response = SimpleNamespace(
        next_batch="s-loss",
        abandoned_rooms={
            "!lost:example.org": [SimpleNamespace(value="fetch_failed")]
        },
    )

    await session._on_sync_response(response)

    assert calls == ["commit", "ack", "settle"]
    assert '"!lost:example.org":["fetch_failed"]' in committed_metadata
    diag = session.diagnostics()
    assert diag.recovery_abandoned_room_count == 1
    assert diag.recovery_last_abandonment == (
        '{"causes":{"fetch_failed":1},"room_count":1}'
    )
    assert "!lost:example.org" not in diag.recovery_last_abandonment

    await session._on_sync_response(
        SimpleNamespace(next_batch="s-clean", abandoned_rooms={})
    )
    assert '"!lost:example.org":["fetch_failed"]' in committed_metadata
    assert session.diagnostics().recovery_abandoned_room_count == 1
