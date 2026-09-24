"""Replay-origin context in event recovery runbooks."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from medre.cli.recover_commands import _build_event_recovery_runbook


class _FakeEvent:
    def __init__(self) -> None:
        self.event_id = "evt-1"
        self.event_kind = "message.created"
        self.source_adapter = "test_adapter"
        self.source_transport_id = "test-transport"
        self.source_channel_id = "ch-0"
        self.timestamp = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        self.schema_version = 1
        self.parent_event_id = None
        self.root_event_id = None
        self.conversation_id = None
        self.lineage: tuple[object, ...] = ()
        self.relations: tuple[object, ...] = ()
        self.payload: dict[str, object] = {"text": "test"}
        self.metadata: object = None


class _FakeReceipt:
    def __init__(
        self,
        *,
        receipt_id: str = "rcpt-1",
        status: str = "failed",
        source: str = "live",
        replay_run_id: str | None = None,
        attempt_number: int = 1,
        outbox_id: str | None = None,
        error: str | None = "permission denied",
    ) -> None:
        self.receipt_id = receipt_id
        self.event_id = "evt-1"
        self.delivery_plan_id = "plan-1"
        self.target_adapter = "adapter_a"
        self.target_channel = None
        self.route_id = "route-1"
        self.status = status
        self.attempt_number = attempt_number
        self.error = error
        self.failure_kind = None
        self.adapter_message_id = None
        self.source = source
        self.replay_run_id = replay_run_id
        self.outbox_id = outbox_id
        self.sequence = 1
        self.created_at = datetime(2026, 1, 15, 12, 0, 1, tzinfo=timezone.utc)


class _FakeOutbox:
    def __init__(
        self,
        *,
        outbox_id: str = "ob-1",
        status: str = "pending",
        attempt_number: int = 1,
        active_attempt: int | None = None,
        receipt_id: str | None = None,
        replay_run_id: str | None = None,
    ) -> None:
        self.outbox_id = outbox_id
        self.event_id = "evt-1"
        self.delivery_plan_id = "plan-1"
        self.target_adapter = "adapter_a"
        self.target_channel = None
        self.status = status
        self.attempt_number = attempt_number
        self.active_attempt = active_attempt
        self.receipt_id = receipt_id
        self.replay_run_id = replay_run_id
        self.created_at = "2026-01-15T12:00:00+00:00"
        self.updated_at = self.created_at


def _storage(
    *,
    receipts: list[_FakeReceipt] | None = None,
    outbox_items: list[_FakeOutbox] | None = None,
) -> AsyncMock:
    storage = AsyncMock()
    storage.get = AsyncMock(return_value=_FakeEvent())
    storage.list_receipts_for_event = AsyncMock(return_value=receipts or [])
    storage.list_native_refs_for_event = AsyncMock(return_value=[])
    storage.list_delivery_observations_for_event = AsyncMock(return_value=[])
    storage.list_relations = AsyncMock(return_value=[])
    storage.list_outbox_items_for_event = AsyncMock(return_value=outbox_items or [])
    return storage


@pytest.mark.asyncio
async def test_replay_context_included_from_receipt() -> None:
    runbook = await _build_event_recovery_runbook(
        _storage(receipts=[_FakeReceipt(source="replay", replay_run_id="run-42")]),
        "evt-1",
        storage_path="/nonexistent",
    )
    assert runbook is not None
    assert runbook["replay_context"] == [
        {
            "replay_run_id": "run-42",
            "dispatch_sources": ["replay"],
            "outbox_count": 0,
            "outbox_statuses": [],
        }
    ]


@pytest.mark.asyncio
async def test_replay_context_tracks_retry_dispatch_origin() -> None:
    runbook = await _build_event_recovery_runbook(
        _storage(
            receipts=[
                _FakeReceipt(
                    source="retry",
                    replay_run_id="run-retry-origin",
                    error="retry failed",
                )
            ]
        ),
        "evt-1",
        storage_path="/nonexistent",
    )
    assert runbook is not None
    assert runbook["replay_context"] == [
        {
            "replay_run_id": "run-retry-origin",
            "dispatch_sources": ["retry"],
            "outbox_count": 0,
            "outbox_statuses": [],
        }
    ]
    assert runbook["failed_targets"][0]["source"] == "retry"
    assert runbook["failed_targets"][0]["replay_run_id"] == "run-retry-origin"


@pytest.mark.asyncio
async def test_admitted_replay_run_is_visible_before_first_receipt() -> None:
    runbook = await _build_event_recovery_runbook(
        _storage(
            outbox_items=[_FakeOutbox(replay_run_id="run-admitted", status="pending")]
        ),
        "evt-1",
        storage_path="/nonexistent",
    )
    assert runbook is not None
    assert runbook["replay_context"] == [
        {
            "replay_run_id": "run-admitted",
            "dispatch_sources": [],
            "outbox_count": 1,
            "outbox_statuses": ["pending"],
        }
    ]


@pytest.mark.asyncio
async def test_fresh_replay_generation_moves_older_failure_to_history() -> None:
    runbook = await _build_event_recovery_runbook(
        _storage(
            receipts=[
                _FakeReceipt(
                    receipt_id="rcpt-old-failed",
                    outbox_id="ob-old",
                    attempt_number=1,
                    error="old failure",
                )
            ],
            outbox_items=[
                _FakeOutbox(
                    outbox_id="ob-old",
                    status="retry_wait",
                    attempt_number=1,
                    receipt_id="rcpt-old-failed",
                ),
                _FakeOutbox(
                    outbox_id="ob-replay",
                    status="pending",
                    attempt_number=2,
                    replay_run_id="run-new",
                ),
            ],
        ),
        "evt-1",
        storage_path="/nonexistent",
    )
    assert runbook is not None
    assert runbook["failed_targets"] == []
    assert len(runbook["historical_failures"]) == 1
    historical = runbook["historical_failures"][0]
    assert historical["receipt_id"] == "rcpt-old-failed"
    assert historical["superseded_by"] == {
        "outbox_id": "ob-replay",
        "status": "pending",
        "attempt_number": 2,
    }
    assert runbook["replay_context"] == [
        {
            "replay_run_id": "run-new",
            "dispatch_sources": [],
            "outbox_count": 1,
            "outbox_statuses": ["pending"],
        }
    ]
