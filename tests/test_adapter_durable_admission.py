"""Durable ingress admission boundary at the adapter contract.

The ``AdapterContract.admit_inbound`` default wires durable admission
through the runtime-provided context callback:

- Without a wired ``ctx.admit_inbound``, admission raises instead of
  silently dropping the recovered event.
- With the callback wired, admission delegates and returns the
  callback's result verbatim.

Split from ``test_adapter_boundary.py`` (which exceeds the 1,200-line
new-test threshold) by behavioral domain.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from medre.core.contracts.adapter import (
    AdapterCapabilities,
    AdapterContext,
    AdapterContract,
    AdapterInfo,
    AdapterRole,
)
from medre.core.rendering.renderer import RenderingResult
from tests.helpers.pipeline import make_event


class _StubAdapter(AdapterContract):
    adapter_id = "stub"
    platform = "stub_platform"
    role = AdapterRole.TRANSPORT

    async def start(self, ctx: AdapterContext) -> None:
        pass

    async def stop(self, timeout: float) -> None:
        pass

    async def health_check(self) -> AdapterInfo:
        return AdapterInfo(
            adapter_id=self.adapter_id,
            platform=self.platform,
            role=self.role,
            version="0.0.1-test",
            capabilities=AdapterCapabilities(),
            health="healthy",
        )

    async def deliver(self, result: RenderingResult) -> object:
        return None


def _make_context() -> AdapterContext:
    import asyncio

    return AdapterContext(
        adapter_id="test",
        event_bus=None,
        publish_inbound=_async_noop,
        logger=logging.getLogger("test.durable-admission"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


async def _async_noop(event: object) -> None:
    pass


async def test_durable_admission_requires_wired_context() -> None:
    adapter = _StubAdapter()
    event = make_event()

    with pytest.raises(RuntimeError, match="durable ingress admission is not wired"):
        await adapter.admit_inbound(event, "live")


async def test_durable_admission_delegates_to_context() -> None:
    adapter = _StubAdapter()
    event = make_event()
    result = object()
    ctx = _make_context()
    ctx.admit_inbound = AsyncMock(return_value=result)
    adapter.ctx = ctx

    assert await adapter.admit_inbound(event, "recovered") is result
    ctx.admit_inbound.assert_awaited_once_with(event, "recovered")
