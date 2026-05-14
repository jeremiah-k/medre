"""Matrix boundary enforcement tests: architectural separation between
core, rendering, and adapter layers; inbound/outbound correlation;
reply resolution; delivery contract; and nio response hardening.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from medre.adapters import FakeMatrixAdapter, FakePresentationAdapter
from medre.adapters.base import AdapterDeliveryResult, AdapterPermanentError, AdapterSendError
from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.matrix.codec import MatrixCodec
from medre.adapters.matrix.compat import HAS_NIO
from medre.adapters.matrix.config import MatrixConfig
from medre.adapters.matrix.errors import MatrixSendError
from medre.adapters.matrix.metadata import MatrixMetadataEnvelope
from medre.adapters.matrix.relations import MatrixRelationHandler
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.core.events import CanonicalEvent, EventMetadata, NativeRef
from medre.core.rendering.renderer import RenderingResult
from tests.fixtures.matrix_packets import (
    make_room_send_error,
    make_room_send_response,
    make_room_send_response_empty_event_id,
    make_room_send_response_none_event_id,
)


class TestMatrixBoundaries:
    """Architectural boundary enforcement for Matrix components."""

    def test_core_does_not_import_matrix(self) -> None:
        """medre.core should not import medre.adapters.matrix at module level."""
        # Import core and check matrix adapter modules are not loaded
        import medre.core  # noqa: F401

        # No core module should contain "matrix" in its name at all.
        # MatrixRenderer is now owned by the adapter package.
        core_modules = [k for k in sys.modules if k.startswith("medre.core.") and "matrix" in k]
        assert len(core_modules) == 0, (
            f"Core modules must not reference matrix: {core_modules}"
        )

    def test_matrix_does_not_import_other_adapters(self) -> None:
        """Matrix adapter package does not import other adapter modules."""
        import medre.adapters.matrix.adapter as adapter_mod
        import medre.adapters.matrix.codec as codec_mod
        import medre.adapters.matrix.renderer as renderer_mod
        import medre.adapters.matrix.config as config_mod

        for mod in (adapter_mod, codec_mod, renderer_mod, config_mod):
            assert mod.__file__ is not None
            source = open(mod.__file__).read()
            assert "meshtastic" not in source.lower(), (
                f"{mod.__name__} references meshtastic"
            )

    def test_matrix_adapter_does_not_route(self) -> None:
        """FakeMatrixAdapter has no route matching or routing methods."""
        adapter = FakeMatrixAdapter("m")
        assert not hasattr(adapter, "match")
        assert not hasattr(adapter, "route")

    def test_matrix_renderer_does_not_deliver(self) -> None:
        """MatrixRenderer has no deliver method."""
        renderer = MatrixRenderer()
        assert not hasattr(renderer, "deliver")

    def test_matrix_codec_does_not_route_or_plan(self) -> None:
        """MatrixCodec has decode but no route/match/plan methods."""
        config = MatrixConfig(
            adapter_id="test",
            homeserver="https://example.com",
            user_id="@bot:example.com",
            access_token="tok",
        )
        codec = MatrixCodec("test", config)
        assert hasattr(codec, "decode")
        assert not hasattr(codec, "route")
        assert not hasattr(codec, "match")
        assert not hasattr(codec, "plan")

    def test_matrix_renderer_lives_outside_core(self) -> None:
        """MatrixRenderer is in the adapter package, not core."""
        from medre.adapters.matrix.renderer import MatrixRenderer
        assert "adapters.matrix" in MatrixRenderer.__module__

    async def test_fake_matrix_rejects_raw_canonical_event(self) -> None:
        """FakeMatrixAdapter.deliver raises AdapterPermanentError for CanonicalEvent."""
        adapter = FakeMatrixAdapter("m")
        event = CanonicalEvent(
            event_id="evt-raw",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "raw"},
            metadata=EventMetadata(),
        )
        # Intentionally pass a CanonicalEvent to verify runtime guard.
        # Use getattr to avoid a static signature mismatch.
        deliver_fn = getattr(adapter, "deliver")
        with pytest.raises(AdapterPermanentError, match="RenderingResult only"):
            await deliver_fn(event)

    async def test_fake_presentation_rejects_raw_canonical_event(self) -> None:
        """FakePresentationAdapter.deliver raises TypeError for CanonicalEvent."""
        adapter = FakePresentationAdapter("p")
        event = CanonicalEvent(
            event_id="evt-raw",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "raw"},
            metadata=EventMetadata(),
        )
        # Intentionally pass a CanonicalEvent to verify runtime guard.
        # Use getattr to avoid a static signature mismatch.
        deliver_fn = getattr(adapter, "deliver")
        with pytest.raises(TypeError, match="RenderingResult only"):
            await deliver_fn(event)

    async def test_outbound_native_ref_uses_adapter_result_id(self) -> None:
        """Outbound native ref uses the adapter's actual result ID, not synthetic."""
        adapter = FakeMatrixAdapter("fake_matrix")
        result = RenderingResult(
            event_id="evt-out-001",
            target_adapter="fake_matrix",
            target_channel="!room:server",
            payload={"msgtype": "m.text", "body": "hello"},
            metadata={"renderer": "matrix"},
        )
        delivery = await adapter.deliver(result)
        assert isinstance(delivery, AdapterDeliveryResult)
        # Must use the adapter-provided ID, not a synthetic one
        assert delivery.native_message_id == "$fake_evt-out-001"
        assert delivery.native_channel_id == "!room:server"

    async def test_failed_matrix_delivery_does_not_store_outbound_ref(
        self,
        temp_storage,
    ) -> None:
        """Failed Matrix delivery must not store an outbound native ref."""
        from medre.adapters.fake_presentation import FaultyPresentationAdapter
        from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
        from medre.core.events.bus import EventBus
        from medre.core.planning import FallbackResolver, RelationResolver
        from medre.core.routing import Route, RouteSource, RouteTarget, Router

        adapter = FaultyPresentationAdapter(
            adapter_id="failing_matrix", failure_mode="always_fail",
        )
        route = Route(
            id="fail-route",
            source=RouteSource(
                adapter="src", event_kinds=("message.created",), channel=None
            ),
            targets=[RouteTarget(adapter="failing_matrix")],
        )
        router = Router(routes=[route])
        config = PipelineConfig(
            storage=temp_storage,
            router=router,
            fallback_resolver=FallbackResolver(),
            relation_resolver=RelationResolver(storage=temp_storage),
            adapters={"failing_matrix": adapter},
            event_bus=EventBus(),
        )
        runner = PipelineRunner(config)
        await runner.start()

        event = CanonicalEvent(
            event_id="fail-out-001",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "will fail"},
            metadata=EventMetadata(),
        )

        try:
            await runner.handle_ingress(event)
            # No outbound native ref stored
            rows = await temp_storage._read_all(
                "SELECT * FROM native_message_refs WHERE event_id = ? AND direction = 'outbound'",
                ("fail-out-001",),
            )
            assert len(rows) == 0
        finally:
            await runner.stop()


# ===================================================================
# Matrix delivery hygiene tests
# ===================================================================


def _make_matrix_config(**overrides: Any) -> MatrixConfig:
    """Build a valid MatrixConfig for testing."""
    defaults: dict[str, Any] = {
        "adapter_id": "matrix-1",
        "homeserver": "https://matrix.example.com",
        "user_id": "@bot:example.com",
        "access_token": "tok",
    }
    defaults.update(overrides)
    return MatrixConfig(**defaults)


class TestMatrixDeliveryHygiene:
    """MatrixAdapter.deliver strips room_id from sent content."""

    async def test_deliver_strips_room_id_from_content(self) -> None:
        """room_id must not leak into the Matrix event content."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)

        # Mock the client to capture what room_send receives.
        sent_content: dict = {}

        class _FakeResponse:
            event_id = "$sent-evt-001"

        mock_client = MagicMock()
        mock_client.room_send = AsyncMock(return_value=_FakeResponse())
        adapter._client = mock_client

        result = RenderingResult(
            event_id="evt-1",
            target_adapter="matrix-1",
            target_channel="!room:server",
            payload={
                "msgtype": "m.text",
                "body": "hello",
                "room_id": "!room:server",
            },
        )
        await adapter.deliver(result)

        # room_send was called
        assert mock_client.room_send.called
        call_kwargs = mock_client.room_send.call_args
        sent_content = call_kwargs.kwargs.get("content") or call_kwargs[1].get("content", {})
        assert "room_id" not in sent_content
        assert sent_content["body"] == "hello"

    async def test_deliver_uses_target_channel_for_room(self) -> None:
        """target_channel is used for room selection."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)

        class _FakeResponse:
            event_id = "$sent-evt-002"

        mock_client = MagicMock()
        mock_client.room_send = AsyncMock(return_value=_FakeResponse())
        adapter._client = mock_client

        result = RenderingResult(
            event_id="evt-2",
            target_adapter="matrix-1",
            target_channel="!target:server",
            payload={"msgtype": "m.text", "body": "hello"},
        )
        await adapter.deliver(result)

        call_kwargs = mock_client.room_send.call_args
        room_id = call_kwargs.kwargs.get("room_id") or call_kwargs[1].get("room_id", "")
        assert room_id == "!target:server"

    async def test_deliver_missing_room_id_raises(self) -> None:
        """Missing room_id in both target_channel and payload raises error."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)

        mock_client = MagicMock()
        adapter._client = mock_client

        result = RenderingResult(
            event_id="evt-3",
            target_adapter="matrix-1",
            target_channel=None,
            payload={"msgtype": "m.text", "body": "hello"},
        )
        with pytest.raises((MatrixSendError, AdapterPermanentError), match="no room_id"):
            await adapter.deliver(result)

    async def test_deliver_does_not_mutate_original_payload(self) -> None:
        """Stripping room_id creates a copy; original payload is untouched."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)

        class _FakeResponse:
            event_id = "$sent-evt-003"

        mock_client = MagicMock()
        mock_client.room_send = AsyncMock(return_value=_FakeResponse())
        adapter._client = mock_client

        payload: dict[str, object] = {
            "msgtype": "m.text",
            "body": "hello",
            "room_id": "!room:server",
        }
        result = RenderingResult(
            event_id="evt-4",
            target_adapter="matrix-1",
            target_channel="!room:server",
            payload=payload,
        )
        await adapter.deliver(result)

        # Original payload still has room_id
        assert "room_id" in payload


# ===================================================================
# Extended boundary: cross-adapter import checks
# ===================================================================


class TestMatrixBoundaryCrossImports:
    """Matrix package must not import meshtastic, meshcore, or lxmf."""

    @pytest.fixture(params=[
        "medre.adapters.matrix.adapter",
        "medre.adapters.matrix.codec",
        "medre.adapters.matrix.renderer",
        "medre.adapters.matrix.config",
        "medre.adapters.matrix.relations",
        "medre.adapters.matrix.metadata",
        "medre.adapters.matrix.compat",
        "medre.adapters.matrix.errors",
    ])
    def matrix_module_file(self, request) -> str:
        """Parametrized fixture yielding the file path of each Matrix module."""
        import importlib
        mod = importlib.import_module(request.param)
        assert mod.__file__ is not None
        return mod.__file__

    def test_no_meshtastic_import(self, matrix_module_file: str) -> None:
        source = open(matrix_module_file).read()
        assert "meshtastic" not in source.lower(), (
            f"{matrix_module_file} references meshtastic"
        )

    def test_no_meshcore_import(self, matrix_module_file: str) -> None:
        source = open(matrix_module_file).read()
        assert "meshcore" not in source.lower(), (
            f"{matrix_module_file} references meshcore"
        )

    def test_no_lxmf_import(self, matrix_module_file: str) -> None:
        source = open(matrix_module_file).read()
        assert "lxmf" not in source.lower(), (
            f"{matrix_module_file} references lxmf"
        )


class TestMatrixCodecBoundaryMethods:
    """MatrixCodec must have no route/plan/deliver/store methods."""

    def test_codec_has_no_route(self) -> None:
        config = _make_matrix_config()
        codec = MatrixCodec("test", config)
        assert not hasattr(codec, "route")

    def test_codec_has_no_plan(self) -> None:
        config = _make_matrix_config()
        codec = MatrixCodec("test", config)
        assert not hasattr(codec, "plan")

    def test_codec_has_no_deliver(self) -> None:
        config = _make_matrix_config()
        codec = MatrixCodec("test", config)
        assert not hasattr(codec, "deliver")

    def test_codec_has_no_store(self) -> None:
        config = _make_matrix_config()
        codec = MatrixCodec("test", config)
        assert not hasattr(codec, "store")

    def test_renderer_has_no_deliver(self) -> None:
        renderer = MatrixRenderer()
        assert not hasattr(renderer, "deliver")

    def test_renderer_has_no_route(self) -> None:
        renderer = MatrixRenderer()
        assert not hasattr(renderer, "route")

    def test_renderer_has_no_store(self) -> None:
        renderer = MatrixRenderer()
        assert not hasattr(renderer, "store")


# ===================================================================
# Delivery hardening: nio response fixtures
# ===================================================================


class TestMatrixDeliveryNioResponseHardening:
    """MatrixAdapter.deliver handles success/error/missing-event-id correctly."""

    async def test_successful_response_returns_native_id(self) -> None:
        """Successful RoomSendResponse event_id persists as native_message_id."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)

        mock_client = MagicMock()
        mock_client.room_send = AsyncMock(
            return_value=make_room_send_response("$good-evt-001")
        )
        adapter._client = mock_client

        result = RenderingResult(
            event_id="evt-ok",
            target_adapter="matrix-1",
            target_channel="!room:server",
            payload={"msgtype": "m.text", "body": "hello"},
        )
        delivery = await adapter.deliver(result)
        assert isinstance(delivery, AdapterDeliveryResult)
        assert delivery.native_message_id == "$good-evt-001"
        assert delivery.native_channel_id == "!room:server"

    async def test_error_response_raises_matrix_send_error(self) -> None:
        """nio error response raises MatrixSendError."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)

        mock_client = MagicMock()
        mock_client.room_send = AsyncMock(
            return_value=make_room_send_error("M_FORBIDDEN")
        )
        adapter._client = mock_client

        result = RenderingResult(
            event_id="evt-err",
            target_adapter="matrix-1",
            target_channel="!room:server",
            payload={"msgtype": "m.text", "body": "hello"},
        )
        with pytest.raises((MatrixSendError, AdapterPermanentError), match="M_FORBIDDEN"):
            await adapter.deliver(result)

    async def test_none_event_id_raises_matrix_send_error(self) -> None:
        """event_id=None is treated as a failed delivery."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)

        mock_client = MagicMock()
        mock_client.room_send = AsyncMock(
            return_value=make_room_send_response_none_event_id()
        )
        adapter._client = mock_client

        result = RenderingResult(
            event_id="evt-none",
            target_adapter="matrix-1",
            target_channel="!room:server",
            payload={"msgtype": "m.text", "body": "hello"},
        )
        with pytest.raises((MatrixSendError, AdapterPermanentError), match="empty/missing event_id"):
            await adapter.deliver(result)

    async def test_empty_event_id_raises_matrix_send_error(self) -> None:
        """event_id='' is treated as a failed delivery."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)

        mock_client = MagicMock()
        mock_client.room_send = AsyncMock(
            return_value=make_room_send_response_empty_event_id()
        )
        adapter._client = mock_client

        result = RenderingResult(
            event_id="evt-empty",
            target_adapter="matrix-1",
            target_channel="!room:server",
            payload={"msgtype": "m.text", "body": "hello"},
        )
        with pytest.raises((MatrixSendError, AdapterPermanentError), match="empty/missing event_id"):
            await adapter.deliver(result)

    async def test_client_not_connected_raises(self) -> None:
        """deliver raises AdapterPermanentError when client is None —
        lifecycle state missing is permanent."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)
        adapter._client = None

        result = RenderingResult(
            event_id="evt-no-client",
            target_adapter="matrix-1",
            target_channel="!room:server",
            payload={"msgtype": "m.text", "body": "hello"},
        )
        with pytest.raises(AdapterPermanentError, match="not connected"):
            await adapter.deliver(result)

    async def test_delivery_without_target_room_fails(self) -> None:
        """Missing target room raises MatrixSendError."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)

        mock_client = MagicMock()
        adapter._client = mock_client

        result = RenderingResult(
            event_id="evt-no-room",
            target_adapter="matrix-1",
            target_channel=None,
            payload={"msgtype": "m.text", "body": "hello"},
        )
        with pytest.raises((MatrixSendError, AdapterPermanentError), match="no room_id"):
            await adapter.deliver(result)

    async def test_failed_delivery_does_not_persist_native_ref(self) -> None:
        """When deliver() raises MatrixSendError, no native ref is returned."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)

        mock_client = MagicMock()
        mock_client.room_send = AsyncMock(
            return_value=make_room_send_response_none_event_id()
        )
        adapter._client = mock_client

        result = RenderingResult(
            event_id="evt-fail-no-ref",
            target_adapter="matrix-1",
            target_channel="!room:server",
            payload={"msgtype": "m.text", "body": "hello"},
        )
        # Must raise, not silently return a bad native ref
        with pytest.raises((MatrixSendError, AdapterPermanentError)):
            await adapter.deliver(result)


class TestMatrixDeliveryIgnoreUnverifiedDevices:
    """room_send receives ignore_unverified_devices based on encryption_mode."""

    async def test_plaintext_mode_passes_false(self) -> None:
        """Plaintext mode (default) passes ignore_unverified_devices=False."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)

        class _FakeResponse:
            event_id = "$evt-iud-default"

        mock_client = MagicMock()
        mock_client.room_send = AsyncMock(return_value=_FakeResponse())
        adapter._client = mock_client

        result = RenderingResult(
            event_id="evt-iud-default",
            target_adapter="matrix-1",
            target_channel="!room:server",
            payload={"msgtype": "m.text", "body": "hello"},
        )
        await adapter.deliver(result)

        call_kwargs = mock_client.room_send.call_args
        assert call_kwargs.kwargs.get("ignore_unverified_devices") is False

    async def test_e2ee_mode_passes_true(self) -> None:
        """E2EE mode passes ignore_unverified_devices=True internally."""
        config = _make_matrix_config(
            encryption_mode="e2ee_required",
        )
        adapter = MatrixAdapter(config)

        class _FakeResponse:
            event_id = "$evt-iud-true"

        mock_client = MagicMock()
        mock_client.room_send = AsyncMock(return_value=_FakeResponse())
        adapter._client = mock_client

        result = RenderingResult(
            event_id="evt-iud-true",
            target_adapter="matrix-1",
            target_channel="!room:server",
            payload={"msgtype": "m.text", "body": "hello"},
        )
        await adapter.deliver(result)

        call_kwargs = mock_client.room_send.call_args
        assert call_kwargs.kwargs.get("ignore_unverified_devices") is True


class TestMatrixCancelledErrorPropagation:
    """CancelledError must propagate through MatrixAdapter.deliver()."""

    async def test_cancelled_error_propagates(self) -> None:
        """CancelledError raised during room_send propagates, not swallowed."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)

        mock_client = MagicMock()
        mock_client.room_send = AsyncMock(side_effect=asyncio.CancelledError())
        adapter._client = mock_client

        result = RenderingResult(
            event_id="evt-cancel",
            target_adapter="matrix-1",
            target_channel="!room:server",
            payload={"msgtype": "m.text", "body": "hello"},
        )
        with pytest.raises(asyncio.CancelledError):
            await adapter.deliver(result)

    async def test_cancelled_error_not_converted_to_permanent(self) -> None:
        """CancelledError must not be caught by the broad handler and
        converted to AdapterPermanentError."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)

        mock_client = MagicMock()
        mock_client.room_send = AsyncMock(side_effect=asyncio.CancelledError())
        adapter._client = mock_client

        result = RenderingResult(
            event_id="evt-cancel-not-perm",
            target_adapter="matrix-1",
            target_channel="!room:server",
            payload={"msgtype": "m.text", "body": "hello"},
        )
        with pytest.raises(asyncio.CancelledError):
            await adapter.deliver(result)
        # Verify no permanent failure was recorded — CancelledError
        # bypasses the transient/permanent counters entirely.
        assert adapter._permanent_delivery_failures == 0

    async def test_not_connected_is_permanent(self) -> None:
        """Client-not-connected raises AdapterPermanentError — lifecycle state."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)
        adapter._client = None

        result = RenderingResult(
            event_id="evt-permanent-nc",
            target_adapter="matrix-1",
            target_channel="!room:server",
            payload={"msgtype": "m.text", "body": "hello"},
        )
        with pytest.raises(AdapterPermanentError, match="not connected"):
            await adapter.deliver(result)

    async def test_matrix_send_error_converted_to_transient(self) -> None:
        """MatrixSendError is converted to AdapterSendError(transient=True) at boundary."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)

        mock_client = MagicMock()
        mock_client.room_send = AsyncMock(
            side_effect=MatrixSendError("forbidden: rejected")
        )
        adapter._client = mock_client

        result = RenderingResult(
            event_id="evt-send-err",
            target_adapter="matrix-1",
            target_channel="!room:server",
            payload={"msgtype": "m.text", "body": "hello"},
        )
        with pytest.raises(AdapterSendError) as exc_info:
            await adapter.deliver(result)
        assert exc_info.value.transient is True
