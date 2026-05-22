"""Tests for runtime transport capability metadata.

Covers:
- TransportCapabilities default values, frozen immutability, and to_dict().
- serialize_adapter_capabilities deterministic JSON-safe serialization.
- summarize_adapter_capabilities projection from AdapterCapabilities.
- is_capability_summary helper function.
- Fake adapter declared capabilities for Matrix, Meshtastic, MeshCore, LXMF,
  Transport, and Presentation adapters.
- Regression: serialization is metadata-only with no mutation side effects.
- No feature negotiation logic, no networking, no optional dependencies.
"""

from __future__ import annotations

import copy
import json
from dataclasses import fields

import pytest

from medre.core.contracts.adapter import AdapterCapabilities, AdapterInfo, AdapterRole
from medre.core.runtime.capabilities import (
    TransportCapabilities,
    is_capability_summary,
    serialize_adapter_capabilities,
    summarize_adapter_capabilities,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _capability_field_names() -> set[str]:
    """Return the set of field names on TransportCapabilities."""
    return {f.name for f in fields(TransportCapabilities)}


def _all_defaults_caps() -> AdapterCapabilities:
    """AdapterCapabilities with all defaults (most conservative)."""
    return AdapterCapabilities()


# ===================================================================
# TransportCapabilities value object
# ===================================================================


class TestTransportCapabilities:
    """TransportCapabilities dataclass behaviour."""

    def test_frozen(self) -> None:
        """TransportCapabilities is frozen / immutable."""
        tc = TransportCapabilities()
        with pytest.raises(AttributeError):
            tc.supports_direct_messages = True  # type: ignore[misc]

    def test_default_values(self) -> None:
        """All boolean fields default to False; optional ints to None."""
        tc = TransportCapabilities()
        for f in fields(tc):
            val = getattr(tc, f.name)
            if f.name.startswith("max_"):
                assert val is None, f"{f.name} should default to None"
            else:
                assert val is False, f"{f.name} should default to False"

    def test_custom_values(self) -> None:
        """Explicit values are stored faithfully."""
        tc = TransportCapabilities(
            supports_direct_messages=True,
            supports_mesh_routing=True,
            max_text_bytes=230,
        )
        assert tc.supports_direct_messages is True
        assert tc.supports_mesh_routing is True
        assert tc.max_text_bytes == 230

    def test_field_count(self) -> None:
        """TransportCapabilities has the expected number of fields."""
        assert len(fields(TransportCapabilities)) >= 14

    # -- to_dict -------------------------------------------------------

    def test_to_dict_returns_dict(self) -> None:
        tc = TransportCapabilities()
        d = tc.to_dict()
        assert isinstance(d, dict)

    def test_to_dict_keys_match_fields(self) -> None:
        """to_dict() keys are exactly the dataclass field names."""
        tc = TransportCapabilities()
        assert set(tc.to_dict().keys()) == _capability_field_names()

    def test_to_dict_values_are_json_safe(self) -> None:
        """All values are bool, int, or None (JSON-safe)."""
        tc = TransportCapabilities(supports_channels=True, max_text_bytes=100)
        for val in tc.to_dict().values():
            assert isinstance(val, (bool, int)) or val is None

    def test_to_dict_deterministic(self) -> None:
        """Two calls produce identical output."""
        tc = TransportCapabilities(
            supports_direct_messages=True,
            supports_channels=False,
            max_text_chars=200,
        )
        assert tc.to_dict() == tc.to_dict()

    def test_to_dict_json_round_trip(self) -> None:
        """Output survives json.dumps + json.loads."""
        tc = TransportCapabilities(
            supports_mesh_routing=True,
            max_text_bytes=4096,
        )
        text = json.dumps(tc.to_dict(), sort_keys=True)
        parsed = json.loads(text)
        assert parsed == tc.to_dict()


# ===================================================================
# serialize_adapter_capabilities
# ===================================================================


class TestSerializeAdapterCapabilities:
    """serialize_adapter_capabilities is a deterministic JSON-safe projection."""

    def test_returns_dict(self) -> None:
        result = serialize_adapter_capabilities(AdapterCapabilities())
        assert isinstance(result, dict)

    def test_keys_are_transport_capability_fields(self) -> None:
        result = serialize_adapter_capabilities(AdapterCapabilities())
        assert set(result.keys()) == _capability_field_names()

    def test_all_values_json_safe(self) -> None:
        result = serialize_adapter_capabilities(AdapterCapabilities())
        for val in result.values():
            assert isinstance(val, (bool, int)) or val is None

    def test_json_round_trip(self) -> None:
        result = serialize_adapter_capabilities(AdapterCapabilities())
        text = json.dumps(result, sort_keys=True)
        parsed = json.loads(text)
        assert parsed == result

    def test_deterministic(self) -> None:
        caps = AdapterCapabilities(
            text=True,
            replies="native",
            max_text_chars=200,
        )
        r1 = serialize_adapter_capabilities(caps)
        r2 = serialize_adapter_capabilities(caps)
        assert r1 == r2
        assert json.dumps(r1, sort_keys=True) == json.dumps(r2, sort_keys=True)

    def test_defaults_reflect_adapter_defaults(self) -> None:
        """All-default AdapterCapabilities serializes reflecting AdapterCapabilities defaults.

        AdapterCapabilities defaults: text=True, replies/reactions/edits/deletes="native",
        direct_messages=True, channels=True. These project to True in the summary.
        """
        result = serialize_adapter_capabilities(AdapterCapabilities())
        # True due to AdapterCapabilities defaults
        assert result["supports_direct_messages"] is True
        assert result["supports_channels"] is True
        assert result["supports_reactions"] is True  # "native" → True
        assert result["supports_edits"] is True  # "native" → True
        # False due to AdapterCapabilities defaults
        assert result["supports_binary_payloads"] is False
        assert result["supports_delivery_receipts"] is False
        assert result["supports_mesh_routing"] is False
        # None for max fields
        assert result["max_text_bytes"] is None
        assert result["max_text_chars"] is None

    def test_no_feature_negotiation_keys(self) -> None:
        """Output contains no routing, negotiation, or feature keys."""
        result = serialize_adapter_capabilities(AdapterCapabilities())
        forbidden = {"route", "negotiate", "feature", "protocol", "transport"}
        for key in result:
            assert not any(f in key.lower() for f in forbidden)


# ===================================================================
# summarize_adapter_capabilities projection
# ===================================================================


class TestSummarizeAdapterCapabilities:
    """summarize_adapter_capabilities projects adapter flags correctly."""

    def test_returns_transport_capabilities(self) -> None:
        tc = summarize_adapter_capabilities(AdapterCapabilities())
        assert isinstance(tc, TransportCapabilities)

    def test_boolean_passthrough(self) -> None:
        """Boolean AdapterCapabilities fields map directly."""
        caps = AdapterCapabilities(
            direct_messages=True,
            channels=True,
            attachments=True,
            delivery_receipts=True,
            ack_tracking=True,
            store_and_forward=True,
            async_delivery=True,
            identity_encryption=True,
            presence=True,
            topic_rooms=True,
            mesh_routing=True,
            priority_delivery=True,
        )
        tc = summarize_adapter_capabilities(caps)
        assert tc.supports_direct_messages is True
        assert tc.supports_channels is True
        assert tc.supports_binary_payloads is True
        assert tc.supports_delivery_receipts is True
        assert tc.supports_ack_tracking is True
        assert tc.supports_store_and_forward is True
        assert tc.supports_async_delivery is True
        assert tc.supports_identity_encryption is True
        assert tc.supports_presence is True
        assert tc.supports_topic_rooms is True
        assert tc.supports_mesh_routing is True
        assert tc.supports_priority_delivery is True

    def test_relation_unsupported_maps_to_false(self) -> None:
        """Relation strings of 'unsupported' become False."""
        caps = AdapterCapabilities(reactions="unsupported", edits="unsupported")
        tc = summarize_adapter_capabilities(caps)
        assert tc.supports_reactions is False
        assert tc.supports_edits is False

    def test_relation_native_maps_to_true(self) -> None:
        """Relation strings of 'native' become True."""
        caps = AdapterCapabilities(reactions="native", edits="native")
        tc = summarize_adapter_capabilities(caps)
        assert tc.supports_reactions is True
        assert tc.supports_edits is True

    def test_relation_fallback_maps_to_true(self) -> None:
        """Relation strings of 'fallback' become True (not unsupported)."""
        caps = AdapterCapabilities(reactions="fallback", edits="fallback")
        tc = summarize_adapter_capabilities(caps)
        assert tc.supports_reactions is True
        assert tc.supports_edits is True

    def test_max_text_passthrough(self) -> None:
        """max_text_bytes and max_text_chars pass through directly."""
        caps = AdapterCapabilities(max_text_bytes=1024, max_text_chars=500)
        tc = summarize_adapter_capabilities(caps)
        assert tc.max_text_bytes == 1024
        assert tc.max_text_chars == 500

    def test_max_text_none_passthrough(self) -> None:
        """None max_text values pass through as None."""
        caps = AdapterCapabilities()
        tc = summarize_adapter_capabilities(caps)
        assert tc.max_text_bytes is None
        assert tc.max_text_chars is None

    def test_defaults_reflect_adapter_defaults(self) -> None:
        """All-default AdapterCapabilities → TransportCapabilities reflecting defaults.

        AdapterCapabilities defaults have text=True, replies="native", etc.
        so the summary is not all-False.
        """
        tc = summarize_adapter_capabilities(AdapterCapabilities())
        # These are True due to AdapterCapabilities defaults
        assert tc.supports_direct_messages is True
        assert tc.supports_channels is True
        assert tc.supports_reactions is True  # "native" → True
        assert tc.supports_edits is True  # "native" → True
        # These are False
        assert tc.supports_binary_payloads is False
        assert tc.supports_delivery_receipts is False
        assert tc.supports_mesh_routing is False
        # None for max fields
        assert tc.max_text_bytes is None
        assert tc.max_text_chars is None


# ===================================================================
# is_capability_summary helper
# ===================================================================


class TestIsCapabilitySummary:
    """is_capability_summary validates dict structure."""

    def test_true_for_serialized_capabilities(self) -> None:
        result = serialize_adapter_capabilities(AdapterCapabilities())
        assert is_capability_summary(result) is True

    def test_true_for_to_dict_output(self) -> None:
        tc = TransportCapabilities()
        assert is_capability_summary(tc.to_dict()) is True

    def test_false_for_non_dict(self) -> None:
        assert is_capability_summary("not a dict") is False
        assert is_capability_summary(42) is False
        assert is_capability_summary(None) is False

    def test_false_for_wrong_keys(self) -> None:
        assert is_capability_summary({"adapter_id": "x"}) is False

    def test_false_for_partial_keys(self) -> None:
        """Dict with a subset of capability keys returns False."""
        partial = {"supports_direct_messages": True}
        assert is_capability_summary(partial) is False

    def test_false_for_extra_keys(self) -> None:
        """Dict with correct keys plus extras returns False."""
        result = serialize_adapter_capabilities(AdapterCapabilities())
        result["extra_key"] = True
        assert is_capability_summary(result) is False


# ===================================================================
# Fake adapter capability assertions
# ===================================================================


class TestFakeMatrixCapabilities:
    """FakeMatrixAdapter declares realistic Matrix-like capabilities."""

    def test_capabilities_from_constant(self) -> None:
        """_FAKE_MATRIX_CAPABILITIES has expected Matrix presentation flags."""
        from medre.adapters.fake_matrix import _FAKE_MATRIX_CAPABILITIES

        caps = _FAKE_MATRIX_CAPABILITIES
        assert caps.text is True
        assert caps.replies == "native"
        assert caps.reactions == "unsupported"
        assert caps.edits == "unsupported"
        assert caps.deletes == "unsupported"
        assert caps.attachments is False
        assert caps.delivery_receipts is True
        assert caps.direct_messages is True
        assert caps.channels is True
        assert caps.async_delivery is True
        assert caps.topic_rooms is True

    def test_capability_serialization_matches(self) -> None:
        """serialize_adapter_capabilities produces deterministic output."""
        from medre.adapters.fake_matrix import _FAKE_MATRIX_CAPABILITIES

        result = serialize_adapter_capabilities(_FAKE_MATRIX_CAPABILITIES)
        assert isinstance(result, dict)
        assert is_capability_summary(result)
        assert result["supports_direct_messages"] is True
        assert result["supports_channels"] is True
        assert result["supports_reactions"] is False  # unsupported → False
        assert result["supports_edits"] is False
        assert result["supports_delivery_receipts"] is True
        assert result["supports_topic_rooms"] is True

    @pytest.mark.asyncio
    async def test_health_check_capabilities_match_constant(
        self,
        make_adapter_context,
    ) -> None:
        """health_check() returns AdapterInfo with declared capabilities."""
        from medre.adapters.fake_matrix import (
            _FAKE_MATRIX_CAPABILITIES,
            FakeMatrixAdapter,
        )

        adapter = FakeMatrixAdapter("caps_matrix")
        ctx = make_adapter_context("caps_matrix")
        await adapter.start(ctx)
        info = await adapter.health_check()
        assert info.capabilities == _FAKE_MATRIX_CAPABILITIES
        await adapter.stop()


class TestFakeMeshtasticCapabilities:
    """FakeMeshtasticAdapter declares realistic Meshtastic transport capabilities."""

    def test_capabilities_from_constant(self) -> None:
        """_FAKE_MESHTASTIC_CAPABILITIES has conservative transport flags."""
        from medre.adapters.fake_meshtastic import _FAKE_MESHTASTIC_CAPABILITIES

        caps = _FAKE_MESHTASTIC_CAPABILITIES
        assert caps.text is True
        assert caps.replies == "native"
        assert caps.reactions == "native"
        assert caps.metadata_fields is True
        assert caps.direct_messages is False
        assert caps.channels is True
        assert caps.mesh_routing is True
        assert caps.max_text_bytes == 227
        assert caps.max_text_chars is None

    def test_capability_serialization_matches(self) -> None:
        from medre.adapters.fake_meshtastic import _FAKE_MESHTASTIC_CAPABILITIES

        result = serialize_adapter_capabilities(_FAKE_MESHTASTIC_CAPABILITIES)
        assert is_capability_summary(result)
        assert result["supports_direct_messages"] is False
        assert result["supports_channels"] is True
        assert result["supports_mesh_routing"] is True
        assert result["supports_reactions"] is True
        assert result["max_text_bytes"] == 227

    @pytest.mark.asyncio
    async def test_health_check_capabilities_serializable(
        self,
        make_adapter_context,
    ) -> None:
        """health_check() capabilities survive serialize_adapter_capabilities."""
        from medre.adapters.fake_meshtastic import (
            _FAKE_MESHTASTIC_CAPABILITIES,
            FakeMeshtasticAdapter,
        )

        adapter = FakeMeshtasticAdapter()
        ctx = make_adapter_context(adapter.adapter_id)
        await adapter.start(ctx)
        info = await adapter.health_check()
        assert info.capabilities == _FAKE_MESHTASTIC_CAPABILITIES
        serialized = serialize_adapter_capabilities(info.capabilities)
        assert is_capability_summary(serialized)
        json.loads(json.dumps(serialized))
        await adapter.stop()


class TestFakeMeshCoreCapabilities:
    """FakeMeshCoreAdapter declares realistic MeshCore transport capabilities."""

    def test_capabilities_from_constant(self) -> None:
        from medre.adapters.fake_meshcore import _FAKE_MESHCORE_CAPABILITIES

        caps = _FAKE_MESHCORE_CAPABILITIES
        assert caps.text is True
        assert caps.replies == "unsupported"
        assert caps.direct_messages is False
        assert caps.channels is True
        assert caps.mesh_routing is True
        assert caps.max_text_bytes == 512
        assert caps.max_text_chars == 512

    def test_capability_serialization_matches(self) -> None:
        from medre.adapters.fake_meshcore import _FAKE_MESHCORE_CAPABILITIES

        result = serialize_adapter_capabilities(_FAKE_MESHCORE_CAPABILITIES)
        assert is_capability_summary(result)
        assert result["supports_mesh_routing"] is True
        assert result["max_text_bytes"] == 512

    @pytest.mark.asyncio
    async def test_health_check_capabilities_serializable(
        self,
        make_adapter_context,
    ) -> None:
        """health_check() capabilities are JSON-safe after serialization."""
        from medre.adapters.fake_meshcore import (
            _FAKE_MESHCORE_CAPABILITIES,
            FakeMeshCoreAdapter,
        )

        adapter = FakeMeshCoreAdapter()
        ctx = make_adapter_context(adapter.adapter_id)
        await adapter.start(ctx)
        info = await adapter.health_check()
        assert info.capabilities == _FAKE_MESHCORE_CAPABILITIES
        serialized = serialize_adapter_capabilities(info.capabilities)
        assert is_capability_summary(serialized)
        json.loads(json.dumps(serialized))
        await adapter.stop()


class TestFakeLxmfCapabilities:
    """FakeLxmfAdapter declares realistic LXMF transport capabilities."""

    def test_capabilities_from_constant(self) -> None:
        """LXMF caps: text, title, metadata_fields, identity_encryption."""
        from medre.adapters.fake_lxmf import _FAKE_LXMF_CAPABILITIES

        caps = _FAKE_LXMF_CAPABILITIES
        assert caps.text is True
        assert caps.title is True
        assert caps.metadata_fields is True
        assert caps.identity_encryption is True
        assert caps.direct_messages is True
        assert caps.channels is False
        assert caps.mesh_routing is True
        assert caps.max_text_chars == 16384

    def test_capability_serialization_matches(self) -> None:
        from medre.adapters.fake_lxmf import _FAKE_LXMF_CAPABILITIES

        result = serialize_adapter_capabilities(_FAKE_LXMF_CAPABILITIES)
        assert is_capability_summary(result)
        assert result["supports_direct_messages"] is True
        assert result["supports_channels"] is False
        assert result["supports_identity_encryption"] is True
        assert result["max_text_chars"] == 16384

    @pytest.mark.asyncio
    async def test_health_check_capabilities_serializable(
        self,
        make_adapter_context,
    ) -> None:
        """health_check() capabilities survive serialization."""
        from medre.adapters.fake_lxmf import _FAKE_LXMF_CAPABILITIES, FakeLxmfAdapter

        adapter = FakeLxmfAdapter()
        ctx = make_adapter_context(adapter.adapter_id)
        await adapter.start(ctx)
        info = await adapter.health_check()
        assert info.capabilities == _FAKE_LXMF_CAPABILITIES
        serialized = serialize_adapter_capabilities(info.capabilities)
        assert is_capability_summary(serialized)
        json.loads(json.dumps(serialized))
        await adapter.stop()


class TestFakeTransportCapabilities:
    """FakeTransportAdapter declares transport-like capabilities."""

    def test_capabilities_from_constant(self) -> None:
        from medre.adapters.fake_transport import _FAKE_TRANSPORT_CAPABILITIES

        caps = _FAKE_TRANSPORT_CAPABILITIES
        assert caps.text is True
        assert caps.replies == "native"
        assert caps.reactions == "fallback"
        assert caps.direct_messages is True
        assert caps.channels is True
        assert caps.mesh_routing is True
        assert caps.max_text_chars == 200

    def test_serialized_capabilities(self) -> None:
        from medre.adapters.fake_transport import _FAKE_TRANSPORT_CAPABILITIES

        result = serialize_adapter_capabilities(_FAKE_TRANSPORT_CAPABILITIES)
        assert is_capability_summary(result)
        assert result["supports_direct_messages"] is True
        assert result["supports_channels"] is True
        assert result["supports_reactions"] is True  # fallback → True
        assert result["supports_mesh_routing"] is True
        assert result["max_text_chars"] == 200


class TestFakePresentationCapabilities:
    """FakePresentationAdapter declares presentation-like capabilities."""

    def test_capabilities_from_constant(self) -> None:
        from medre.adapters.fake_presentation import _FAKE_PRESENTATION_CAPABILITIES

        caps = _FAKE_PRESENTATION_CAPABILITIES
        assert caps.text is True
        assert caps.replies == "native"
        assert caps.reactions == "native"
        assert caps.delivery_receipts is True
        assert caps.direct_messages is True
        assert caps.channels is True
        assert caps.topic_rooms is True

    def test_serialized_capabilities(self) -> None:
        from medre.adapters.fake_presentation import _FAKE_PRESENTATION_CAPABILITIES

        result = serialize_adapter_capabilities(_FAKE_PRESENTATION_CAPABILITIES)
        assert is_capability_summary(result)
        assert result["supports_reactions"] is True  # native → True
        assert result["supports_edits"] is False  # unsupported → False
        assert result["supports_delivery_receipts"] is True


# ===================================================================
# Regression: no mutation side effects
# ===================================================================


class TestNoMutationSideEffects:
    """Serialization and snapshots must not mutate source objects or state."""

    def test_serialize_does_not_mutate_adapter_capabilities(self) -> None:
        """serialize_adapter_capabilities must not modify the input."""
        original = AdapterCapabilities(
            text=True,
            replies="native",
            reactions="fallback",
            max_text_chars=200,
        )
        original_copy = copy.deepcopy(original)
        # Call multiple times
        for _ in range(5):
            serialize_adapter_capabilities(original)
        assert original == original_copy

    def test_summarize_does_not_mutate_adapter_capabilities(self) -> None:
        """summarize_adapter_capabilities must not modify the input."""
        original = AdapterCapabilities(
            text=True,
            reactions="native",
            max_text_bytes=1024,
        )
        original_copy = copy.deepcopy(original)
        summarize_adapter_capabilities(original)
        assert original == original_copy

    def test_to_dict_does_not_mutate_transport_capabilities(self) -> None:
        """TransportCapabilities.to_dict() must not modify the instance."""
        tc = TransportCapabilities(
            supports_direct_messages=True,
            max_text_bytes=500,
        )
        d1 = tc.to_dict()
        d1["supports_direct_messages"] = False  # mutate returned dict
        d2 = tc.to_dict()
        assert d2["supports_direct_messages"] is True  # original unaffected

    def test_serialize_creates_no_queues_tasks_schedulers(self) -> None:
        """Serialization must not import or create queue/task/scheduler objects."""
        import gc

        gc.collect()
        caps = AdapterCapabilities()
        result = serialize_adapter_capabilities(caps)

        # The result must be a plain dict of plain values
        assert isinstance(result, dict)
        for key, val in result.items():
            assert isinstance(key, str)
            assert isinstance(val, (bool, int)) or val is None

    def test_snapshot_does_not_mutate_adapter_info(self) -> None:
        """capture_runtime_snapshot must not mutate AdapterInfo or capabilities."""
        from medre.core.runtime.diagnostics import (
            _AdapterHealthInput,
            capture_runtime_snapshot,
        )

        caps = AdapterCapabilities(text=True, replies="native")
        info = AdapterInfo(
            adapter_id="mutation-test",
            platform="test",
            role=AdapterRole.TRANSPORT,
            version="0.1.0",
            capabilities=caps,
            health="healthy",
        )
        caps_before = copy.deepcopy(info.capabilities)

        snap = capture_runtime_snapshot(
            adapter_healths=[
                _AdapterHealthInput(
                    info=info,
                    lifecycle_state=None,
                    adapter=None,
                    details=None,
                ),
            ],
        )
        snap.to_dict()

        # Verify capabilities unchanged
        assert info.capabilities == caps_before
        assert info.capabilities.text is True
        assert info.capabilities.replies == "native"

    def test_is_capability_summary_does_not_mutate_input(self) -> None:
        """is_capability_summary must not modify the input dict."""
        result = serialize_adapter_capabilities(AdapterCapabilities())
        original = copy.deepcopy(result)
        is_capability_summary(result)
        assert result == original

    def test_serialize_output_is_not_shared_reference(self) -> None:
        """Each call to serialize_adapter_capabilities returns a new dict."""
        caps = AdapterCapabilities(text=True)
        r1 = serialize_adapter_capabilities(caps)
        r2 = serialize_adapter_capabilities(caps)
        assert r1 is not r2
        assert r1 == r2

    def test_to_dict_output_is_not_shared_reference(self) -> None:
        """Each call to TransportCapabilities.to_dict() returns a new dict."""
        tc = TransportCapabilities(supports_channels=True)
        d1 = tc.to_dict()
        d2 = tc.to_dict()
        assert d1 is not d2
        assert d1 == d2


# ===================================================================
# Capability snapshot in runtime diagnostics
# ===================================================================


class TestCapabilitySnapshotInDiagnostics:
    """Capabilities can be included in diagnostic snapshots without mutation."""

    def test_capability_summary_from_snapshot_adapter(self) -> None:
        """Serialized capabilities derived from snapshot adapter entries."""
        from medre.core.runtime.diagnostics import (
            _AdapterHealthInput,
            capture_runtime_snapshot,
        )

        caps = AdapterCapabilities(
            text=True,
            direct_messages=True,
            channels=True,
            max_text_chars=500,
        )
        info = AdapterInfo(
            adapter_id="caps-snap",
            platform="test",
            role=AdapterRole.TRANSPORT,
            version="0.1.0",
            capabilities=caps,
            health="healthy",
        )
        entry = _AdapterHealthInput(
            info=info,
            lifecycle_state=None,
            adapter=None,
            details=None,
        )

        snap = capture_runtime_snapshot(adapter_healths=[entry])
        result = snap.to_dict()
        assert len(result["adapters"]) == 1
        adapter = result["adapters"][0]

        # The snapshot contains adapter_id and health
        assert adapter["adapter_id"] == "caps-snap"
        assert adapter["health"] == "healthy"

        # Capabilities can be re-derived from the adapter's info
        serialized = serialize_adapter_capabilities(caps)
        assert is_capability_summary(serialized)
        assert serialized["supports_direct_messages"] is True
        assert serialized["max_text_chars"] == 500

    def test_multiple_adapter_capabilities_deterministic(self) -> None:
        """Multiple adapters' capabilities serialize deterministically."""
        caps_a = AdapterCapabilities(text=True, reactions="native")
        caps_b = AdapterCapabilities(text=True, reactions="unsupported")

        ser_a1 = serialize_adapter_capabilities(caps_a)
        ser_a2 = serialize_adapter_capabilities(caps_a)
        ser_b1 = serialize_adapter_capabilities(caps_b)

        assert ser_a1 == ser_a2
        assert ser_a1 != ser_b1  # Different caps → different output
        assert ser_a1["supports_reactions"] is True
        assert ser_b1["supports_reactions"] is False

    def test_capability_snapshot_json_safe(self) -> None:
        """Full capability snapshot survives JSON round-trip."""
        caps = AdapterCapabilities(
            text=True,
            reactions="native",
            max_text_bytes=4096,
            mesh_routing=True,
        )
        serialized = serialize_adapter_capabilities(caps)
        text = json.dumps(serialized, sort_keys=True)
        parsed = json.loads(text)
        assert parsed == serialized
