"""Integration tests for runtime operational evidence fields.

Proves that operator evidence artifacts include:
- ``evidence_tier`` (machine-readable tier label)
- ``delivery_outcome_ledger`` (delivery outcome lineage)
- ``retry_outbox_summary`` (retry/outbox accountability)
- ``adapter_status`` (per-adapter status evidence)
- ``shutdown_evidence`` (shutdown state evidence)

Honesty constraints tested:
- Storage-only is NOT live/hardware.
- Suppressed receipts appear in retry/outbox summary but not queued/retrying.
- ``replay_run_id`` appears only on replay ledger entries.
- Pending outbox counts at stopped/shutdown are ``shutdown_pending``, not cancelled.
- Running runtime has shutdown evidence as running/not_executed, not false success.
- Core EvidenceBundle includes delivery_outcome_ledger and retry_outbox_summary.
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
from medre.core.evidence.adapter_status import build_adapter_status_evidence
from medre.core.evidence.bundle import (
    EvidenceBundle,
)
from medre.core.evidence.collector import EvidenceCollector
from medre.core.evidence.delivery_ledger import build_delivery_outcome_ledger
from medre.core.evidence.retry_outbox import build_retry_outbox_summary
from medre.core.evidence.shutdown import build_shutdown_evidence
from medre.core.evidence.tiers import infer_evidence_tier
from medre.core.storage.backend import DeliveryOutboxItem
from medre.runtime.evidence._bundle import collect_evidence_bundle
from tests.helpers.storage import make_storage_event

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FIXED_NOW = datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc)


def _fixed_now() -> datetime:
    return _FIXED_NOW


def _make_receipt(
    receipt_id: str = "rcpt-1",
    event_id: str = "evt-1",
    sequence: int = 1,
    delivery_plan_id: str = "plan-1",
    target_adapter: str = "adapter_a",
    target_channel: str | None = None,
    route_id: str = "route-1",
    status: str = "sent",
    attempt_number: int = 1,
    source: str = "live",
    replay_run_id: str | None = None,
    failure_kind: str | None = None,
    error: str | None = None,
    created_at: datetime | None = None,
) -> DeliveryReceipt:
    return DeliveryReceipt(
        sequence=sequence,
        receipt_id=receipt_id,
        event_id=event_id,
        delivery_plan_id=delivery_plan_id,
        target_adapter=target_adapter,
        target_channel=target_channel,
        route_id=route_id,
        status=status,  # type: ignore[arg-type]
        attempt_number=attempt_number,
        source=source,
        replay_run_id=replay_run_id,
        failure_kind=failure_kind,
        error=error,
        created_at=created_at or _FIXED_NOW,
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


# ---------------------------------------------------------------------------
# 1. Core EvidenceBundle includes delivery_outcome_ledger
# ---------------------------------------------------------------------------


class TestCoreBundleDeliveryLedger:
    """EvidenceBundle includes delivery_outcome_ledger derived from receipts."""

    @pytest.mark.asyncio
    async def test_ledger_in_bundle_dict(self) -> None:
        """to_dict() includes delivery_outcome_ledger key."""
        receipt = _make_receipt("rcpt-ledger", event_id="evt-ledger", status="sent")
        storage = FakeStorage()
        storage._events["evt-ledger"] = make_storage_event(event_id="evt-ledger")
        storage._receipts["evt-ledger"] = [receipt]

        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-ledger")
        d = bundle.to_dict()

        assert "delivery_outcome_ledger" in d
        assert d["delivery_outcome_ledger"] is not None
        # Ledger should have entries and aggregate_counts.
        ledger = d["delivery_outcome_ledger"]
        assert "entries" in ledger
        assert "aggregate_counts" in ledger

    @pytest.mark.asyncio
    async def test_ledger_has_sent_entry(self) -> None:
        receipt = _make_receipt("rcpt-sent", event_id="evt-sent-ledger", status="sent")
        storage = FakeStorage()
        storage._events["evt-sent-ledger"] = make_storage_event(
            event_id="evt-sent-ledger"
        )
        storage._receipts["evt-sent-ledger"] = [receipt]

        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-sent-ledger")
        ledger = bundle.delivery_outcome_ledger

        assert ledger is not None
        # Should have at least one entry.
        assert len(ledger["entries"]) >= 1
        # Check aggregate counts.
        assert ledger["aggregate_counts"]["by_status"].get("sent", 0) >= 1

    @pytest.mark.asyncio
    async def test_ledger_empty_when_no_receipts(self) -> None:
        storage = FakeStorage()
        storage._events["evt-empty-ledger"] = make_storage_event(
            event_id="evt-empty-ledger"
        )

        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-empty-ledger")

        assert bundle.delivery_outcome_ledger is not None
        assert bundle.delivery_outcome_ledger["entries"] == {}


# ---------------------------------------------------------------------------
# 2. Core EvidenceBundle includes retry_outbox_summary
# ---------------------------------------------------------------------------


class TestCoreBundleRetryOutboxSummary:
    """EvidenceBundle includes retry_outbox_summary derived from receipts."""

    @pytest.mark.asyncio
    async def test_summary_in_bundle_dict(self) -> None:
        receipt = _make_receipt("rcpt-ro", event_id="evt-ro", status="sent")
        storage = FakeStorage()
        storage._events["evt-ro"] = make_storage_event(event_id="evt-ro")
        storage._receipts["evt-ro"] = [receipt]

        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-ro")
        d = bundle.to_dict()

        assert "retry_outbox_summary" in d
        assert d["retry_outbox_summary"] is not None
        summary = d["retry_outbox_summary"]
        assert "counts" in summary
        assert "items" in summary
        assert "retry_worker" in summary

    @pytest.mark.asyncio
    async def test_summary_counts_include_all_known_statuses(self) -> None:
        receipt = _make_receipt("rcpt-counts", event_id="evt-counts", status="sent")
        storage = FakeStorage()
        storage._events["evt-counts"] = make_storage_event(event_id="evt-counts")
        storage._receipts["evt-counts"] = [receipt]

        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-counts")
        summary = bundle.retry_outbox_summary

        assert summary is not None
        counts = summary["counts"]
        # All known outbox statuses should be present (even if zero).
        for status in (
            "pending",
            "in_progress",
            "queued",
            "retry_wait",
            "sent",
            "dead_lettered",
            "cancelled",
            "abandoned",
        ):
            assert status in counts


# ---------------------------------------------------------------------------
# 3. Suppressed receipts appear in retry/outbox summary but NOT as queued/retrying
# ---------------------------------------------------------------------------


class TestSuppressedReceiptsInOutboxSummary:
    """Suppressed receipts appear in retry/outbox summary with correct state."""

    def test_suppressed_appears_in_summary(self) -> None:
        receipt = _make_receipt(
            "rcpt-sup",
            event_id="evt-sup-ro",
            status="suppressed",
            error="capability_suppressed: reactions unsupported",
            failure_kind="capability_suppressed",
        )
        summary = build_retry_outbox_summary(receipts=[receipt])

        # Should have items.
        assert len(summary.items) == 1
        item = summary.items[0]
        assert item.status == "suppressed"
        assert item.retry_state == "suppressed"
        # NOT queued or retrying.
        assert item.retry_state not in ("queued", "retrying", "pending", "in_progress")

    def test_suppressed_count_not_in_queued(self) -> None:
        receipt = _make_receipt(
            "rcpt-sup2",
            event_id="evt-sup2-ro",
            status="suppressed",
        )
        summary = build_retry_outbox_summary(receipts=[receipt])
        counts = summary.counts

        # Suppressed should be counted.
        assert counts.get("suppressed", 0) == 1
        # NOT counted as queued/retrying.
        assert counts.get("queued", 0) == 0
        assert counts.get("retry_wait", 0) == 0


# ---------------------------------------------------------------------------
# 4. replay_run_id appears only on replay ledger entries
# ---------------------------------------------------------------------------


class TestReplayRunIdOnlyOnReplay:
    """replay_run_id is populated only when source is 'replay'."""

    def test_replay_run_id_on_replay_entry(self) -> None:
        live_receipt = _make_receipt(
            "rcpt-live-replay",
            event_id="evt-replay-test",
            source="live",
            replay_run_id=None,
        )
        replay_receipt = _make_receipt(
            "rcpt-replay-1",
            event_id="evt-replay-test",
            source="replay",
            replay_run_id="run-abc",
        )
        ledger = build_delivery_outcome_ledger(
            receipts=[live_receipt, replay_receipt],
        )

        # Find the replay entry.
        replay_entries = [e for e in ledger.entries.values() if e.source == "replay"]
        assert len(replay_entries) >= 1
        assert replay_entries[0].replay_run_id == "run-abc"

    def test_no_replay_run_id_on_live_entry(self) -> None:
        live_receipt = _make_receipt(
            "rcpt-live-no-replay",
            event_id="evt-live-no-replay",
            source="live",
            replay_run_id=None,
        )
        ledger = build_delivery_outcome_ledger(receipts=[live_receipt])

        for entry in ledger.entries.values():
            if entry.source == "live":
                assert entry.replay_run_id is None


# ---------------------------------------------------------------------------
# 5. Pending outbox counts at shutdown are shutdown_pending, not cancelled
# ---------------------------------------------------------------------------


class TestPendingOutboxNotCancelled:
    """Pending outbox work at shutdown is shutdown_pending, not cancelled."""

    def test_pending_outbox_is_shutdown_pending(self) -> None:
        """When runtime is stopped with pending outbox items,
        shutdown_status is 'shutdown_pending', not 'cancellation'."""
        evidence = build_shutdown_evidence(
            runtime_state="stopped",
            outbox_counts={"pending": 3, "sent": 5, "retry_wait": 1},
        )
        d = evidence.to_dict()

        assert d["shutdown_status"] == "shutdown_pending"
        assert d["pending_outbox_counts"] is not None
        assert d["pending_outbox_counts"].get("pending") == 3
        assert d["pending_outbox_counts"].get("retry_wait") == 1

    def test_pending_outbox_not_cancelled(self) -> None:
        """Pending work is NOT reported as cancelled."""
        evidence = build_shutdown_evidence(
            runtime_state="stopped",
            outbox_counts={"pending": 2},
        )
        d = evidence.to_dict()

        # Must not say cancelled.
        assert d["shutdown_status"] != "cancellation"
        assert d.get("tasks_cancelled") is None

    def test_retry_outbox_shutdown_pending_count(self) -> None:
        """Retry outbox summary counts shutdown_pending from non-terminal items."""
        outbox_items = [
            {
                "outbox_id": "ob-1",
                "event_id": "e1",
                "delivery_plan_id": "p1",
                "target_adapter": "a",
                "status": "pending",
            },
            {
                "outbox_id": "ob-2",
                "event_id": "e2",
                "delivery_plan_id": "p2",
                "target_adapter": "b",
                "status": "retry_wait",
            },
            {
                "outbox_id": "ob-3",
                "event_id": "e3",
                "delivery_plan_id": "p3",
                "target_adapter": "c",
                "status": "sent",
            },
        ]
        summary = build_retry_outbox_summary(outbox_items=outbox_items)
        counts = summary.counts

        # shutdown_pending should equal pending + retry_wait.
        assert counts["shutdown_pending"] == 2
        assert counts["pending"] == 1
        assert counts["retry_wait"] == 1
        # NOT cancelled.
        assert counts["cancelled"] == 0


# ---------------------------------------------------------------------------
# 6. Running runtime has shutdown evidence as running, not false success
# ---------------------------------------------------------------------------


class TestRunningShutdownNotSuccess:
    """Running runtime has shutdown_status='running', not success."""

    def test_running_state_not_graceful_stop(self) -> None:
        evidence = build_shutdown_evidence(runtime_state="running")
        d = evidence.to_dict()

        assert d["shutdown_status"] == "running"
        # NOT success/falsely claiming shutdown completed.
        assert d["shutdown_status"] != "graceful_stop"
        assert d["shutdown_status"] != "cancellation"

    def test_initialized_state_is_running(self) -> None:
        evidence = build_shutdown_evidence(runtime_state="initialized")
        d = evidence.to_dict()

        assert d["shutdown_status"] == "running"

    def test_starting_state_is_running(self) -> None:
        evidence = build_shutdown_evidence(runtime_state="starting")
        d = evidence.to_dict()

        assert d["shutdown_status"] == "running"


# ---------------------------------------------------------------------------
# 7. Storage-only evidence is not live/hardware
# ---------------------------------------------------------------------------


class TestStorageOnlyNotLive:
    """Storage-only evidence bundles have synthetic tier, never live/hardware."""

    @pytest.mark.asyncio
    async def test_storage_path_bundle_tier(self, tmp_path) -> None:
        """Storage-path bundle always has evidence_tier='synthetic'."""
        from medre.core.storage.sqlite.storage import SQLiteStorage

        db_path = str(tmp_path / "test.db")
        storage = SQLiteStorage(db_path)
        await storage.initialize()
        await storage.close()

        report = await collect_evidence_bundle(storage_path=db_path)

        assert report["evidence_tier"] == "synthetic"
        assert report["evidence_tier"] != "live_service"
        assert report["evidence_tier"] != "hardware"
        assert report["adapter_status"] is None
        assert report["shutdown_evidence"] is None

    def test_core_bundle_synthetic_default(self) -> None:
        """EvidenceBundle defaults to synthetic tier."""
        bundle = EvidenceBundle(event_id="evt-syn")
        d = bundle.to_dict()

        assert d["evidence_tier"] == "synthetic"

    def test_infer_tier_never_live_from_real_adapter(self) -> None:
        """adapter_kind='real' does NOT produce live_service tier."""
        tier = infer_evidence_tier(adapter_kind="real")
        assert tier != "live_service"
        assert tier != "hardware"
        assert tier == "synthetic"  # Conservative default.

    def test_explicit_live_service_works(self) -> None:
        """Explicit tier='live_service' is respected."""
        tier = infer_evidence_tier(explicit_tier="live_service")
        assert tier == "live_service"


# ---------------------------------------------------------------------------
# 8. Evidence bundle JSON safety with new fields
# ---------------------------------------------------------------------------


class TestBundleJsonSafetyWithNewFields:
    """Bundle with new fields is JSON-safe."""

    @pytest.mark.asyncio
    async def test_bundle_json_safe_with_ledger_and_summary(self) -> None:
        receipt = _make_receipt("rcpt-json", event_id="evt-json-new", status="sent")
        storage = FakeStorage()
        storage._events["evt-json-new"] = make_storage_event(event_id="evt-json-new")
        storage._receipts["evt-json-new"] = [receipt]

        collector = EvidenceCollector(storage, now_fn=_fixed_now)
        bundle = await collector.collect_for_event("evt-json-new")

        # Must succeed.
        json_str = json.dumps(bundle.to_dict(), sort_keys=True)
        parsed = json.loads(json_str)

        assert "delivery_outcome_ledger" in parsed
        assert "retry_outbox_summary" in parsed
        assert "evidence_tier" in parsed
        assert parsed["evidence_tier"] == "synthetic"


# ---------------------------------------------------------------------------
# 9. Package exports are complete
# ---------------------------------------------------------------------------


class TestPackageExportsComplete:
    """All new helpers are importable from medre.core.evidence."""

    def test_delivery_ledger_exports(self) -> None:
        from medre.core.evidence import (
            DeliveryOutcomeEntry,
            DeliveryOutcomeLedger,
            build_delivery_outcome_ledger,
        )

        assert DeliveryOutcomeEntry is not None
        assert DeliveryOutcomeLedger is not None
        assert build_delivery_outcome_ledger is not None

    def test_retry_outbox_exports(self) -> None:
        from medre.core.evidence import (
            RetryOutboxItemSummary,
            RetryOutboxSummary,
            build_retry_outbox_summary,
        )

        assert RetryOutboxItemSummary is not None
        assert RetryOutboxSummary is not None
        assert build_retry_outbox_summary is not None

    def test_shutdown_exports(self) -> None:
        from medre.core.evidence import (
            ShutdownEvidence,
            ShutdownStatus,
            build_shutdown_evidence,
        )

        assert ShutdownEvidence is not None
        assert ShutdownStatus is not None
        assert build_shutdown_evidence is not None

    def test_adapter_status_exports(self) -> None:
        from medre.core.evidence import (
            OPERATOR_STATUSES,
            AdapterStatusEvidence,
            build_adapter_status_evidence,
            derive_operator_status,
        )

        assert AdapterStatusEvidence is not None
        assert OPERATOR_STATUSES is not None
        assert build_adapter_status_evidence is not None
        assert derive_operator_status is not None

    def test_tier_exports(self) -> None:
        from medre.core.evidence import (
            EVIDENCE_TIER_UNKNOWN,
            EvidenceTier,
            tier_is_live,
        )

        assert EVIDENCE_TIER_UNKNOWN == ""
        assert EvidenceTier.SYNTHETIC.value == "synthetic"
        assert not tier_is_live("synthetic")
        assert tier_is_live("live_service")
        assert tier_is_live("hardware")


# ---------------------------------------------------------------------------
# 10. Storage-only runtime bundle has delivery_outcome_ledger and
#     retry_outbox_summary in storage section data when event_id provided
# ---------------------------------------------------------------------------


class TestStorageOnlyOperationalEvidence:
    """Storage-only bundles include operational evidence when data available."""

    @pytest.mark.asyncio
    async def test_storage_only_with_event_has_ledger(self, tmp_path) -> None:

        from medre.core.storage.sqlite.storage import SQLiteStorage

        db_path = str(tmp_path / "test.db")
        storage = SQLiteStorage(db_path)
        await storage.initialize()

        event = make_storage_event(event_id="ev-integ-001")
        await storage.append(event)

        receipt = DeliveryReceipt(
            receipt_id="rcpt-integ-001",
            event_id="ev-integ-001",
            delivery_plan_id="dp-integ-001",
            target_adapter="radio",
            status="sent",
            source="live",
            created_at=_FIXED_NOW,
        )
        await storage.append_receipt(receipt)
        await storage.close()

        report = await collect_evidence_bundle(
            storage_path=db_path,
            event_id="ev-integ-001",
        )

        storage_data = report["sections"]["storage"]["data"]
        assert storage_data["delivery_outcome_ledger"] is not None
        assert "entries" in storage_data["delivery_outcome_ledger"]

    @pytest.mark.asyncio
    async def test_storage_only_with_event_has_retry_summary(self, tmp_path) -> None:

        from medre.core.storage.sqlite.storage import SQLiteStorage

        db_path = str(tmp_path / "test.db")
        storage = SQLiteStorage(db_path)
        await storage.initialize()

        event = make_storage_event(event_id="ev-integ-002")
        await storage.append(event)

        receipt = DeliveryReceipt(
            receipt_id="rcpt-integ-002",
            event_id="ev-integ-002",
            delivery_plan_id="dp-integ-002",
            target_adapter="radio",
            status="sent",
            source="live",
            created_at=_FIXED_NOW,
        )
        await storage.append_receipt(receipt)
        await storage.close()

        report = await collect_evidence_bundle(
            storage_path=db_path,
            event_id="ev-integ-002",
        )

        storage_data = report["sections"]["storage"]["data"]
        assert storage_data["retry_outbox_summary"] is not None
        assert "counts" in storage_data["retry_outbox_summary"]
        assert "items" in storage_data["retry_outbox_summary"]

    @pytest.mark.asyncio
    async def test_storage_only_no_adapter_status(self, tmp_path) -> None:
        """Storage-only has no adapter_status at top level."""

        from medre.core.storage.sqlite.storage import SQLiteStorage

        db_path = str(tmp_path / "test.db")
        storage = SQLiteStorage(db_path)
        await storage.initialize()
        await storage.close()

        report = await collect_evidence_bundle(storage_path=db_path)
        assert report["adapter_status"] is None

    @pytest.mark.asyncio
    async def test_storage_only_no_shutdown_evidence(self, tmp_path) -> None:
        """Storage-only has no shutdown_evidence at top level."""

        from medre.core.storage.sqlite.storage import SQLiteStorage

        db_path = str(tmp_path / "test.db")
        storage = SQLiteStorage(db_path)
        await storage.initialize()
        await storage.close()

        report = await collect_evidence_bundle(storage_path=db_path)
        assert report["shutdown_evidence"] is None

    @pytest.mark.asyncio
    async def test_storage_only_evidence_tier_synthetic(self, tmp_path) -> None:
        """Storage-only bundles always have synthetic evidence tier."""

        from medre.core.storage.sqlite.storage import SQLiteStorage

        db_path = str(tmp_path / "test.db")
        storage = SQLiteStorage(db_path)
        await storage.initialize()
        await storage.close()

        report = await collect_evidence_bundle(storage_path=db_path)
        assert report["evidence_tier"] == "synthetic"


# ---------------------------------------------------------------------------
# 11. Shutdown evidence edge cases
# ---------------------------------------------------------------------------


class TestShutdownEdgeCases:
    """Shutdown evidence handles edge cases correctly."""

    def test_stopped_no_pending_is_graceful(self) -> None:
        evidence = build_shutdown_evidence(
            runtime_state="stopped",
            outbox_counts={"sent": 5},
        )
        d = evidence.to_dict()
        assert d["shutdown_status"] == "graceful_stop"

    def test_stopped_with_pending_is_shutdown_pending(self) -> None:
        evidence = build_shutdown_evidence(
            runtime_state="stopped",
            outbox_counts={"pending": 1, "sent": 3},
        )
        d = evidence.to_dict()
        assert d["shutdown_status"] == "shutdown_pending"

    def test_cancellation_requires_explicit_signal(self) -> None:
        """Cancellation not inferred from stopped state alone."""
        evidence = build_shutdown_evidence(
            runtime_state="stopped",
            outbox_counts={"sent": 1},
        )
        d = evidence.to_dict()
        assert d["shutdown_status"] != "cancellation"
        assert d["shutdown_status"] == "graceful_stop"

    def test_explicit_cancellation(self) -> None:
        evidence = build_shutdown_evidence(
            runtime_state="stopped",
            reason="cancellation",
        )
        d = evidence.to_dict()
        assert d["shutdown_status"] == "cancellation"

    def test_failed_state(self) -> None:
        evidence = build_shutdown_evidence(runtime_state="failed")
        d = evidence.to_dict()
        assert d["shutdown_status"] == "failed"


# ---------------------------------------------------------------------------
# 12. Adapter status evidence
# ---------------------------------------------------------------------------


class TestAdapterStatusEvidence:
    """Adapter status evidence is derivable from config/snapshot data."""

    def test_basic_adapter_status(self) -> None:
        evidence = build_adapter_status_evidence(
            "matrix_main",
            config={"enabled": True, "adapter_kind": "fake", "config": None},
            lifecycle_state="ready",
            transport="matrix",
        )
        d = evidence.to_dict()

        assert d["adapter_id"] == "matrix_main"
        assert d["transport"] == "matrix"
        assert d["enabled"] is True
        assert d["adapter_kind"] == "fake"
        assert d["operator_status"] == "not_configured"
        assert d["current_state"] == "ready"

    def test_disabled_adapter(self) -> None:
        evidence = build_adapter_status_evidence(
            "disabled_adp",
            config={"enabled": False, "adapter_kind": "real", "config": None},
            transport="meshtastic",
        )
        d = evidence.to_dict()

        assert d["operator_status"] == "disabled"

    def test_no_sdk_objects_in_output(self) -> None:
        """to_dict output contains only JSON-safe types."""
        evidence = build_adapter_status_evidence(
            "test_adp",
            config={"enabled": True, "adapter_kind": "real"},
            lifecycle_state="ready",
        )
        d = evidence.to_dict()
        # Must be JSON-serializable.
        json_str = json.dumps(d, sort_keys=True)
        parsed = json.loads(json_str)
        assert isinstance(parsed, dict)


# ---------------------------------------------------------------------------
# 13. Adapter status derivation through diagnostics integration path
# ---------------------------------------------------------------------------


class TestDiagnosticsAdapterStatusDerivation:
    """Prove READY→connected and FAILED→failed through diagnostics derivation."""

    def test_ready_state_produces_connected(self) -> None:
        """When config has a real transport config and lifecycle is READY,
        operator_status MUST be 'connected', NOT 'not_configured'."""
        evidence = build_adapter_status_evidence(
            "matrix_main",
            config={
                "enabled": True,
                "adapter_kind": "fake",
                "config": {"homeserver": "https://example.org"},
            },
            lifecycle_state="ready",
            transport="matrix",
        )
        d = evidence.to_dict()
        assert d["operator_status"] == "connected"
        assert d["current_state"] == "ready"
        assert d["configured"] is True

    def test_failed_state_produces_failed(self) -> None:
        """When config exists and lifecycle state is FAILED,
        operator_status MUST be 'failed', NOT 'not_configured'."""
        evidence = build_adapter_status_evidence(
            "radio_out",
            config={
                "enabled": True,
                "adapter_kind": "fake",
                "config": {"connection_type": "tcp"},
            },
            lifecycle_state="failed",
            transport="meshtastic",
        )
        d = evidence.to_dict()
        assert d["operator_status"] == "failed"
        assert d["current_state"] == "failed"
        assert d["configured"] is True

    def test_config_none_still_not_configured(self) -> None:
        """When config key is explicitly None, not_configured is correct."""
        evidence = build_adapter_status_evidence(
            "no_cfg_adp",
            config={"enabled": True, "adapter_kind": "fake", "config": None},
            lifecycle_state="ready",
        )
        d = evidence.to_dict()
        assert d["operator_status"] == "not_configured"

    def test_no_config_key_lifecycle_wins(self) -> None:
        """When config key is absent from dict, lifecycle state drives status."""
        evidence = build_adapter_status_evidence(
            "snapshot_only_adp",
            config={"enabled": True, "adapter_kind": "fake"},
            lifecycle_state="ready",
        )
        d = evidence.to_dict()
        assert d["operator_status"] == "connected"

    def test_stopping_state_produces_stopping(self) -> None:
        """STOPPING lifecycle state maps to 'stopping' operator status."""
        evidence = build_adapter_status_evidence(
            "stopping_adp",
            config={
                "enabled": True,
                "adapter_kind": "real",
                "config": {"mode": "fake"},
            },
            lifecycle_state="stopping",
        )
        d = evidence.to_dict()
        assert d["operator_status"] == "stopping"

    def test_derive_from_snapshot_with_real_config(self) -> None:
        """_derive_adapter_status_from_snapshot passes real config through."""
        from medre.runtime.evidence._diagnostics_sections import (
            _derive_adapter_status_from_snapshot,
        )

        # Simulate a snapshot with a READY adapter.
        snapshot = {
            "adapters": {"matrix_main": {"platform": "matrix"}},
            "lifecycle": {"adapters": {"matrix_main": "ready"}},
        }

        # Simulate config that provides a real config object for this adapter.
        class FakeRTC:
            enabled = True
            adapter_kind = "fake"
            config = {"homeserver": "https://matrix.org"}

        class FakeAdapters:
            def all_configs(self):
                return [("matrix", "matrix_main", FakeRTC())]

        class FakeConfig:
            adapters = FakeAdapters()

        results = _derive_adapter_status_from_snapshot(snapshot, FakeConfig())
        assert len(results) == 1
        assert results[0]["adapter_id"] == "matrix_main"
        assert results[0]["operator_status"] == "connected"
        assert results[0]["configured"] is True

    def test_derive_from_snapshot_no_config(self) -> None:
        """_derive_adapter_status_from_snapshot with no runtime config still
        derives lifecycle state correctly (no forced not_configured)."""
        from medre.runtime.evidence._diagnostics_sections import (
            _derive_adapter_status_from_snapshot,
        )

        snapshot = {
            "adapters": {"radio_out": {"platform": "meshtastic"}},
            "lifecycle": {"adapters": {"radio_out": "failed"}},
        }

        # No config at all.
        results = _derive_adapter_status_from_snapshot(snapshot, None)
        assert len(results) == 1
        assert results[0]["adapter_id"] == "radio_out"
        # Without config dict, configured=None → lifecycle state wins.
        assert results[0]["operator_status"] == "failed"


# ---------------------------------------------------------------------------
# 14. Storage-section outbox items in ledger and retry summary
# ---------------------------------------------------------------------------


class TestStorageSectionOutboxItems:
    """Outbox items show up in storage-section delivery ledger and retry summary."""

    def test_ledger_includes_outbox_items(self) -> None:
        """build_delivery_outcome_ledger includes entries from outbox items."""
        outbox_item = DeliveryOutboxItem(
            outbox_id="ob-test-1",
            event_id="evt-ob-ledger",
            route_id="route-ob",
            delivery_plan_id="plan-ob",
            target_adapter="radio",
            target_channel="ch-1",
            status="pending",
            created_at=_FIXED_NOW.isoformat(),
            updated_at=_FIXED_NOW.isoformat(),
        )
        ledger = build_delivery_outcome_ledger(
            receipts=[],
            outbox_items=[outbox_item],
        )
        d = ledger.to_dict()
        assert len(d["entries"]) >= 1
        # Check that the pending outbox item appears.
        found = any(
            e.final_status == "pending" and e.outbox_id == "ob-test-1"
            for e in ledger.entries.values()
        )
        assert found, f"Expected pending outbox item in ledger, got {d}"

    def test_retry_summary_includes_outbox_items(self) -> None:
        """build_retry_outbox_summary includes outbox items with correct state."""
        outbox_pending = DeliveryOutboxItem(
            outbox_id="ob-pend",
            event_id="evt-ob-pend",
            route_id="route-1",
            delivery_plan_id="plan-1",
            target_adapter="adapter_a",
            status="pending",
            created_at=_FIXED_NOW.isoformat(),
            updated_at=_FIXED_NOW.isoformat(),
        )
        outbox_retry = DeliveryOutboxItem(
            outbox_id="ob-retry",
            event_id="evt-ob-retry",
            route_id="route-2",
            delivery_plan_id="plan-2",
            target_adapter="adapter_b",
            status="retry_wait",
            next_attempt_at=_FIXED_NOW.isoformat(),
            created_at=_FIXED_NOW.isoformat(),
            updated_at=_FIXED_NOW.isoformat(),
        )
        summary = build_retry_outbox_summary(
            outbox_items=[outbox_pending, outbox_retry],
        )
        assert summary.counts["pending"] == 1
        assert summary.counts["retry_wait"] == 1
        assert summary.counts["shutdown_pending"] == 2
        # Items should be present.
        ids = {it.outbox_id for it in summary.items}
        assert "ob-pend" in ids
        assert "ob-retry" in ids

    @pytest.mark.asyncio
    async def test_storage_outbox_items_in_runtime_bundle(self, tmp_path) -> None:
        """Storage-section evidence bundle includes outbox items in ledger/summary."""
        from medre.core.storage.sqlite.storage import SQLiteStorage

        db_path = str(tmp_path / "test_outbox.db")
        storage = SQLiteStorage(db_path)
        await storage.initialize()

        event = make_storage_event(event_id="evt-outbox-integ")
        await storage.append(event)

        receipt = DeliveryReceipt(
            receipt_id="rcpt-outbox-1",
            event_id="evt-outbox-integ",
            delivery_plan_id="plan-outbox-1",
            target_adapter="radio",
            status="sent",
            source="live",
            created_at=_FIXED_NOW,
        )
        await storage.append_receipt(receipt)

        # Add an outbox item for this event.
        outbox_item = DeliveryOutboxItem(
            outbox_id="ob-integ-1",
            event_id="evt-outbox-integ",
            route_id="route-outbox",
            delivery_plan_id="plan-outbox-2",
            target_adapter="radio",
            target_channel="ch-1",
            status="pending",
            created_at=_FIXED_NOW.isoformat(),
            updated_at=_FIXED_NOW.isoformat(),
        )
        await storage.create_outbox_item(outbox_item)
        await storage.close()

        report = await collect_evidence_bundle(
            storage_path=db_path,
            event_id="evt-outbox-integ",
        )
        storage_data = report["sections"]["storage"]["data"]

        # Ledger should have entries (from both receipt and outbox).
        ledger = storage_data["delivery_outcome_ledger"]
        assert ledger is not None
        assert len(ledger["entries"]) >= 2  # at least receipt + outbox item

        # Retry summary should include the pending outbox item.
        retry_summary = storage_data["retry_outbox_summary"]
        assert retry_summary is not None
        assert retry_summary["counts"]["pending"] >= 1
        outbox_ids = {
            it["outbox_id"] for it in retry_summary["items"] if it.get("outbox_id")
        }
        assert "ob-integ-1" in outbox_ids

    def test_storage_outbox_items_receipt_only_fallback(self) -> None:
        """When storage lacks list_outbox_items_for_event, degrade to
        receipts-only without crashing."""
        # This tests the graceful degradation path.
        receipt = _make_receipt(
            "rcpt-fallback",
            event_id="evt-fallback",
            status="sent",
        )
        ledger = build_delivery_outcome_ledger(receipts=[receipt])
        summary = build_retry_outbox_summary(receipts=[receipt])

        assert len(ledger.entries) >= 1
        assert (
            summary.counts["sent"] == 0
        )  # sent is terminal, not counted in outbox counts


# ---------------------------------------------------------------------------
# 15. Shutdown evidence from runtime events
# ---------------------------------------------------------------------------


class TestShutdownEvidenceFromSnapshot:
    """Prove build_shutdown_evidence detects signals from runtime events."""

    def test_empty_events_no_false_signals(self) -> None:
        """Empty events list preserves existing shutdown classification."""
        evidence = build_shutdown_evidence(
            runtime_state="stopped",
            outbox_counts={"sent": 5},
            events=[],
        )
        d = evidence.to_dict()
        assert d["shutdown_status"] == "graceful_stop"
        assert d["drain_timeout_detected"] is False
        assert d["tasks_cancelled"] is None

    def test_adapter_start_failed_event(self) -> None:
        """adapter_start_failed event produces adapter_failure shutdown status."""
        events = [
            {
                "event_type": "adapter_start_failed",
                "detail": {"adapter_id": "radio_out"},
            },
        ]
        evidence = build_shutdown_evidence(
            runtime_state="stopped",
            outbox_counts={"sent": 3},
            events=events,
        )
        d = evidence.to_dict()
        assert d["shutdown_status"] == "adapter_failure"
        assert d["shutdown_reason"] == "adapter_failure"

    def test_drain_timeout_event(self) -> None:
        """Event with drain timeout detail produces drain_timeout status."""
        events = [
            {
                "event_type": "delivery_rejected",
                "detail": {"error": "shutdown_drain_timeout"},
            },
        ]
        evidence = build_shutdown_evidence(
            runtime_state="stopped",
            outbox_counts={"sent": 2, "pending": 1},
            events=events,
        )
        d = evidence.to_dict()
        assert d["shutdown_status"] == "drain_timeout"
        assert d["drain_timeout_detected"] is True

    def test_cancellation_event(self) -> None:
        """Event with cancellation detail produces cancellation status."""
        events = [
            {"event_type": "shutdown_signal", "detail": {"cancellation": True}},
        ]
        evidence = build_shutdown_evidence(
            runtime_state="stopped",
            outbox_counts={"sent": 4},
            events=events,
        )
        d = evidence.to_dict()
        assert d["shutdown_status"] == "cancellation"
        assert d["shutdown_reason"] == "cancellation"

    def test_tasks_cancelled_extracted_from_event_detail(self) -> None:
        """tasks_cancelled is extracted from event details when present."""
        events = [
            {
                "event_type": "shutdown_signal",
                "detail": {"cancellation": True, "tasks_cancelled": 7},
            },
        ]
        evidence = build_shutdown_evidence(
            runtime_state="stopped",
            outbox_counts={"sent": 2},
            events=events,
        )
        d = evidence.to_dict()
        assert d["shutdown_status"] == "cancellation"
        assert d["tasks_cancelled"] == 7

    def test_derive_shutdown_from_snapshot_with_events(self) -> None:
        """_derive_shutdown_evidence_from_snapshot extracts events from diagnostics."""
        from medre.runtime.evidence._diagnostics_sections import (
            _derive_shutdown_evidence_from_snapshot,
        )

        snapshot = {
            "lifecycle": {"runtime_state": "stopped"},
            "outbox": {"counts": {"sent": 3}},
            "retry": {},
            "capacity": {},
            "diagnostics": {
                "runtime_events": {
                    "events": [
                        {
                            "event_type": "adapter_start_failed",
                            "detail": {"adapter_id": "a"},
                        },
                    ],
                },
            },
        }
        result = _derive_shutdown_evidence_from_snapshot(snapshot)
        assert result["shutdown_status"] == "adapter_failure"


# ---------------------------------------------------------------------------
# 16. Lifecycle convergence report in storage section
# ---------------------------------------------------------------------------


class TestStorageSectionLifecycleConvergence:
    """Storage section includes lifecycle_convergence_report when event data available."""

    @pytest.mark.asyncio
    async def test_storage_section_with_event_has_lifecycle_report(
        self, tmp_path
    ) -> None:
        from medre.core.storage.sqlite.storage import SQLiteStorage

        db_path = str(tmp_path / "test.db")
        storage = SQLiteStorage(db_path)
        await storage.initialize()

        event = make_storage_event(event_id="ev-lc-001")
        await storage.append(event)

        receipt = DeliveryReceipt(
            receipt_id="rcpt-lc-001",
            event_id="ev-lc-001",
            delivery_plan_id="dp-lc-001",
            target_adapter="radio",
            status="sent",
            source="live",
            created_at=_FIXED_NOW,
        )
        await storage.append_receipt(receipt)
        await storage.close()

        report = await collect_evidence_bundle(
            storage_path=db_path,
            event_id="ev-lc-001",
        )

        storage_data = report["sections"]["storage"]["data"]
        assert storage_data["lifecycle_convergence_report"] is not None
        lc = storage_data["lifecycle_convergence_report"]
        assert "findings" in lc
        assert "total_findings" in lc
        assert "severity_counts" in lc
        assert "worst_severity" in lc

    @pytest.mark.asyncio
    async def test_storage_section_lifecycle_report_empty_when_clean(
        self, tmp_path
    ) -> None:
        """Clean delivery state produces empty lifecycle findings."""
        from medre.core.storage.sqlite.storage import SQLiteStorage

        db_path = str(tmp_path / "test.db")
        storage = SQLiteStorage(db_path)
        await storage.initialize()

        event = make_storage_event(event_id="ev-lc-clean")
        await storage.append(event)

        receipt = DeliveryReceipt(
            receipt_id="rcpt-lc-clean",
            event_id="ev-lc-clean",
            delivery_plan_id="dp-lc-clean",
            target_adapter="radio",
            status="sent",
            source="live",
            created_at=_FIXED_NOW,
        )
        await storage.append_receipt(receipt)
        await storage.close()

        report = await collect_evidence_bundle(
            storage_path=db_path,
            event_id="ev-lc-clean",
        )

        storage_data = report["sections"]["storage"]["data"]
        lc = storage_data["lifecycle_convergence_report"]
        assert lc is not None
        assert lc["total_findings"] == 0

    @pytest.mark.asyncio
    async def test_storage_section_no_event_lifecycle_report_null(
        self, tmp_path
    ) -> None:
        """Without event_id, lifecycle_convergence_report stays None."""
        from medre.core.storage.sqlite.storage import SQLiteStorage

        db_path = str(tmp_path / "test.db")
        storage = SQLiteStorage(db_path)
        await storage.initialize()
        await storage.close()

        report = await collect_evidence_bundle(storage_path=db_path)

        storage_data = report["sections"]["storage"]["data"]
        assert storage_data["lifecycle_convergence_report"] is None
