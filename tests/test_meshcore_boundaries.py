"""Boundary tests enforcing isolation between core, Matrix, Meshtastic,
LXMF, and MeshCore.

These tests verify:
- Core does not import MeshCore adapter
- MeshCore does not import Matrix/Meshtastic/LXMF
- MeshCore codec/classifier remain pure
- MeshCore renderer does not deliver
- MeshCore adapter does not route/plan/render
- MeshCore adapter rejects raw CanonicalEvent delivery
- Inbound MeshCore native refs persist through pipeline
- Outbound MeshCore native refs use adapter-provided IDs
- Failed MeshCore delivery does not create native refs
- Strict source scanning: no cross-platform imports in MeshCore modules
- compat.py does not leak SDK imports
- config.py does not import SDK or other adapters
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone

import pytest

from medre.adapters.fakes.meshcore import FakeMeshCoreAdapter
from medre.adapters.meshcore.adapter import MeshCoreAdapter
from medre.adapters.meshcore.codec import MeshCoreCodec
from medre.adapters.meshcore.compat import HAS_MESHCORE
from medre.adapters.meshcore.errors import MeshCoreSendError
from medre.adapters.meshcore.renderer import MeshCoreRenderer
from medre.config.adapters.meshcore import MeshCoreConfig
from medre.core.contracts.adapter import AdapterPermanentError, AdapterSendError
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.rendering.renderer import RenderingContext, RenderingResult


def _read_module_source(module) -> str:
    """Read the source file of a loaded module."""
    with open(module.__file__) as f:
        return f.read()


# ===================================================================
# Core ↔ MeshCore isolation
# ===================================================================


class TestCoreMeshCoreIsolation:
    """Core does not import MeshCore adapter code."""

    def test_core_events_does_not_import_meshcore(self) -> None:
        """medre.core.events has no meshcore references."""
        import medre.core.events as events_mod

        source = _read_module_source(events_mod)
        assert "meshcore" not in source.lower()

    def test_core_rendering_does_not_import_meshcore(self) -> None:
        """medre.core.rendering.renderer has no meshcore references."""
        import medre.core.rendering.renderer as renderer_mod

        source = _read_module_source(renderer_mod)
        assert "meshcore" not in source.lower()

    def test_core_engine_does_not_import_meshcore(self) -> None:
        """medre.core.engine.pipeline has no meshcore references."""
        import medre.core.engine.pipeline as pipeline_mod

        source = _read_module_source(pipeline_mod)
        assert "meshcore" not in source.lower()


# ===================================================================
# MeshCore ↔ Matrix isolation (source scanning)
# ===================================================================


class TestMeshCoreMatrixIsolation:
    """MeshCore adapter does not import Matrix adapter code."""

    def test_meshcore_adapter_does_not_import_matrix(self) -> None:
        import medre.adapters.meshcore.adapter as mod

        source = _read_module_source(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "matrix" not in line.lower()
            ), f"MeshCore adapter must not import Matrix code; found: {line!r}"

    def test_meshcore_codec_does_not_import_matrix(self) -> None:
        import medre.adapters.meshcore.codec as mod

        source = _read_module_source(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "matrix" not in line.lower()
            ), f"MeshCore codec must not import Matrix code; found: {line!r}"

    def test_meshcore_renderer_does_not_import_matrix(self) -> None:
        import medre.adapters.meshcore.renderer as mod

        source = _read_module_source(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "matrix" not in line.lower()
            ), f"MeshCore renderer must not import Matrix code; found: {line!r}"

    def test_meshcore_packet_classifier_does_not_import_matrix(self) -> None:
        import medre.adapters.meshcore.packet_classifier as mod

        source = _read_module_source(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "matrix" not in line.lower()
            ), f"Packet classifier must not import Matrix code; found: {line!r}"

    def test_meshcore_config_does_not_import_matrix(self) -> None:
        import medre.config.adapters.meshcore as mod

        source = _read_module_source(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "matrix" not in line.lower()
            ), f"MeshCore config must not import Matrix code; found: {line!r}"

    def test_meshcore_compat_does_not_import_matrix(self) -> None:
        import medre.adapters.meshcore.compat as mod

        source = _read_module_source(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "matrix" not in line.lower()
            ), f"MeshCore compat must not import Matrix code; found: {line!r}"


# ===================================================================
# MeshCore ↔ Meshtastic isolation (source scanning)
# ===================================================================


class TestMeshCoreMeshtasticIsolation:
    """MeshCore adapter does not import Meshtastic adapter code."""

    def test_meshcore_adapter_does_not_import_meshtastic(self) -> None:
        import medre.adapters.meshcore.adapter as mod

        source = _read_module_source(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "meshtastic" not in line.lower()
            ), f"MeshCore adapter must not import Meshtastic code; found: {line!r}"

    def test_meshcore_codec_does_not_import_meshtastic(self) -> None:
        import medre.adapters.meshcore.codec as mod

        source = _read_module_source(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "meshtastic" not in line.lower()
            ), f"MeshCore codec must not import Meshtastic code; found: {line!r}"

    def test_meshcore_renderer_does_not_import_meshtastic(self) -> None:
        import medre.adapters.meshcore.renderer as mod

        source = _read_module_source(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "meshtastic" not in line.lower()
            ), f"MeshCore renderer must not import Meshtastic code; found: {line!r}"

    def test_meshcore_classifier_does_not_import_meshtastic(self) -> None:
        import medre.adapters.meshcore.packet_classifier as mod

        source = _read_module_source(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "meshtastic" not in line.lower()
            ), f"Packet classifier must not import Meshtastic code; found: {line!r}"

    def test_fake_meshcore_does_not_import_meshtastic(self) -> None:
        import medre.adapters.fakes.meshcore as mod

        source = _read_module_source(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "meshtastic" not in line.lower()
            ), f"FakeMeshCoreAdapter must not import Meshtastic code; found: {line!r}"

    def test_meshcore_config_does_not_import_meshtastic(self) -> None:
        import medre.config.adapters.meshcore as mod

        source = _read_module_source(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "meshtastic" not in line.lower()
            ), f"MeshCore config must not import Meshtastic code; found: {line!r}"

    def test_meshcore_compat_does_not_import_meshtastic(self) -> None:
        import medre.adapters.meshcore.compat as mod

        source = _read_module_source(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "meshtastic" not in line.lower()
            ), f"MeshCore compat must not import Meshtastic code; found: {line!r}"


# ===================================================================
# MeshCore ↔ LXMF isolation (source scanning)
# ===================================================================


class TestMeshCoreLxmfIsolation:
    """MeshCore adapter does not import LXMF adapter code."""

    def test_meshcore_adapter_does_not_import_lxmf(self) -> None:
        import medre.adapters.meshcore.adapter as mod

        source = _read_module_source(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "lxmf" not in line.lower()
            ), f"MeshCore adapter must not import LXMF code; found: {line!r}"

    def test_meshcore_codec_does_not_import_lxmf(self) -> None:
        import medre.adapters.meshcore.codec as mod

        source = _read_module_source(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "lxmf" not in line.lower()
            ), f"MeshCore codec must not import LXMF code; found: {line!r}"

    def test_meshcore_renderer_does_not_import_lxmf(self) -> None:
        import medre.adapters.meshcore.renderer as mod

        source = _read_module_source(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "lxmf" not in line.lower()
            ), f"MeshCore renderer must not import LXMF code; found: {line!r}"

    def test_meshcore_classifier_does_not_import_lxmf(self) -> None:
        import medre.adapters.meshcore.packet_classifier as mod

        source = _read_module_source(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "lxmf" not in line.lower()
            ), f"Packet classifier must not import LXMF code; found: {line!r}"

    def test_fake_meshcore_does_not_import_lxmf(self) -> None:
        import medre.adapters.fakes.meshcore as mod

        source = _read_module_source(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "lxmf" not in line.lower()
            ), f"FakeMeshCoreAdapter must not import LXMF code; found: {line!r}"


# ===================================================================
# Codec does not route/plan/deliver (source scanning)
# ===================================================================


class TestMeshCoreCodecIsolation:
    """MeshCore codec is a pure decoder — no routing, planning, or delivery."""

    def test_codec_has_no_route_methods(self) -> None:
        codec = MeshCoreCodec("meshcore-1", MeshCoreConfig(adapter_id="meshcore-1"))
        assert not hasattr(codec, "route")
        assert not hasattr(codec, "plan")
        assert not hasattr(codec, "deliver")
        assert not hasattr(codec, "publish")

    def test_codec_decode_returns_canonical_event(self) -> None:
        codec = MeshCoreCodec("meshcore-1", MeshCoreConfig(adapter_id="meshcore-1"))
        packet = {
            "text": "hello",
            "pubkey_prefix": "abc123",
            "sender_timestamp": 1,
            "type": "PRIV",
            "txt_type": 0,
        }
        event = codec.decode(packet)
        assert isinstance(event, CanonicalEvent)

    def test_codec_does_not_import_routing(self) -> None:
        import medre.adapters.meshcore.codec as mod

        source = _read_module_source(mod)
        assert "routing" not in source
        assert "Router" not in source
        assert "planning" not in source
        assert "storage" not in source

    def test_codec_source_has_no_route_or_deliver_definitions(self) -> None:
        import medre.adapters.meshcore.codec as mod

        source = _read_module_source(mod)
        method_defs = re.findall(r"def\s+(\w+)", source)
        assert "route" not in method_defs
        assert "deliver" not in method_defs
        assert "plan" not in method_defs
        assert "store" not in method_defs


# ===================================================================
# Renderer does not deliver (source scanning)
# ===================================================================


def _make_renderer() -> MeshCoreRenderer:
    """Create a MeshCoreRenderer with a default config for boundary tests."""
    cfg = MeshCoreConfig(adapter_id="meshcore_node")
    return MeshCoreRenderer(configs={"meshcore_node": cfg})


class TestMeshCoreRendererIsolation:
    """MeshCore renderer does not deliver."""

    def test_renderer_has_no_deliver_method(self) -> None:
        renderer = _make_renderer()
        assert not hasattr(renderer, "deliver")

    def test_renderer_has_no_send_method(self) -> None:
        renderer = _make_renderer()
        assert not hasattr(renderer, "send")

    def test_renderer_source_does_not_import_deliver(self) -> None:
        import medre.adapters.meshcore.renderer as mod

        source = _read_module_source(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "deliver" not in line.lower()
            ), f"Renderer must not import delivery code; found: {line!r}"

    async def test_renderer_returns_rendering_result_not_delivery(self) -> None:
        renderer = _make_renderer()
        event = CanonicalEvent(
            event_id="evt-1",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="meshcore-1",
            source_transport_id="abc123",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "test"},
            metadata=EventMetadata(),
        )
        result = await renderer.render(event, RenderingContext(target_adapter="meshcore_node", delivery_strategy="direct"))
        assert isinstance(result, RenderingResult)
        assert not isinstance(result, CanonicalEvent)


# ===================================================================
# Adapter does not route/plan/render
# ===================================================================


class TestMeshCoreAdapterIsolation:
    """MeshCore adapter does not route, plan, or render."""

    def test_adapter_has_no_route_method(self) -> None:
        config = MeshCoreConfig(adapter_id="meshcore-1")
        adapter = MeshCoreAdapter(config)
        assert not hasattr(adapter, "route")

    def test_adapter_has_no_plan_method(self) -> None:
        config = MeshCoreConfig(adapter_id="meshcore-1")
        adapter = MeshCoreAdapter(config)
        assert not hasattr(adapter, "plan")

    def test_adapter_has_no_render_method(self) -> None:
        config = MeshCoreConfig(adapter_id="meshcore-1")
        adapter = MeshCoreAdapter(config)
        assert not hasattr(adapter, "render")


# ===================================================================
# Adapter rejects CanonicalEvent delivery
# ===================================================================


class TestMeshCoreAdapterDeliveryBoundary:
    """Adapter rejects raw CanonicalEvent delivery."""

    async def test_fake_meshcore_rejects_canonical_event(self) -> None:
        adapter = FakeMeshCoreAdapter()
        event = CanonicalEvent(
            event_id="evt-1",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="meshcore-1",
            source_transport_id="abc123",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "hello"},
            metadata=EventMetadata(),
        )
        with pytest.raises(
            (TypeError, AdapterPermanentError), match="RenderingResult only"
        ):
            await adapter.deliver(event)

    async def test_real_meshcore_rejects_canonical_event(self) -> None:
        config = MeshCoreConfig(adapter_id="meshcore-1")
        adapter = MeshCoreAdapter(config)
        event = CanonicalEvent(
            event_id="evt-1",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="meshcore-1",
            source_transport_id="abc123",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "hello"},
            metadata=EventMetadata(),
        )
        with pytest.raises(
            (TypeError, AdapterPermanentError), match="RenderingResult only"
        ):
            await adapter.deliver(event)


# ===================================================================
# Inbound native refs persist through pipeline
# ===================================================================


class TestMeshCoreNativeRefPersistence:
    """Inbound MeshCore native refs persist through simulation."""

    async def test_inbound_native_ref_preserved(
        self, make_adapter_context, inbound_collector
    ) -> None:
        config = MeshCoreConfig(adapter_id="meshcore-native")
        adapter = FakeMeshCoreAdapter(config)
        ctx = make_adapter_context("meshcore-native")
        await adapter.start(ctx)

        packet = {
            "text": "native ref test",
            "pubkey_prefix": "sender1",
            "sender_timestamp": 99999,
            "type": "CHAN",
            "channel_idx": 3,
            "txt_type": 0,
        }
        await adapter.simulate_inbound(packet)

        event = inbound_collector.events[0]
        assert event.source_native_ref is not None
        assert event.source_native_ref.adapter == "meshcore-native"
        assert event.source_native_ref.native_channel_id == "3"
        assert event.source_native_ref.native_message_id == "99999"


# ===================================================================
# Outbound native refs use adapter-provided IDs
# ===================================================================


class TestMeshCoreOutboundNativeRefs:
    """Outbound delivery uses adapter-provided IDs, not fabricated ones."""

    async def test_fake_adapter_returns_delivery_result_with_native_id(self) -> None:
        """Fake adapter returns AdapterDeliveryResult with deterministic native_message_id."""
        config = MeshCoreConfig(adapter_id="meshcore-1")
        adapter = FakeMeshCoreAdapter(config)
        result = RenderingResult(
            event_id="evt-1",
            target_adapter="meshcore-1",
            target_channel="0",
            payload={"text": "test", "channel_index": 0},
        )
        delivery = await adapter.deliver(result)
        assert delivery is not None
        assert delivery.native_message_id is not None
        assert delivery.native_channel_id == "0"

    async def test_real_adapter_returns_none_in_tranche1(self) -> None:
        """Real MeshCoreAdapter.deliver() returns None (scaffolded)."""
        config = MeshCoreConfig(adapter_id="meshcore-1")
        adapter = MeshCoreAdapter(config)
        result = RenderingResult(
            event_id="evt-1",
            target_adapter="meshcore-1",
            target_channel="0",
            payload={"text": "test", "channel_index": 0},
        )
        delivery = await adapter.deliver(result)
        # Real adapter is scaffolded — returns None (no outbound native refs)
        assert delivery is None


# ===================================================================
# Failed delivery does not create native refs
# ===================================================================


class TestMeshCoreFailedDelivery:
    """Failed MeshCore delivery does not create native refs."""

    async def test_fake_adapter_failure_raises_and_no_native_ref(self) -> None:
        """Fake adapter failure raises AdapterSendError, no native ref persisted."""
        from medre.core.contracts.adapter import AdapterSendError

        adapter = FakeMeshCoreAdapter()
        adapter.set_deliver_failure(True)
        result = RenderingResult(
            event_id="evt-fail",
            target_adapter="meshcore-1",
            target_channel="0",
            payload={"text": "test", "channel_index": 0},
        )
        with pytest.raises(AdapterSendError):
            await adapter.deliver(result)
        # No packets sent through fake client
        assert adapter.fake_client.sent_count == 0


# ===================================================================
# Compat guard isolation
# ===================================================================


class TestMeshCoreCompatIsolation:
    """compat.py does not leak SDK imports into other modules."""

    def test_compat_exposes_has_meshcore_bool(self) -> None:
        assert isinstance(HAS_MESHCORE, bool)

    def test_compat_is_only_module_with_meshcore_import(self) -> None:
        """Only compat.py should import the meshcore SDK package."""
        meshcore_modules = [
            "medre.adapters.meshcore.adapter",
            "medre.adapters.meshcore.codec",
            "medre.config.adapters.meshcore",
            "medre.adapters.meshcore.errors",
            "medre.adapters.meshcore.packet_classifier",
            "medre.adapters.meshcore.renderer",
        ]
        for mod_name in meshcore_modules:
            mod = sys.modules.get(mod_name)
            if mod is None:
                continue
            source = _read_module_source(mod)
            # Look for bare "import meshcore" (not compat)
            lines = [
                line.strip()
                for line in source.splitlines()
                if line.strip().startswith(("import ", "from "))
            ]
            for line in lines:
                # Allow "from medre.adapters.meshcore.compat import HAS_MESHCORE"
                if "meshcore.compat" in line:
                    continue
                # Allow "from medre.adapters.meshcore.X import Y"
                if "medre.adapters.meshcore" in line:
                    continue
                # Reject bare "import meshcore" or "from meshcore import ..."
                assert not (
                    line.startswith("import meshcore")
                    or line.startswith("from meshcore ")
                ), (
                    f"{mod_name} must not import meshcore SDK directly "
                    f"(use compat.py); found: {line!r}"
                )

    def test_config_does_not_import_compat_or_sdk(self) -> None:
        """config.py must not import compat or SDK — it is pure validation."""
        import medre.config.adapters.meshcore as mod

        source = _read_module_source(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "compat" not in line
            ), f"config must not import compat; found: {line!r}"
            assert (
                "meshcore" not in line.lower()
                or "medre.adapters.meshcore.errors" in line
                or "medre.config.adapters.errors" in line
            ), f"config must not import meshcore SDK; found: {line!r}"

    def test_classifier_does_not_import_compat(self) -> None:
        """packet_classifier is pure — no compat import."""
        import medre.adapters.meshcore.packet_classifier as mod

        source = _read_module_source(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "compat" not in line
            ), f"classifier must not import compat; found: {line!r}"


# ===================================================================
# Error classification at adapter boundary
# ===================================================================


class TestMeshCoreErrorClassification:
    """MeshCore adapter maps session errors to AdapterSendError/AdapterPermanentError."""

    @pytest.mark.asyncio
    async def test_not_connected_maps_to_permanent(self) -> None:
        """MeshCoreSendError(transient=False) maps to AdapterPermanentError."""
        from unittest.mock import AsyncMock, MagicMock

        config = MeshCoreConfig(
            adapter_id="mesh-ec", connection_type="tcp", host="localhost", port=5000
        )
        adapter = MeshCoreAdapter(config)
        adapter._started = True

        session = MagicMock()
        session.send_text = AsyncMock(
            side_effect=MeshCoreSendError("Session is not connected", transient=False)
        )
        adapter._session = session

        result = RenderingResult(
            event_id="evt-1",
            target_adapter="mesh-ec",
            target_channel="0",
            payload={"text": "hello", "contact_id": "abc123", "channel_index": 0},
        )
        with pytest.raises(AdapterPermanentError, match="not connected"):
            await adapter.deliver(result)

    @pytest.mark.asyncio
    async def test_transient_session_error_maps_to_send_error(self) -> None:
        """MeshCoreSendError(transient=True) maps to AdapterSendError."""
        from unittest.mock import AsyncMock, MagicMock

        config = MeshCoreConfig(
            adapter_id="mesh-ec", connection_type="tcp", host="localhost", port=5000
        )
        adapter = MeshCoreAdapter(config)
        adapter._started = True

        session = MagicMock()
        session.send_text = AsyncMock(
            side_effect=MeshCoreSendError("timeout", transient=True)
        )
        adapter._session = session

        result = RenderingResult(
            event_id="evt-2",
            target_adapter="mesh-ec",
            target_channel="0",
            payload={"text": "hello", "contact_id": "abc123", "channel_index": 0},
        )
        with pytest.raises(AdapterSendError) as exc_info:
            await adapter.deliver(result)
        assert exc_info.value.transient is True

    @pytest.mark.asyncio
    async def test_oserror_maps_to_transient(self) -> None:
        """OSError from session maps to AdapterSendError(transient=True)."""
        from unittest.mock import AsyncMock, MagicMock

        config = MeshCoreConfig(
            adapter_id="mesh-ec", connection_type="tcp", host="localhost", port=5000
        )
        adapter = MeshCoreAdapter(config)
        adapter._started = True

        session = MagicMock()
        session.send_text = AsyncMock(side_effect=OSError("serial error"))
        adapter._session = session

        result = RenderingResult(
            event_id="evt-3",
            target_adapter="mesh-ec",
            target_channel="0",
            payload={"text": "hello", "contact_id": "abc123", "channel_index": 0},
        )
        with pytest.raises(AdapterSendError) as exc_info:
            await adapter.deliver(result)
        assert exc_info.value.transient is True

    @pytest.mark.asyncio
    async def test_sdk_error_maps_to_permanent(self) -> None:
        """SDK-level rejection maps to AdapterPermanentError."""
        from unittest.mock import AsyncMock, MagicMock

        config = MeshCoreConfig(
            adapter_id="mesh-ec", connection_type="tcp", host="localhost", port=5000
        )
        adapter = MeshCoreAdapter(config)
        adapter._started = True

        session = MagicMock()
        session.send_text = AsyncMock(
            side_effect=MeshCoreSendError("SDK send error: invalid", transient=False)
        )
        adapter._session = session

        result = RenderingResult(
            event_id="evt-4",
            target_adapter="mesh-ec",
            target_channel="0",
            payload={"text": "hello", "contact_id": "abc123", "channel_index": 0},
        )
        with pytest.raises(AdapterPermanentError, match="SDK send error"):
            await adapter.deliver(result)
