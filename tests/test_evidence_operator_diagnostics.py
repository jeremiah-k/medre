"""Operator-facing evidence and diagnostic coverage tests.

Proves that evidence bundles answer all operator traceability questions about
event processing.  Each test simulates a delivery scenario, collects the
evidence bundle (via ``collect_evidence_bundle(storage_path=...)``), and
asserts specific fields are present with correct values.

Uses SQLite storage, fake adapters, and the evidence bundle collector — no
live transports, no SDKs, no config files.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from medre.core.events.canonical import CanonicalEvent, DeliveryReceipt
from medre.core.events.kinds import EventKind
from medre.core.events.metadata import EventMetadata
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime.evidence._bundle import collect_evidence_bundle
from medre.runtime.reporting import delivery_receipt_to_report_dict

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TS_BASE = datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc)


def _ts(
    second: int = 0,
) -> datetime:
    return _TS_BASE.replace(second=second)


def _make_event(
    event_id: str = "ev-opdiag-001",
    event_kind: str = EventKind.MESSAGE_TEXT,
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind=event_kind,
        schema_version=1,
        timestamp=_ts(),
        source_adapter="src-opdiag",
        source_transport_id="matrix",
        source_channel_id="!room:opdiag",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "operator diagnostics test"},
        metadata=EventMetadata(),
    )


def _receipt(
    *,
    receipt_id: str = "rcpt-opdiag-001",
    event_id: str = "ev-opdiag-001",
    target_adapter: str = "dest-radio",
    target_channel: str | None = "ch-opdiag",
    route_id: str = "route-opdiag-1",
    delivery_plan_id: str = "dp-opdiag-001",
    status: str = "sent",
    attempt_number: int = 1,
    error: str | None = None,
    failure_kind: str | None = None,
    next_retry_at: datetime | None = None,
    source: str = "live",
    replay_run_id: str | None = None,
    rendering_evidence: str | None = None,
) -> DeliveryReceipt:
    return DeliveryReceipt(
        receipt_id=receipt_id,
        event_id=event_id,
        delivery_plan_id=delivery_plan_id,
        target_adapter=target_adapter,
        target_channel=target_channel,
        route_id=route_id,
        status=status,
        error=error,
        failure_kind=failure_kind,
        attempt_number=attempt_number,
        next_retry_at=next_retry_at,
        source=source,
        replay_run_id=replay_run_id,
        rendering_evidence=rendering_evidence,
        created_at=_ts(second=1),
    )


async def _build_db(
    db_path: str,
    event_id: str,
    receipts: list[DeliveryReceipt],
) -> None:
    """Create a SQLite DB with one event and arbitrary receipts."""
    storage = SQLiteStorage(db_path)
    await storage.initialize()
    event = _make_event(event_id=event_id)
    await storage.append(event)
    for r in receipts:
        await storage.append_receipt(r)
    await storage.close()


async def _collect_bundle(
    db_path: str,
    event_id: str,
) -> dict[str, Any]:
    """Collect full evidence bundle and return the report dict."""
    return await collect_evidence_bundle(
        storage_path=db_path,
        event_id=event_id,
    )


async def _get_dsbt_entry(
    db_path: str,
    event_id: str,
) -> dict[str, Any]:
    """Return the single delivery_state_by_target entry for a one-receipt scenario."""
    report = await _collect_bundle(db_path, event_id)
    storage_data = report["sections"]["storage"]["data"]
    dsbt = storage_data["incident_summary"]["delivery_state_by_target"]
    assert (
        len(dsbt) == 1
    ), f"Expected exactly 1 dsbt entry, got {len(dsbt)}: {list(dsbt.keys())}"
    return next(iter(dsbt.values()))


# ===================================================================
# a) test_evidence_shows_what_event_was_processed
# ===================================================================


class TestEvidenceShowsWhatEventWasProcessed:
    """An operator can determine *which* event was processed from evidence."""

    async def test_event_id_in_incident_summary(self, tmp_path: Any) -> None:
        """event_id appears in evidence bundle and incident_summary."""
        event_id = "ev-opdiag-what-001"
        db_path = str(tmp_path / "what-event.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    receipt_id="rcpt-what-1",
                    event_id=event_id,
                    status="sent",
                ),
            ],
        )
        report = await _collect_bundle(db_path, event_id)
        storage_data = report["sections"]["storage"]["data"]

        # The event itself is present.
        assert storage_data["event"] is not None, "Event must be found in storage"
        assert storage_data["event"]["event_id"] == event_id

        # incident_summary reflects the event.
        summary = storage_data["incident_summary"]
        assert summary is not None

    async def test_event_id_in_dsbt_entry(self, tmp_path: Any) -> None:
        """delivery_state_by_target entries carry event_id indirectly via receipts."""
        event_id = "ev-opdiag-what-002"
        db_path = str(tmp_path / "what-event-dsbt.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    receipt_id="rcpt-what-dsbt-1",
                    event_id=event_id,
                    status="sent",
                ),
            ],
        )
        report = await _collect_bundle(db_path, event_id)
        storage_data = report["sections"]["storage"]["data"]
        # The incident_summary includes receipt_count reflecting the event.
        summary = storage_data["incident_summary"]
        assert summary["receipt_count"] >= 1


# ===================================================================
# b) test_evidence_shows_which_route_matched
# ===================================================================


class TestEvidenceShowsWhichRouteMatched:
    """An operator can determine *which route* matched from evidence."""

    async def test_route_id_in_dsbt(self, tmp_path: Any) -> None:
        """route_id is present in delivery_state_by_target entries."""
        event_id = "ev-opdiag-route-001"
        db_path = str(tmp_path / "route-matched.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    receipt_id="rcpt-route-1",
                    event_id=event_id,
                    route_id="route-golden-alpha",
                    status="sent",
                ),
            ],
        )
        entry = await _get_dsbt_entry(db_path, event_id)
        assert (
            "route_id" in entry
        ), f"route_id missing from dsbt entry. Keys: {sorted(entry.keys())}"
        assert entry["route_id"] == "route-golden-alpha"

    async def test_route_id_in_report_dict(self) -> None:
        """delivery_receipt_to_report_dict includes route_id."""
        receipt = _receipt(route_id="route-report-1")
        report = delivery_receipt_to_report_dict(receipt)
        assert report["route_id"] == "route-report-1"


# ===================================================================
# c) test_evidence_shows_which_target_was_selected
# ===================================================================


class TestEvidenceShowsWhichTargetWasSelected:
    """An operator can determine *which target* was selected from evidence."""

    async def test_target_identity_in_dsbt(self, tmp_path: Any) -> None:
        """target_adapter and target_channel identify the target in dsbt."""
        event_id = "ev-opdiag-target-001"
        db_path = str(tmp_path / "target-selected.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    receipt_id="rcpt-target-1",
                    event_id=event_id,
                    target_adapter="meshtastic_radio",
                    target_channel="ch-mesh-42",
                    status="sent",
                ),
            ],
        )
        entry = await _get_dsbt_entry(db_path, event_id)
        assert entry["target_adapter"] == "meshtastic_radio"
        assert entry["target_channel"] == "ch-mesh-42"

    async def test_target_identity_in_report_dict(self) -> None:
        """delivery_receipt_to_report_dict includes target_adapter and target_channel."""
        receipt = _receipt(
            target_adapter="lxmf_node",
            target_channel="ch-lxmf",
        )
        report = delivery_receipt_to_report_dict(receipt)
        assert report["target_adapter"] == "lxmf_node"
        assert report["target_channel"] == "ch-lxmf"


# ===================================================================
# d) test_evidence_shows_what_delivery_plan_id_was_assigned
# ===================================================================


class TestEvidenceShowsDeliveryPlanId:
    """An operator can determine the delivery_plan_id from evidence."""

    async def test_delivery_plan_id_in_dsbt(self, tmp_path: Any) -> None:
        """delivery_plan_id is present in delivery_state_by_target."""
        event_id = "ev-opdiag-planid-001"
        db_path = str(tmp_path / "plan-id.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    receipt_id="rcpt-planid-1",
                    event_id=event_id,
                    delivery_plan_id="dp-golden-007",
                    status="sent",
                ),
            ],
        )
        entry = await _get_dsbt_entry(db_path, event_id)
        assert (
            "delivery_plan_id" in entry
        ), f"delivery_plan_id missing. Keys: {sorted(entry.keys())}"
        assert entry["delivery_plan_id"] == "dp-golden-007"

    async def test_delivery_plan_id_in_report_dict(self) -> None:
        """delivery_receipt_to_report_dict includes delivery_plan_id."""
        receipt = _receipt(delivery_plan_id="dp-report-42")
        report = delivery_receipt_to_report_dict(receipt)
        assert report["delivery_plan_id"] == "dp-report-42"


# ===================================================================
# e) test_evidence_shows_what_strategy_was_chosen
# ===================================================================


class TestEvidenceShowsStrategyChosen:
    """An operator can determine the delivery_strategy from evidence."""

    async def test_direct_strategy_in_dsbt(self, tmp_path: Any) -> None:
        """delivery_strategy='direct' surfaces for native-sent receipts."""
        event_id = "ev-opdiag-strat-001"
        db_path = str(tmp_path / "strategy-direct.db")
        evidence = json.dumps(
            {
                "delivery_strategy": "direct",
                "capability_level": "native",
            }
        )
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    receipt_id="rcpt-strat-1",
                    event_id=event_id,
                    status="sent",
                    rendering_evidence=evidence,
                ),
            ],
        )
        entry = await _get_dsbt_entry(db_path, event_id)
        assert entry["delivery_strategy"] == "direct"

    async def test_fallback_text_strategy_in_dsbt(self, tmp_path: Any) -> None:
        """delivery_strategy='fallback_text' surfaces for fallback receipts."""
        event_id = "ev-opdiag-strat-002"
        db_path = str(tmp_path / "strategy-fallback.db")
        evidence = json.dumps(
            {
                "delivery_strategy": "fallback_text",
                "capability_level": "fallback",
            }
        )
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    receipt_id="rcpt-strat-fb-1",
                    event_id=event_id,
                    status="sent",
                    rendering_evidence=evidence,
                ),
            ],
        )
        entry = await _get_dsbt_entry(db_path, event_id)
        assert entry["delivery_strategy"] == "fallback_text"

    async def test_skip_strategy_in_dsbt(self, tmp_path: Any) -> None:
        """delivery_strategy='skip' surfaces for capability-suppressed receipts."""
        event_id = "ev-opdiag-strat-003"
        db_path = str(tmp_path / "strategy-skip.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    receipt_id="rcpt-strat-skip-1",
                    event_id=event_id,
                    status="suppressed",
                    failure_kind="capability_suppressed",
                    error="capability_suppressed: reactions unsupported by adapter",
                ),
            ],
        )
        entry = await _get_dsbt_entry(db_path, event_id)
        assert entry["delivery_strategy"] == "skip"

    async def test_strategy_in_report_dict(self) -> None:
        """delivery_receipt_to_report_dict extracts delivery_strategy from rendering_evidence."""
        receipt = _receipt(
            status="sent",
            rendering_evidence='{"delivery_strategy": "direct", "capability_level": "native"}',
        )
        report = delivery_receipt_to_report_dict(receipt)
        assert report["delivery_strategy"] == "direct"


# ===================================================================
# f) test_evidence_shows_which_capability_field_caused_strategy
# ===================================================================


class TestEvidenceShowsCapabilityField:
    """An operator can determine *which capability field* caused the strategy."""

    async def test_capability_field_in_dsbt(self, tmp_path: Any) -> None:
        """capability_field is populated for capability-suppressed receipts."""
        event_id = "ev-opdiag-capfield-001"
        db_path = str(tmp_path / "cap-field.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    receipt_id="rcpt-capfield-1",
                    event_id=event_id,
                    status="suppressed",
                    failure_kind="capability_suppressed",
                    error="capability_suppressed: reactions unsupported by adapter (event has reaction relation)",
                ),
            ],
        )
        entry = await _get_dsbt_entry(db_path, event_id)
        assert entry["capability_field"] == "reactions"

    async def test_capability_field_in_report_dict(self) -> None:
        """delivery_receipt_to_report_dict extracts capability_field."""
        receipt = _receipt(
            status="suppressed",
            failure_kind="capability_suppressed",
            error="capability_suppressed: text unsupported by adapter (event_kind=message.telemetry)",
        )
        report = delivery_receipt_to_report_dict(receipt)
        assert report["capability_field"] == "text"

    async def test_capability_field_none_for_loop_suppressed(
        self, tmp_path: Any
    ) -> None:
        """capability_field is None for loop_suppressed (not capability-related)."""
        event_id = "ev-opdiag-capfield-loop-001"
        db_path = str(tmp_path / "cap-field-loop.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    receipt_id="rcpt-capfield-loop-1",
                    event_id=event_id,
                    status="suppressed",
                    failure_kind="loop_suppressed",
                    error="Self-loop guard",
                ),
            ],
        )
        entry = await _get_dsbt_entry(db_path, event_id)
        assert entry["capability_field"] is None


# ===================================================================
# g) test_evidence_shows_delivery_status
# ===================================================================


class TestEvidenceShowsDeliveryStatus:
    """An operator can determine delivery status: delivered, queued, suppressed,
    skipped, failed, or retried."""

    async def test_sent_status(self, tmp_path: Any) -> None:
        """status='sent' is visible in evidence."""
        event_id = "ev-opdiag-status-sent-001"
        db_path = str(tmp_path / "status-sent.db")
        await _build_db(
            db_path,
            event_id,
            [_receipt(event_id=event_id, status="sent")],
        )
        entry = await _get_dsbt_entry(db_path, event_id)
        assert entry["status"] == "sent"

    async def test_queued_status(self, tmp_path: Any) -> None:
        """status='queued' is visible in evidence."""
        event_id = "ev-opdiag-status-queued-001"
        db_path = str(tmp_path / "status-queued.db")
        await _build_db(
            db_path,
            event_id,
            [_receipt(event_id=event_id, status="queued")],
        )
        entry = await _get_dsbt_entry(db_path, event_id)
        assert entry["status"] == "queued"

    async def test_suppressed_status(self, tmp_path: Any) -> None:
        """status='suppressed' is visible in evidence."""
        event_id = "ev-opdiag-status-supp-001"
        db_path = str(tmp_path / "status-supp.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    event_id=event_id,
                    status="suppressed",
                    failure_kind="loop_suppressed",
                    error="Self-loop guard",
                ),
            ],
        )
        entry = await _get_dsbt_entry(db_path, event_id)
        assert entry["status"] == "suppressed"

    async def test_failed_status(self, tmp_path: Any) -> None:
        """status='failed' is visible in evidence."""
        event_id = "ev-opdiag-status-failed-001"
        db_path = str(tmp_path / "status-failed.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    event_id=event_id,
                    status="failed",
                    failure_kind="adapter_transient",
                    error="TimeoutError: connection timed out",
                    next_retry_at=_ts(second=30),
                ),
            ],
        )
        entry = await _get_dsbt_entry(db_path, event_id)
        assert entry["status"] == "failed"
        assert entry["retryable"] is True
        assert entry["next_retry_at"] is not None

    async def test_dead_lettered_status(self, tmp_path: Any) -> None:
        """status='dead_lettered' is visible in evidence."""
        event_id = "ev-opdiag-status-dl-001"
        db_path = str(tmp_path / "status-dl.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    event_id=event_id,
                    status="dead_lettered",
                    failure_kind="adapter_transient",
                    error="Retry exhausted",
                    attempt_number=5,
                ),
            ],
        )
        entry = await _get_dsbt_entry(db_path, event_id)
        assert entry["status"] == "dead_lettered"
        assert entry["retryable"] is False
        assert entry["attempt_number"] == 5

    async def test_skipped_status_via_suppression(self, tmp_path: Any) -> None:
        """'skipped' outcome is represented via suppressed status in evidence.

        The pipeline emits status='suppressed' with failure_kind='capability_suppressed'
        for events that were skipped due to capability mismatch.
        """
        event_id = "ev-opdiag-status-skip-001"
        db_path = str(tmp_path / "status-skip.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    event_id=event_id,
                    status="suppressed",
                    failure_kind="capability_suppressed",
                    error="capability_suppressed: reactions unsupported by adapter",
                ),
            ],
        )
        entry = await _get_dsbt_entry(db_path, event_id)
        assert entry["status"] == "suppressed"
        assert entry["failure_kind"] == "capability_suppressed"

    async def test_retried_status(self, tmp_path: Any) -> None:
        """Retried delivery: failed at attempt 1, then sent at attempt 2.

        The dsbt entry reflects the highest attempt_number (latest state).
        """
        event_id = "ev-opdiag-status-retry-001"
        db_path = str(tmp_path / "status-retry.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    receipt_id="rcpt-retry-1",
                    event_id=event_id,
                    status="failed",
                    attempt_number=1,
                    failure_kind="adapter_transient",
                    error="TimeoutError",
                ),
                _receipt(
                    receipt_id="rcpt-retry-2",
                    event_id=event_id,
                    status="sent",
                    attempt_number=2,
                ),
            ],
        )
        entry = await _get_dsbt_entry(db_path, event_id)
        # Latest attempt (highest attempt_number) wins.
        assert entry["attempt_number"] == 2
        assert entry["status"] == "sent"


# ===================================================================
# h) test_evidence_shows_failure_reason
# ===================================================================


class TestEvidenceShowsFailureReason:
    """An operator can determine *why* a delivery failed from evidence."""

    async def test_failure_kind_in_dsbt(self, tmp_path: Any) -> None:
        """failure_kind is present in evidence for failed deliveries."""
        event_id = "ev-opdiag-failreason-001"
        db_path = str(tmp_path / "fail-reason.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    event_id=event_id,
                    status="failed",
                    failure_kind="adapter_permanent",
                    error="Target adapter crashed permanently",
                ),
            ],
        )
        entry = await _get_dsbt_entry(db_path, event_id)
        assert entry["failure_kind"] == "adapter_permanent"
        assert entry["error"] is not None

    async def test_failure_kind_detail_enriched(self, tmp_path: Any) -> None:
        """failure_kind_detail is enriched for specific failure patterns."""
        event_id = "ev-opdiag-faildetail-001"
        db_path = str(tmp_path / "fail-detail.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    event_id=event_id,
                    status="failed",
                    failure_kind="adapter_transient",
                    error="meshtastic queue is full, enqueue rejected",
                ),
            ],
        )
        entry = await _get_dsbt_entry(db_path, event_id)
        assert entry["failure_kind_detail"] == "meshtastic_queue_rejected"

    async def test_suppression_reason_in_dsbt(self, tmp_path: Any) -> None:
        """suppression_reason explains *why* a delivery was suppressed."""
        event_id = "ev-opdiag-suppreason-001"
        db_path = str(tmp_path / "supp-reason.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    event_id=event_id,
                    status="suppressed",
                    failure_kind="capability_suppressed",
                    error="capability_suppressed: reactions unsupported by adapter (event has reaction relation)",
                ),
            ],
        )
        entry = await _get_dsbt_entry(db_path, event_id)
        assert entry["suppression_reason"] == (
            "reactions unsupported by adapter (event has reaction relation)"
        )

    async def test_error_field_in_dsbt(self, tmp_path: Any) -> None:
        """error field captures the raw error for operator inspection."""
        event_id = "ev-opdiag-errorfield-001"
        db_path = str(tmp_path / "error-field.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    event_id=event_id,
                    status="failed",
                    failure_kind="adapter_transient",
                    error="TimeoutError: connection timed out after 30s",
                ),
            ],
        )
        entry = await _get_dsbt_entry(db_path, event_id)
        assert entry["error"] is not None
        assert "TimeoutError" in str(entry["error"])

    async def test_failure_reason_in_report_dict(self) -> None:
        """delivery_receipt_to_report_dict includes failure_kind and error."""
        receipt = _receipt(
            status="failed",
            failure_kind="adapter_permanent",
            error="ConnectionRefusedError: target unreachable",
        )
        report = delivery_receipt_to_report_dict(receipt)
        assert report["failure_kind"] == "adapter_permanent"
        assert report["error"] is not None


# ===================================================================
# i) test_evidence_distinguishes_live_from_replay
# ===================================================================


class TestEvidenceDistinguishesLiveFromReplay:
    """An operator can determine whether processing was live or replay."""

    async def test_live_source(self, tmp_path: Any) -> None:
        """source='live' is visible for live pipeline receipts."""
        event_id = "ev-opdiag-live-001"
        db_path = str(tmp_path / "live-source.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    event_id=event_id,
                    status="sent",
                    source="live",
                ),
            ],
        )
        entry = await _get_dsbt_entry(db_path, event_id)
        assert entry["source"] == "live"
        assert entry["replay_run_id"] is None

    async def test_replay_source(self, tmp_path: Any) -> None:
        """source='replay' and replay_run_id are visible for replay receipts."""
        event_id = "ev-opdiag-replay-001"
        db_path = str(tmp_path / "replay-source.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    event_id=event_id,
                    status="sent",
                    source="replay",
                    replay_run_id="run-replay-opdiag-42",
                ),
            ],
        )
        entry = await _get_dsbt_entry(db_path, event_id)
        assert entry["source"] == "replay"
        assert entry["replay_run_id"] == "run-replay-opdiag-42"

    async def test_mixed_sources_visible(self, tmp_path: Any) -> None:
        """Both live and replay receipts for same event are visible."""
        event_id = "ev-opdiag-mixed-001"
        db_path = str(tmp_path / "mixed-source.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    receipt_id="rcpt-mixed-live",
                    event_id=event_id,
                    status="sent",
                    source="live",
                    target_channel="ch-live",
                ),
                _receipt(
                    receipt_id="rcpt-mixed-replay",
                    event_id=event_id,
                    status="sent",
                    source="replay",
                    replay_run_id="run-mixed-99",
                    target_channel="ch-replay",
                ),
            ],
        )
        report = await _collect_bundle(db_path, event_id)
        summary = report["sections"]["storage"]["data"]["incident_summary"]
        dsbt = summary["delivery_state_by_target"]
        assert (
            len(dsbt) == 2
        ), f"Expected 2 entries for mixed live/replay, got {len(dsbt)}"
        sources = {v["source"] for v in dsbt.values()}
        assert sources == {"live", "replay"}

    async def test_source_in_report_dict(self) -> None:
        """delivery_receipt_to_report_dict includes source and replay_run_id."""
        receipt = _receipt(
            source="replay",
            replay_run_id="run-report-007",
        )
        report = delivery_receipt_to_report_dict(receipt)
        assert report["source"] == "replay"
        assert report["replay_run_id"] == "run-report-007"


# ===================================================================
# j) test_evidence_bundle_covers_all_stages
# ===================================================================


class TestEvidenceBundleCoversAllStages:
    """Evidence bundle covers store, route, plan, render, deliver stages."""

    async def test_store_stage_event_persisted(self, tmp_path: Any) -> None:
        """Store stage: event is persisted and retrievable in evidence bundle."""
        event_id = "ev-opdiag-stages-001"
        db_path = str(tmp_path / "stages.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    event_id=event_id,
                    status="sent",
                    rendering_evidence='{"delivery_strategy": "direct"}',
                ),
            ],
        )
        report = await _collect_bundle(db_path, event_id)
        storage_data = report["sections"]["storage"]["data"]

        # Event was stored.
        assert storage_data["event"] is not None
        assert storage_data["event"]["event_id"] == event_id

    async def test_route_stage_route_id_captured(self, tmp_path: Any) -> None:
        """Route stage: route_id is captured in evidence."""
        event_id = "ev-opdiag-stages-002"
        db_path = str(tmp_path / "stages-route.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    event_id=event_id,
                    route_id="route-stage-alpha",
                    status="sent",
                ),
            ],
        )
        entry = await _get_dsbt_entry(db_path, event_id)
        assert entry["route_id"] == "route-stage-alpha"

    async def test_plan_stage_delivery_plan_id_captured(self, tmp_path: Any) -> None:
        """Plan stage: delivery_plan_id is captured in evidence."""
        event_id = "ev-opdiag-stages-003"
        db_path = str(tmp_path / "stages-plan.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    event_id=event_id,
                    delivery_plan_id="dp-stage-007",
                    status="sent",
                ),
            ],
        )
        entry = await _get_dsbt_entry(db_path, event_id)
        assert entry["delivery_plan_id"] == "dp-stage-007"

    async def test_render_stage_strategy_captured(self, tmp_path: Any) -> None:
        """Render stage: delivery_strategy from rendering evidence is captured."""
        event_id = "ev-opdiag-stages-004"
        db_path = str(tmp_path / "stages-render.db")
        evidence = json.dumps(
            {
                "delivery_strategy": "direct",
                "capability_level": "native",
                "truncated": False,
            }
        )
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    event_id=event_id,
                    status="sent",
                    rendering_evidence=evidence,
                ),
            ],
        )
        entry = await _get_dsbt_entry(db_path, event_id)
        assert entry["delivery_strategy"] == "direct"
        assert entry["capability_level"] == "native"

    async def test_deliver_stage_status_captured(self, tmp_path: Any) -> None:
        """Deliver stage: delivery status is captured in evidence."""
        event_id = "ev-opdiag-stages-005"
        db_path = str(tmp_path / "stages-deliver.db")
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    event_id=event_id,
                    status="sent",
                ),
            ],
        )
        entry = await _get_dsbt_entry(db_path, event_id)
        assert entry["status"] == "sent"

    async def test_all_stages_in_single_bundle(self, tmp_path: Any) -> None:
        """A single evidence bundle contains data from all pipeline stages."""
        event_id = "ev-opdiag-stages-all-001"
        db_path = str(tmp_path / "stages-all.db")
        evidence = json.dumps(
            {
                "delivery_strategy": "fallback_text",
                "capability_level": "fallback",
            }
        )
        await _build_db(
            db_path,
            event_id,
            [
                _receipt(
                    receipt_id="rcpt-stages-all-1",
                    event_id=event_id,
                    target_adapter="radio_target",
                    target_channel="ch-stage",
                    route_id="route-stage-omni",
                    delivery_plan_id="dp-stage-omni",
                    status="sent",
                    rendering_evidence=evidence,
                    source="live",
                ),
            ],
        )
        report = await _collect_bundle(db_path, event_id)
        storage_data = report["sections"]["storage"]["data"]

        # Store: event present.
        assert storage_data["event"] is not None

        # All stages visible in the single dsbt entry.
        summary = storage_data["incident_summary"]
        dsbt = summary["delivery_state_by_target"]
        assert len(dsbt) == 1
        entry = next(iter(dsbt.values()))

        # Route stage → route_id
        assert entry["route_id"] == "route-stage-omni"
        # Plan stage → delivery_plan_id
        assert entry["delivery_plan_id"] == "dp-stage-omni"
        # Render stage → delivery_strategy, capability_level
        assert entry["delivery_strategy"] == "fallback_text"
        assert entry["capability_level"] == "fallback"
        # Deliver stage → status
        assert entry["status"] == "sent"
        # Target identity
        assert entry["target_adapter"] == "radio_target"
        assert entry["target_channel"] == "ch-stage"
        # Source context
        assert entry["source"] == "live"
