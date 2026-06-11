"""Matrix boundary enforcement tests: architectural separation between
core, rendering, and adapter layers; inbound/outbound correlation;
reply resolution; delivery contract; and nio response hardening.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from medre.adapters.fakes.matrix import FakeMatrixAdapter
from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.adapters.matrix.adapter import MatrixAdapter
from medre.adapters.matrix.codec import MatrixCodec
from medre.adapters.matrix.errors import MatrixSendError
from medre.adapters.matrix.renderer import MatrixRenderer
from medre.config.adapters.matrix import MatrixConfig
from medre.core.contracts.adapter import (
    AdapterDeliveryResult,
    AdapterPermanentError,
    AdapterSendError,
)
from medre.core.events import CanonicalEvent, EventMetadata, EventRelation, NativeRef
from medre.core.rendering.renderer import RenderingContext, RenderingResult
from tests.fixtures.matrix_packets import (
    make_room_send_error,
    make_room_send_response,
    make_room_send_response_empty_event_id,
    make_room_send_response_none_event_id,
)
from tests.helpers.matrix_adapter import wire_mock_session as _wire_mock_session


class TestMatrixBoundaries:
    """Architectural boundary enforcement for Matrix components."""

    def test_core_does_not_import_matrix(self) -> None:
        """medre.core should not import medre.adapters.matrix at module level."""
        # Import core and check matrix adapter modules are not loaded
        import medre.core  # noqa: F401

        # No core module should contain "matrix" in its name at all.
        # MatrixRenderer is now owned by the adapter package.
        core_modules = [
            k for k in sys.modules if k.startswith("medre.core.") and "matrix" in k
        ]
        assert (
            len(core_modules) == 0
        ), f"Core modules must not reference matrix: {core_modules}"

    def test_matrix_does_not_import_other_adapters(self) -> None:
        """Matrix adapter package does not import other adapter modules."""
        import medre.adapters.matrix.adapter as adapter_mod
        import medre.adapters.matrix.codec as codec_mod
        import medre.adapters.matrix.renderer as renderer_mod
        import medre.config.adapters.matrix as config_mod

        forbidden_prefixes = (
            "from medre.adapters.meshtastic",
            "import medre.adapters.meshtastic",
            "from medre.adapters.meshcore",
            "import medre.adapters.meshcore",
            "from medre.adapters.lxmf",
            "import medre.adapters.lxmf",
        )

        for mod in (adapter_mod, codec_mod, renderer_mod, config_mod):
            assert mod.__file__ is not None
            with open(mod.__file__) as fh:
                for i, line in enumerate(fh, 1):
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#"):
                        continue
                    assert not any(
                        stripped.startswith(p) for p in forbidden_prefixes
                    ), f"{mod.__name__}:{i} imports another adapter: {stripped}"

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
        deliver_fn = adapter.deliver
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
        deliver_fn = adapter.deliver
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
        from medre.adapters.fakes.presentation import FaultyPresentationAdapter
        from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
        from medre.core.events.bus import EventBus
        from medre.core.planning import FallbackResolver, RelationResolver
        from medre.core.routing import Route, Router, RouteSource, RouteTarget

        adapter = FaultyPresentationAdapter(
            adapter_id="failing_matrix",
            failure_mode="always_fail",
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
        _wire_mock_session(adapter, mock_client, config=config)

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
        sent_content = call_kwargs.kwargs.get("content") or call_kwargs[1].get(
            "content", {}
        )
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
        _wire_mock_session(adapter, mock_client, config=config)

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
        _wire_mock_session(adapter, mock_client, config=config)

        result = RenderingResult(
            event_id="evt-3",
            target_adapter="matrix-1",
            target_channel=None,
            payload={"msgtype": "m.text", "body": "hello"},
        )
        with pytest.raises(
            (MatrixSendError, AdapterPermanentError), match="no room_id"
        ):
            await adapter.deliver(result)

    async def test_deliver_does_not_mutate_original_payload(self) -> None:
        """Stripping room_id creates a copy; original payload is untouched."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)

        class _FakeResponse:
            event_id = "$sent-evt-003"

        mock_client = MagicMock()
        mock_client.room_send = AsyncMock(return_value=_FakeResponse())
        _wire_mock_session(adapter, mock_client, config=config)

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

    @pytest.fixture(
        params=[
            "medre.adapters.matrix.adapter",
            "medre.adapters.matrix.codec",
            "medre.adapters.matrix.renderer",
            "medre.config.adapters.matrix",
            "medre.adapters.matrix.relations",
            "medre.adapters.matrix.metadata",
            "medre.adapters.matrix.compat",
            "medre.adapters.matrix.errors",
        ]
    )
    def matrix_module_file(self, request) -> str:
        """Parametrized fixture yielding the file path of each Matrix module."""
        import importlib

        mod = importlib.import_module(request.param)
        assert mod.__file__ is not None
        return mod.__file__

    def test_no_meshtastic_import(self, matrix_module_file: str) -> None:
        with open(matrix_module_file) as fh:
            for i, line in enumerate(fh, 1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                assert not (
                    stripped.startswith("from medre.adapters.meshtastic")
                    or stripped.startswith("import medre.adapters.meshtastic")
                ), f"{matrix_module_file}:{i} imports meshtastic: {stripped}"

    def test_no_meshcore_import(self, matrix_module_file: str) -> None:
        with open(matrix_module_file) as fh:
            source = fh.read()
        assert (
            "meshcore" not in source.lower()
        ), f"{matrix_module_file} references meshcore"

    def test_no_lxmf_import(self, matrix_module_file: str) -> None:
        with open(matrix_module_file) as fh:
            source = fh.read()
        assert "lxmf" not in source.lower(), f"{matrix_module_file} references lxmf"


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
        _wire_mock_session(adapter, mock_client, config=config)

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

    async def test_error_response_raises_permanent(self) -> None:
        """nio error response raises AdapterPermanentError."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)

        mock_client = MagicMock()
        mock_client.room_send = AsyncMock(
            return_value=make_room_send_error("M_FORBIDDEN")
        )
        _wire_mock_session(adapter, mock_client, config=config)

        result = RenderingResult(
            event_id="evt-err",
            target_adapter="matrix-1",
            target_channel="!room:server",
            payload={"msgtype": "m.text", "body": "hello"},
        )
        with pytest.raises(AdapterPermanentError, match="M_FORBIDDEN"):
            await adapter.deliver(result)

    async def test_none_event_id_raises_permanent(self) -> None:
        """event_id=None is treated as a permanent delivery failure."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)

        mock_client = MagicMock()
        mock_client.room_send = AsyncMock(
            return_value=make_room_send_response_none_event_id()
        )
        _wire_mock_session(adapter, mock_client, config=config)

        result = RenderingResult(
            event_id="evt-none",
            target_adapter="matrix-1",
            target_channel="!room:server",
            payload={"msgtype": "m.text", "body": "hello"},
        )
        with pytest.raises(AdapterPermanentError, match="empty/missing event_id"):
            await adapter.deliver(result)

    async def test_empty_event_id_raises_permanent(self) -> None:
        """event_id='' is treated as a permanent delivery failure."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)

        mock_client = MagicMock()
        mock_client.room_send = AsyncMock(
            return_value=make_room_send_response_empty_event_id()
        )
        _wire_mock_session(adapter, mock_client, config=config)

        result = RenderingResult(
            event_id="evt-empty",
            target_adapter="matrix-1",
            target_channel="!room:server",
            payload={"msgtype": "m.text", "body": "hello"},
        )
        with pytest.raises(AdapterPermanentError, match="empty/missing event_id"):
            await adapter.deliver(result)

    async def test_client_not_connected_raises(self) -> None:
        """deliver raises AdapterPermanentError when session is not initialized —
        lifecycle state missing is permanent."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)

        result = RenderingResult(
            event_id="evt-no-client",
            target_adapter="matrix-1",
            target_channel="!room:server",
            payload={"msgtype": "m.text", "body": "hello"},
        )
        with pytest.raises(AdapterPermanentError, match="session is not initialized"):
            await adapter.deliver(result)

    async def test_delivery_without_target_room_fails(self) -> None:
        """Missing target room raises AdapterPermanentError."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)

        mock_client = MagicMock()
        _wire_mock_session(adapter, mock_client, config=config)

        result = RenderingResult(
            event_id="evt-no-room",
            target_adapter="matrix-1",
            target_channel=None,
            payload={"msgtype": "m.text", "body": "hello"},
        )
        with pytest.raises(AdapterPermanentError, match="no room_id"):
            await adapter.deliver(result)

    async def test_failed_delivery_does_not_persist_native_ref(self) -> None:
        """When deliver() raises AdapterPermanentError, no native ref is returned."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)

        mock_client = MagicMock()
        mock_client.room_send = AsyncMock(
            return_value=make_room_send_response_none_event_id()
        )
        _wire_mock_session(adapter, mock_client, config=config)

        result = RenderingResult(
            event_id="evt-fail-no-ref",
            target_adapter="matrix-1",
            target_channel="!room:server",
            payload={"msgtype": "m.text", "body": "hello"},
        )
        # Must raise, not silently return a bad native ref
        with pytest.raises(AdapterPermanentError):
            await adapter.deliver(result)

    async def test_duplicate_annotation_is_permanent(self) -> None:
        """M_DUPLICATE_ANNOTATION is classified as a permanent error."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)

        mock_client = MagicMock()
        mock_client.room_send = AsyncMock(
            return_value=make_room_send_error("M_DUPLICATE_ANNOTATION")
        )
        _wire_mock_session(adapter, mock_client, config=config)

        result = RenderingResult(
            event_id="evt-dup-annot",
            target_adapter="matrix-1",
            target_channel="!room:server",
            payload={"msgtype": "m.text", "body": "hello"},
        )
        with pytest.raises(AdapterPermanentError, match="M_DUPLICATE_ANNOTATION"):
            await adapter.deliver(result)

    async def test_duplicate_annotation_message_includes_errcode(self) -> None:
        """Error message for M_DUPLICATE_ANNOTATION includes the errcode."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)

        mock_client = MagicMock()
        mock_client.room_send = AsyncMock(
            return_value=make_room_send_error("M_DUPLICATE_ANNOTATION")
        )
        _wire_mock_session(adapter, mock_client, config=config)

        result = RenderingResult(
            event_id="evt-dup-annot-msg",
            target_adapter="matrix-1",
            target_channel="!room:server",
            payload={"msgtype": "m.text", "body": "hello"},
        )
        with pytest.raises(AdapterPermanentError) as exc_info:
            await adapter.deliver(result)
        assert "M_DUPLICATE_ANNOTATION" in str(exc_info.value)

    async def test_rate_limit_retry_reuses_transaction_id(self) -> None:
        """Pipeline retry of the same RenderingResult reuses Matrix tx_id."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)

        rate_limited = SimpleNamespace(
            errcode="M_LIMIT_EXCEEDED",
            status_code=429,
            retry_after_ms=250,
        )
        mock_client = MagicMock()
        mock_client.room_send = AsyncMock(
            side_effect=[
                rate_limited,
                make_room_send_response("$rate-limit-retry-ok"),
            ]
        )
        _wire_mock_session(adapter, mock_client, config=config)

        result = RenderingResult(
            event_id="evt-rate-limit-retry",
            target_adapter="matrix-1",
            target_channel="!room:server",
            payload={"msgtype": "m.text", "body": "hello"},
        )

        with pytest.raises(AdapterSendError) as exc_info:
            await adapter.deliver(result)
        assert exc_info.value.transient is True
        first_tx_id = mock_client.room_send.call_args.kwargs["tx_id"]

        delivery = await adapter.deliver(result)
        second_tx_id = mock_client.room_send.call_args.kwargs["tx_id"]

        assert mock_client.room_send.await_count == 2
        assert first_tx_id == second_tx_id
        assert delivery is not None
        assert delivery.metadata["matrix"]["txn_id"] == first_tx_id


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
        _wire_mock_session(adapter, mock_client, config=config)

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
        _wire_mock_session(adapter, mock_client, config=config)

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
        _wire_mock_session(adapter, mock_client, config=config)

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
        _wire_mock_session(adapter, mock_client, config=config)

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

        result = RenderingResult(
            event_id="evt-permanent-nc",
            target_adapter="matrix-1",
            target_channel="!room:server",
            payload={"msgtype": "m.text", "body": "hello"},
        )
        with pytest.raises(AdapterPermanentError, match="session is not initialized"):
            await adapter.deliver(result)

    async def test_matrix_send_error_transient_converted_to_adapter_send_error(
        self,
    ) -> None:
        """MatrixSendError(transient=True) is converted to AdapterSendError at boundary."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)

        mock_client = MagicMock()
        mock_client.room_send = AsyncMock(
            side_effect=MatrixSendError("timeout: network", transient=True)
        )
        _wire_mock_session(adapter, mock_client, config=config)

        result = RenderingResult(
            event_id="evt-send-transient",
            target_adapter="matrix-1",
            target_channel="!room:server",
            payload={"msgtype": "m.text", "body": "hello"},
        )
        with pytest.raises(AdapterSendError) as exc_info:
            await adapter.deliver(result)
        assert exc_info.value.transient is True

    async def test_matrix_send_error_permanent_converted_to_adapter_permanent(
        self,
    ) -> None:
        """MatrixSendError(transient=False) is converted to AdapterPermanentError at boundary."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)

        mock_client = MagicMock()
        mock_client.room_send = AsyncMock(
            side_effect=MatrixSendError("forbidden: rejected", transient=False)
        )
        _wire_mock_session(adapter, mock_client, config=config)

        result = RenderingResult(
            event_id="evt-send-permanent",
            target_adapter="matrix-1",
            target_channel="!room:server",
            payload={"msgtype": "m.text", "body": "hello"},
        )
        with pytest.raises(AdapterPermanentError):
            await adapter.deliver(result)


# ===================================================================
# Matrix capabilities: edits and deletes explicitly unsupported
# ===================================================================


class TestMatrixCapabilitiesEditsDeletes:
    """Matrix adapter explicitly declares edits and deletes as unsupported.

    The Matrix protocol supports m.replace (edits) and redactions (deletes),
    but MEDRE's Matrix adapter does not implement them.  The capabilities
    must reflect this explicitly so the pipeline can degrade gracefully.
    """

    def test_edits_unsupported(self) -> None:
        """edits capability is 'unsupported'."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)
        caps = adapter._capabilities
        assert caps.edits == "unsupported"

    def test_deletes_unsupported(self) -> None:
        """deletes capability is 'unsupported'."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)
        caps = adapter._capabilities
        assert caps.deletes == "unsupported"

    def test_replies_native(self) -> None:
        """replies capability is 'native'."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)
        caps = adapter._capabilities
        assert caps.replies == "native"

    def test_reactions_native(self) -> None:
        """reactions capability is 'native'."""
        config = _make_matrix_config()
        adapter = MatrixAdapter(config)
        caps = adapter._capabilities
        assert caps.reactions == "native"

    async def test_renderer_ignores_edit_relations(self) -> None:
        """MatrixRenderer does not crash or produce malformed output for edit relations.

        Since edits are unsupported, an edit relation should not cause
        the renderer to produce a malformed payload.  The renderer
        treats unknown relation types as pass-through (no special handling).
        """
        renderer = MatrixRenderer()
        # Build an event with an edit relation (which the renderer doesn't
        # handle specially — it falls through to plain text rendering)
        edit_rel = EventRelation(
            relation_type="edit",
            target_event_id="orig-001",
            target_native_ref=NativeRef(
                adapter="matrix-1",
                native_channel_id="!room:server",
                native_message_id="$orig-native",
            ),
            key=None,
            fallback_text="original text",
        )
        event = CanonicalEvent(
            event_id="evt-edit-1",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="src",
            source_transport_id="node-1",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(edit_rel,),
            payload={"body": "edited message"},
            metadata=EventMetadata(),
        )
        rendering = await renderer.render(
            event,
            RenderingContext(target_adapter="matrix-1", delivery_strategy="direct"),
        )
        # The render coroutine must return a valid result without crashing
        # The renderer handles edit relations the same as unrecognized
        # relation types — no special m.relates_to for edits.
        assert rendering.payload["msgtype"] == "m.text"
        assert rendering.payload["body"] == "edited message"
        # No m.relates_to for edit (unsupported)
        assert "m.relates_to" not in rendering.payload


# ---------------------------------------------------------------------------
# start() failure-path cleanup tests
# ---------------------------------------------------------------------------


class TestStartFailureCleanup:
    """Ensure ALL failure paths in MatrixAdapter.start() properly clean up
    session state and set ``_started = False``.
    """

    @staticmethod
    def _make_adapter(
        *,
        auto_join_rooms: tuple[str, ...] | None = None,
    ) -> tuple[MatrixAdapter, MatrixConfig]:
        from tests.helpers.matrix_adapter import make_matrix_config

        overrides: dict[str, Any] = {}
        if auto_join_rooms is not None:
            overrides["auto_join_rooms"] = auto_join_rooms
        config = make_matrix_config(**overrides)
        return MatrixAdapter(config), config

    async def test_session_start_raises_sets_started_false(
        self, make_adapter_context: Any
    ) -> None:
        """If MatrixSession.start() raises, _started is False and _session
        is stopped."""
        import medre.adapters.matrix.adapter as _adapter_mod

        adapter, _config = self._make_adapter()
        ctx = make_adapter_context()

        mock_session = MagicMock(name="MatrixSession")
        mock_session.start = AsyncMock(side_effect=RuntimeError("boom"))
        mock_session.stop = AsyncMock()
        mock_session.closed = True
        mock_session.last_sync_error = None

        with (
            patch.object(_adapter_mod, "HAS_NIO", True),
            patch.object(_adapter_mod, "MatrixSession", return_value=mock_session),
        ):
            with pytest.raises(RuntimeError, match="boom"):
                await adapter.start(ctx)

        assert adapter._started is False

    async def test_auto_join_raises_stops_session_and_sets_started_false(
        self, make_adapter_context: Any
    ) -> None:
        """If ensure_joined_rooms() raises after a successful session.start(),
        the session is stopped, _session cleared, and _started set to False."""
        import medre.adapters.matrix.adapter as _adapter_mod

        rooms = ("!room1:example.com", "!room2:example.com")
        adapter, _config = self._make_adapter(auto_join_rooms=rooms)
        ctx = make_adapter_context()

        mock_session = MagicMock(name="MatrixSession")
        mock_session.start = AsyncMock()  # succeeds
        mock_session.stop = AsyncMock()
        mock_session.closed = False
        mock_session.connected = True
        mock_session.last_sync_error = None
        mock_session.ensure_joined_rooms = AsyncMock(
            side_effect=RuntimeError("join failed")
        )

        with (
            patch.object(_adapter_mod, "HAS_NIO", True),
            patch.object(_adapter_mod, "MatrixSession", return_value=mock_session),
        ):
            with pytest.raises(RuntimeError, match="join failed"):
                await adapter.start(ctx)

        assert adapter._started is False
        assert adapter._session is None
        mock_session.stop.assert_awaited_once()

    async def test_post_failed_start_room_callback_does_not_publish(
        self, make_adapter_context: Any, inbound_collector: Any
    ) -> None:
        """After start() fails, _on_room_message must not publish events."""
        import medre.adapters.matrix.adapter as _adapter_mod

        adapter, _config = self._make_adapter()
        ctx = make_adapter_context()

        mock_session = MagicMock(name="MatrixSession")
        mock_session.start = AsyncMock(side_effect=RuntimeError("start died"))
        mock_session.stop = AsyncMock()
        mock_session.closed = True
        mock_session.last_sync_error = None

        with (
            patch.object(_adapter_mod, "HAS_NIO", True),
            patch.object(_adapter_mod, "MatrixSession", return_value=mock_session),
        ):
            with pytest.raises(RuntimeError, match="start died"):
                await adapter.start(ctx)

        assert adapter._started is False

        # Simulate an inbound room message after the failed start.
        event_dict: dict[str, Any] = {
            "room_id": "!test:example.com",
            "sender": "@alice:example.com",
            "body": "hello",
            "event_id": "$evt-1",
            "source": {
                "content": {"msgtype": "m.text", "body": "hello"},
                "event_id": "$evt-1",
                "sender": "@alice:example.com",
                "type": "m.room.message",
            },
            "msgtype": "m.text",
            "server_timestamp": 0,
        }
        await adapter._on_room_message(event_dict)

        assert inbound_collector.events == []
