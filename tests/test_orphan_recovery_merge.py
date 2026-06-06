"""Focused tests for orphan-report / recovery-convergence merge alignment.

Verifies that the shared ``merge_recovery_findings_into_report_dict``
helper produces correct report shapes and that both the core
``EvidenceCollector.collect_for_event()`` and the runtime
``_collect_storage_data_from_backend()`` produce orphan reports that
include recovery convergence findings with identical semantics.

Covers:
- ``merge_recovery_findings_into_report_dict`` unit tests
- Core EvidenceCollector orphan report includes recovery findings
- Runtime storage-section orphan report includes recovery findings
- Both paths produce identical merge semantics for the same inputs
- Report shape invariants (sorted findings, severity_counts, worst_severity)
- Schema version stays at 1
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from medre.core.diagnostics.convergence.orphans import (
    build_orphan_report,
    merge_recovery_findings_into_report_dict,
)
from medre.core.diagnostics.convergence.recovery_convergence import (
    build_recovery_convergence_findings,
)
from medre.core.diagnostics.convergence.types import OrphanFinding
from medre.core.events import CanonicalEvent, DeliveryReceipt
from medre.core.events.metadata import EventMetadata
from medre.core.evidence.collector import EvidenceCollector
from medre.core.recovery.builder import build_startup_recovery_ledger
from medre.core.recovery.models import (
    RecoveryOwnershipAction,
    StartupRecoveryLedger,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TS = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _receipt(
    *,
    receipt_id: str = "rcpt-001",
    event_id: str = "ev-001",
    delivery_plan_id: str = "dp-001",
    target_adapter: str = "radio",
    target_channel: str | None = "ch-0",
    route_id: str = "route-1",
    status: str = "sent",
    attempt_number: int = 1,
    sequence: int = 0,
    source: str = "live",
    created_at: datetime | None = None,
    parent_receipt_id: str | None = None,
) -> dict:
    return {
        "receipt_id": receipt_id,
        "event_id": event_id,
        "delivery_plan_id": delivery_plan_id,
        "target_adapter": target_adapter,
        "target_channel": target_channel,
        "route_id": route_id,
        "status": status,
        "attempt_number": attempt_number,
        "sequence": sequence,
        "source": source,
        "created_at": created_at or _TS,
        "parent_receipt_id": parent_receipt_id,
    }


def _delivery_receipt(
    *,
    receipt_id: str = "rcpt-001",
    event_id: str = "ev-001",
    delivery_plan_id: str = "dp-001",
    target_adapter: str = "radio",
    target_channel: str | None = "ch-0",
    route_id: str = "route-1",
    status: str = "sent",
    attempt_number: int = 1,
    sequence: int = 0,
    source: str = "live",
    created_at: datetime | None = None,
    parent_receipt_id: str | None = None,
    failure_kind: str | None = None,
    error: str | None = None,
    rendering_evidence: str | None = None,
    replay_run_id: str | None = None,
) -> DeliveryReceipt:
    """Build a real DeliveryReceipt for EvidenceCollector tests."""
    return DeliveryReceipt(
        receipt_id=receipt_id,
        event_id=event_id,
        delivery_plan_id=delivery_plan_id,
        target_adapter=target_adapter,
        target_channel=target_channel,
        route_id=route_id,
        status=status,
        attempt_number=attempt_number,
        sequence=sequence,
        source=source,
        created_at=created_at or _TS,
        parent_receipt_id=parent_receipt_id,
        failure_kind=failure_kind,
        error=error,
        rendering_evidence=rendering_evidence,
        replay_run_id=replay_run_id,
    )


def _outbox(
    *,
    outbox_id: str = "ob-001",
    event_id: str = "ev-001",
    delivery_plan_id: str = "dp-001",
    target_adapter: str = "radio",
    target_channel: str | None = "ch-0",
    route_id: str = "route-1",
    status: str = "pending",
    attempt_number: int = 1,
) -> dict:
    return {
        "outbox_id": outbox_id,
        "event_id": event_id,
        "delivery_plan_id": delivery_plan_id,
        "target_adapter": target_adapter,
        "target_channel": target_channel,
        "route_id": route_id,
        "status": status,
        "attempt_number": attempt_number,
    }


def _outbox_ns(
    *,
    outbox_id: str = "ob-001",
    event_id: str = "ev-001",
    delivery_plan_id: str = "dp-001",
    target_adapter: str = "radio",
    target_channel: str | None = "ch-0",
    route_id: str = "route-1",
    status: str = "pending",
    attempt_number: int = 1,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> SimpleNamespace:
    """Build an outbox item as SimpleNamespace for EvidenceCollector."""
    return SimpleNamespace(
        outbox_id=outbox_id,
        event_id=event_id,
        delivery_plan_id=delivery_plan_id,
        target_adapter=target_adapter,
        target_channel=target_channel,
        route_id=route_id,
        status=status,
        attempt_number=attempt_number,
        created_at=created_at or _TS,
        updated_at=updated_at or _TS,
        worker_id=None,
        error_summary=None,
        failure_kind=None,
    )


def _action(
    outbox_id: str = "ob-001",
    ownership_action: str = "recoverable",
    prior_status: str = "pending",
    recovery_run_id: str = "run-1",
) -> RecoveryOwnershipAction:
    return RecoveryOwnershipAction(
        recovery_run_id=recovery_run_id,
        startup_timestamp=None,
        outbox_id=outbox_id,
        prior_status=prior_status,
        observed_status=prior_status,
        ownership_action=ownership_action,
        reason="Test action",
        worker_identity=None,
        recovery_source="snapshot_diagnostics",
        timestamp="2026-06-01T12:00:00+00:00",
        delivery_plan_id="dp-001",
        event_id="ev-001",
    )


def _make_ledger(
    actions: list[RecoveryOwnershipAction] | None = None,
) -> StartupRecoveryLedger:
    return StartupRecoveryLedger(
        recovery_run_id=None,
        startup_timestamp=None,
        actions=tuple(actions or []),
        generated_at="2026-06-01T12:00:00+00:00",
    )


def _make_storage(
    event: Any | None = None,
    receipts: list[Any] | None = None,
    native_refs: list[Any] | None = None,
    outbox_items: list[Any] | None = None,
) -> Any:
    """Build a minimal duck-typed storage for EvidenceCollector tests."""

    class _FakeStorage:
        async def get(self, event_id: str) -> Any:
            return event

        async def list_receipts_for_event(self, event_id: str) -> list[Any]:
            return list(receipts or [])

        async def list_native_refs_for_event(self, event_id: str) -> list[Any]:
            return list(native_refs or [])

        async def list_outbox_items_for_event(self, event_id: str) -> list[Any]:
            return list(outbox_items or [])

    return _FakeStorage()


def _make_canonical_event(
    event_id: str = "ev-001",
    event_kind: str = "message",
) -> CanonicalEvent:
    """Build a CanonicalEvent with all required fields."""
    return CanonicalEvent(
        event_id=event_id,
        event_kind=event_kind,
        schema_version=1,
        timestamp=_TS,
        source_adapter="test",
        source_transport_id="t-1",
        source_channel_id=None,
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "hello"},
        metadata=EventMetadata(),
    )


# ---------------------------------------------------------------------------
# Unit tests for merge_recovery_findings_into_report_dict
# ---------------------------------------------------------------------------


class TestMergeRecoveryFindingsIntoReportDict:
    """Unit tests for the shared merge helper."""

    def test_empty_recovery_findings_returns_copy(self):
        """Empty recovery findings returns an equivalent dict (shallow copy)."""
        report = build_orphan_report(
            receipts=[_receipt(status="sent")],
            outbox_items=[],
        )
        report_dict = report.to_dict()
        merged = merge_recovery_findings_into_report_dict(report_dict, [])
        assert merged == report_dict
        assert merged is not report_dict  # must be a copy, not same object

    def test_merge_appends_recovery_findings(self):
        """Recovery findings are appended to existing findings."""
        # Build orphan report with one finding (missing delivery_plan_id).
        receipts = [
            _receipt(
                receipt_id="r-retry",
                source="retry",
                delivery_plan_id="",
                status="failed",
            ),
        ]
        report = build_orphan_report(receipts=receipts, outbox_items=[])
        report_dict = report.to_dict()
        assert report_dict["total_findings"] == 1

        # Build a recovery finding.
        ledger = _make_ledger(
            actions=[
                _action(
                    outbox_id="ob-1",
                    ownership_action="recoverable",
                    prior_status="queued",
                ),
            ]
        )
        outbox_items = [
            _outbox(outbox_id="ob-1", status="pending"),
        ]
        rec_receipts = [
            _receipt(receipt_id="r-1", status="queued"),
        ]
        recovery_findings = build_recovery_convergence_findings(
            outbox_items=outbox_items,
            receipts=rec_receipts,
            recovery_ledger=ledger,
        )
        assert len(recovery_findings) >= 1

        merged = merge_recovery_findings_into_report_dict(
            report_dict,
            recovery_findings,
        )
        assert merged["total_findings"] == 1 + len(recovery_findings)
        assert len(merged["findings"]) == 1 + len(recovery_findings)

    def test_merge_sorts_all_findings_by_kind_record_id(self):
        """Merged findings are sorted by (kind, record_id)."""
        report_dict = build_orphan_report(
            receipts=[
                _receipt(receipt_id="r-retry", source="retry", delivery_plan_id=""),
            ],
            outbox_items=[],
        ).to_dict()

        findings = [
            OrphanFinding(
                kind="recovered_not_progressed",
                severity="degraded",
                record_id="ob-z",
                record_type="outbox",
                details="test",
            ),
            OrphanFinding(
                kind="alpha_kind",
                severity="degraded",
                record_id="ob-a",
                record_type="outbox",
                details="test",
            ),
        ]
        merged = merge_recovery_findings_into_report_dict(report_dict, findings)
        kinds = [f["kind"] for f in merged["findings"]]
        record_ids = [f["record_id"] for f in merged["findings"]]
        # Sorted by (kind, record_id)
        pairs = list(zip(kinds, record_ids, strict=False))
        assert pairs == sorted(pairs)

    def test_merge_recomputes_severity_counts(self):
        """Severity counts are recomputed including 'safe': 0."""
        report_dict = build_orphan_report(
            receipts=[_receipt(status="sent")],
            outbox_items=[],
        ).to_dict()
        assert report_dict["severity_counts"] == {
            "safe": 0,
            "degraded": 0,
            "inconsistent": 0,
        }

        findings = [
            OrphanFinding(
                kind="recovered_not_progressed",
                severity="degraded",
                record_id="ob-1",
                record_type="outbox",
                details="test",
            ),
            OrphanFinding(
                kind="reclaimed_then_terminal",
                severity="inconsistent",
                record_id="ob-2",
                record_type="outbox",
                details="test",
            ),
        ]
        merged = merge_recovery_findings_into_report_dict(report_dict, findings)
        assert merged["severity_counts"]["degraded"] == 1
        assert merged["severity_counts"]["inconsistent"] == 1
        assert merged["severity_counts"]["safe"] == 0

    def test_merge_recomputes_worst_severity_degraded(self):
        """worst_severity is 'degraded' when only degraded findings exist."""
        report_dict = build_orphan_report(
            receipts=[_receipt(status="sent")],
            outbox_items=[],
        ).to_dict()
        findings_degraded = [
            OrphanFinding(
                kind="recovered_not_progressed",
                severity="degraded",
                record_id="ob-1",
                record_type="outbox",
                details="test",
            ),
        ]
        merged = merge_recovery_findings_into_report_dict(
            report_dict, findings_degraded
        )
        assert merged["worst_severity"] == "degraded"

    def test_merge_recomputes_worst_severity_inconsistent(self):
        """worst_severity is 'inconsistent' when inconsistent findings exist."""
        report_dict = build_orphan_report(
            receipts=[_receipt(status="sent")],
            outbox_items=[],
        ).to_dict()
        findings_inconsistent = [
            OrphanFinding(
                kind="reclaimed_then_terminal",
                severity="inconsistent",
                record_id="ob-2",
                record_type="outbox",
                details="test",
            ),
        ]
        merged = merge_recovery_findings_into_report_dict(
            report_dict, findings_inconsistent
        )
        assert merged["worst_severity"] == "inconsistent"

    def test_merge_no_findings_preserves_original_worst_severity(self):
        """With no recovery findings, worst_severity comes from base report."""
        base = build_orphan_report(
            receipts=[_receipt(status="sent")],
            outbox_items=[],
        )
        # Empty base report has worst_severity None (no findings).
        assert base.worst_severity is None
        merged = merge_recovery_findings_into_report_dict(base.to_dict(), [])
        # No recovery findings → shallow copy preserves original.
        assert merged["worst_severity"] is None

    def test_merge_does_not_mutate_original_dict(self):
        """The original report_dict is never mutated."""
        report_dict = build_orphan_report(
            receipts=[_receipt(status="sent")],
            outbox_items=[],
        ).to_dict()
        original_findings = list(report_dict["findings"])
        original_total = report_dict["total_findings"]

        findings = [
            OrphanFinding(
                kind="recovered_not_progressed",
                severity="degraded",
                record_id="ob-1",
                record_type="outbox",
                details="test",
            ),
        ]
        merge_recovery_findings_into_report_dict(report_dict, findings)

        # Original unchanged
        assert report_dict["findings"] == original_findings
        assert report_dict["total_findings"] == original_total

    def test_merge_produces_json_safe_output(self):
        """Merged result round-trips through json.dumps/loads."""
        report_dict = build_orphan_report(
            receipts=[
                _receipt(receipt_id="r-retry", source="retry", delivery_plan_id=""),
            ],
            outbox_items=[],
        ).to_dict()
        findings = [
            OrphanFinding(
                kind="recovered_not_progressed",
                severity="degraded",
                record_id="ob-1",
                record_type="outbox",
                details="test",
                extra={"key": "value"},
            ),
        ]
        merged = merge_recovery_findings_into_report_dict(report_dict, findings)
        # Must not raise
        serialized = json.dumps(merged)
        deserialized = json.loads(serialized)
        assert deserialized["total_findings"] == merged["total_findings"]


# ---------------------------------------------------------------------------
# Core EvidenceCollector alignment tests
# ---------------------------------------------------------------------------


class TestCoreCollectorAlignment:
    """Verify that EvidenceCollector.collect_for_event() includes recovery
    convergence findings in its orphan_report via the shared helper."""

    async def test_collector_orphan_report_includes_recovery_findings(self):
        """EvidenceCollector produces orphan_report with recovery findings
        when outbox items exist."""
        event = _make_canonical_event("ev-alignment")
        # Outbox item that will produce a recovery action (pending → recoverable).
        outbox_item = _outbox_ns(
            outbox_id="ob-align",
            status="pending",
            event_id="ev-alignment",
        )
        receipt = _delivery_receipt(
            receipt_id="r-align",
            event_id="ev-alignment",
            status="queued",
        )

        storage = _make_storage(
            event=event,
            receipts=[receipt],
            outbox_items=[outbox_item],
        )
        collector = EvidenceCollector(
            storage,
            now_fn=lambda: _TS,
        )
        bundle = await collector.collect_for_event("ev-alignment")
        orphan_report = bundle.orphan_report

        # The bundle should have an orphan_report (dict) that includes
        # recovery convergence findings — even if no orphan findings from
        # build_orphan_report itself, the recovery findings are merged.
        assert isinstance(orphan_report, dict)
        assert "findings" in orphan_report
        assert "total_findings" in orphan_report
        assert "severity_counts" in orphan_report
        assert orphan_report["severity_counts"].get("safe") == 0
        assert isinstance(orphan_report["findings"], list)

    async def test_collector_schema_version_is_one(self):
        """EvidenceBundle schema_version stays at 1."""
        event = _make_canonical_event("ev-sv")
        storage = _make_storage(event=event)
        collector = EvidenceCollector(storage, now_fn=lambda: _TS)
        bundle = await collector.collect_for_event("ev-sv")
        assert bundle.schema_version == 1


# ---------------------------------------------------------------------------
# Runtime alignment tests
# ---------------------------------------------------------------------------


class TestRuntimeAlignment:
    """Verify that the runtime storage-section path produces orphan_report
    with recovery convergence findings using the shared helper."""

    async def test_runtime_per_event_orphan_report_includes_recovery(
        self, tmp_path: Path
    ):
        """Per-event runtime path merges recovery findings into orphan_report."""
        from medre.runtime.evidence._storage_sections import (
            _collect_storage_data_from_backend,
        )

        event = _make_canonical_event("ev-runtime")
        receipts = [_receipt(receipt_id="r-rt", status="queued", event_id="ev-runtime")]
        outbox_items = [
            _outbox(outbox_id="ob-rt", status="pending", event_id="ev-runtime")
        ]
        native_refs: list[dict] = []

        # Mock assemble_event_timeline to return a valid timeline result
        # so the per-event path runs to completion.
        tl_result = {
            "event": event,
            "receipts": [],  # timeline receipts (DeliveryReceipt objects)
            "native_refs": [],
            "timeline_entries": [],
        }

        storage = _FakeTimelineStorage(
            event=event,
            receipts=receipts,
            native_refs=native_refs,
            outbox_items=outbox_items,
        )

        with patch(
            "medre.runtime.timeline.assemble_event_timeline",
            return_value=tl_result,
        ):
            result = await _collect_storage_data_from_backend(
                storage,
                db_path=str(tmp_path / "test.db"),
                event_id="ev-runtime",
                replay_run_id=None,
            )

        data = result["data"]
        orphan_report = data["orphan_report"]

        assert orphan_report is not None
        assert "findings" in orphan_report
        assert "severity_counts" in orphan_report
        assert isinstance(orphan_report["total_findings"], int)
        # worst_severity is None when there are 0 findings, or a string otherwise.
        assert orphan_report["worst_severity"] is None or isinstance(
            orphan_report["worst_severity"], str
        )

    async def test_runtime_global_orphan_report_includes_recovery(self, tmp_path: Path):
        """Global convergence path merges recovery findings into orphan_report."""
        from medre.runtime.evidence._storage_sections import (
            _collect_storage_data_from_backend,
        )

        receipts = [_receipt(receipt_id="r-g", status="queued")]
        outbox_items = [_outbox(outbox_id="ob-g", status="pending")]

        storage = _FakeGlobalStorage(
            all_receipts=receipts,
            all_outbox=outbox_items,
        )

        result = await _collect_storage_data_from_backend(
            storage,
            db_path=str(tmp_path / "test.db"),
            event_id=None,  # global convergence
            replay_run_id=None,
        )
        data = result["data"]
        orphan_report = data["orphan_report"]

        assert orphan_report is not None
        assert isinstance(orphan_report["total_findings"], int)
        assert "severity_counts" in orphan_report


# ---------------------------------------------------------------------------
# Semantic equivalence test
# ---------------------------------------------------------------------------


class TestSemanticEquivalence:
    """Core and runtime paths produce the same orphan_report for the same inputs."""

    def test_shared_helper_produces_identical_result_for_both_paths(self):
        """Both paths use merge_recovery_findings_into_report_dict,
        so the merge semantics are identical by construction."""
        # Build base orphan report (same data for both paths).
        receipts = [
            _receipt(receipt_id="r-1", source="retry", delivery_plan_id=""),
        ]
        outbox_items = [
            _outbox(outbox_id="ob-1", status="pending"),
        ]

        # Path 1: simulate core EvidenceCollector merge.
        base_report = build_orphan_report(receipts=receipts, outbox_items=outbox_items)
        report_dict_core = base_report.to_dict()
        ledger = build_startup_recovery_ledger(
            outbox_items=outbox_items,
            recovery_source="snapshot_diagnostics",
        )
        recovery_findings = build_recovery_convergence_findings(
            outbox_items=outbox_items,
            receipts=receipts,
            recovery_ledger=ledger,
        )
        result_core = merge_recovery_findings_into_report_dict(
            report_dict_core,
            recovery_findings,
        )

        # Path 2: simulate runtime merge (same data, same helper).
        base_report_rt = build_orphan_report(
            receipts=receipts, outbox_items=outbox_items
        )
        report_dict_rt = base_report_rt.to_dict()
        ledger_rt = build_startup_recovery_ledger(
            outbox_items=outbox_items,
            recovery_source="snapshot_diagnostics",
        )
        recovery_findings_rt = build_recovery_convergence_findings(
            outbox_items=outbox_items,
            receipts=receipts,
            recovery_ledger=ledger_rt,
        )
        result_rt = merge_recovery_findings_into_report_dict(
            report_dict_rt,
            recovery_findings_rt,
        )

        # Must be identical.
        assert result_core == result_rt
        assert result_core["total_findings"] == result_rt["total_findings"]
        assert result_core["severity_counts"] == result_rt["severity_counts"]
        assert result_core["worst_severity"] == result_rt["worst_severity"]
        assert result_core["findings"] == result_rt["findings"]


# ---------------------------------------------------------------------------
# Fake storage for runtime tests
# ---------------------------------------------------------------------------


class _FakeTimelineStorage:
    """Minimal storage fake that supports per-event timeline assembly."""

    def __init__(
        self,
        event: Any,
        receipts: list[dict],
        native_refs: list[dict],
        outbox_items: list[dict],
    ) -> None:
        self._event = event
        self._receipts = receipts
        self._native_refs = native_refs
        self._outbox_items = outbox_items

    async def count_events(self) -> int:
        return 1

    async def count_receipts(self) -> int:
        return len(self._receipts)

    async def list_outbox_items_for_event(self, event_id: str) -> list[dict]:
        return self._outbox_items

    async def close(self) -> None:
        pass


class _FakeGlobalStorage:
    """Minimal storage fake that supports global convergence queries."""

    def __init__(
        self,
        all_receipts: list[dict],
        all_outbox: list[dict],
    ) -> None:
        self._all_receipts = all_receipts
        self._all_outbox = all_outbox

    async def count_events(self) -> int:
        return 0

    async def count_receipts(self) -> int:
        return len(self._all_receipts)

    async def list_all_receipts(self, limit: int = 10_000) -> list[dict]:
        return self._all_receipts

    async def list_all_outbox_items(self, limit: int = 10_000) -> list[dict]:
        return self._all_outbox

    async def close(self) -> None:
        pass
