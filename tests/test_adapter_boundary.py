"""Adapter boundary contract tests.

Documents the uniform contract expectations for BaseAdapter subclasses
without adding features.  These tests verify:

* ``start(ctx)`` requires an :class:`AdapterContext`.
* ``stop(timeout)`` accepts a numeric timeout.
* ``health_check()`` returns an :class:`AdapterInfo` with JSON-safe fields.
* ``deliver(result)`` accepts a :class:`RenderingResult` and returns
  ``AdapterDeliveryResult | None``.
* ``BaseAdapter`` is abstract and cannot be instantiated directly.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from datetime import datetime, timezone
from typing import Any

import pytest

from medre.adapters.base import (
    AdapterCapabilities,
    AdapterContext,
    AdapterDeliveryResult,
    AdapterInfo,
    AdapterRole,
    BaseAdapter,
)
from medre.core.rendering.renderer import RenderingResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_context(adapter_id: str = "test") -> AdapterContext:
    return AdapterContext(
        adapter_id=adapter_id,
        event_bus=None,
        publish_inbound=_async_noop,
        logger=logging.getLogger(f"test.{adapter_id}"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


async def _async_noop(event: object) -> None:
    pass


def _make_rendering_result() -> RenderingResult:
    return RenderingResult(
        event_id="evt-001",
        target_adapter="test",
        target_channel="ch-0",
        payload={"body": "hello"},
    )


# Minimal concrete adapter for contract verification.
class _StubAdapter(BaseAdapter):
    adapter_id = "stub"
    platform = "stub_platform"
    role = AdapterRole.TRANSPORT

    def __init__(self) -> None:
        self._started = False

    async def start(self, ctx: AdapterContext) -> None:
        self._started = True

    async def stop(self, timeout: float) -> None:
        self._started = False

    async def health_check(self) -> AdapterInfo:
        return AdapterInfo(
            adapter_id=self.adapter_id,
            platform=self.platform,
            role=self.role,
            version="0.0.1-test",
            capabilities=AdapterCapabilities(),
            health="healthy",
        )

    async def deliver(self, result: RenderingResult) -> AdapterDeliveryResult | None:
        return AdapterDeliveryResult(
            native_message_id="stub-msg-001",
            native_channel_id="stub-ch-001",
        )


# ===================================================================
# 1. BaseAdapter is abstract
# ===================================================================


class TestBaseAdapterAbstract:
    """BaseAdapter cannot be instantiated directly."""

    def test_cannot_instantiate_base_adapter(self) -> None:
        """BaseAdapter is ABC — instantiation raises TypeError."""
        with pytest.raises(TypeError):
            BaseAdapter()  # type: ignore[abstract]


# ===================================================================
# 2. start(ctx) contract
# ===================================================================


class TestStartContract:
    """start() accepts AdapterContext."""

    @pytest.mark.asyncio
    async def test_start_accepts_adapter_context(self) -> None:
        adapter = _StubAdapter()
        ctx = _make_context()
        await adapter.start(ctx)
        assert adapter._started

    def test_start_signature_requires_ctx(self) -> None:
        """start() has a parameter named 'ctx' typed as AdapterContext."""
        sig = inspect.signature(BaseAdapter.start)
        params = list(sig.parameters.keys())
        assert "ctx" in params
        ann = sig.parameters["ctx"].annotation
        # The annotation should reference AdapterContext (str or eval'd)
        assert "AdapterContext" in str(ann)


# ===================================================================
# 3. stop(timeout) contract
# ===================================================================


class TestStopContract:
    """stop() accepts a numeric timeout."""

    @pytest.mark.asyncio
    async def test_stop_accepts_timeout(self) -> None:
        adapter = _StubAdapter()
        await adapter.start(_make_context())
        await adapter.stop(timeout=5.0)
        assert not adapter._started

    def test_stop_signature_has_timeout(self) -> None:
        """stop() has a 'timeout' parameter."""
        sig = inspect.signature(BaseAdapter.stop)
        params = list(sig.parameters.keys())
        assert "timeout" in params


# ===================================================================
# 4. health_check / diagnostics contract
# ===================================================================


class TestHealthCheckContract:
    """health_check() returns AdapterInfo with JSON-safe fields."""

    @pytest.mark.asyncio
    async def test_health_check_returns_adapter_info(self) -> None:
        adapter = _StubAdapter()
        info = await adapter.health_check()
        assert isinstance(info, AdapterInfo)
        assert info.adapter_id == "stub"
        assert info.platform == "stub_platform"
        assert info.role == AdapterRole.TRANSPORT

    @pytest.mark.asyncio
    async def test_adapter_info_is_json_safe(self) -> None:
        """AdapterInfo fields round-trip through json.dumps."""
        adapter = _StubAdapter()
        info = await adapter.health_check()
        data = {
            "adapter_id": info.adapter_id,
            "platform": info.platform,
            "role": info.role.value,
            "version": info.version,
            "health": info.health,
            "capabilities": {
                "text": info.capabilities.text,
                "replies": info.capabilities.replies,
            },
        }
        result = json.dumps(data, sort_keys=True)
        assert '"adapter_id": "stub"' in result
        assert '"health": "healthy"' in result


# ===================================================================
# 5. deliver contract
# ===================================================================


class TestDeliverContract:
    """deliver() accepts RenderingResult, returns AdapterDeliveryResult | None."""

    @pytest.mark.asyncio
    async def test_deliver_accepts_rendering_result(self) -> None:
        adapter = _StubAdapter()
        result = _make_rendering_result()
        delivery = await adapter.deliver(result)
        assert isinstance(delivery, AdapterDeliveryResult)
        assert delivery.native_message_id == "stub-msg-001"
        assert delivery.native_channel_id == "stub-ch-001"

    @pytest.mark.asyncio
    async def test_deliver_can_return_none(self) -> None:
        """Adapters may return None when no native ID is available."""

        class _NoResultAdapter(_StubAdapter):
            async def deliver(self, result: RenderingResult) -> None:
                return None

        adapter = _NoResultAdapter()
        out = await adapter.deliver(_make_rendering_result())
        assert out is None

    def test_deliver_signature(self) -> None:
        sig = inspect.signature(BaseAdapter.deliver)
        params = list(sig.parameters.keys())
        assert "result" in params
