"""Evidence tier label tests.

Proves that evidence outputs can label synthetic/conformance/docker/live_service/hardware
without overclaiming, that existing bundle behavior remains backward-compatible,
and that tier inference is conservative.

Covers:

1. Synthetic defaults: fake adapters and tests produce ``"synthetic"`` tier.
2. Storage-only evidence is never mislabeled as ``"live_service"`` or ``"hardware"``.
3. Docker artifact/helper path labels ``"docker"`` only when explicit.
4. ``"live_service"`` / ``"hardware"`` not inferred from ``adapter_kind="real"`` alone.
5. Replay markers remain present; replay evidence does not imply live tier.
6. ``to_dict()`` includes ``evidence_tier``.
7. Explicit tier overrides inference.
8. Empty-source bundles default to ``"synthetic"``.
9. Runtime storage-path bundles always carry ``"synthetic"`` tier.
10. Backward-compatible: old EvidenceBundle construction still works.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from medre.core.events import (
    CanonicalEvent,
    DeliveryReceipt,
    NativeMessageRef,
)
from medre.core.evidence.bundle import (
    EvidenceBundle,
)
from medre.core.evidence.collector import EvidenceCollector
from medre.core.evidence.tiers import (
    EVIDENCE_TIER_UNKNOWN,
    EvidenceTier,
    infer_evidence_tier,
    tier_is_live,
)
from tests.helpers.storage import make_storage_event

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FIXED_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


def _fixed_now() -> datetime:
    return _FIXED_NOW


def _make_receipt(
    receipt_id: str = "rcpt-1",
    event_id: str = "evt-1",
    sequence: int = 1,
    source: str = "live",
    replay_run_id: str | None = None,
    created_at: datetime | None = None,
) -> DeliveryReceipt:
    return DeliveryReceipt(
        sequence=sequence,
        receipt_id=receipt_id,
        event_id=event_id,
        delivery_plan_id="plan-1",
        target_adapter="adapter_a",
        target_channel=None,
        route_id="route-1",
        status="sent",
        attempt_number=1,
        source=source,
        replay_run_id=replay_run_id,
        rendering_evidence=None,
        created_at=created_at or _FIXED_NOW,
    )


class FakeStorage:
    """Minimal fake storage for unit tests."""

    def __init__(self) -> None:
        self._events: dict[str, CanonicalEvent] = {}
        self._receipts: dict[str, list[DeliveryReceipt]] = {}
        self._native_refs: dict[str, list[NativeMessageRef]] = {}

    async def get(self, event_id: str) -> CanonicalEvent | None:
        return self._events.get(event_id)

    async def list_receipts_for_event(self, event_id: str) -> list[DeliveryReceipt]:
        return self._receipts.get(event_id, [])

    async def list_native_refs_for_event(self, event_id: str) -> list[NativeMessageRef]:
        return self._native_refs.get(event_id, [])


def _populated_fake(
    *,
    event_id: str = "evt-1",
    source_adapter: str = "fake_transport",
    include_event: bool = True,
    receipts: list[DeliveryReceipt] | None = None,
) -> FakeStorage:
    """Build a FakeStorage pre-populated with the given data."""
    fs = FakeStorage()
    if include_event:
        fs._events[event_id] = make_storage_event(
            event_id=event_id,
            source_adapter=source_adapter,
        )
    if receipts:
        fs._receipts[event_id] = receipts
    return fs


# ===========================================================================
# Tier inference unit tests
# ===========================================================================


class TestInferEvidenceTier:
    """Unit tests for infer_evidence_tier function."""

    def test_explicit_tier_overrides_all(self) -> None:
        """Explicit tier takes priority over all signals."""
        assert (
            infer_evidence_tier(
                adapter_kind="fake",
                explicit_tier="live_service",
            )
            == "live_service"
        )

    def test_explicit_hardware(self) -> None:
        assert (
            infer_evidence_tier(
                adapter_kind="real",
                explicit_tier="hardware",
            )
            == "hardware"
        )

    def test_explicit_conformance(self) -> None:
        assert infer_evidence_tier(explicit_tier="conformance") == "conformance"

    def test_explicit_invalid_falls_through(self) -> None:
        """Invalid explicit tier is ignored; falls through to inference."""
        assert infer_evidence_tier(explicit_tier="invalid_tier") == "synthetic"

    def test_fake_adapter_kind_is_synthetic(self) -> None:
        assert infer_evidence_tier(adapter_kind="fake") == "synthetic"

    def test_real_adapter_kind_is_synthetic_by_default(self) -> None:
        """adapter_kind="real" alone does not promote past synthetic."""
        assert infer_evidence_tier(adapter_kind="real") == "synthetic"

    def test_none_adapter_kind_defaults_synthetic(self) -> None:
        assert infer_evidence_tier(adapter_kind=None) == "synthetic"

    def test_fake_source_adapter_name_is_synthetic(self) -> None:
        assert infer_evidence_tier(source_adapter="fake_transport") == "synthetic"

    def test_real_source_adapter_name_defaults_synthetic(self) -> None:
        """A real-sounding adapter name alone does not upgrade the tier."""
        assert infer_evidence_tier(source_adapter="matrix_prod") == "synthetic"

    def test_replay_source_is_synthetic(self) -> None:
        assert infer_evidence_tier(sources_seen=("replay",)) == "synthetic"

    def test_replay_among_sources_is_synthetic(self) -> None:
        assert infer_evidence_tier(sources_seen=("live", "replay")) == "synthetic"

    def test_live_source_without_replay_defaults_synthetic(self) -> None:
        """Just having source="live" does not prove live_service tier."""
        assert infer_evidence_tier(sources_seen=("live",)) == "synthetic"

    def test_docker_artifact_is_docker(self) -> None:
        assert infer_evidence_tier(is_docker_artifact=True) == "docker"

    def test_docker_artifact_beats_real_adapter_kind(self) -> None:
        """Docker artifact flag takes precedence over real adapter_kind."""
        assert (
            infer_evidence_tier(adapter_kind="real", is_docker_artifact=True)
            == "docker"
        )

    def test_fake_adapter_kind_beats_docker(self) -> None:
        """Fake adapter_kind takes priority over docker flag."""
        assert (
            infer_evidence_tier(adapter_kind="fake", is_docker_artifact=True)
            == "synthetic"
        )

    def test_empty_inputs_default_synthetic(self) -> None:
        assert infer_evidence_tier() == "synthetic"

    def test_list_sources_accepted(self) -> None:
        """Function accepts list as well as tuple."""
        assert infer_evidence_tier(sources_seen=["replay"]) == "synthetic"


class TestTierIsLive:
    """Tests for tier_is_live helper."""

    def test_live_service_is_live(self) -> None:
        assert tier_is_live("live_service") is True

    def test_hardware_is_live(self) -> None:
        assert tier_is_live("hardware") is True

    def test_synthetic_is_not_live(self) -> None:
        assert tier_is_live("synthetic") is False

    def test_docker_is_not_live(self) -> None:
        assert tier_is_live("docker") is False

    def test_conformance_is_not_live(self) -> None:
        assert tier_is_live("conformance") is False

    def test_empty_is_not_live(self) -> None:
        assert tier_is_live("") is False


class TestEvidenceTierEnum:
    """EvidenceTier enum values are correct."""

    def test_all_values(self) -> None:
        values = {t.value for t in EvidenceTier}
        assert values == {
            "synthetic",
            "conformance",
            "docker",
            "live_service",
            "hardware",
        }

    def test_json_serializable(self) -> None:
        """Enum values are plain strings and JSON-serializable."""
        for tier in EvidenceTier:
            assert isinstance(tier.value, str)
            assert json.dumps(tier.value) is not None

    def test_evidence_tier_unknown_constant(self) -> None:
        assert EVIDENCE_TIER_UNKNOWN == ""


# ===========================================================================
# Collector integration tests
# ===========================================================================


class TestCollectorTierSynthetic:
    """Collector with fake_transport source produces synthetic tier."""

    @pytest.mark.asyncio
    async def test_fake_transport_produces_synthetic(self) -> None:
        storage = _populated_fake(event_id="evt-syn", source_adapter="fake_transport")
        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-syn")

        assert bundle.evidence_tier == "synthetic"

    @pytest.mark.asyncio
    async def test_fake_transport_to_dict_includes_tier(self) -> None:
        storage = _populated_fake(event_id="evt-td", source_adapter="fake_transport")
        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-td")

        d = bundle.to_dict()
        assert d["evidence_tier"] == "synthetic"
        # Verify JSON-safe.
        json_str = json.dumps(d, sort_keys=True)
        parsed = json.loads(json_str)
        assert parsed["evidence_tier"] == "synthetic"


class TestCollectorTierReplay:
    """Replay evidence carries synthetic tier and preserves replay markers."""

    @pytest.mark.asyncio
    async def test_replay_receipts_synthetic_tier(self) -> None:
        r_live = _make_receipt("rcpt-live", event_id="evt-rp", source="live")
        r_replay = _make_receipt(
            "rcpt-replay",
            event_id="evt-rp",
            source="replay",
            replay_run_id="run-1",
        )
        storage = _populated_fake(
            event_id="evt-rp",
            source_adapter="matrix_prod",
            receipts=[r_live, r_replay],
        )
        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-rp")

        # Tier is synthetic because replay is present.
        assert bundle.evidence_tier == "synthetic"
        # But replay markers are preserved.
        assert "replay" in bundle.sources_seen
        assert "run-1" in bundle.replay_run_ids

    @pytest.mark.asyncio
    async def test_replay_to_dict_preserves_markers(self) -> None:
        r_replay = _make_receipt(
            "rcpt-rp2",
            event_id="evt-rp2",
            source="replay",
            replay_run_id="run-2",
        )
        storage = _populated_fake(
            event_id="evt-rp2",
            source_adapter="some_adapter",
            receipts=[r_replay],
        )
        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-rp2")

        d = bundle.to_dict()
        assert d["evidence_tier"] == "synthetic"
        assert "replay" in d["sources_seen"]
        assert "run-2" in d["replay_run_ids"]


class TestCollectorTierNotLive:
    """Storage-only / unknown evidence never becomes live_service or hardware."""

    @pytest.mark.asyncio
    async def test_missing_event_synthetic_not_live(self) -> None:
        """Completely missing event defaults to synthetic, never live."""
        storage = FakeStorage()
        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-missing")

        assert bundle.evidence_tier == "synthetic"
        assert not tier_is_live(bundle.evidence_tier)

    @pytest.mark.asyncio
    async def test_real_adapter_kind_not_live(self) -> None:
        """Real adapter name alone does not infer live_service."""
        storage = _populated_fake(
            event_id="evt-real",
            source_adapter="matrix_production",
        )
        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-real")

        # Conservative: synthetic even with "real" adapter name.
        assert bundle.evidence_tier == "synthetic"
        assert not tier_is_live(bundle.evidence_tier)

    @pytest.mark.asyncio
    async def test_orphan_receipts_synthetic(self) -> None:
        """Receipts without event are still synthetic."""
        receipt = _make_receipt("rcpt-orphan", event_id="evt-orphan")
        storage = _populated_fake(
            event_id="evt-orphan",
            include_event=False,
            receipts=[receipt],
        )
        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-orphan")

        assert bundle.evidence_tier == "synthetic"


# ===========================================================================
# Bundle model backward-compatibility
# ===========================================================================


class TestBundleBackwardCompat:
    """Old EvidenceBundle construction still works (conservative synthetic default)."""

    def test_old_construction_no_tier_arg(self) -> None:
        """Constructing EvidenceBundle without evidence_tier defaults to synthetic."""
        bundle = EvidenceBundle(
            event_id="evt-compat",
            generated_at="2026-01-15T12:00:00+00:00",
        )
        assert bundle.evidence_tier == "synthetic"

    def test_old_construction_to_dict_includes_tier(self) -> None:
        bundle = EvidenceBundle(
            event_id="evt-compat2",
            generated_at="2026-01-15T12:00:00+00:00",
        )
        d = bundle.to_dict()
        assert "evidence_tier" in d
        assert d["evidence_tier"] == "synthetic"

    def test_old_construction_json_safe(self) -> None:
        bundle = EvidenceBundle(
            event_id="evt-compat3",
            generated_at="2026-01-15T12:00:00+00:00",
        )
        json_str = json.dumps(bundle.to_dict(), sort_keys=True)
        parsed = json.loads(json_str)
        assert parsed["evidence_tier"] == "synthetic"

    def test_explicit_tier_on_bundle(self) -> None:
        bundle = EvidenceBundle(
            event_id="evt-explicit",
            evidence_tier="docker",
            generated_at="2026-01-15T12:00:00+00:00",
        )
        assert bundle.evidence_tier == "docker"
        assert bundle.to_dict()["evidence_tier"] == "docker"


# ===========================================================================
# Runtime storage-path bundle tier
# ===========================================================================


class TestRuntimeStoragePathTier:
    """Runtime evidence bundle in storage-path mode always carries synthetic tier."""

    @pytest.mark.asyncio
    async def test_storage_path_bundle_has_synthetic_tier(self, tmp_path) -> None:

        from medre.core.storage.sqlite.storage import SQLiteStorage
        from medre.runtime.evidence._bundle import collect_evidence_bundle

        db_path = str(tmp_path / "test.db")
        storage = SQLiteStorage(db_path)
        await storage.initialize()
        await storage.close()

        report = await collect_evidence_bundle(storage_path=db_path)
        assert report["evidence_tier"] == "synthetic"

    @pytest.mark.asyncio
    async def test_storage_path_with_event_still_synthetic(self, tmp_path) -> None:

        from medre.core.events import CanonicalEvent, EventMetadata
        from medre.core.events.kinds import EventKind
        from medre.core.storage.sqlite.storage import SQLiteStorage
        from medre.runtime.evidence._bundle import collect_evidence_bundle

        db_path = str(tmp_path / "test.db")
        storage = SQLiteStorage(db_path)
        await storage.initialize()

        event = CanonicalEvent(
            event_id="ev-tier-test-001",
            event_kind=EventKind.MESSAGE_TEXT,
            schema_version=1,
            timestamp=datetime(2026, 3, 15, 12, 0, 0, tzinfo=timezone.utc),
            source_adapter="matrix",
            source_transport_id="matrix",
            source_channel_id="!room:test",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "tier test"},
            metadata=EventMetadata(),
        )
        await storage.append(event)
        await storage.close()

        report = await collect_evidence_bundle(
            storage_path=db_path,
            event_id="ev-tier-test-001",
        )
        # Storage-only: always synthetic, never live.
        assert report["evidence_tier"] == "synthetic"
        assert not tier_is_live(report["evidence_tier"])


# ===========================================================================
# Explicit live/hardware tier requires explicit input
# ===========================================================================


class TestExplicitTierRequired:
    """live_service and hardware tiers require explicit input — never inferred."""

    def test_real_adapter_no_explicit_not_live(self) -> None:
        """adapter_kind="real" with no explicit_tier → synthetic, not live."""
        tier = infer_evidence_tier(adapter_kind="real")
        assert tier == "synthetic"
        assert not tier_is_live(tier)

    def test_live_source_no_explicit_not_live(self) -> None:
        """sources_seen=("live",) with no explicit_tier → synthetic."""
        tier = infer_evidence_tier(sources_seen=("live",))
        assert not tier_is_live(tier)

    def test_explicit_live_service(self) -> None:
        tier = infer_evidence_tier(
            adapter_kind="real",
            sources_seen=("live",),
            explicit_tier="live_service",
        )
        assert tier == "live_service"
        assert tier_is_live(tier)

    def test_explicit_hardware(self) -> None:
        tier = infer_evidence_tier(explicit_tier="hardware")
        assert tier == "hardware"
        assert tier_is_live(tier)


# ===========================================================================
# Docker tier only with explicit marker
# ===========================================================================


class TestDockerTierExplicit:
    """Docker tier only when is_docker_artifact=True."""

    def test_no_docker_marker_not_docker(self) -> None:
        tier = infer_evidence_tier(adapter_kind="real", is_docker_artifact=False)
        assert tier != "docker"

    def test_docker_marker_produces_docker(self) -> None:
        tier = infer_evidence_tier(is_docker_artifact=True)
        assert tier == "docker"

    def test_fake_beats_docker(self) -> None:
        tier = infer_evidence_tier(adapter_kind="fake", is_docker_artifact=True)
        assert tier == "synthetic"

    def test_replay_beats_docker(self) -> None:
        tier = infer_evidence_tier(
            sources_seen=("replay",),
            is_docker_artifact=True,
        )
        assert tier == "synthetic"


# ===========================================================================
# Imports and public API
# ===========================================================================


class TestTiersPublicApi:
    """Tiers module is importable from core.evidence."""

    def test_import_from_package(self) -> None:
        from medre.core.evidence import (
            EVIDENCE_TIER_UNKNOWN,
            EvidenceTier,
            infer_evidence_tier,
            tier_is_live,
        )

        assert EvidenceTier is not None
        assert infer_evidence_tier is not None
        assert tier_is_live is not None
        assert EVIDENCE_TIER_UNKNOWN == ""

    def test_in_all(self) -> None:
        import medre.core.evidence

        assert "EvidenceTier" in medre.core.evidence.__all__
        assert "infer_evidence_tier" in medre.core.evidence.__all__
        assert "tier_is_live" in medre.core.evidence.__all__
        assert "EVIDENCE_TIER_UNKNOWN" in medre.core.evidence.__all__
