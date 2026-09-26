"""Adapter boundary contract tests.

Documents the uniform contract expectations for AdapterContract subclasses
without adding features.  These tests verify:

* ``start(ctx)`` requires an :class:`AdapterContext`.
* ``stop(timeout)`` accepts a numeric timeout.
* ``health_check()`` returns an :class:`AdapterInfo` with JSON-safe fields.
* ``deliver(result)`` accepts a :class:`RenderingResult` and returns
  ``AdapterHandoffResult``.
* ``AdapterContract`` is abstract and cannot be instantiated directly.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from medre.core.contracts.adapter import (
    AdapterCapabilities,
    AdapterContext,
    AdapterContract,
    AdapterHandoffResult,
    AdapterInfo,
    AdapterPermanentError,
    AdapterRole,
    AdapterSendError,
)
from medre.core.rendering.renderer import RenderingResult
from tests.helpers.delivery_callbacks import (
    make_attempt_provenance,
    make_deferred_completion,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_context(adapter_id: str = "test") -> AdapterContext:
    return AdapterContext(
        adapter_id=adapter_id,
        publish_inbound=_async_noop,
        logger=logging.getLogger(f"test.{adapter_id}"),
        clock=lambda: datetime.now(timezone.utc),
        shutdown_event=asyncio.Event(),
        report_delivery_feedback=_async_noop,
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


def _make_deferred_rendering_result() -> RenderingResult:
    provenance = make_attempt_provenance(
        event_id="evt-001",
        target_adapter="test",
        target_channel="ch-0",
        outbox_id="outbox-adapter-boundary",
        attempt_number=1,
    )
    return RenderingResult(
        event_id=provenance.event_id,
        target_adapter=provenance.target_adapter,
        target_channel=provenance.target_channel,
        payload={"body": "hello"},
        delivery_plan_id=provenance.delivery_plan_id,
        outbox_id=provenance.outbox_id,
        attempt_number=provenance.attempt_number,
        attempt_provenance=provenance,
    )


# Minimal concrete adapter for contract verification.
class _StubAdapter(AdapterContract):
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

    async def deliver(self, result: RenderingResult) -> AdapterHandoffResult:
        return AdapterHandoffResult(
            native_message_id="stub-msg-001",
            native_channel_id="stub-ch-001",
        )


# ===================================================================
# 1. AdapterContract is abstract
# ===================================================================


class TestAdapterContractAbstract:
    """AdapterContract cannot be instantiated directly."""

    def test_cannot_instantiate_base_adapter(self) -> None:
        """AdapterContract is ABC — instantiation raises TypeError."""
        with pytest.raises(TypeError):
            AdapterContract()  # type: ignore[abstract]


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
        sig = inspect.signature(AdapterContract.start)
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
        sig = inspect.signature(AdapterContract.stop)
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
    """deliver() accepts RenderingResult and returns AdapterHandoffResult."""

    @pytest.mark.asyncio
    async def test_deliver_accepts_rendering_result(self) -> None:
        adapter = _StubAdapter()
        result = _make_rendering_result()
        delivery = await adapter.deliver(result)
        assert isinstance(delivery, AdapterHandoffResult)
        assert delivery.native_message_id == "stub-msg-001"
        assert delivery.native_channel_id == "stub-ch-001"

    def test_deliver_annotation_is_closed_handoff_result(self) -> None:
        """The adapter contract does not advertise a nullable success result."""
        sig = inspect.signature(AdapterContract.deliver)
        assert "AdapterHandoffResult" in str(sig.return_annotation)
        assert "None" not in str(sig.return_annotation)

    def test_deliver_signature(self) -> None:
        sig = inspect.signature(AdapterContract.deliver)
        params = list(sig.parameters.keys())
        assert "result" in params


# ===================================================================
# 6. AdapterSendError / AdapterPermanentError hierarchy
# ===================================================================


class TestAdapterSendErrorHierarchy:
    """Error hierarchy: transient/permanent classification."""

    def test_send_error_is_transient_by_default(self) -> None:
        exc = AdapterSendError("network timeout")
        assert exc.transient is True
        assert isinstance(exc, Exception)

    def test_permanent_error_is_not_transient(self) -> None:
        exc = AdapterPermanentError("bad config")
        assert exc.transient is False

    def test_permanent_error_inherits_send_error(self) -> None:
        exc = AdapterPermanentError("auth failed")
        assert isinstance(exc, AdapterSendError)
        assert isinstance(exc, Exception)

    def test_send_error_carries_message(self) -> None:
        exc = AdapterSendError("something broke")
        assert str(exc) == "something broke"


# ===================================================================
# 7. classify_failure with AdapterSendError.transient
# ===================================================================


class TestClassifyFailureWithAdapterSendError:
    """classify_failure inspects AdapterSendError.transient flag."""

    def test_transient_send_error_classifies_as_adapter_transient(self) -> None:
        from medre.core.planning.delivery_plan import (
            DeliveryFailureKind,
            RetryExecutor,
        )

        exc = AdapterSendError("timeout", transient=True)
        kind = RetryExecutor.classify_failure(exc, adapter_registered=True)
        assert kind == DeliveryFailureKind.ADAPTER_TRANSIENT

    def test_permanent_error_classifies_as_adapter_permanent(self) -> None:
        from medre.core.planning.delivery_plan import (
            DeliveryFailureKind,
            RetryExecutor,
        )

        exc = AdapterPermanentError("bad payload")
        kind = RetryExecutor.classify_failure(exc, adapter_registered=True)
        assert kind == DeliveryFailureKind.ADAPTER_PERMANENT

    def test_send_error_with_transient_false_classifies_permanent(self) -> None:
        from medre.core.planning.delivery_plan import (
            DeliveryFailureKind,
            RetryExecutor,
        )

        exc = AdapterSendError("retries exhausted", transient=False)
        kind = RetryExecutor.classify_failure(exc, adapter_registered=True)
        assert kind == DeliveryFailureKind.ADAPTER_PERMANENT

    def test_non_adapter_error_falls_back_to_transient_types(self) -> None:
        from medre.core.planning.delivery_plan import (
            DeliveryFailureKind,
            RetryExecutor,
        )

        # TimeoutError is a standard transient type
        kind = RetryExecutor.classify_failure(
            TimeoutError("connection timed out"), adapter_registered=True
        )
        assert kind == DeliveryFailureKind.ADAPTER_TRANSIENT

    def test_non_adapter_error_falls_back_to_permanent(self) -> None:
        from medre.core.planning.delivery_plan import (
            DeliveryFailureKind,
            RetryExecutor,
        )

        kind = RetryExecutor.classify_failure(
            ValueError("bad value"), adapter_registered=True
        )
        assert kind == DeliveryFailureKind.ADAPTER_PERMANENT


# ===================================================================
# 8. AdapterHandoffResult fields and note
# ===================================================================


class TestAdapterHandoffResultFields:
    """AdapterHandoffResult has consistent field shapes."""

    def test_default_disposition_is_closed_transport_handoff_fact(self) -> None:
        from medre.core.contracts.delivery import ADAPTER_HANDOFF_DISPOSITION_VALUES

        result = AdapterHandoffResult()
        assert result.disposition == "transport_handoff"
        assert result.disposition in ADAPTER_HANDOFF_DISPOSITION_VALUES

    def test_note_defaults_to_empty(self) -> None:
        result = AdapterHandoffResult()
        assert result.note == ""
        assert result.native_message_id is None
        assert result.native_channel_id is None
        assert result.native_thread_id is None
        assert result.native_relation_id is None

    def test_note_can_be_set(self) -> None:
        result = AdapterHandoffResult(
            native_message_id=None,
            native_channel_id="1",
            note="locally enqueued",
        )
        assert result.note == "locally enqueued"
        assert result.native_message_id is None

    def test_result_with_all_native_ids(self) -> None:
        result = AdapterHandoffResult(
            native_message_id="msg-123",
            native_channel_id="ch-456",
            native_thread_id="thread-789",
            note="",
        )
        assert result.native_message_id == "msg-123"
        assert result.native_channel_id == "ch-456"
        assert result.native_thread_id == "thread-789"


# ===================================================================
# 9. Native ref persistence semantics
# ===================================================================


class TestNativeRefPersistenceSemantics:
    """Native refs are only persisted when adapter returns native_message_id."""

    def test_result_with_native_id_signals_ref_storage(self) -> None:
        result = AdapterHandoffResult(
            native_message_id="$event_id:abc",
            native_channel_id="!room:server",
        )
        # Pipeline stores native ref when native_message_id is not None
        assert result.native_message_id is not None

    def test_result_without_native_id_signals_no_ref(self) -> None:
        result = AdapterHandoffResult(
            native_message_id=None,
            native_channel_id="1",
            note="locally enqueued",
        )
        # Pipeline skips native ref when native_message_id is None
        assert result.native_message_id is None


# ===================================================================
# 10. CancelledError propagation
# ===================================================================


class TestCancelledErrorPropagation:
    """CancelledError must propagate through pipeline adapter call."""

    @pytest.mark.asyncio
    async def test_cancelled_error_propagates_from_deliver(self) -> None:
        """CancelledError raised inside deliver() is not swallowed."""

        class _CancellingAdapter(_StubAdapter):
            async def deliver(self, result: RenderingResult) -> AdapterHandoffResult:
                raise asyncio.CancelledError()

        adapter = _CancellingAdapter()
        with pytest.raises(asyncio.CancelledError):
            await adapter.deliver(_make_rendering_result())


# ===================================================================
# 11. Fake adapter returns consistent field shapes
# ===================================================================


class TestFakeAdapterFieldShapes:
    """Each fake adapter returns consistent AdapterHandoffResult fields."""

    @pytest.mark.asyncio
    async def test_stub_adapter_returns_consistent_shape(self) -> None:
        adapter = _StubAdapter()
        result = await adapter.deliver(_make_rendering_result())
        assert isinstance(result, AdapterHandoffResult)
        assert isinstance(result.native_message_id, (str, type(None)))
        assert isinstance(result.native_channel_id, (str, type(None)))
        assert isinstance(result.native_thread_id, (str, type(None)))
        assert isinstance(result.note, str)

    @pytest.mark.asyncio
    async def test_adapter_with_note(self) -> None:
        class _QueueAdapter(_StubAdapter):
            async def deliver(self, result: RenderingResult) -> AdapterHandoffResult:
                return AdapterHandoffResult(
                    disposition="deferred",
                    native_message_id=None,
                    native_channel_id="1",
                    note="locally enqueued",
                )

        adapter = _QueueAdapter()
        out = await adapter.deliver(_make_rendering_result())
        assert out is not None
        assert out.native_message_id is None
        assert out.note == "locally enqueued"


# ===================================================================
# 12. Fake adapter delivery failures use base error hierarchy
# ===================================================================


class TestFakeAdapterErrorClassification:
    """Fake adapters raise AdapterSendError (from base.py) for simulated
    delivery failures, allowing RetryExecutor.classify_failure() to
    classify them correctly.
    """

    @pytest.mark.asyncio
    async def test_fake_meshcore_failure_classifies_transient(self) -> None:
        """FakeMeshCoreAdapter simulated failure raises AdapterSendError
        that classify_failure maps to ADAPTER_TRANSIENT."""
        from medre.adapters.fakes.meshcore import FakeMeshCoreAdapter
        from medre.core.planning.delivery_plan import (
            DeliveryFailureKind,
            RetryExecutor,
        )

        adapter = FakeMeshCoreAdapter()
        adapter.set_deliver_failure(True)

        with pytest.raises(AdapterSendError) as exc_info:
            await adapter.deliver(_make_rendering_result())

        assert exc_info.value.transient is True
        kind = RetryExecutor.classify_failure(exc_info.value, adapter_registered=True)
        assert kind is DeliveryFailureKind.ADAPTER_TRANSIENT

    @pytest.mark.asyncio
    async def test_fake_meshtastic_failure_classifies_transient(self) -> None:
        """FakeMeshtasticAdapter simulated failure raises AdapterSendError
        that classify_failure maps to ADAPTER_TRANSIENT."""
        from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
        from medre.core.planning.delivery_plan import (
            DeliveryFailureKind,
            RetryExecutor,
        )

        adapter = FakeMeshtasticAdapter()
        adapter.set_deliver_failure(True)

        with pytest.raises(AdapterSendError) as exc_info:
            await adapter.deliver(_make_rendering_result())

        assert exc_info.value.transient is True
        kind = RetryExecutor.classify_failure(exc_info.value, adapter_registered=True)
        assert kind is DeliveryFailureKind.ADAPTER_TRANSIENT

    @pytest.mark.asyncio
    async def test_fake_lxmf_failure_classifies_transient(self) -> None:
        """FakeLxmfAdapter simulated failure raises AdapterSendError
        that classify_failure maps to ADAPTER_TRANSIENT."""
        from medre.adapters.fakes.lxmf import FakeLxmfAdapter
        from medre.core.planning.delivery_plan import (
            DeliveryFailureKind,
            RetryExecutor,
        )

        adapter = FakeLxmfAdapter()
        adapter.set_deliver_failure(True)

        with pytest.raises(AdapterSendError) as exc_info:
            await adapter.deliver(_make_rendering_result())

        assert exc_info.value.transient is True
        kind = RetryExecutor.classify_failure(exc_info.value, adapter_registered=True)
        assert kind is DeliveryFailureKind.ADAPTER_TRANSIENT


# ===================================================================
# 13. Per-adapter error classification audit
# ===================================================================


class TestPerAdapterErrorClassification:
    """Focused tests proving each real adapter's transient and permanent
    error paths are correct.
    """

    @pytest.mark.asyncio
    async def test_matrix_not_connected_permanent(self) -> None:
        """MatrixAdapter raises AdapterPermanentError when
        client is not connected — lifecycle state missing is permanent."""
        from medre.adapters.matrix.adapter import MatrixAdapter
        from medre.config.adapters.matrix import MatrixConfig

        config = MatrixConfig(
            adapter_id="test",
            user_id="@test:server",
            homeserver="https://server",
            access_token="tok",
        )
        adapter = MatrixAdapter(config)

        result = _make_rendering_result()
        with pytest.raises(AdapterPermanentError, match="session is not initialized"):
            await adapter.deliver(result)

    @pytest.mark.asyncio
    async def test_matrix_no_room_id_permanent(self) -> None:
        """MatrixAdapter raises AdapterPermanentError when room_id is missing."""
        from medre.adapters.matrix.adapter import MatrixAdapter
        from medre.config.adapters.matrix import MatrixConfig

        config = MatrixConfig(
            adapter_id="test",
            user_id="@test:server",
            homeserver="https://server",
            access_token="tok",
        )
        adapter = MatrixAdapter(config)
        adapter._session = MagicMock()

        result = RenderingResult(
            event_id="evt-no-room",
            target_adapter="test",
            target_channel=None,
            payload={"msgtype": "m.text", "body": "hello"},
        )
        with pytest.raises(AdapterPermanentError, match="no room_id"):
            await adapter.deliver(result)

    @pytest.mark.asyncio
    async def test_matrix_send_error_converted_to_transient(self) -> None:
        """MatrixAdapter converts MatrixSendError to AdapterSendError(transient=True)."""
        from medre.adapters.matrix.adapter import MatrixAdapter
        from medre.adapters.matrix.errors import MatrixSendError
        from medre.config.adapters.matrix import MatrixConfig

        config = MatrixConfig(
            adapter_id="test",
            user_id="@test:server",
            homeserver="https://server",
            access_token="tok",
        )
        adapter = MatrixAdapter(config)

        mock_session = MagicMock()
        mock_session.room_send = AsyncMock(side_effect=MatrixSendError("forbidden"))
        adapter._session = mock_session

        result = _make_rendering_result()
        with pytest.raises(AdapterSendError) as exc_info:
            await adapter.deliver(result)
        assert exc_info.value.transient is True

    @pytest.mark.asyncio
    async def test_meshcore_session_not_initialised_permanent(self) -> None:
        """MeshCoreAdapter raises AdapterPermanentError for session not initialised."""
        from medre.adapters.meshcore.adapter import MeshCoreAdapter
        from medre.config.adapters.meshcore import MeshCoreConfig

        config = MeshCoreConfig(adapter_id="test")
        adapter = MeshCoreAdapter(config)
        adapter._session = None
        # Force non-fake mode
        adapter._config = MagicMock()
        adapter._config.connection_type = "tcp"

        result = _make_rendering_result()
        with pytest.raises(AdapterPermanentError, match="Session not initialised"):
            await adapter.deliver(result)

    @pytest.mark.asyncio
    async def test_meshcore_timeout_transient(self) -> None:
        """MeshCoreAdapter raises AdapterSendError(transient=True) for timeout."""
        from medre.adapters.meshcore.adapter import MeshCoreAdapter
        from medre.config.adapters.meshcore import MeshCoreConfig

        config = MeshCoreConfig(adapter_id="test")
        adapter = MeshCoreAdapter(config)
        adapter._config = MagicMock()
        adapter._config.connection_type = "tcp"

        mock_session = MagicMock()
        mock_session.send_text = AsyncMock(side_effect=TimeoutError("timed out"))
        adapter._session = mock_session

        result = _make_deferred_rendering_result()
        result.payload["channel_index"] = 0
        with pytest.raises(AdapterSendError) as exc_info:
            await adapter.deliver(result)
        assert exc_info.value.transient is True

    @pytest.mark.asyncio
    async def test_meshtastic_timeout_transient(self) -> None:
        """MeshtasticAdapter raises AdapterSendError(transient=True) for timeout."""
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter
        from medre.config.adapters.meshtastic import MeshtasticConfig

        config = MeshtasticConfig(adapter_id="test")
        adapter = MeshtasticAdapter(config)
        adapter._started = True
        adapter.ctx = _make_context("test")

        # Mock the queue to raise TimeoutError
        adapter._queue = MagicMock()
        adapter._queue.enqueue = AsyncMock(side_effect=TimeoutError("timed out"))

        result = _make_deferred_rendering_result()
        result.payload["channel_index"] = 0
        with pytest.raises(AdapterSendError) as exc_info:
            await adapter.deliver(result)
        assert exc_info.value.transient is True

    @pytest.mark.asyncio
    async def test_lxmf_timeout_transient(self) -> None:
        """LxmfAdapter raises AdapterSendError(transient=True) for timeout."""
        from medre.adapters.lxmf.adapter import LxmfAdapter
        from medre.config.adapters.lxmf import LxmfConfig

        config = LxmfConfig(adapter_id="test")
        adapter = LxmfAdapter(config)
        adapter._started = True

        mock_session = MagicMock()
        mock_session.send_text = AsyncMock(side_effect=TimeoutError("timed out"))
        adapter._session = mock_session

        result = _make_rendering_result()
        result.payload["content"] = "hello"
        result.payload["destination_hash"] = "ab" * 16
        with pytest.raises(AdapterSendError) as exc_info:
            await adapter.deliver(result)
        assert exc_info.value.transient is True

    @pytest.mark.asyncio
    async def test_lxmf_send_error_transient(self) -> None:
        """LxmfAdapter raises AdapterSendError(transient=True) for LxmfSendError."""
        from medre.adapters.lxmf.adapter import LxmfAdapter
        from medre.adapters.lxmf.errors import LxmfSendError
        from medre.config.adapters.lxmf import LxmfConfig

        config = LxmfConfig(adapter_id="test")
        adapter = LxmfAdapter(config)
        adapter._started = True

        mock_session = MagicMock()
        mock_session.send_text = AsyncMock(
            side_effect=LxmfSendError("propagation failed")
        )
        adapter._session = mock_session

        result = _make_rendering_result()
        result.payload["content"] = "hello"
        result.payload["destination_hash"] = "ab" * 16
        with pytest.raises(AdapterSendError) as exc_info:
            await adapter.deliver(result)
        assert exc_info.value.transient is True

    @pytest.mark.asyncio
    async def test_meshtastic_not_started_permanent(self) -> None:
        """MeshtasticAdapter raises AdapterPermanentError when not started in real mode."""
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter
        from medre.config.adapters.meshtastic import MeshtasticConfig

        config = MeshtasticConfig(
            adapter_id="test", connection_type="tcp", host="localhost", port=4403
        )
        adapter = MeshtasticAdapter(config)
        # _started is False by default, tcp mode requires start

        result = _make_rendering_result()
        result.payload["channel_index"] = 0
        with pytest.raises(AdapterPermanentError, match="not started"):
            await adapter.deliver(result)

    @pytest.mark.asyncio
    async def test_meshtastic_send_error_converted_to_transient(self) -> None:
        """MeshtasticAdapter converts MeshtasticSendError to AdapterSendError(transient=True)."""
        from medre.adapters.meshtastic.adapter import MeshtasticAdapter
        from medre.adapters.meshtastic.errors import MeshtasticSendError
        from medre.config.adapters.meshtastic import MeshtasticConfig

        config = MeshtasticConfig(adapter_id="test")
        adapter = MeshtasticAdapter(config)
        adapter._started = True
        adapter.ctx = _make_context("test")

        adapter._queue = MagicMock()
        adapter._queue.enqueue = AsyncMock(
            side_effect=MeshtasticSendError("send failed")
        )

        result = _make_deferred_rendering_result()
        result.payload["channel_index"] = 0
        with pytest.raises(AdapterSendError) as exc_info:
            await adapter.deliver(result)
        assert exc_info.value.transient is True

    @pytest.mark.asyncio
    async def test_lxmf_not_started_permanent(self) -> None:
        """LxmfAdapter raises AdapterPermanentError when not started."""
        from medre.adapters.lxmf.adapter import LxmfAdapter
        from medre.config.adapters.lxmf import LxmfConfig

        config = LxmfConfig(adapter_id="test")
        adapter = LxmfAdapter(config)
        # _started is False by default

        result = _make_rendering_result()
        result.payload["content"] = "hello"
        result.payload["destination_hash"] = "ab" * 16
        with pytest.raises(AdapterPermanentError, match="not started"):
            await adapter.deliver(result)

    @pytest.mark.asyncio
    async def test_meshcore_send_error_converted_to_transient(self) -> None:
        """MeshCoreAdapter converts MeshCoreSendError to AdapterSendError(transient=True)."""
        from medre.adapters.meshcore.adapter import MeshCoreAdapter
        from medre.adapters.meshcore.errors import MeshCoreSendError
        from medre.config.adapters.meshcore import MeshCoreConfig

        config = MeshCoreConfig(adapter_id="test")
        adapter = MeshCoreAdapter(config)
        adapter._config = MagicMock()
        adapter._config.connection_type = "tcp"

        mock_session = MagicMock()
        mock_session.send_text = AsyncMock(side_effect=MeshCoreSendError("send failed"))
        adapter._session = mock_session

        result = _make_deferred_rendering_result()
        result.payload["channel_index"] = 0
        with pytest.raises(AdapterSendError) as exc_info:
            await adapter.deliver(result)
        assert exc_info.value.transient is True


class TestErrorClassificationPipeline:
    """Tests proving classify_failure works via AdapterSendError.transient,
    not via transport-specific *SendError inheritance.
    """

    def test_classify_adapter_transient_via_transient_flag(self) -> None:
        """AdapterSendError(transient=True) classifies as ADAPTER_TRANSIENT."""
        from medre.core.planning.delivery_plan import DeliveryFailureKind, RetryExecutor

        error = AdapterSendError("timeout", transient=True)
        kind = RetryExecutor.classify_failure(error)
        assert kind is DeliveryFailureKind.ADAPTER_TRANSIENT

    def test_classify_adapter_permanent_via_transient_flag(self) -> None:
        """AdapterSendError(transient=False) classifies as ADAPTER_PERMANENT."""
        from medre.core.planning.delivery_plan import DeliveryFailureKind, RetryExecutor

        error = AdapterSendError("config error", transient=False)
        kind = RetryExecutor.classify_failure(error)
        assert kind is DeliveryFailureKind.ADAPTER_PERMANENT

    def test_classify_permanent_error_subclass(self) -> None:
        """AdapterPermanentError classifies as ADAPTER_PERMANENT."""
        from medre.core.planning.delivery_plan import DeliveryFailureKind, RetryExecutor

        error = AdapterPermanentError("not started")
        kind = RetryExecutor.classify_failure(error)
        assert kind is DeliveryFailureKind.ADAPTER_PERMANENT

    def test_transport_send_errors_not_in_classify(self) -> None:
        """Transport-specific *SendError classes are NOT AdapterSendError subclasses."""
        from medre.adapters.lxmf.errors import LxmfSendError
        from medre.adapters.matrix.errors import MatrixSendError
        from medre.adapters.meshcore.errors import MeshCoreSendError
        from medre.adapters.meshtastic.errors import MeshtasticSendError

        for error_cls in (
            MatrixSendError,
            MeshCoreSendError,
            MeshtasticSendError,
            LxmfSendError,
        ):
            assert not issubclass(
                error_cls, AdapterSendError
            ), f"{error_cls.__name__} must NOT inherit from AdapterSendError"

    def test_transport_send_errors_classify_as_permanent_fallback(self) -> None:
        """If a raw transport *SendError escaped to classify_failure,
        it would fall through to ADAPTER_PERMANENT (default)."""
        from medre.adapters.matrix.errors import MatrixSendError
        from medre.core.planning.delivery_plan import DeliveryFailureKind, RetryExecutor

        error = MatrixSendError("leaked")
        kind = RetryExecutor.classify_failure(error)
        assert kind is DeliveryFailureKind.ADAPTER_PERMANENT

    def test_cancelled_error_not_adapter_error(self) -> None:
        """CancelledError is not an AdapterSendError and does not match."""
        import asyncio

        assert not isinstance(asyncio.CancelledError(), AdapterSendError)


# ===================================================================
# 14. DeferredHandoffCompleted metadata immutability
# ===================================================================


class TestDeferredHandoffCompletedMetadataFrozen:
    """DeferredHandoffCompleted.metadata is frozen after construction."""

    def test_metadata_readable(self) -> None:
        record = make_deferred_completion(
            event_id="evt-1",
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="42",
            metadata={"packet_id": 1},
            attempt_provenance=make_attempt_provenance(
                event_id="evt-1",
                target_adapter="mesh-1",
                outbox_id="obox-contract",
                attempt_number=1,
                target_channel="0",
            ),
        )
        assert record.handoff.metadata["packet_id"] == 1

    def test_metadata_immutable_assignment_raises(self) -> None:
        record = make_deferred_completion(
            event_id="evt-1",
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="42",
            metadata={"packet_id": 1},
            attempt_provenance=make_attempt_provenance(
                event_id="evt-1",
                target_adapter="mesh-1",
                outbox_id="obox-contract",
                attempt_number=1,
                target_channel="0",
            ),
        )
        with pytest.raises(TypeError):
            record.handoff.metadata["x"] = "y"  # type: ignore[index]

    def test_metadata_isolated_from_mutable_original(self) -> None:
        original = {"packet_id": 1}
        record = make_deferred_completion(
            event_id="evt-1",
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="42",
            metadata=original,
            attempt_provenance=make_attempt_provenance(
                event_id="evt-1",
                target_adapter="mesh-1",
                outbox_id="obox-contract",
                attempt_number=1,
                target_channel="0",
            ),
        )
        original["packet_id"] = 999
        assert record.handoff.metadata["packet_id"] == 1


# ===================================================================
# 15. DeferredHandoffCompleted.native_message_id validation
# ===================================================================


class TestDeferredHandoffCompletedMessageIdValidation:
    """Deferred completion carries an optional, validated native message ID."""

    def test_empty_string_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty string"):
            make_deferred_completion(
                event_id="evt-1",
                adapter="mesh-1",
                native_channel_id="0",
                native_message_id="",
                attempt_provenance=make_attempt_provenance(
                    event_id="evt-1",
                    target_adapter="mesh-1",
                    outbox_id="obox-contract",
                    attempt_number=1,
                    target_channel="0",
                ),
            )

    def test_whitespace_only_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty string"):
            make_deferred_completion(
                event_id="evt-1",
                adapter="mesh-1",
                native_channel_id="0",
                native_message_id="   ",
                attempt_provenance=make_attempt_provenance(
                    event_id="evt-1",
                    target_adapter="mesh-1",
                    outbox_id="obox-contract",
                    attempt_number=1,
                    target_channel="0",
                ),
            )

    def test_none_allowed_when_transport_assigns_no_message_id(self) -> None:
        record = make_deferred_completion(
            event_id="evt-1",
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id=None,
            attempt_provenance=make_attempt_provenance(
                event_id="evt-1",
                target_adapter="mesh-1",
                outbox_id="obox-contract",
                attempt_number=1,
                target_channel="0",
            ),
        )
        assert record.handoff.native_message_id is None

    def test_valid_string_accepted(self) -> None:
        record = make_deferred_completion(
            event_id="evt-1",
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="42",
            attempt_provenance=make_attempt_provenance(
                event_id="evt-1",
                target_adapter="mesh-1",
                outbox_id="obox-contract",
                attempt_number=1,
                target_channel="0",
            ),
        )
        assert record.handoff.native_message_id == "42"
        assert record.handoff.confirmation_level == "local_transport"

    def test_metadata_still_frozen_after_valid_id(self) -> None:
        record = make_deferred_completion(
            event_id="evt-1",
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="42",
            metadata={"packet_id": 1},
            attempt_provenance=make_attempt_provenance(
                event_id="evt-1",
                target_adapter="mesh-1",
                outbox_id="obox-contract",
                attempt_number=1,
                target_channel="0",
            ),
        )
        with pytest.raises(TypeError):
            record.handoff.metadata["x"] = "y"  # type: ignore[index]


# ===================================================================
# 16. Cross-cutting: SDK import safety (compat layer)
# ===================================================================

_COMPAT_SPECS = [
    ("lxmf", "HAS_LXMF"),
    ("meshcore", "HAS_MESHCORE"),
    ("matrix", "HAS_NIO"),
    ("meshtastic", "HAS_MESHTASTIC"),
]

_ADAPTER_NAMES = ["lxmf", "meshcore", "matrix", "meshtastic"]


class TestCompatImportSafety:
    """Each adapter's compat.py is importable without the optional SDK.

    Tier 1 (fake_pipeline) — proves the compat guard pattern is consistent
    across all four transport adapters.
    """

    @pytest.mark.parametrize(
        ("adapter_name", "flag_name"),
        _COMPAT_SPECS,
        ids=[s[0] for s in _COMPAT_SPECS],
    )
    def test_compat_importable_without_sdk(
        self, adapter_name: str, flag_name: str
    ) -> None:
        """Importing compat.py does not raise ImportError."""
        import importlib

        mod = importlib.import_module(f"medre.adapters.{adapter_name}.compat")
        assert hasattr(mod, flag_name)

    @pytest.mark.parametrize(
        ("adapter_name", "flag_name"),
        _COMPAT_SPECS,
        ids=[s[0] for s in _COMPAT_SPECS],
    )
    def test_has_flag_is_bool(self, adapter_name: str, flag_name: str) -> None:
        """HAS_* flag is a boolean value."""
        import importlib

        mod = importlib.import_module(f"medre.adapters.{adapter_name}.compat")
        value = getattr(mod, flag_name)
        assert isinstance(
            value, bool
        ), f"{adapter_name}.compat.{flag_name} is {type(value).__name__}, expected bool"

    @pytest.mark.parametrize("adapter_name", _ADAPTER_NAMES, ids=_ADAPTER_NAMES)
    def test_adapter_init_importable(self, adapter_name: str) -> None:
        """The adapter package __init__.py imports without the SDK."""
        import importlib

        mod = importlib.import_module(f"medre.adapters.{adapter_name}")
        assert mod is not None


class TestLxmfRequireGuard:
    """LXMF compat._require_lxmf() raises ImportError when HAS_LXMF is False."""

    def test_require_lxmf_raises_when_sdk_missing(self) -> None:
        """When HAS_LXMF is False, _require_lxmf() raises ImportError."""
        import medre.adapters.lxmf.compat as compat

        original = compat.HAS_LXMF
        try:
            compat.HAS_LXMF = False
            with pytest.raises(ImportError, match="lxmf"):
                compat._require_lxmf()
        finally:
            compat.HAS_LXMF = original

    def test_require_lxmf_raises_clear_message(self) -> None:
        """Error message mentions the install command."""
        import medre.adapters.lxmf.compat as compat

        original = compat.HAS_LXMF
        try:
            compat.HAS_LXMF = False
            with pytest.raises(ImportError, match="medre\\[lxmf\\]") as exc_info:
                compat._require_lxmf()
            assert "connection_type='fake'" in str(exc_info.value)
        finally:
            compat.HAS_LXMF = original


# ===================================================================
# 17. Cross-cutting: pipeline ignores metadata for lifecycle
# ===================================================================


def _classify_handoff(result: AdapterHandoffResult) -> str:
    """Map the closed adapter hand-off fact to durable receipt admission."""
    return "queued" if result.disposition == "deferred" else "sent"


class TestPipelineMetadataIgnoredForLifecycle:
    """Handoff disposition, not adapter metadata, selects initial lifecycle state."""

    @pytest.mark.parametrize(
        "metadata",
        [
            {"meshcore": {"local_acceptance": False}},
            {"lxmf": {"delivery_state": "outbound"}},
            {"meshtastic": {"hop_limit": 3}},
            {},
        ],
    )
    def test_transport_handoff_metadata_does_not_change_sent_classification(
        self, metadata: dict[str, object]
    ) -> None:
        result = AdapterHandoffResult(
            native_message_id="native-1",
            disposition="transport_handoff",
            metadata=metadata,
        )
        assert _classify_handoff(result) == "sent"

    @pytest.mark.parametrize(
        "metadata",
        [
            {"lxmf": {"delivery_state": "outbound"}},
            {"meshcore": {"local_acceptance": False}},
            {},
        ],
    )
    def test_deferred_metadata_does_not_change_queued_classification(
        self, metadata: dict[str, object]
    ) -> None:
        result = AdapterHandoffResult(
            disposition="deferred",
            native_channel_id="1",
            note="locally queued",
            metadata=metadata,
        )
        assert _classify_handoff(result) == "queued"

    def test_default_disposition_classifies_as_sent(self) -> None:
        assert _classify_handoff(AdapterHandoffResult()) == "sent"

    @pytest.mark.parametrize(
        ("disposition", "expected"),
        [("transport_handoff", "sent"), ("deferred", "queued")],
    )
    def test_classification_matches_pipeline(
        self, disposition: str, expected: str
    ) -> None:
        result = AdapterHandoffResult(disposition=disposition)  # type: ignore[arg-type]
        assert _classify_handoff(result) == expected


def test_invalid_confirmation_level_rejected() -> None:
    with pytest.raises(ValueError, match="confirmation_level"):
        make_deferred_completion(
            event_id="evt-1",
            adapter="mesh-1",
            native_channel_id="0",
            native_message_id="42",
            # Deliberately outside the Literal to exercise runtime validation.
            confirmation_level="delivered",  # type: ignore[arg-type]
            attempt_provenance=make_attempt_provenance(
                event_id="evt-1",
                target_adapter="mesh-1",
                outbox_id="obox-contract",
                attempt_number=1,
                target_channel="0",
            ),
        )
