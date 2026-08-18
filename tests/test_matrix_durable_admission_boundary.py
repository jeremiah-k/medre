"""Matrix adapter durable-admission boundary tests."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from medre.adapters.matrix.adapter import MatrixAdapter
from medre.core.ingress import AdmissionResult
from tests.helpers.matrix_adapter import (
    make_adapter_context,
    make_fake_nio_event,
    make_fake_room,
    make_matrix_config,
    to_event_dict,
)


async def test_durable_storage_failure_propagates_to_nio_admission_owner() -> None:
    adapter = MatrixAdapter(make_matrix_config())
    _published, ctx = make_adapter_context()
    ctx.admit_inbound = AsyncMock(side_effect=RuntimeError("disk full"))
    adapter.ctx = ctx
    adapter._started = True

    with pytest.raises(RuntimeError, match="disk full"):
        await adapter._on_room_message(
            to_event_dict(make_fake_room(), make_fake_nio_event()), "live"
        )

    ctx.admit_inbound.assert_awaited_once()
    assert adapter.diagnostics()["inbound_published"] == 0


async def test_history_provenance_is_forwarded_to_durable_core() -> None:
    adapter = MatrixAdapter(make_matrix_config())
    _published, ctx = make_adapter_context()
    ctx.admit_inbound = AsyncMock(
        return_value=AdmissionResult(
            event_id="evt-existing",
            created=False,
            provenance="history",
            work_status="suppressed_history",
        )
    )
    adapter.ctx = ctx
    adapter._started = True

    await adapter._on_room_message(
        to_event_dict(make_fake_room(), make_fake_nio_event()), "history"
    )

    admitted_event, provenance = ctx.admit_inbound.await_args.args
    assert provenance == "history"
    assert admitted_event.source_native_ref is not None
    assert admitted_event.source_native_ref.native_message_id == "$evt-001"
    assert adapter.diagnostics()["inbound_published"] == 1
