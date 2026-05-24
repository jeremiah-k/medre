"""Boundary tests enforcing isolation between core, Matrix, Meshtastic,
MeshCore, and LXMF.

These tests verify:
- Core does not import LXMF adapter or RNS/Reticulum
- LXMF does not import Matrix, Meshtastic, or MeshCore
- LXMF codec/classifier remain pure (no routing/delivery)
- LXMF renderer does not deliver
- LXMF adapter does not route/plan/render
- LXMF adapter rejects raw CanonicalEvent delivery
- LXMF compat module is the sole import site for lxmf/RNS
- Inbound LXMF native refs persist through pipeline
- Outbound LXMF native refs use adapter-provided IDs
- Failed LXMF delivery does not create native refs
- Strict source scanning: no cross-platform imports in LXMF modules
- Platform-aware rendering still passes
- Fake tests still pass
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

from medre.adapters.fakes.lxmf import FakeLxmfAdapter
from medre.adapters.lxmf.adapter import LxmfAdapter
from medre.adapters.lxmf.codec import LxmfCodec
from medre.adapters.lxmf.errors import LxmfSendError
from medre.adapters.lxmf.packet_classifier import LxmfPacketClassifier
from medre.adapters.lxmf.renderer import LxmfRenderer
from medre.config.adapters.lxmf import LxmfConfig
from medre.core.contracts.adapter import AdapterPermanentError, AdapterSendError
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.rendering.renderer import RenderingResult


def _read_module_source(module) -> str:
    """Read the source file of a loaded module."""
    with open(module.__file__) as f:
        return f.read()


# ===================================================================
# Core ↔ LXMF isolation
# ===================================================================


class TestCoreLxmfIsolation:
    """Core does not import LXMF adapter code."""

    def test_core_events_does_not_import_lxmf(self) -> None:
        """medre.core.events has no lxmf references."""
        import medre.core.events as events_mod

        source = _read_module_source(events_mod)
        assert "lxmf" not in source.lower()

    def test_core_rendering_does_not_import_lxmf(self) -> None:
        """medre.core.rendering.renderer has no lxmf references."""
        import medre.core.rendering.renderer as renderer_mod

        source = _read_module_source(renderer_mod)
        assert "lxmf" not in source.lower()

    def test_core_engine_does_not_import_lxmf(self) -> None:
        """medre.core.engine.pipeline has no lxmf references."""
        import medre.core.engine.pipeline as pipeline_mod

        source = _read_module_source(pipeline_mod)
        assert "lxmf" not in source.lower()


# ===================================================================
# LXMF ↔ Matrix isolation (source scanning)
# ===================================================================


class TestLxmfMatrixIsolation:
    """LXMF adapter does not import Matrix adapter code."""

    def test_lxmf_adapter_does_not_import_matrix(self) -> None:
        import medre.adapters.lxmf.adapter as mod

        source = _read_module_source(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "matrix" not in line.lower()
            ), f"LXMF adapter must not import Matrix code; found: {line!r}"

    def test_lxmf_codec_does_not_import_matrix(self) -> None:
        import medre.adapters.lxmf.codec as mod

        source = _read_module_source(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "matrix" not in line.lower()
            ), f"LXMF codec must not import Matrix code; found: {line!r}"

    def test_lxmf_renderer_does_not_import_matrix(self) -> None:
        import medre.adapters.lxmf.renderer as mod

        source = _read_module_source(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "matrix" not in line.lower()
            ), f"LXMF renderer must not import Matrix code; found: {line!r}"

    def test_lxmf_packet_classifier_does_not_import_matrix(self) -> None:
        import medre.adapters.lxmf.packet_classifier as mod

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


# ===================================================================
# LXMF ↔ Meshtastic ↔ MeshCore isolation (source scanning)
# ===================================================================


class TestLxmfMeshtasticMeshCoreIsolation:
    """LXMF adapter does not import Meshtastic or MeshCore code."""

    def test_lxmf_adapter_does_not_import_meshtastic(self) -> None:
        import medre.adapters.lxmf.adapter as mod

        source = _read_module_source(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "meshtastic" not in line.lower()
            ), f"LXMF adapter must not import Meshtastic code; found: {line!r}"

    def test_lxmf_adapter_does_not_import_meshcore(self) -> None:
        import medre.adapters.lxmf.adapter as mod

        source = _read_module_source(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "meshcore" not in line.lower()
            ), f"LXMF adapter must not import MeshCore code; found: {line!r}"

    def test_lxmf_codec_does_not_import_meshtastic(self) -> None:
        import medre.adapters.lxmf.codec as mod

        source = _read_module_source(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "meshtastic" not in line.lower()
            ), f"LXMF codec must not import Meshtastic code; found: {line!r}"

    def test_lxmf_codec_does_not_import_meshcore(self) -> None:
        import medre.adapters.lxmf.codec as mod

        source = _read_module_source(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "meshcore" not in line.lower()
            ), f"LXMF codec must not import MeshCore code; found: {line!r}"

    def test_lxmf_renderer_does_not_import_meshtastic(self) -> None:
        import medre.adapters.lxmf.renderer as mod

        source = _read_module_source(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "meshtastic" not in line.lower()
            ), f"LXMF renderer must not import Meshtastic code; found: {line!r}"

    def test_lxmf_renderer_does_not_import_meshcore(self) -> None:
        import medre.adapters.lxmf.renderer as mod

        source = _read_module_source(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "meshcore" not in line.lower()
            ), f"LXMF renderer must not import MeshCore code; found: {line!r}"

    def test_lxmf_classifier_does_not_import_meshtastic(self) -> None:
        import medre.adapters.lxmf.packet_classifier as mod

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

    def test_lxmf_classifier_does_not_import_meshcore(self) -> None:
        import medre.adapters.lxmf.packet_classifier as mod

        source = _read_module_source(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "meshcore" not in line.lower()
            ), f"Packet classifier must not import MeshCore code; found: {line!r}"

    def test_fake_lxmf_does_not_import_meshtastic(self) -> None:
        import medre.adapters.fakes.lxmf as mod

        source = _read_module_source(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "meshtastic" not in line.lower()
            ), f"FakeLxmfAdapter must not import Meshtastic code; found: {line!r}"

    def test_fake_lxmf_does_not_import_meshcore(self) -> None:
        import medre.adapters.fakes.lxmf as mod

        source = _read_module_source(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "meshcore" not in line.lower()
            ), f"FakeLxmfAdapter must not import MeshCore code; found: {line!r}"


# ===================================================================
# Codec does not route/plan/deliver (source scanning)
# ===================================================================


class TestLxmfCodecIsolation:
    """LXMF codec is a pure decoder — no routing, planning, or delivery."""

    def test_codec_has_no_route_methods(self) -> None:
        codec = LxmfCodec("lxmf-1", LxmfConfig(adapter_id="lxmf-1"))
        assert not hasattr(codec, "route")
        assert not hasattr(codec, "plan")
        assert not hasattr(codec, "deliver")
        assert not hasattr(codec, "publish")

    def test_codec_decode_returns_canonical_event(self) -> None:
        codec = LxmfCodec("lxmf-1", LxmfConfig(adapter_id="lxmf-1"))
        packet = {
            "content": "hello",
            "source_hash": "ab" * 16,
            "message_id": "cd" * 32,
        }
        event = codec.decode(packet)
        assert isinstance(event, CanonicalEvent)

    def test_codec_does_not_import_routing(self) -> None:
        import medre.adapters.lxmf.codec as mod

        source = _read_module_source(mod)
        assert "routing" not in source
        assert "Router" not in source
        assert "planning" not in source
        assert "storage" not in source

    def test_codec_source_has_no_route_or_deliver_definitions(self) -> None:
        import medre.adapters.lxmf.codec as mod

        source = _read_module_source(mod)
        method_defs = re.findall(r"def\s+(\w+)", source)
        assert "route" not in method_defs
        assert "deliver" not in method_defs
        assert "plan" not in method_defs
        assert "store" not in method_defs


# ===================================================================
# Renderer does not deliver (source scanning)
# ===================================================================


class TestLxmfRendererIsolation:
    """LXMF renderer does not deliver."""

    def test_renderer_has_no_deliver_method(self) -> None:
        renderer = LxmfRenderer()
        assert not hasattr(renderer, "deliver")

    def test_renderer_has_no_send_method(self) -> None:
        renderer = LxmfRenderer()
        assert not hasattr(renderer, "send")

    def test_renderer_source_does_not_import_deliver(self) -> None:
        import medre.adapters.lxmf.renderer as mod

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
        renderer = LxmfRenderer()
        event = CanonicalEvent(
            event_id="evt-1",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="lxmf-1",
            source_transport_id="ab" * 16,
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "test"},
            metadata=EventMetadata(),
        )
        result = await renderer.render(event, "lxmf_node")
        assert isinstance(result, RenderingResult)
        assert not isinstance(result, CanonicalEvent)


# ===================================================================
# Adapter does not route/plan/render
# ===================================================================


class TestLxmfAdapterIsolation:
    """LXMF adapter does not route, plan, or render."""

    def test_adapter_has_no_route_method(self) -> None:
        config = LxmfConfig(adapter_id="lxmf-1")
        adapter = LxmfAdapter(config)
        assert not hasattr(adapter, "route")

    def test_adapter_has_no_plan_method(self) -> None:
        config = LxmfConfig(adapter_id="lxmf-1")
        adapter = LxmfAdapter(config)
        assert not hasattr(adapter, "plan")

    def test_adapter_has_no_render_method(self) -> None:
        config = LxmfConfig(adapter_id="lxmf-1")
        adapter = LxmfAdapter(config)
        assert not hasattr(adapter, "render")


# ===================================================================
# Adapter rejects CanonicalEvent delivery
# ===================================================================


class TestLxmfAdapterDeliveryBoundary:
    """Adapter rejects raw CanonicalEvent delivery."""

    async def test_fake_lxmf_rejects_canonical_event(self) -> None:
        adapter = FakeLxmfAdapter()
        event = CanonicalEvent(
            event_id="evt-1",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="lxmf-1",
            source_transport_id="ab" * 16,
            source_channel_id=None,
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

    async def test_real_lxmf_rejects_canonical_event(self) -> None:
        config = LxmfConfig(adapter_id="lxmf-1")
        adapter = LxmfAdapter(config)
        event = CanonicalEvent(
            event_id="evt-1",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="lxmf-1",
            source_transport_id="ab" * 16,
            source_channel_id=None,
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


class TestLxmfNativeRefPersistence:
    """Inbound LXMF native refs persist through simulation."""

    async def test_inbound_native_ref_preserved(
        self, make_adapter_context, inbound_collector
    ) -> None:
        config = LxmfConfig(adapter_id="lxmf-native")
        adapter = FakeLxmfAdapter(config)
        ctx = make_adapter_context("lxmf-native")
        await adapter.start(ctx)

        packet = {
            "source_hash": "ab" * 16,
            "destination_hash": "00" * 16,
            "message_id": "aa" * 32,
            "timestamp": 1700000000.0,
            "title": "",
            "content": "native ref test",
            "fields": {},
            "signature_validated": True,
        }
        await adapter.simulate_inbound(packet)

        event = inbound_collector.events[0]
        assert event.source_native_ref is not None
        assert event.source_native_ref.adapter == "lxmf-native"
        assert event.source_native_ref.native_channel_id is None
        assert event.source_native_ref.native_message_id == "aa" * 32


# ===================================================================
# Outbound native refs use adapter-provided IDs
# ===================================================================


class TestLxmfOutboundNativeRefs:
    """Outbound delivery uses adapter-provided IDs."""

    async def test_fake_adapter_returns_delivery_result_with_native_id(self) -> None:
        """Fake adapter returns AdapterDeliveryResult with native_message_id."""
        config = LxmfConfig(adapter_id="lxmf-1")
        adapter = FakeLxmfAdapter(config)
        result = RenderingResult(
            event_id="evt-1",
            target_adapter="lxmf-1",
            target_channel=None,
            payload={"content": "test", "title": "", "fields": {}},
        )
        delivery = await adapter.deliver(result)
        assert delivery is not None
        assert delivery.native_message_id is not None
        assert delivery.native_channel_id is None

    async def test_real_adapter_returns_none_in_tranche1(self) -> None:
        """Real LxmfAdapter.deliver() now returns AdapterDeliveryResult via session.

        With session integration, deliver() delegates to LxmfSession.send_text()
        which requires the adapter to be started first.  Without start,
        deliver raises AdapterPermanentError.
        """
        config = LxmfConfig(adapter_id="lxmf-1", connection_type="fake")
        adapter = LxmfAdapter(config)
        # Without start, deliver raises because adapter not started — lifecycle state.
        result = RenderingResult(
            event_id="evt-1",
            target_adapter="lxmf-1",
            target_channel=None,
            payload={"content": "test", "title": "", "fields": {}},
        )
        with pytest.raises(AdapterPermanentError, match="not started"):
            await adapter.deliver(result)


# ===================================================================
# Failed delivery does not create native refs
# ===================================================================


class TestLxmfFailedDelivery:
    """Failed LXMF delivery does not create native refs."""

    async def test_fake_adapter_failure_raises_and_no_native_ref(self) -> None:
        """Fake adapter failure raises AdapterSendError, no native ref persisted."""
        from medre.core.contracts.adapter import AdapterSendError

        adapter = FakeLxmfAdapter()
        adapter.set_deliver_failure(True)
        result = RenderingResult(
            event_id="evt-fail",
            target_adapter="lxmf-1",
            target_channel=None,
            payload={"content": "test", "title": "", "fields": {}},
        )
        with pytest.raises(AdapterSendError):
            await adapter.deliver(result)
        assert adapter.fake_client.sent_count == 0


# ===================================================================
# Core does not import LXMF or RNS/Reticulum
# ===================================================================


class TestCoreNoLxmfOrRns:
    """Core modules do not import LXMF or RNS/Reticulum directly."""

    def test_core_events_does_not_import_rns(self) -> None:
        """medre.core.events has no RNS references."""
        import medre.core.events as events_mod

        source = _read_module_source(events_mod)
        # Check import lines only to avoid false positives from words like "returns"
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "rns" not in line.lower()
            ), f"Core events must not import RNS; found: {line!r}"
            assert (
                "reticulum" not in line.lower()
            ), f"Core events must not import Reticulum; found: {line!r}"

    def test_core_rendering_does_not_import_rns(self) -> None:
        """medre.core.rendering.renderer has no RNS references."""
        import medre.core.rendering.renderer as renderer_mod

        source = _read_module_source(renderer_mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "rns" not in line.lower()
            ), f"Core rendering must not import RNS; found: {line!r}"
            assert (
                "reticulum" not in line.lower()
            ), f"Core rendering must not import Reticulum; found: {line!r}"

    def test_core_engine_does_not_import_rns(self) -> None:
        """medre.core.engine.pipeline has no RNS references."""
        import medre.core.engine.pipeline as pipeline_mod

        source = _read_module_source(pipeline_mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "rns" not in line.lower()
            ), f"Core engine must not import RNS; found: {line!r}"
            assert (
                "reticulum" not in line.lower()
            ), f"Core engine must not import Reticulum; found: {line!r}"


# ===================================================================
# LXMF compat module isolation
# ===================================================================


class TestLxmfCompatIsolation:
    """compat.py is the sole import site for lxmf/RNS."""

    def test_adapter_does_not_import_lxmf_directly(self) -> None:
        """LXMF adapter uses compat.HAS_LXMF, not direct lxmf import."""
        import medre.adapters.lxmf.adapter as mod

        source = _read_module_source(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
            and not line.strip().startswith("#")
        ]
        for line in import_lines:
            # Allow: from medre.adapters.lxmf.compat import HAS_LXMF
            # Disallow: import lxmf, from lxmf import, import RNS, from RNS import
            if "lxmf.compat" in line or "lxmf." in line and ".compat" in line:
                continue
            assert not (
                line.startswith("import lxmf") or line.startswith("from lxmf")
            ), f"LXMF adapter must not import lxmf directly; found: {line!r}"
            assert not (
                line.startswith("import RNS") or line.startswith("from RNS")
            ), f"LXMF adapter must not import RNS directly; found: {line!r}"

    def test_codec_does_not_import_lxmf_or_rns(self) -> None:
        """LXMF codec is pure — no lxmf/RNS imports."""
        import medre.adapters.lxmf.codec as mod

        source = _read_module_source(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "lxmf" not in line.lower() or "medre.adapters.lxmf" in line
            ), f"LXMF codec must not import lxmf; found: {line!r}"
            assert (
                "rns" not in line.lower()
            ), f"LXMF codec must not import RNS; found: {line!r}"

    def test_classifier_does_not_import_lxmf_or_rns(self) -> None:
        """LXMF classifier is pure — no lxmf/RNS imports."""
        import medre.adapters.lxmf.packet_classifier as mod

        source = _read_module_source(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "lxmf" not in line.lower() or "medre.adapters.lxmf" in line
            ), f"Classifier must not import lxmf; found: {line!r}"
            assert (
                "rns" not in line.lower()
            ), f"Classifier must not import RNS; found: {line!r}"

    def test_renderer_does_not_import_lxmf_or_rns(self) -> None:
        """LXMF renderer does not import lxmf/RNS."""
        import medre.adapters.lxmf.renderer as mod

        source = _read_module_source(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert (
                "lxmf" not in line.lower() or "medre.adapters.lxmf" in line
            ), f"Renderer must not import lxmf; found: {line!r}"
            assert (
                "rns" not in line.lower()
            ), f"Renderer must not import RNS; found: {line!r}"


# ===================================================================
# Classifier purity
# ===================================================================


class TestLxmfClassifierPurity:
    """LXMF classifier has no side effects."""

    def test_classifier_has_no_deliver_method(self) -> None:
        config = LxmfConfig(adapter_id="lxmf-1")
        classifier = LxmfPacketClassifier(config)
        assert not hasattr(classifier, "deliver")

    def test_classifier_has_no_route_method(self) -> None:
        config = LxmfConfig(adapter_id="lxmf-1")
        classifier = LxmfPacketClassifier(config)
        assert not hasattr(classifier, "route")

    def test_classifier_has_no_publish_method(self) -> None:
        config = LxmfConfig(adapter_id="lxmf-1")
        classifier = LxmfPacketClassifier(config)
        assert not hasattr(classifier, "publish")

    def test_classifier_source_has_no_side_effects(self) -> None:
        import medre.adapters.lxmf.packet_classifier as mod

        source = _read_module_source(mod)
        method_defs = re.findall(r"def\s+(\w+)", source)
        assert "deliver" not in method_defs
        assert "route" not in method_defs
        assert "publish" not in method_defs
        assert "store" not in method_defs


# ===================================================================
# Platform-aware rendering still passes
# ===================================================================


class TestLxmfPlatformRendering:
    """Platform-aware rendering works for LXMF targets."""

    async def test_renderer_matches_platform(self) -> None:
        renderer = LxmfRenderer()
        event = CanonicalEvent(
            event_id="evt-1",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="lxmf-1",
            source_transport_id="ab" * 16,
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "test"},
            metadata=EventMetadata(),
        )
        assert renderer.can_render(event, "lxmf_node", target_platform="lxmf")
        assert not renderer.can_render(event, "matrix_bot", target_platform="matrix")

    async def test_renderer_produces_valid_result(self) -> None:
        renderer = LxmfRenderer()
        event = CanonicalEvent(
            event_id="evt-1",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="lxmf-1",
            source_transport_id="ab" * 16,
            source_channel_id=None,
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"body": "test"},
            metadata=EventMetadata(),
        )
        result = await renderer.render(event, "lxmf_node")
        assert isinstance(result, RenderingResult)
        assert "content" in result.payload
        assert "fields" in result.payload


# ===================================================================
# Fake adapter still works
# ===================================================================


class TestFakeLxmfStillWorks:
    """FakeLxmfAdapter basic operations still pass."""

    async def test_fake_adapter_starts(self, make_adapter_context) -> None:
        adapter = FakeLxmfAdapter()
        ctx = make_adapter_context("lxmf-1")
        await adapter.start(ctx)
        assert adapter.is_started is True

    async def test_fake_adapter_delivers(self) -> None:
        adapter = FakeLxmfAdapter()
        result = RenderingResult(
            event_id="evt-1",
            target_adapter="lxmf-1",
            target_channel=None,
            payload={"content": "test", "title": "", "fields": {}},
        )
        delivery = await adapter.deliver(result)
        assert delivery is not None
        assert len(adapter.delivered_payloads) == 1


# ===================================================================
# Error classification at adapter boundary
# ===================================================================


class TestLxmfErrorClassification:
    """LXMF adapter maps session errors to AdapterSendError/AdapterPermanentError."""

    @pytest.mark.asyncio
    async def test_not_connected_maps_to_permanent(self) -> None:
        """LxmfSendError(transient=False) maps to AdapterPermanentError."""
        from unittest.mock import AsyncMock, MagicMock

        config = LxmfConfig(adapter_id="lxmf-ec")
        adapter = LxmfAdapter(config)
        adapter._started = True

        session = MagicMock()
        session.send_text = AsyncMock(
            side_effect=LxmfSendError("Session is not connected", transient=False)
        )
        adapter._session = session

        result = RenderingResult(
            event_id="evt-1",
            target_adapter="lxmf-ec",
            target_channel="abc123",
            payload={"content": "hello", "title": "", "destination_hash": "abc123"},
        )
        with pytest.raises(AdapterPermanentError, match="not connected"):
            await adapter.deliver(result)

    @pytest.mark.asyncio
    async def test_invalid_destination_maps_to_permanent(self) -> None:
        """LxmfSendError for invalid destination maps to AdapterPermanentError."""
        from unittest.mock import AsyncMock, MagicMock

        config = LxmfConfig(adapter_id="lxmf-ec")
        adapter = LxmfAdapter(config)
        adapter._started = True

        session = MagicMock()
        session.send_text = AsyncMock(
            side_effect=LxmfSendError(
                "Invalid destination hash: 'bad'", transient=False
            )
        )
        adapter._session = session

        result = RenderingResult(
            event_id="evt-2",
            target_adapter="lxmf-ec",
            target_channel="bad",
            payload={"content": "hello", "title": "", "destination_hash": "bad"},
        )
        with pytest.raises(AdapterPermanentError, match="Invalid destination"):
            await adapter.deliver(result)

    @pytest.mark.asyncio
    async def test_transient_session_error_maps_to_send_error(self) -> None:
        """LxmfSendError(transient=True) maps to AdapterSendError."""
        from unittest.mock import AsyncMock, MagicMock

        config = LxmfConfig(adapter_id="lxmf-ec")
        adapter = LxmfAdapter(config)
        adapter._started = True

        session = MagicMock()
        session.send_text = AsyncMock(
            side_effect=LxmfSendError("Send failed after 3 attempts", transient=True)
        )
        adapter._session = session

        result = RenderingResult(
            event_id="evt-3",
            target_adapter="lxmf-ec",
            target_channel="abc123",
            payload={"content": "hello", "title": "", "destination_hash": "abc123"},
        )
        with pytest.raises(AdapterSendError) as exc_info:
            await adapter.deliver(result)
        assert exc_info.value.transient is True

    @pytest.mark.asyncio
    async def test_oserror_maps_to_transient(self) -> None:
        """OSError from session maps to AdapterSendError(transient=True)."""
        from unittest.mock import AsyncMock, MagicMock

        config = LxmfConfig(adapter_id="lxmf-ec")
        adapter = LxmfAdapter(config)
        adapter._started = True

        session = MagicMock()
        session.send_text = AsyncMock(side_effect=OSError("transport error"))
        adapter._session = session

        result = RenderingResult(
            event_id="evt-4",
            target_adapter="lxmf-ec",
            target_channel="abc123",
            payload={"content": "hello", "title": "", "destination_hash": "abc123"},
        )
        with pytest.raises(AdapterSendError) as exc_info:
            await adapter.deliver(result)
        assert exc_info.value.transient is True

    @pytest.mark.asyncio
    async def test_adapter_not_started_is_permanent(self) -> None:
        """Adapter not started raises AdapterPermanentError."""
        config = LxmfConfig(adapter_id="lxmf-ec")
        adapter = LxmfAdapter(config)
        # NOT started

        result = RenderingResult(
            event_id="evt-5",
            target_adapter="lxmf-ec",
            target_channel="abc123",
            payload={"content": "hello", "title": "", "destination_hash": "abc123"},
        )
        with pytest.raises(AdapterPermanentError, match="not started"):
            await adapter.deliver(result)
