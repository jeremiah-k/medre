"""Convergence summary and lifecycle convergence report tests for evidence bundles.

Split from ``test_evidence_bundle.py`` so that each module stays under 1500 lines.
Helpers are intentionally duplicated (see NOTE in ``test_evidence_bundle.py``)
so each file remains independently runnable.

Covers:
- Convergence summary (safe / degraded / inconsistent / empty / multiple targets)
- Lifecycle convergence report (empty / with findings / JSON safety / determinism)
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
from medre.core.evidence.collector import EvidenceCollector
from medre.core.storage.backend import DeliveryOutboxItem
from tests.helpers.storage import make_storage_event

# ---------------------------------------------------------------------------
# Helpers (duplicated for independent runnability)
# ---------------------------------------------------------------------------

_FIXED_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


def _fixed_now() -> datetime:
    return _FIXED_NOW


def _make_receipt(
    receipt_id: str = "rcpt-1",
    event_id: str = "evt-1",
    sequence: int = 1,
    delivery_plan_id: str = "plan-1",
    target_adapter: str = "adapter_a",
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


def _make_outbox_item(
    event_id: str = "evt-1",
    outbox_id: str = "ob-1",
    target_adapter: str = "adapter_a",
    status: str = "sent",
    created_at: str | None = None,
) -> DeliveryOutboxItem:
    return DeliveryOutboxItem(
        outbox_id=outbox_id,
        event_id=event_id,
        route_id="route-1",
        delivery_plan_id="plan-1",
        target_adapter=target_adapter,
        status=status,
        created_at=created_at or "2026-01-15T12:00:00+00:00",
        updated_at="2026-01-15T12:00:01+00:00",
    )


class FakeStorage:
    """Minimal fake storage for unit tests."""

    def __init__(self) -> None:
        self._events: dict[str, CanonicalEvent] = {}
        self._receipts: dict[str, list[DeliveryReceipt]] = {}
        self._native_refs: dict[str, list[NativeMessageRef]] = {}
        self._outbox: dict[str, list[DeliveryOutboxItem]] = {}

    async def get(self, event_id: str) -> CanonicalEvent | None:
        return self._events.get(event_id)

    async def list_receipts_for_event(self, event_id: str) -> list[DeliveryReceipt]:
        return sorted(self._receipts.get(event_id, []), key=lambda r: r.sequence)

    async def list_native_refs_for_event(self, event_id: str) -> list[NativeMessageRef]:
        return sorted(
            self._native_refs.get(event_id, []),
            key=lambda r: (r.created_at, r.id),
        )

    async def list_outbox_items_for_event(
        self, event_id: str
    ) -> list[DeliveryOutboxItem]:
        return self._outbox.get(event_id, [])


def _populated_fake(
    *,
    event_id: str = "evt-1",
    include_event: bool = True,
    receipts: list[DeliveryReceipt] | None = None,
    native_refs: list[NativeMessageRef] | None = None,
    outbox_items: list[DeliveryOutboxItem] | None = None,
) -> FakeStorage:
    """Build a FakeStorage pre-populated with the given data."""
    fs = FakeStorage()
    if include_event:
        fs._events[event_id] = make_storage_event(event_id=event_id)
    if receipts:
        fs._receipts[event_id] = receipts
    if native_refs:
        fs._native_refs[event_id] = native_refs
    if outbox_items:
        fs._outbox[event_id] = outbox_items
    return fs


# ===========================================================================
# Convergence summary in collected bundles
# ===========================================================================


class TestConvergenceSummarySafeEvent:
    """collect_for_event produces convergence_summary with worst_severity=safe
    when all delivery targets are in terminal consistent states."""

    @pytest.mark.asyncio
    async def test_safe_convergence_summary(self) -> None:
        receipt = _make_receipt(
            "rcpt-conv-safe",
            event_id="evt-conv-safe",
            status="sent",
            delivery_plan_id="plan-conv",
        )
        outbox = _make_outbox_item(
            event_id="evt-conv-safe",
            outbox_id="ob-conv-safe",
            target_adapter="adapter_a",
            status="sent",
        )
        # Override delivery_plan_id to match receipt grouping key.
        outbox = DeliveryOutboxItem(
            outbox_id="ob-conv-safe",
            event_id="evt-conv-safe",
            route_id="route-1",
            delivery_plan_id="plan-conv",
            target_adapter="adapter_a",
            target_channel=None,
            status="sent",
            created_at="2026-01-15T12:00:00+00:00",
            updated_at="2026-01-15T12:00:01+00:00",
        )
        storage = _populated_fake(
            event_id="evt-conv-safe",
            receipts=[receipt],
            outbox_items=[outbox],
        )
        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-conv-safe")

        assert bundle.convergence_summary is not None
        cs = bundle.convergence_summary
        assert cs["total_targets"] == 1
        assert cs["worst_severity"] == "safe"
        assert cs["severity_counts"]["safe"] == 1
        assert cs["severity_counts"]["degraded"] == 0
        assert cs["severity_counts"]["inconsistent"] == 0
        assert len(cs["targets"]) == 1
        assert cs["targets"][0]["severity"] == "safe"
        assert cs["targets"][0]["outbox_status"] == "sent"
        assert cs["targets"][0]["latest_receipt_status"] == "sent"

    @pytest.mark.asyncio
    async def test_safe_convergence_json_safe(self) -> None:
        """convergence_summary is JSON-safe within the full bundle."""
        receipt = _make_receipt(
            "rcpt-conv-json",
            event_id="evt-conv-json",
            status="sent",
        )
        storage = _populated_fake(
            event_id="evt-conv-json",
            receipts=[receipt],
        )
        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-conv-json")

        d = bundle.to_dict()
        json_str = json.dumps(d, sort_keys=True)
        parsed = json.loads(json_str)
        assert parsed["convergence_summary"]["worst_severity"] == "safe"


class TestConvergenceSummaryDegradedEvent:
    """collect_for_event produces convergence_summary with degraded severity
    for non-terminal states (pending outbox, failed receipt)."""

    @pytest.mark.asyncio
    async def test_degraded_convergence_pending_outbox(self) -> None:
        outbox = DeliveryOutboxItem(
            outbox_id="ob-conv-deg",
            event_id="evt-conv-deg",
            route_id="route-1",
            delivery_plan_id="plan-deg",
            target_adapter="adapter_a",
            target_channel=None,
            status="pending",
            created_at="2026-01-15T12:00:00+00:00",
            updated_at="2026-01-15T12:00:01+00:00",
        )
        receipt = _make_receipt(
            "rcpt-conv-deg",
            event_id="evt-conv-deg",
            status="failed",
            delivery_plan_id="plan-deg",
        )
        storage = _populated_fake(
            event_id="evt-conv-deg",
            receipts=[receipt],
            outbox_items=[outbox],
        )
        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-conv-deg")

        assert bundle.convergence_summary is not None
        cs = bundle.convergence_summary
        assert cs["worst_severity"] == "degraded"
        assert cs["severity_counts"]["degraded"] == 1
        assert cs["targets"][0]["severity"] == "degraded"

    @pytest.mark.asyncio
    async def test_degraded_in_progress_no_receipt(self) -> None:
        """Outbox in_progress with no receipt → degraded."""
        outbox = DeliveryOutboxItem(
            outbox_id="ob-conv-ip",
            event_id="evt-conv-ip",
            route_id="route-1",
            delivery_plan_id="plan-ip",
            target_adapter="adapter_a",
            target_channel=None,
            status="in_progress",
            created_at="2026-01-15T12:00:00+00:00",
            updated_at="2026-01-15T12:00:01+00:00",
        )
        storage = _populated_fake(
            event_id="evt-conv-ip",
            outbox_items=[outbox],
        )
        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-conv-ip")

        assert bundle.convergence_summary is not None
        assert bundle.convergence_summary["worst_severity"] == "degraded"


class TestConvergenceSummaryInconsistentEvent:
    """collect_for_event produces convergence_summary with inconsistent severity
    for unreconcilable state mismatches."""

    @pytest.mark.asyncio
    async def test_inconsistent_terminal_outbox_non_terminal_receipt(self) -> None:
        receipt = _make_receipt(
            "rcpt-conv-inc",
            event_id="evt-conv-inc",
            status="queued",
            delivery_plan_id="plan-inc",
        )
        outbox = DeliveryOutboxItem(
            outbox_id="ob-conv-inc",
            event_id="evt-conv-inc",
            route_id="route-1",
            delivery_plan_id="plan-inc",
            target_adapter="adapter_a",
            target_channel=None,
            status="sent",
            created_at="2026-01-15T12:00:00+00:00",
            updated_at="2026-01-15T12:00:01+00:00",
        )
        storage = _populated_fake(
            event_id="evt-conv-inc",
            receipts=[receipt],
            outbox_items=[outbox],
        )
        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-conv-inc")

        assert bundle.convergence_summary is not None
        cs = bundle.convergence_summary
        assert cs["worst_severity"] == "inconsistent"
        assert cs["severity_counts"]["inconsistent"] == 1
        target = cs["targets"][0]
        assert target["severity"] == "inconsistent"
        assert target["outbox_status"] == "sent"
        assert target["latest_receipt_status"] == "queued"

    @pytest.mark.asyncio
    async def test_inconsistent_non_terminal_outbox_sent_receipt(self) -> None:
        """Pending outbox but sent receipt → inconsistent."""
        receipt = _make_receipt(
            "rcpt-conv-inc2",
            event_id="evt-conv-inc2",
            status="sent",
            delivery_plan_id="plan-inc2",
        )
        outbox = DeliveryOutboxItem(
            outbox_id="ob-conv-inc2",
            event_id="evt-conv-inc2",
            route_id="route-1",
            delivery_plan_id="plan-inc2",
            target_adapter="adapter_a",
            target_channel=None,
            status="pending",
            created_at="2026-01-15T12:00:00+00:00",
            updated_at="2026-01-15T12:00:01+00:00",
        )
        storage = _populated_fake(
            event_id="evt-conv-inc2",
            receipts=[receipt],
            outbox_items=[outbox],
        )
        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-conv-inc2")

        assert bundle.convergence_summary is not None
        assert bundle.convergence_summary["worst_severity"] == "inconsistent"


class TestConvergenceSummaryEmptyEvent:
    """collect_for_event produces convergence_summary even with no data
    (empty summary with total_targets=0, worst_severity=None)."""

    @pytest.mark.asyncio
    async def test_empty_convergence_summary(self) -> None:
        storage = _populated_fake(event_id="evt-conv-empty")
        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-conv-empty")

        assert bundle.convergence_summary is not None
        cs = bundle.convergence_summary
        assert cs["total_targets"] == 0
        assert cs["worst_severity"] is None
        assert cs["targets"] == []
        assert cs["severity_counts"] == {"safe": 0, "degraded": 0, "inconsistent": 0}

    @pytest.mark.asyncio
    async def test_empty_convergence_json_safe(self) -> None:
        storage = _populated_fake(event_id="evt-conv-empty2")
        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-conv-empty2")

        d = bundle.to_dict()
        json_str = json.dumps(d, sort_keys=True)
        parsed = json.loads(json_str)
        assert parsed["convergence_summary"]["total_targets"] == 0


class TestConvergenceSummaryMultipleTargets:
    """Multiple delivery targets produce independent convergence results."""

    @pytest.mark.asyncio
    async def test_mixed_severity_targets(self) -> None:
        r_safe = _make_receipt(
            "rcpt-safe",
            event_id="evt-conv-mix",
            delivery_plan_id="plan-safe",
            target_adapter="adapter_a",
            status="sent",
            sequence=1,
        )
        r_inc = _make_receipt(
            "rcpt-inc",
            event_id="evt-conv-mix",
            delivery_plan_id="plan-inc",
            target_adapter="adapter_b",
            status="sent",
            sequence=2,
        )
        ob_safe = DeliveryOutboxItem(
            outbox_id="ob-safe",
            event_id="evt-conv-mix",
            route_id="route-1",
            delivery_plan_id="plan-safe",
            target_adapter="adapter_a",
            target_channel=None,
            status="sent",
            created_at="2026-01-15T12:00:00+00:00",
            updated_at="2026-01-15T12:00:01+00:00",
        )
        ob_inc = DeliveryOutboxItem(
            outbox_id="ob-inc",
            event_id="evt-conv-mix",
            route_id="route-1",
            delivery_plan_id="plan-inc",
            target_adapter="adapter_b",
            target_channel=None,
            status="pending",
            created_at="2026-01-15T12:00:00+00:00",
            updated_at="2026-01-15T12:00:01+00:00",
        )
        storage = _populated_fake(
            event_id="evt-conv-mix",
            receipts=[r_safe, r_inc],
            outbox_items=[ob_safe, ob_inc],
        )
        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-conv-mix")

        cs = bundle.convergence_summary
        assert cs is not None
        assert cs["total_targets"] == 2
        assert cs["severity_counts"]["safe"] == 1
        assert cs["severity_counts"]["inconsistent"] == 1
        assert cs["worst_severity"] == "inconsistent"


# ===========================================================================
# Lifecycle convergence report in collected bundles
# ===========================================================================


class TestLifecycleConvergenceReportEmpty:
    """collect_for_event produces lifecycle_convergence_report with no findings
    when all delivery states are clean."""

    @pytest.mark.asyncio
    async def test_empty_lifecycle_report(self) -> None:
        storage = _populated_fake(event_id="evt-lc-empty")
        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-lc-empty")

        assert bundle.lifecycle_convergence_report is not None
        report = bundle.lifecycle_convergence_report
        assert report["total_findings"] == 0
        assert report["findings"] == []
        assert report["worst_severity"] is None
        assert report["severity_counts"] == {
            "safe": 0,
            "degraded": 0,
            "inconsistent": 0,
        }

    @pytest.mark.asyncio
    async def test_empty_lifecycle_report_json_safe(self) -> None:
        storage = _populated_fake(event_id="evt-lc-json")
        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-lc-json")

        d = bundle.to_dict()
        json_str = json.dumps(d, sort_keys=True)
        parsed = json.loads(json_str)
        assert parsed["lifecycle_convergence_report"]["total_findings"] == 0


class TestLifecycleConvergenceReportWithFindings:
    """collect_for_event produces lifecycle_convergence_report with findings
    when receipt/outbox states contradict normal delivery flow."""

    @pytest.mark.asyncio
    async def test_terminal_receipt_nonterminal_outbox(self) -> None:
        """Sent receipt + pending outbox → terminal_receipt_nonterminal_outbox finding."""
        receipt = _make_receipt(
            "rcpt-lc-1",
            event_id="evt-lc-trno",
            status="sent",
            delivery_plan_id="plan-lc",
        )
        outbox = DeliveryOutboxItem(
            outbox_id="ob-lc-1",
            event_id="evt-lc-trno",
            route_id="route-1",
            delivery_plan_id="plan-lc",
            target_adapter="adapter_a",
            target_channel=None,
            status="pending",
            created_at="2026-01-15T12:00:00+00:00",
            updated_at="2026-01-15T12:00:01+00:00",
        )
        storage = _populated_fake(
            event_id="evt-lc-trno",
            receipts=[receipt],
            outbox_items=[outbox],
        )
        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-lc-trno")

        report = bundle.lifecycle_convergence_report
        assert report is not None
        assert report["total_findings"] >= 1
        # Find the specific finding.
        kinds = [f["kind"] for f in report["findings"]]
        assert "terminal_receipt_nonterminal_outbox" in kinds
        # Severity should be degraded.
        assert report["severity_counts"]["degraded"] >= 1
        assert report["worst_severity"] == "degraded"

    @pytest.mark.asyncio
    async def test_lifecycle_findings_json_safe(self) -> None:
        """Lifecycle convergence findings are JSON-safe within full bundle."""
        receipt = _make_receipt(
            "rcpt-lc-js",
            event_id="evt-lc-js",
            status="sent",
            delivery_plan_id="plan-js",
        )
        outbox = DeliveryOutboxItem(
            outbox_id="ob-lc-js",
            event_id="evt-lc-js",
            route_id="route-1",
            delivery_plan_id="plan-js",
            target_adapter="adapter_a",
            target_channel=None,
            status="pending",
            created_at="2026-01-15T12:00:00+00:00",
            updated_at="2026-01-15T12:00:01+00:00",
        )
        storage = _populated_fake(
            event_id="evt-lc-js",
            receipts=[receipt],
            outbox_items=[outbox],
        )
        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-lc-js")

        d = bundle.to_dict()
        json_str = json.dumps(d, sort_keys=True)
        parsed = json.loads(json_str)
        assert json.loads(json_str) == parsed
        report = parsed["lifecycle_convergence_report"]
        assert report["total_findings"] >= 1
        # Finding dict keys must be sorted (from OrphanFinding.to_dict).
        for finding in report["findings"]:
            assert sorted(finding.keys()) == list(finding.keys())

    @pytest.mark.asyncio
    async def test_lifecycle_findings_deterministic_ordering(self) -> None:
        """Lifecycle findings are sorted by (kind, record_id)."""
        # Create two mismatched targets to get multiple findings.
        r1 = _make_receipt(
            "rcpt-lc-det1",
            event_id="evt-lc-det",
            status="sent",
            delivery_plan_id="plan-det1",
            target_adapter="adapter_a",
            sequence=1,
        )
        r2 = _make_receipt(
            "rcpt-lc-det2",
            event_id="evt-lc-det",
            status="sent",
            delivery_plan_id="plan-det2",
            target_adapter="adapter_b",
            sequence=2,
        )
        ob1 = DeliveryOutboxItem(
            outbox_id="ob-det1",
            event_id="evt-lc-det",
            route_id="route-1",
            delivery_plan_id="plan-det1",
            target_adapter="adapter_a",
            target_channel=None,
            status="pending",
            created_at="2026-01-15T12:00:00+00:00",
            updated_at="2026-01-15T12:00:01+00:00",
        )
        ob2 = DeliveryOutboxItem(
            outbox_id="ob-det2",
            event_id="evt-lc-det",
            route_id="route-1",
            delivery_plan_id="plan-det2",
            target_adapter="adapter_b",
            target_channel=None,
            status="pending",
            created_at="2026-01-15T12:00:00+00:00",
            updated_at="2026-01-15T12:00:01+00:00",
        )
        storage = _populated_fake(
            event_id="evt-lc-det",
            receipts=[r1, r2],
            outbox_items=[ob1, ob2],
        )
        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-lc-det")

        report = bundle.lifecycle_convergence_report
        assert report is not None
        findings = report["findings"]
        # Verify deterministic ordering: sorted by (kind, record_id).
        keys = [(f["kind"], f["record_id"]) for f in findings]
        assert keys == sorted(keys)

    @pytest.mark.asyncio
    async def test_lifecycle_report_not_duplicated_in_orphan_report(self) -> None:
        """Lifecycle findings use separate kinds from orphan findings."""
        receipt = _make_receipt(
            "rcpt-lc-sep",
            event_id="evt-lc-sep",
            status="sent",
            delivery_plan_id="plan-sep",
        )
        outbox = DeliveryOutboxItem(
            outbox_id="ob-lc-sep",
            event_id="evt-lc-sep",
            route_id="route-1",
            delivery_plan_id="plan-sep",
            target_adapter="adapter_a",
            target_channel=None,
            status="pending",
            created_at="2026-01-15T12:00:00+00:00",
            updated_at="2026-01-15T12:00:01+00:00",
        )
        storage = _populated_fake(
            event_id="evt-lc-sep",
            receipts=[receipt],
            outbox_items=[outbox],
        )
        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-lc-sep")

        # Lifecycle report has lifecycle-specific kinds.
        lc_kinds = [f["kind"] for f in bundle.lifecycle_convergence_report["findings"]]
        # Orphan report should NOT contain any lifecycle kinds.
        lifecycle_kind_set = {
            "receipt_outbox_mismatch",
            "terminal_receipt_nonterminal_outbox",
            "terminal_outbox_nonterminal_receipt",
            "retry_wait_missing_next_retry",
            "next_retry_in_past",
            "retryable_without_retry_metadata",
            "stalled_delivery_plan",
            "attempt_count_regression",
            "receipt_sequence_gap",
        }
        orphan_kinds = [f["kind"] for f in bundle.orphan_report["findings"]]
        overlap = set(orphan_kinds) & lifecycle_kind_set
        assert not overlap, f"Orphan report contains lifecycle kinds: {overlap}"
        # At least one lifecycle finding should exist.
        assert len(lc_kinds) >= 1
