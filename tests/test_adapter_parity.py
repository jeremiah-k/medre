"""Parity tests: verify fake and real adapters share consistent contracts.

These tests verify that:
- Both fake and real adapters raise ``AdapterPermanentError`` for invalid
  input types in ``deliver()`` (not ``TypeError``).
- Both fake and real adapters raise ``AdapterSendError(transient=True)`` for
  transient failure conditions.
- ``AdapterDeliveryResult`` fields are consistent between fake and real
  adapters for each transport pair.
- ``start()``, ``stop()``, ``health_check()``, ``diagnostics()`` have
  consistent signatures across each pair.

No live network access required.  Real adapters use fake sessions or
monkeypatching to avoid SDK dependencies.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from medre.adapters.fake_lxmf import FakeLxmfAdapter
from medre.adapters.fake_matrix import FakeMatrixAdapter
from medre.adapters.fake_meshcore import FakeMeshCoreAdapter
from medre.adapters.fake_meshtastic import FakeMeshtasticAdapter
from medre.config.adapters.lxmf import LxmfConfig
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.config.adapters.meshtastic import MeshtasticConfig
from medre.core.contracts.adapter import (
    AdapterContext,
    AdapterDeliveryResult,
    AdapterInfo,
    AdapterPermanentError,
    AdapterSendError,
)
from medre.core.rendering.renderer import RenderingResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_COLLECTED: list[Any] = []


async def _collect_inbound(event: Any) -> None:
    _COLLECTED.append(event)


def _make_ctx(adapter_id: str = "test_adapter") -> AdapterContext:
    return AdapterContext(
        adapter_id=adapter_id,
        event_bus=None,
        publish_inbound=_collect_inbound,
        logger=logging.getLogger(f"test.parity.{adapter_id}"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
    )


def _make_rendering_result(
    event_id: str = "evt-001",
    target_adapter: str = "test",
    target_channel: str | None = "test_channel",
    payload: dict[str, Any] | None = None,
) -> RenderingResult:
    return RenderingResult(
        event_id=event_id,
        target_adapter=target_adapter,
        target_channel=target_channel,
        payload=payload or {"text": "hello"},
    )


# ---------------------------------------------------------------------------
# 1. Signature parity
# ---------------------------------------------------------------------------


class TestSignatureParity:
    """start/stop/health_check/diagnostics/deliver signatures match per pair."""

    @staticmethod
    def _sig(fn: Any) -> set[str]:
        return set(inspect.signature(fn).parameters)

    # -- Matrix pair --
    def test_matrix_start_sig(self) -> None:
        from medre.adapters.matrix.adapter import MatrixAdapter

        fake_sig = self._sig(FakeMatrixAdapter.start)
        real_sig = self._sig(MatrixAdapter.start)
        assert fake_sig == real_sig

    def test_matrix_stop_sig(self) -> None:
        from medre.adapters.matrix.adapter import MatrixAdapter

        fake_sig = self._sig(FakeMatrixAdapter.stop)
        real_sig = self._sig(MatrixAdapter.stop)
        assert fake_sig == real_sig

    def test_matrix_deliver_sig(self) -> None:
        from medre.adapters.matrix.adapter import MatrixAdapter

        fake_sig = self._sig(FakeMatrixAdapter.deliver)
        real_sig = self._sig(MatrixAdapter.deliver)
        assert fake_sig == real_sig

    # -- MeshCore pair --
    def test_meshcore_start_sig(self) -> None:
        from medre.adapters.meshcore.adapter import MeshCoreAdapter

        fake_sig = self._sig(FakeMeshCoreAdapter.start)
        real_sig = self._sig(MeshCoreAdapter.start)
        assert fake_sig == real_sig

    def test_meshcore_stop_sig(self) -> None:
        from medre.adapters.meshcore.adapter import MeshCoreAdapter

        fake_sig = self._sig(FakeMeshCoreAdapter.stop)
        real_sig = self._sig(MeshCoreAdapter.stop)
        assert fake_sig == real_sig

    def test_meshcore_deliver_sig(self) -> None:
        from medre.adapters.meshcore.adapter import MeshCoreAdapter

        fake_sig = self._sig(FakeMeshCoreAdapter.deliver)
        real_sig = self._sig(MeshCoreAdapter.deliver)
        assert fake_sig == real_sig

    # -- Meshtastic pair --
    def test_meshtastic_start_sig(self) -> None:
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        fake_sig = self._sig(FakeMeshtasticAdapter.start)
        real_sig = self._sig(MeshtasticAdapter.start)
        assert fake_sig == real_sig

    def test_meshtastic_stop_sig(self) -> None:
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        fake_sig = self._sig(FakeMeshtasticAdapter.stop)
        real_sig = self._sig(MeshtasticAdapter.stop)
        assert fake_sig == real_sig

    def test_meshtastic_deliver_sig(self) -> None:
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        fake_sig = self._sig(FakeMeshtasticAdapter.deliver)
        real_sig = self._sig(MeshtasticAdapter.deliver)
        assert fake_sig == real_sig

    # -- LXMF pair --
    def test_lxmf_start_sig(self) -> None:
        from medre.adapters.lxmf.adapter import LxmfAdapter

        fake_sig = self._sig(FakeLxmfAdapter.start)
        real_sig = self._sig(LxmfAdapter.start)
        assert fake_sig == real_sig

    def test_lxmf_stop_sig(self) -> None:
        from medre.adapters.lxmf.adapter import LxmfAdapter

        fake_sig = self._sig(FakeLxmfAdapter.stop)
        real_sig = self._sig(LxmfAdapter.stop)
        assert fake_sig == real_sig

    def test_lxmf_deliver_sig(self) -> None:
        from medre.adapters.lxmf.adapter import LxmfAdapter

        fake_sig = self._sig(FakeLxmfAdapter.deliver)
        real_sig = self._sig(LxmfAdapter.deliver)
        assert fake_sig == real_sig

    # -- health_check signature parity --

    def test_matrix_health_check_sig(self) -> None:
        from medre.adapters.matrix.adapter import MatrixAdapter

        fake_sig = self._sig(FakeMatrixAdapter.health_check)
        real_sig = self._sig(MatrixAdapter.health_check)
        assert fake_sig == real_sig

    def test_meshcore_health_check_sig(self) -> None:
        from medre.adapters.meshcore.adapter import MeshCoreAdapter

        fake_sig = self._sig(FakeMeshCoreAdapter.health_check)
        real_sig = self._sig(MeshCoreAdapter.health_check)
        assert fake_sig == real_sig

    def test_meshtastic_health_check_sig(self) -> None:
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        fake_sig = self._sig(FakeMeshtasticAdapter.health_check)
        real_sig = self._sig(MeshtasticAdapter.health_check)
        assert fake_sig == real_sig

    def test_lxmf_health_check_sig(self) -> None:
        from medre.adapters.lxmf.adapter import LxmfAdapter

        fake_sig = self._sig(FakeLxmfAdapter.health_check)
        real_sig = self._sig(LxmfAdapter.health_check)
        assert fake_sig == real_sig


# ---------------------------------------------------------------------------
# 2. Fake adapter deliver() raises AdapterPermanentError for bad input
# ---------------------------------------------------------------------------


class TestFakeAdapterTypeError:
    """All fake adapters raise AdapterPermanentError (not TypeError) for
    non-RenderingResult input to deliver()."""

    @pytest.fixture
    def fake_matrix(self) -> FakeMatrixAdapter:
        return FakeMatrixAdapter("parity_matrix")

    @pytest.fixture
    def fake_meshcore(self) -> FakeMeshCoreAdapter:
        return FakeMeshCoreAdapter()

    @pytest.fixture
    def fake_meshtastic(self) -> FakeMeshtasticAdapter:
        return FakeMeshtasticAdapter()

    @pytest.fixture
    def fake_lxmf(self) -> FakeLxmfAdapter:
        return FakeLxmfAdapter()

    @pytest.mark.asyncio
    async def test_matrix_raises_permanent(
        self, fake_matrix: FakeMatrixAdapter
    ) -> None:
        with pytest.raises(AdapterPermanentError, match="RenderingResult only"):
            await fake_matrix.deliver("not_a_result")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_meshcore_raises_permanent(
        self, fake_meshcore: FakeMeshCoreAdapter
    ) -> None:
        with pytest.raises(AdapterPermanentError, match="RenderingResult only"):
            await fake_meshcore.deliver("not_a_result")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_meshtastic_raises_permanent(
        self, fake_meshtastic: FakeMeshtasticAdapter
    ) -> None:
        with pytest.raises(AdapterPermanentError, match="RenderingResult only"):
            await fake_meshtastic.deliver("not_a_result")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_lxmf_raises_permanent(self, fake_lxmf: FakeLxmfAdapter) -> None:
        with pytest.raises(AdapterPermanentError, match="RenderingResult only"):
            await fake_lxmf.deliver("not_a_result")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 3. Fake adapter deliver() raises AdapterSendError for simulated failures
# ---------------------------------------------------------------------------


class TestFakeAdapterTransientError:
    """All fake adapters with set_deliver_failure raise AdapterSendError
    with transient=True."""

    @pytest.mark.asyncio
    async def test_meshcore_transient_failure(self) -> None:
        adapter = FakeMeshCoreAdapter()
        adapter.set_deliver_failure(True)
        result = _make_rendering_result()
        with pytest.raises(AdapterSendError) as exc_info:
            await adapter.deliver(result)
        assert exc_info.value.transient is True

    @pytest.mark.asyncio
    async def test_meshtastic_transient_failure(self) -> None:
        adapter = FakeMeshtasticAdapter()
        adapter.set_deliver_failure(True)
        result = _make_rendering_result()
        with pytest.raises(AdapterSendError) as exc_info:
            await adapter.deliver(result)
        assert exc_info.value.transient is True

    @pytest.mark.asyncio
    async def test_lxmf_transient_failure(self) -> None:
        adapter = FakeLxmfAdapter()
        adapter.set_deliver_failure(True)
        result = _make_rendering_result()
        with pytest.raises(AdapterSendError) as exc_info:
            await adapter.deliver(result)
        assert exc_info.value.transient is True


# ---------------------------------------------------------------------------
# 4. Fake adapter AdapterDeliveryResult field parity
# ---------------------------------------------------------------------------


class TestFakeAdapterDeliveryResultFields:
    """Verify AdapterDeliveryResult fields from fake adapters."""

    @pytest.mark.asyncio
    async def test_matrix_returns_delivery_result(self) -> None:
        adapter = FakeMatrixAdapter("p_matrix", channel="!room:server")
        await adapter.start(_make_ctx("p_matrix"))
        result = _make_rendering_result(target_channel="!room:server")
        dr = await adapter.deliver(result)
        assert dr is not None
        assert isinstance(dr, AdapterDeliveryResult)
        assert isinstance(dr.native_message_id, str)
        assert dr.native_message_id.startswith("$fake_")
        assert dr.native_channel_id == "!room:server"

    @pytest.mark.asyncio
    async def test_meshcore_returns_delivery_result(self) -> None:
        adapter = FakeMeshCoreAdapter()
        await adapter.start(_make_ctx("p_meshcore"))
        result = _make_rendering_result(payload={"text": "hello", "channel_index": 1})
        dr = await adapter.deliver(result)
        assert dr is not None
        assert isinstance(dr, AdapterDeliveryResult)
        assert isinstance(dr.native_message_id, str)
        assert dr.native_channel_id == "1"
        # delivery_note must be a top-level field, not embedded in metadata
        assert isinstance(dr.delivery_note, str)
        assert dr.delivery_note != ""
        # metadata should contain delivery_status, NOT delivery_note
        assert "delivery_status" in dr.metadata
        assert "delivery_note" not in dr.metadata

    @pytest.mark.asyncio
    async def test_meshtastic_returns_delivery_result(self) -> None:
        adapter = FakeMeshtasticAdapter()
        await adapter.start(_make_ctx("p_meshtastic"))
        result = _make_rendering_result(payload={"text": "hello", "channel_index": 2})
        dr = await adapter.deliver(result)
        assert dr is not None
        assert isinstance(dr, AdapterDeliveryResult)
        assert isinstance(dr.native_message_id, str)
        assert dr.native_channel_id == "2"

    @pytest.mark.asyncio
    async def test_lxmf_returns_delivery_result(self) -> None:
        adapter = FakeLxmfAdapter()
        await adapter.start(_make_ctx("p_lxmf"))
        result = _make_rendering_result(
            payload={
                "content": "hello",
                "title": "",
                "fields": None,
                "destination_hash": "",
            }
        )
        dr = await adapter.deliver(result)
        assert dr is not None
        assert isinstance(dr, AdapterDeliveryResult)
        assert isinstance(dr.native_message_id, str)
        assert "lxmf" in dr.metadata
        assert "delivery_state" in dr.metadata["lxmf"]


# ---------------------------------------------------------------------------
# 5. Fake adapter lifecycle parity: health_check / diagnostics
# ---------------------------------------------------------------------------


class TestFakeAdapterLifecycleParity:
    """health_check returns AdapterInfo; diagnostics returns dict."""

    @pytest.mark.asyncio
    async def test_matrix_health_and_diag(self) -> None:
        adapter = FakeMatrixAdapter("p_matrix")
        await adapter.start(_make_ctx("p_matrix"))

        info = await adapter.health_check()
        assert isinstance(info, AdapterInfo)
        assert info.adapter_id == "p_matrix"
        assert info.platform == "matrix"
        assert info.health == "healthy"

        diag = adapter.diagnostics()
        assert isinstance(diag, dict)
        assert diag["adapter_id"] == "p_matrix"
        assert diag["platform"] == "matrix"
        assert diag["started"] is True
        assert diag["mode"] == "fake"

    @pytest.mark.asyncio
    async def test_meshcore_health_and_diag(self) -> None:
        adapter = FakeMeshCoreAdapter()
        await adapter.start(_make_ctx("p_meshcore"))

        info = await adapter.health_check()
        assert isinstance(info, AdapterInfo)
        assert info.platform == "meshcore"
        assert info.health == "healthy"

        diag = adapter.diagnostics()
        assert isinstance(diag, dict)
        assert diag["platform"] == "meshcore"
        assert diag["started"] is True
        assert diag["mode"] == "fake"

    @pytest.mark.asyncio
    async def test_meshtastic_health_and_diag(self) -> None:
        adapter = FakeMeshtasticAdapter()
        await adapter.start(_make_ctx("p_meshtastic"))

        info = await adapter.health_check()
        assert isinstance(info, AdapterInfo)
        assert info.platform == "meshtastic"
        assert info.health == "healthy"

        diag = adapter.diagnostics()
        assert isinstance(diag, dict)
        assert diag["platform"] == "meshtastic"
        assert diag["started"] is True
        assert diag["mode"] == "fake"

    @pytest.mark.asyncio
    async def test_lxmf_health_and_diag(self) -> None:
        adapter = FakeLxmfAdapter()
        await adapter.start(_make_ctx("p_lxmf"))

        info = await adapter.health_check()
        assert isinstance(info, AdapterInfo)
        assert info.platform == "lxmf"
        assert info.health == "healthy"

        diag = adapter.diagnostics()
        assert isinstance(diag, dict)
        assert diag["platform"] == "lxmf"
        assert diag["started"] is True
        assert diag["mode"] == "fake"


# ---------------------------------------------------------------------------
# 6. Real adapter deliver() error behavior (mock sessions, no network)
# ---------------------------------------------------------------------------


class TestRealAdapterDeliverErrors:
    """Test that real adapters raise the correct error types via
    AdapterSendError / AdapterPermanentError."""

    @pytest.mark.asyncio
    async def test_meshcore_real_permanent_for_bad_input(self) -> None:
        """MeshCore real adapter raises AdapterPermanentError for non-RenderingResult."""
        from medre.adapters.meshcore.adapter import MeshCoreAdapter

        config = MeshCoreConfig(adapter_id="parity_mc", connection_type="fake")
        adapter = MeshCoreAdapter(config)
        # start with fake mode (no real SDK needed)
        await adapter.start(_make_ctx("parity_mc"))
        with pytest.raises(AdapterPermanentError, match="RenderingResult only"):
            await adapter.deliver("bad_input")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_meshcore_real_transient_on_send_error(self) -> None:
        """MeshCore real adapter converts MeshCoreSendError to AdapterSendError."""
        from medre.adapters.meshcore.adapter import MeshCoreAdapter
        from medre.adapters.meshcore.errors import MeshCoreSendError

        config = MeshCoreConfig(adapter_id="parity_mc2", connection_type="fake")
        adapter = MeshCoreAdapter(config)
        await adapter.start(_make_ctx("parity_mc2"))

        # Patch session to raise MeshCoreSendError on send_text
        # For fake mode, deliver returns None — we need to test non-fake path.
        # Use monkeypatch via object attribute to simulate send failure.
        adapter._config = MeshCoreConfig(adapter_id="parity_mc2", connection_type="tcp")
        # Mock a session that raises
        mock_session = MagicMock()
        mock_session.send_text = AsyncMock(side_effect=MeshCoreSendError("radio error"))
        mock_session.connected = True
        mock_session.reconnecting = False
        mock_session.diagnostics.return_value = MagicMock(
            connected=False,
            reconnecting=False,
            reconnect_attempts=0,
            last_error=None,
        )
        adapter._session = mock_session

        result = _make_rendering_result(payload={"text": "hello", "channel_index": 0})
        with pytest.raises(AdapterSendError) as exc_info:
            await adapter.deliver(result)
        assert exc_info.value.transient is True

    @pytest.mark.asyncio
    async def test_meshcore_real_transient_on_network_error(self) -> None:
        """MeshCore real adapter converts OSError to AdapterSendError."""
        from medre.adapters.meshcore.adapter import MeshCoreAdapter

        config = MeshCoreConfig(adapter_id="parity_mc3", connection_type="fake")
        adapter = MeshCoreAdapter(config)
        await adapter.start(_make_ctx("parity_mc3"))

        adapter._config = MeshCoreConfig(adapter_id="parity_mc3", connection_type="tcp")
        mock_session = MagicMock()
        mock_session.send_text = AsyncMock(side_effect=ConnectionError("network down"))
        mock_session.connected = True
        mock_session.reconnecting = False
        mock_session.diagnostics.return_value = MagicMock(
            connected=False,
            reconnecting=False,
            reconnect_attempts=0,
            last_error=None,
        )
        adapter._session = mock_session

        result = _make_rendering_result(payload={"text": "hello", "channel_index": 0})
        with pytest.raises(AdapterSendError) as exc_info:
            await adapter.deliver(result)
        assert exc_info.value.transient is True

    @pytest.mark.asyncio
    async def test_meshtastic_real_permanent_for_bad_input(self) -> None:
        """Meshtastic real adapter raises AdapterPermanentError for non-RenderingResult."""
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = MeshtasticConfig(adapter_id="parity_mt", connection_type="fake")
        adapter = MeshtasticAdapter(config)
        await adapter.start(_make_ctx("parity_mt"))
        with pytest.raises(AdapterPermanentError, match="RenderingResult only"):
            await adapter.deliver("bad_input")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_meshtastic_real_transient_on_send_error(self) -> None:
        """Meshtastic real adapter converts MeshtasticSendError to AdapterSendError."""
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter
        from medre.adapters.meshtastic.errors import MeshtasticSendError

        config = MeshtasticConfig(adapter_id="parity_mt2", connection_type="fake")
        adapter = MeshtasticAdapter(config)
        await adapter.start(_make_ctx("parity_mt2"))

        # Mock the queue to raise MeshtasticSendError
        adapter._queue.enqueue = AsyncMock(  # type: ignore[assignment]
            side_effect=MeshtasticSendError("radio busy")
        )

        result = _make_rendering_result(payload={"text": "hello", "channel_index": 0})
        with pytest.raises(AdapterSendError) as exc_info:
            await adapter.deliver(result)
        assert exc_info.value.transient is True

    @pytest.mark.asyncio
    async def test_meshtastic_real_transient_on_network_error(self) -> None:
        """Meshtastic real adapter converts OSError to AdapterSendError."""
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = MeshtasticConfig(adapter_id="parity_mt3", connection_type="fake")
        adapter = MeshtasticAdapter(config)
        await adapter.start(_make_ctx("parity_mt3"))

        adapter._queue.enqueue = AsyncMock(  # type: ignore[assignment]
            side_effect=ConnectionError("no radio")
        )

        result = _make_rendering_result(payload={"text": "hello", "channel_index": 0})
        with pytest.raises(AdapterSendError) as exc_info:
            await adapter.deliver(result)
        assert exc_info.value.transient is True

    @pytest.mark.asyncio
    async def test_lxmf_real_permanent_for_bad_input(self) -> None:
        """LXMF real adapter raises AdapterPermanentError for non-RenderingResult."""
        from medre.adapters.lxmf.adapter import LxmfAdapter

        config = LxmfConfig(adapter_id="parity_lx", connection_type="fake")
        adapter = LxmfAdapter(config)
        await adapter.start(_make_ctx("parity_lx"))
        with pytest.raises(AdapterPermanentError, match="RenderingResult only"):
            await adapter.deliver("bad_input")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_lxmf_real_permanent_when_not_started(self) -> None:
        """LXMF real adapter raises AdapterPermanentError when not started."""
        from medre.adapters.lxmf.adapter import LxmfAdapter

        config = LxmfConfig(adapter_id="parity_lx2", connection_type="fake")
        adapter = LxmfAdapter(config)
        result = _make_rendering_result(payload={"content": "hello"})
        with pytest.raises(AdapterPermanentError, match="not started"):
            await adapter.deliver(result)

    @pytest.mark.asyncio
    async def test_lxmf_real_transient_on_send_error(self) -> None:
        """LXMF real adapter converts LxmfSendError to AdapterSendError."""
        from medre.adapters.lxmf.adapter import LxmfAdapter
        from medre.adapters.lxmf.errors import LxmfSendError
        from medre.adapters.lxmf.session import LxmfSession

        config = LxmfConfig(adapter_id="parity_lx3", connection_type="fake")
        adapter = LxmfAdapter(config)
        await adapter.start(_make_ctx("parity_lx3"))

        # LxmfSession uses __slots__; patch on the class.
        with patch.object(
            LxmfSession,
            "send_text",
            AsyncMock(side_effect=LxmfSendError("router unavailable")),
        ):
            result = _make_rendering_result(
                payload={
                    "content": "hello",
                    "title": "",
                    "destination_hash": "ab" * 16,
                }
            )
            with pytest.raises(AdapterSendError) as exc_info:
                await adapter.deliver(result)
            assert exc_info.value.transient is True

    @pytest.mark.asyncio
    async def test_lxmf_real_transient_on_network_error(self) -> None:
        """LXMF real adapter converts OSError to AdapterSendError."""
        from medre.adapters.lxmf.adapter import LxmfAdapter
        from medre.adapters.lxmf.session import LxmfSession

        config = LxmfConfig(adapter_id="parity_lx4", connection_type="fake")
        adapter = LxmfAdapter(config)
        await adapter.start(_make_ctx("parity_lx4"))

        with patch.object(
            LxmfSession,
            "send_text",
            AsyncMock(side_effect=OSError("network unreachable")),
        ):
            result = _make_rendering_result(
                payload={
                    "content": "hello",
                    "title": "",
                    "destination_hash": "ab" * 16,
                }
            )
            with pytest.raises(AdapterSendError) as exc_info:
                await adapter.deliver(result)
            assert exc_info.value.transient is True


# ---------------------------------------------------------------------------
# 7. Real adapter delivery result shape (mock sessions)
# ---------------------------------------------------------------------------


class TestRealAdapterDeliveryResultShape:
    """Verify AdapterDeliveryResult fields from real adapters with mocked
    sessions."""

    @pytest.mark.asyncio
    async def test_meshcore_real_deliver_result(self) -> None:
        """MeshCore real adapter returns proper AdapterDeliveryResult."""
        from medre.adapters.meshcore.adapter import MeshCoreAdapter

        config = MeshCoreConfig(adapter_id="shape_mc", connection_type="fake")
        adapter = MeshCoreAdapter(config)
        await adapter.start(_make_ctx("shape_mc"))

        # Override config to non-fake so deliver doesn't return None
        adapter._config = MeshCoreConfig(adapter_id="shape_mc", connection_type="tcp")
        mock_session = MagicMock()
        mock_session.send_text = AsyncMock(return_value="pkt-42")
        mock_session.connected = True
        mock_session.reconnecting = False
        mock_session.diagnostics.return_value = MagicMock(
            connected=True,
            reconnecting=False,
            reconnect_attempts=0,
            last_error=None,
        )
        adapter._session = mock_session

        result = _make_rendering_result(payload={"text": "hi", "channel_index": 3})
        dr = await adapter.deliver(result)
        assert dr is not None
        assert dr.native_message_id == "pkt-42"
        assert dr.native_channel_id == "3"
        assert isinstance(dr.delivery_note, str)
        assert dr.delivery_note != ""
        assert "delivery_status" in dr.metadata

    @pytest.mark.asyncio
    async def test_meshtastic_real_deliver_result(self) -> None:
        """Meshtastic real adapter returns AdapterDeliveryResult with delivery_note."""
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = MeshtasticConfig(adapter_id="shape_mt", connection_type="fake")
        adapter = MeshtasticAdapter(config)
        await adapter.start(_make_ctx("shape_mt"))

        # enqueue returns normally for queue-based delivery
        adapter._queue.enqueue = AsyncMock()  # type: ignore[assignment]

        result = _make_rendering_result(payload={"text": "hi", "channel_index": 0})
        dr = await adapter.deliver(result)
        assert dr is not None
        assert isinstance(dr.native_channel_id, str)
        assert isinstance(dr.delivery_note, str)
        assert dr.delivery_note != ""

    @pytest.mark.asyncio
    async def test_lxmf_real_deliver_result(self) -> None:
        """LXMF real adapter returns AdapterDeliveryResult with lxmf metadata."""
        from medre.adapters.lxmf.adapter import LxmfAdapter
        from medre.adapters.lxmf.session import LxmfDeliveryState, LxmfSession

        config = LxmfConfig(adapter_id="shape_lx", connection_type="fake")
        adapter = LxmfAdapter(config)
        await adapter.start(_make_ctx("shape_lx"))

        # Mock session.send_text to return a (native_id, delivery_state) tuple
        with patch.object(
            LxmfSession,
            "send_text",
            AsyncMock(return_value=("fake_hash_123", LxmfDeliveryState.OUTBOUND)),
        ):
            result = _make_rendering_result(
                payload={
                    "content": "hello",
                    "title": "",
                    "destination_hash": "ab" * 16,
                }
            )
            dr = await adapter.deliver(result)
            assert dr is not None
            assert dr.native_message_id == "fake_hash_123"
            assert "lxmf" in dr.metadata
            assert "delivery_state" in dr.metadata["lxmf"]


# ---------------------------------------------------------------------------
# 8. Real adapter health_check returns AdapterInfo
# ---------------------------------------------------------------------------


class TestRealAdapterHealthCheck:
    """Real adapter health_check returns AdapterInfo."""

    @pytest.mark.asyncio
    async def test_meshcore_real_health_check(self) -> None:
        from medre.adapters.meshcore.adapter import MeshCoreAdapter

        config = MeshCoreConfig(adapter_id="health_mc", connection_type="fake")
        adapter = MeshCoreAdapter(config)
        await adapter.start(_make_ctx("health_mc"))

        info = await adapter.health_check()
        assert isinstance(info, AdapterInfo)
        assert info.adapter_id == "health_mc"
        assert info.platform == "meshcore"
        assert info.health in ("healthy", "degraded", "unknown", "failed")

    @pytest.mark.asyncio
    async def test_meshtastic_real_health_check(self) -> None:
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = MeshtasticConfig(adapter_id="health_mt", connection_type="fake")
        adapter = MeshtasticAdapter(config)
        await adapter.start(_make_ctx("health_mt"))

        info = await adapter.health_check()
        assert isinstance(info, AdapterInfo)
        assert info.adapter_id == "health_mt"
        assert info.platform == "meshtastic"

    @pytest.mark.asyncio
    async def test_lxmf_real_health_check(self) -> None:
        from medre.adapters.lxmf.adapter import LxmfAdapter

        config = LxmfConfig(adapter_id="health_lx", connection_type="fake")
        adapter = LxmfAdapter(config)
        await adapter.start(_make_ctx("health_lx"))

        info = await adapter.health_check()
        assert isinstance(info, AdapterInfo)
        assert info.adapter_id == "health_lx"
        assert info.platform == "lxmf"


# ---------------------------------------------------------------------------
# 8b. Not-started health_check parity: all adapters return "unknown"
# ---------------------------------------------------------------------------


class TestNotStartedHealthCheckParity:
    """health_check() called before start() returns health="unknown" for all
    adapters, both fake and real.  No send/delivery side effects.
    No network access — real adapters use connection_type="fake"."""

    @pytest.mark.asyncio
    async def test_fake_matrix_not_started_returns_unknown(self) -> None:
        adapter = FakeMatrixAdapter("not_started_mx")
        info = await adapter.health_check()
        assert isinstance(info, AdapterInfo)
        assert info.health == "unknown"

    @pytest.mark.asyncio
    async def test_fake_meshtastic_not_started_returns_unknown(self) -> None:
        adapter = FakeMeshtasticAdapter()
        info = await adapter.health_check()
        assert isinstance(info, AdapterInfo)
        assert info.health == "unknown"

    @pytest.mark.asyncio
    async def test_fake_meshcore_not_started_returns_unknown(self) -> None:
        adapter = FakeMeshCoreAdapter()
        info = await adapter.health_check()
        assert isinstance(info, AdapterInfo)
        assert info.health == "unknown"

    @pytest.mark.asyncio
    async def test_fake_lxmf_not_started_returns_unknown(self) -> None:
        adapter = FakeLxmfAdapter()
        info = await adapter.health_check()
        assert isinstance(info, AdapterInfo)
        assert info.health == "unknown"

    @pytest.mark.asyncio
    async def test_real_meshcore_not_started_returns_unknown(self) -> None:
        from medre.adapters.meshcore.adapter import MeshCoreAdapter

        config = MeshCoreConfig(adapter_id="ns_mc", connection_type="fake")
        adapter = MeshCoreAdapter(config)
        # NOT calling start()
        info = await adapter.health_check()
        assert isinstance(info, AdapterInfo)
        assert info.health == "unknown"

    @pytest.mark.asyncio
    async def test_real_meshtastic_not_started_returns_unknown(self) -> None:
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = MeshtasticConfig(adapter_id="ns_mt", connection_type="fake")
        adapter = MeshtasticAdapter(config)
        info = await adapter.health_check()
        assert isinstance(info, AdapterInfo)
        assert info.health == "unknown"

    @pytest.mark.asyncio
    async def test_real_lxmf_not_started_returns_unknown(self) -> None:
        from medre.adapters.lxmf.adapter import LxmfAdapter

        config = LxmfConfig(adapter_id="ns_lx", connection_type="fake")
        adapter = LxmfAdapter(config)
        info = await adapter.health_check()
        assert isinstance(info, AdapterInfo)
        assert info.health == "unknown"


# ---------------------------------------------------------------------------
# 9. Real adapter diagnostics returns dict
# ---------------------------------------------------------------------------


class TestRealAdapterDiagnostics:
    """Real adapter diagnostics() returns a dict with standard keys."""

    @pytest.mark.asyncio
    async def test_meshcore_real_diagnostics(self) -> None:
        from medre.adapters.meshcore.adapter import MeshCoreAdapter

        config = MeshCoreConfig(adapter_id="diag_mc", connection_type="fake")
        adapter = MeshCoreAdapter(config)
        await adapter.start(_make_ctx("diag_mc"))

        diag = adapter.diagnostics()
        assert isinstance(diag, dict)
        assert diag["adapter_id"] == "diag_mc"
        assert diag["platform"] == "meshcore"
        assert "started" in diag
        assert "mode" in diag

    @pytest.mark.asyncio
    async def test_meshtastic_real_diagnostics(self) -> None:
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter

        config = MeshtasticConfig(adapter_id="diag_mt", connection_type="fake")
        adapter = MeshtasticAdapter(config)
        await adapter.start(_make_ctx("diag_mt"))

        diag = adapter.diagnostics()
        assert isinstance(diag, dict)
        assert diag["adapter_id"] == "diag_mt"
        assert diag["platform"] == "meshtastic"
        assert "started" in diag

    @pytest.mark.asyncio
    async def test_lxmf_real_diagnostics(self) -> None:
        from medre.adapters.lxmf.adapter import LxmfAdapter

        config = LxmfConfig(adapter_id="diag_lx", connection_type="fake")
        adapter = LxmfAdapter(config)
        await adapter.start(_make_ctx("diag_lx"))

        diag = adapter.diagnostics()
        assert isinstance(diag, dict)
        assert diag["adapter_id"] == "diag_lx"
        assert diag["platform"] == "lxmf"
        assert "started" in diag
        assert "mode" in diag


# ---------------------------------------------------------------------------
# 10. AdapterSendError.transient classification
# ---------------------------------------------------------------------------


class TestAdapterSendErrorTransient:
    """AdapterSendError has transient=True; AdapterPermanentError has
    transient=False."""

    def test_send_error_transient_default(self) -> None:
        err = AdapterSendError("test")
        assert err.transient is True

    def test_send_error_transient_explicit_false(self) -> None:
        err = AdapterSendError("test", transient=False)
        assert err.transient is False

    def test_permanent_error_not_transient(self) -> None:
        err = AdapterPermanentError("test")
        assert err.transient is False

    def test_permanent_inherits_send_error(self) -> None:
        err = AdapterPermanentError("test")
        assert isinstance(err, AdapterSendError)
