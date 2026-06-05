"""Tests for uncovered MeshtasticAdapter methods: stop delegation,
health_check edge cases, _enrich_with_node_info exception path,
and _record_delayed_outbound_ref native_message_id guard.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from medre.adapters.meshtastic.adapter import MeshtasticAdapter
from medre.adapters.meshtastic.queue import QueueDeliveryResult
from medre.core.contracts.adapter import (
    AdapterDeliveryResult,
)
from tests.helpers.meshtastic import make_meshtastic_config

# ===================================================================
# stop() session delegation
# ===================================================================


class TestStopSessionDelegation:
    """MeshtasticAdapter.stop() delegates to session.stop()."""

    async def test_stop_calls_session_stop_with_timeout(
        self, make_adapter_context
    ) -> None:
        """stop() calls session.stop(timeout=...) when session exists."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)

        # Replace session with a mock to verify delegation
        mock_session = AsyncMock()
        adapter._session = mock_session

        await adapter.stop(timeout=5.0)
        mock_session.stop.assert_awaited_once_with(timeout=5.0)
        assert adapter._session is None

    async def test_stop_session_none_no_error(self) -> None:
        """stop() with _session=None does not call session.stop."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        adapter._session = None
        # Should not raise
        await adapter.stop()

    async def test_stop_logs_when_ctx_present(self, make_adapter_context) -> None:
        """stop() calls ctx.logger.info when ctx is set."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)

        logger = ctx.logger
        with patch.object(logger, "info") as mock_info:
            await adapter.stop()
            # Session and adapter both log via the same logger
            mock_info.assert_any_call(
                "MeshtasticAdapter %s stopped", adapter.adapter_id
            )


# ===================================================================
# health_check edge cases
# ===================================================================


class TestHealthCheckEdgeCases:
    """MeshtasticAdapter.health_check() edge cases for 'failed' and 'unknown'."""

    async def test_session_exists_but_not_started_returns_failed(self) -> None:
        """health_check returns 'failed' when session exists but _started=False."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        # Manually set internal state: session exists but not started
        adapter._session = MagicMock()
        adapter._started = False
        adapter.ctx = None

        info = await adapter.health_check()
        assert info.health == "failed"

    async def test_session_none_returns_unknown(self) -> None:
        """health_check returns 'unknown' when session is None."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        adapter._session = None
        adapter._started = False

        info = await adapter.health_check()
        assert info.health == "unknown"


# ===================================================================
# _enrich_with_node_info exception path
# ===================================================================


class TestEnrichWithNodeInfoException:
    """_enrich_with_node_info returns None on exception, does not propagate."""

    def test_session_get_node_info_raises_returns_none(self) -> None:
        """When session.get_node_info raises, _enrich_with_node_info returns None."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)

        mock_session = MagicMock()
        mock_session.get_node_info.side_effect = RuntimeError("SDK error")
        adapter._session = mock_session

        packet = {"fromId": "!node1"}
        result = adapter._enrich_with_node_info(packet)
        assert result is None

    def test_session_get_node_info_returns_data(self) -> None:
        """_enrich_with_node_info returns data when get_node_info succeeds."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)

        mock_session = MagicMock()
        mock_session.get_node_info.return_value = {
            "longname": "TestNode",
            "shortname": "TN",
        }
        adapter._session = mock_session

        packet = {"fromId": "!node1"}
        result = adapter._enrich_with_node_info(packet)
        assert result == {"longname": "TestNode", "shortname": "TN"}

    def test_no_session_returns_none(self) -> None:
        """_enrich_with_node_info returns None when _session is None."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        adapter._session = None

        packet = {"fromId": "!node1"}
        result = adapter._enrich_with_node_info(packet)
        assert result is None

    def test_empty_from_id_returns_none(self) -> None:
        """_enrich_with_node_info returns None when fromId is empty."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)

        mock_session = MagicMock()
        adapter._session = mock_session

        packet = {"fromId": ""}
        result = adapter._enrich_with_node_info(packet)
        assert result is None
        mock_session.get_node_info.assert_not_called()


# ===================================================================
# _record_delayed_outbound_ref native_message_id guard
# ===================================================================


class TestRecordDelayedOutboundRefGuard:
    """_record_delayed_outbound_ref raises RuntimeError when native_message_id is None."""

    async def test_native_message_id_none_raises_runtime_error(self) -> None:
        """Raises RuntimeError when delivery.native_message_id is None."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)

        delivery = AdapterDeliveryResult(
            native_message_id=None,
            native_channel_id="0",
            delivery_note="test",
        )
        queue_result = QueueDeliveryResult(
            item={"payload": {"text": "hello"}},
            delivery_result=delivery,
        )

        with pytest.raises(RuntimeError, match="native_message_id must be non-None"):
            await adapter._record_delayed_outbound_ref(
                result=queue_result,
                event_id="evt-1",
                delivery=delivery,
            )


# ===================================================================
# _on_packet ctx=None guard (line 615)
# ===================================================================


class TestOnPacketCtxNoneGuard:
    """_on_packet silently drops packets when ctx is None.

    When the adapter is started but ctx has not been set (or was cleared),
    inbound packets must not proceed to classification or coroutine scheduling.
    """

    async def test_on_packet_ctx_none_no_classification(
        self, make_adapter_context
    ) -> None:
        """_on_packet returns early when _started=True but ctx=None."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        ctx = make_adapter_context("mesh-1")
        await adapter.start(ctx)

        try:
            # Simulate ctx being cleared after start (e.g. partial teardown).
            adapter.ctx = None
            adapter._started = True

            # Patch classifier to detect if classify() is called.
            with patch.object(adapter._classifier, "classify") as mock_classify:
                adapter._on_packet(
                    {
                        "fromId": "!node1",
                        "id": 1,
                        "decoded": {"portnum": "text_message"},
                    }
                )
                mock_classify.assert_not_called()
        finally:
            await adapter.stop()

    async def test_on_packet_not_started_no_classification(self) -> None:
        """_on_packet returns early when _started=False regardless of ctx."""
        config = make_meshtastic_config(connection_type="fake")
        adapter = MeshtasticAdapter(config)
        adapter._started = False
        adapter.ctx = None

        with patch.object(adapter._classifier, "classify") as mock_classify:
            adapter._on_packet(
                {"fromId": "!node1", "id": 1, "decoded": {"portnum": "text_message"}}
            )
            mock_classify.assert_not_called()
