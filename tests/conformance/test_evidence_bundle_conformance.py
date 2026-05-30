"""Conformance tests for evidence bundle assembly.

Covers the lifecycle conformance of evidence bundle construction:
- Sent delivery evidence bundle
- Queued delivery evidence bundle
- Queued -> sent evidence bundle
- Suppressed / non-rendered bundle behavior
- Replay-origin receipt evidence bundle
- Invalid rendering_evidence JSON warning

These tests validate that EvidenceCollector produces structurally
correct, JSON-safe, deterministically ordered bundles for each
delivery lifecycle scenario.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from medre.core.events import (
    DeliveryReceipt,
)
from medre.core.evidence.bundle import (
    BUNDLE_SCHEMA_VERSION,
    EvidenceBundle,
)
from medre.core.evidence.collector import EvidenceCollector
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.storage import make_storage_event

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
# NOTE: _make_receipt / _fixed_now are intentionally duplicated from
# tests/test_evidence_bundle.py (with different defaults) so each test file
# remains independently runnable without cross-directory imports coupling
# their setups together.
# ---------------------------------------------------------------------------

_FIXED_NOW = datetime(2026, 3, 15, 10, 30, 0, tzinfo=timezone.utc)


def _fixed_now() -> datetime:
    return _FIXED_NOW


def _make_receipt(
    receipt_id: str = "rcpt-1",
    event_id: str = "evt-1",
    sequence: int = 1,
    delivery_plan_id: str = "plan-1",
    target_adapter: str = "meshtastic_adapter",
    target_channel: str | None = None,
    status: str = "sent",
    attempt_number: int = 1,
    source: str = "live",
    replay_run_id: str | None = None,
    rendering_evidence: str | None = None,
    created_at: datetime | None = None,
) -> DeliveryReceipt:
    return DeliveryReceipt(
        sequence=sequence,
        receipt_id=receipt_id,
        event_id=event_id,
        delivery_plan_id=delivery_plan_id,
        target_adapter=target_adapter,
        target_channel=target_channel,
        route_id="route-1",
        status=status,  # type: ignore[arg-type]
        attempt_number=attempt_number,
        source=source,
        replay_run_id=replay_run_id,
        rendering_evidence=rendering_evidence,
        created_at=created_at or _FIXED_NOW,
    )


def _assert_json_safe(bundle: EvidenceBundle) -> None:
    """Assert bundle serializes to valid JSON without custom encoder."""
    d = bundle.to_dict()
    serialized = json.dumps(d, sort_keys=True)
    assert json.loads(serialized) == d


# ===========================================================================
# Conformance: Sent delivery evidence bundle
# ===========================================================================


class TestSentDeliveryEvidenceConformance:
    """A sent delivery produces a complete evidence bundle."""

    @pytest.mark.asyncio
    async def test_sent_bundle_structure(self, temp_storage: SQLiteStorage) -> None:
        event_id = "evt-sent-conf"
        event = make_storage_event(event_id=event_id)
        await temp_storage.append(event)

        receipt = _make_receipt(
            "rcpt-sent",
            event_id=event_id,
            status="sent",
            rendering_evidence='{"delivery_strategy": "direct", "truncated": false}',
        )
        await temp_storage.append_receipt(receipt)

        collector = EvidenceCollector(temp_storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event(event_id)

        assert bundle.event_id == event_id
        assert bundle.event_summary is not None
        assert bundle.event_summary["event_kind"] == "message.created"
        assert len(bundle.delivery_receipts) == 1

        rcpt = bundle.delivery_receipts[0]
        assert rcpt.status == "sent"
        assert rcpt.rendering_evidence == {
            "delivery_strategy": "direct",
            "truncated": False,
        }
        assert bundle.sources_seen == ("live",)
        assert bundle.replay_run_ids == ()
        _assert_json_safe(bundle)


# ===========================================================================
# Conformance: Queued delivery evidence bundle
# ===========================================================================


class TestQueuedDeliveryEvidenceConformance:
    """A queued delivery (adapter-local queue acceptance) evidence bundle."""

    @pytest.mark.asyncio
    async def test_queued_bundle_structure(self, temp_storage: SQLiteStorage) -> None:
        event_id = "evt-queued-conf"
        event = make_storage_event(event_id=event_id)
        await temp_storage.append(event)

        receipt = _make_receipt(
            "rcpt-queued",
            event_id=event_id,
            status="queued",
            target_adapter="meshtastic_adapter",
        )
        await temp_storage.append_receipt(receipt)

        collector = EvidenceCollector(temp_storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event(event_id)

        rcpt = bundle.delivery_receipts[0]
        assert rcpt.status == "queued"
        assert rcpt.rendering_evidence is None
        # No rendering_evidence warning for None.
        assert not any("rendering_evidence" in w for w in bundle.warnings)
        _assert_json_safe(bundle)


# ===========================================================================
# Conformance: Queued -> Sent evidence bundle
# ===========================================================================


class TestQueuedToSentEvidenceConformance:
    """A queued then sent delivery produces both receipts in the bundle."""

    @pytest.mark.asyncio
    async def test_queued_to_sent_bundle(self, temp_storage: SQLiteStorage) -> None:
        event_id = "evt-qs-conf"
        event = make_storage_event(event_id=event_id)
        await temp_storage.append(event)

        queued = _make_receipt(
            "rcpt-q",
            event_id=event_id,
            status="queued",
            sequence=1,
        )
        sent = _make_receipt(
            "rcpt-s",
            event_id=event_id,
            status="sent",
            sequence=2,
            rendering_evidence='{"delivery_strategy": "direct"}',
        )
        await temp_storage.append_receipt(queued)
        await temp_storage.append_receipt(sent)

        collector = EvidenceCollector(temp_storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event(event_id)

        assert len(bundle.delivery_receipts) == 2
        statuses = [r.status for r in bundle.delivery_receipts]
        assert statuses == ["queued", "sent"]

        # Second receipt has rendering evidence.
        assert bundle.delivery_receipts[1].rendering_evidence == {
            "delivery_strategy": "direct",
        }
        _assert_json_safe(bundle)


# ===========================================================================
# Conformance: Suppressed / non-rendered bundle behavior
# ===========================================================================


class TestSuppressedEvidenceConformance:
    """Suppressed deliveries carry no rendering evidence and produce no warning."""

    @pytest.mark.asyncio
    async def test_suppressed_bundle(self, temp_storage: SQLiteStorage) -> None:
        event_id = "evt-sup-conf"
        event = make_storage_event(event_id=event_id)
        await temp_storage.append(event)

        receipt = _make_receipt(
            "rcpt-sup",
            event_id=event_id,
            status="suppressed",
            rendering_evidence=None,
        )
        await temp_storage.append_receipt(receipt)

        collector = EvidenceCollector(temp_storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event(event_id)

        rcpt = bundle.delivery_receipts[0]
        assert rcpt.status == "suppressed"
        assert rcpt.rendering_evidence is None
        # No warnings about missing rendering_evidence for suppressed.
        assert not any("rendering_evidence" in w for w in bundle.warnings)
        _assert_json_safe(bundle)


# ===========================================================================
# Conformance: Replay-origin receipt evidence bundle
# ===========================================================================


class TestReplayOriginEvidenceConformance:
    """Replay-origin receipts are tracked with replay_run_id."""

    @pytest.mark.asyncio
    async def test_replay_bundle(self, temp_storage: SQLiteStorage) -> None:
        event_id = "evt-replay-conf"
        event = make_storage_event(event_id=event_id)
        await temp_storage.append(event)

        live = _make_receipt(
            "rcpt-live",
            event_id=event_id,
            source="live",
            sequence=1,
        )
        replay = _make_receipt(
            "rcpt-replay",
            event_id=event_id,
            source="replay",
            replay_run_id="run-42",
            sequence=2,
        )
        await temp_storage.append_receipt(live)
        await temp_storage.append_receipt(replay)

        collector = EvidenceCollector(temp_storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event(event_id)

        assert bundle.replay_run_ids == ("run-42",)
        assert "live" in bundle.sources_seen
        assert "replay" in bundle.sources_seen

        # Receipts ordered by sequence.
        assert bundle.delivery_receipts[0].source == "live"
        assert bundle.delivery_receipts[1].source == "replay"
        assert bundle.delivery_receipts[1].replay_run_id == "run-42"
        _assert_json_safe(bundle)


# ===========================================================================
# Conformance: Invalid rendering_evidence JSON warning
# ===========================================================================


class TestInvalidRenderingEvidenceConformance:
    """Invalid rendering_evidence produces a warning, not a crash."""

    @pytest.mark.asyncio
    async def test_invalid_json_warns(self, temp_storage: SQLiteStorage) -> None:
        event_id = "evt-invalid-conf"
        event = make_storage_event(event_id=event_id)
        await temp_storage.append(event)

        receipt = _make_receipt(
            "rcpt-invalid",
            event_id=event_id,
            rendering_evidence="{broken json",
        )
        await temp_storage.append_receipt(receipt)

        collector = EvidenceCollector(temp_storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event(event_id)

        rcpt = bundle.delivery_receipts[0]
        assert rcpt.rendering_evidence is None
        assert any("Invalid rendering_evidence" in w for w in bundle.warnings)
        # Bundle is still JSON-safe.
        _assert_json_safe(bundle)

    @pytest.mark.asyncio
    async def test_non_object_json_warns(self, temp_storage: SQLiteStorage) -> None:
        event_id = "evt-nonobj-conf"
        event = make_storage_event(event_id=event_id)
        await temp_storage.append(event)

        receipt = _make_receipt(
            "rcpt-nonobj",
            event_id=event_id,
            rendering_evidence="42",
        )
        await temp_storage.append_receipt(receipt)

        collector = EvidenceCollector(temp_storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event(event_id)

        assert any("Non-object rendering_evidence" in w for w in bundle.warnings)
        _assert_json_safe(bundle)


# ===========================================================================
# Conformance: Bundle schema and deterministic ordering
# ===========================================================================


class TestBundleSchemaConformance:
    """Every bundle must conform to the schema contract."""

    @pytest.mark.asyncio
    async def test_schema_version_is_correct(self, temp_storage: SQLiteStorage) -> None:
        event_id = "evt-schema"
        await temp_storage.append(make_storage_event(event_id=event_id))
        collector = EvidenceCollector(temp_storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event(event_id)
        assert bundle.schema_version == BUNDLE_SCHEMA_VERSION
        assert bundle.to_dict()["schema_version"] == BUNDLE_SCHEMA_VERSION

    @pytest.mark.asyncio
    async def test_deterministic_json_output(self, temp_storage: SQLiteStorage) -> None:
        """Two collections with the same clock produce identical JSON."""
        event_id = "evt-deterministic"
        await temp_storage.append(make_storage_event(event_id=event_id))
        await temp_storage.append_receipt(_make_receipt("rcpt-det", event_id=event_id))

        collector = EvidenceCollector(temp_storage, now_fn=_fixed_now)
        bundle1 = await collector.collect_for_event(event_id)
        bundle2 = await collector.collect_for_event(event_id)

        assert bundle1.to_json() == bundle2.to_json()
