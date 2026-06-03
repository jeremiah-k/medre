"""Suppression-evidence integration tests for delivery evidence.

Moved from test_delivery_evidence_unification.py to keep the main file under
the 1500-line limit.  These tests exercise the real PipelineRunner with fake
adapters and collect_evidence_bundle to produce operator-facing evidence.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from medre.core.contracts.adapter import AdapterCapabilities
from medre.core.events.canonical import CanonicalEvent, DeliveryReceipt
from medre.core.events.kinds import EventKind
from medre.core.events.metadata import EventMetadata
from medre.core.planning.delivery_plan import DeliveryFailureKind
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime.evidence._bundle import collect_evidence_bundle
from medre.runtime.reporting import (
    _derive_capability_evidence,
    delivery_receipt_to_report_dict,
)
from tests.helpers.pipeline import make_event

if TYPE_CHECKING:
    from medre.core.planning.capability_decision import CapabilityDecision

# ===================================================================
# 13. Loop-suppression suppression-evidence: PipelineRunner → suppressed
#     receipt → collect_evidence_bundle → incident_summary
# ===================================================================


class TestLoopSuppressionEvidence:
    """Focused suppression-evidence test proving that a loop-suppression scenario
    exercised through the **real PipelineRunner** self-loop guard produces
    a ``skipped`` / ``loop_suppressed`` outcome, persists a
    ``status="suppressed"`` receipt in SQLite storage, and surfaces
    coherent operator-facing evidence via ``incident_summary`` and
    ``delivery_state_by_target``.

    Uses fake adapters (``FakeMatrixAdapter``), temp SQLite storage,
    ``PipelineRunner``, ``Router``, ``RenderingPipeline``, and
    ``collect_evidence_bundle(storage_path=...)`` — no config file, no
    live adapters, no external services.
    """

    @pytest.mark.asyncio
    async def test_loop_suppression_evidence(self, tmp_path: Path) -> None:
        """End-to-end suppression-evidence via PipelineRunner self-loop guard.

        Scenario: an event arriving from adapter ``golden-loop-mx`` is
        routed back to the same adapter (self-loop route).  The
        PipelineRunner's self-loop guard suppresses delivery, persists a
        suppressed receipt, and the evidence bundle surfaces coherent
        operator-facing data.
        """
        from medre.adapters.fakes.matrix import FakeMatrixAdapter
        from medre.core.engine.pipeline import PipelineRunner
        from medre.core.events.kinds import EventKind
        from medre.core.routing import Route, Router, RouteSource, RouteTarget
        from medre.core.routing.stats import RouteStats
        from medre.core.storage.sqlite.storage import SQLiteStorage
        from medre.core.supervision.accounting import RuntimeAccounting
        from tests.helpers.bridge import make_adapter_context, make_pipeline_config

        # -- Setup: self-loop route where source == target adapter ----------
        adapter_id = "golden-loop-mx"
        target_channel = "!golden-loop:fake"

        db_path = str(tmp_path / "suppression_loop.db")
        storage = SQLiteStorage(db_path)
        await storage.initialize()

        fake_adapter = FakeMatrixAdapter(adapter_id, channel=target_channel)

        route = Route(
            id="golden-loop-route",
            source=RouteSource(
                adapter=adapter_id,
                event_kinds=("message.created",),
                channel=None,
            ),
            targets=[RouteTarget(adapter=adapter_id, channel=target_channel)],
        )
        router = Router(routes=[route])

        accounting = RuntimeAccounting()
        route_stats = RouteStats()
        config = make_pipeline_config(
            storage=storage,
            router=router,
            adapters={adapter_id: fake_adapter},
            accounting=accounting,
            route_stats=route_stats,
        )
        runner = PipelineRunner(config)
        await runner.start()

        try:
            await fake_adapter.start(make_adapter_context(adapter_id, runner))

            # Create event via fake adapter — source_adapter == adapter_id.
            event = fake_adapter.make_event(
                text="golden loop suppression test",
                event_kind=EventKind.MESSAGE_CREATED,
            )
            outcomes = await runner.handle_ingress(event)

            # -- Phase 1: DeliveryOutcome from real PipelineRunner ----------------
            assert len(outcomes) == 1
            outcome = outcomes[0]

            # Outcome is skipped with LOOP_SUPPRESSED failure kind.
            assert outcome.status == "skipped"
            assert outcome.failure_kind is DeliveryFailureKind.LOOP_SUPPRESSED
            assert outcome.failure_kind.is_retryable is False

            # Outcome carries the suppressed receipt with correct fields.
            assert outcome.receipt is not None
            assert outcome.receipt.status == "suppressed"
            assert outcome.receipt.failure_kind == "loop_suppressed"
            assert outcome.receipt.next_retry_at is None
            assert outcome.receipt.attempt_number == 1

            # event_id in outcome and receipt match the actual pipeline event.
            assert outcome.event_id == event.event_id
            assert outcome.receipt.event_id == event.event_id

            # -- Phase 2: Suppressed receipt persisted in storage ----------------
            stored_receipts = await storage.list_receipts_for_event(
                event.event_id,
            )
            assert (
                len(stored_receipts) == 1
            ), f"Expected exactly 1 receipt, got {len(stored_receipts)}"
            suppressed = [r for r in stored_receipts if r.status == "suppressed"]
            assert len(suppressed) == 1
            assert suppressed[0].failure_kind == "loop_suppressed"

            # No actual delivery to the adapter.
            assert len(fake_adapter.delivered_payloads) == 0
        finally:
            await fake_adapter.stop()
            await runner.stop()
            await storage.close()

        # -- Phase 3: Evidence bundle via storage_path -----------------------
        report = await collect_evidence_bundle(
            storage_path=db_path,
            event_id=event.event_id,
        )

        storage_section = report["sections"]["storage"]
        assert (
            storage_section["status"] == "passed"
        ), f"Storage section error: {storage_section.get('error')}"

        data = storage_section["data"]
        assert data["event"] is not None, "Event should be found in storage"
        assert (
            data["receipt_count"] == 1
        ), f"Expected exactly 1 receipt, got {data['receipt_count']}"

        # -- Phase 4: Incident summary assertions ----------------------------
        summary = data["incident_summary"]
        assert summary is not None, "incident_summary must be present"

        # suppressed_count reflects the suppressed receipt.
        assert (
            summary["suppressed_count"] == 1
        ), f"Expected suppressed_count == 1, got {summary['suppressed_count']}"

        # Classification is permanent for loop_suppressed.
        assert summary["classification"] == "permanent", (
            f"Expected 'permanent' for loop_suppressed, "
            f"got {summary['classification']!r}"
        )

        # first_failure_kind is loop_suppressed.
        assert summary["first_failure_kind"] == "loop_suppressed"

        # No failed or dead_lettered receipts.
        assert summary["failed_count"] == 0
        assert summary["dead_lettered_count"] == 0

        # -- Phase 5: delivery_state_by_target assertions -------------------
        dsbt = summary["delivery_state_by_target"]
        assert isinstance(
            dsbt, dict
        ), f"delivery_state_by_target must be dict, got {type(dsbt).__name__}"
        assert len(dsbt) == 1, (
            f"Expected exactly 1 entry in delivery_state_by_target, "
            f"got {len(dsbt)}: {list(dsbt.keys())}"
        )

        target_state = next(iter(dsbt.values()))
        assert target_state["status"] == "suppressed"
        assert target_state["failure_kind"] == "loop_suppressed"
        assert target_state["retryable"] is False
        assert (
            "target_channel" in target_state
        ), "delivery_state_by_target entry must include 'target_channel' key"
        assert target_state["target_channel"] == target_channel, (
            f"Expected target_channel {target_channel!r}, "
            f"got {target_state['target_channel']!r}"
        )
        assert target_state["target_adapter"] == adapter_id

        # -- Phase 6: recommended_commands / commands -------------------------
        assert "recommended_commands" in summary
        cmds = summary["recommended_commands"]
        assert len(cmds) > 0, "Expected non-empty recommended_commands"

        # commands object with primary/specialized exists.
        assert "commands" in summary
        commands = summary["commands"]
        assert "primary" in commands
        assert "specialized" in commands
        assert len(commands["primary"]) > 0
        assert len(commands["specialized"]) > 0


# ===================================================================
# 14. Capability-suppressed evidence: structured operator observability
# ===================================================================


def _ts(
    year: int = 2026,
    month: int = 1,
    day: int = 1,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


def _make_event(event_id: str = "ev-cap-sup-001") -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind=EventKind.MESSAGE_TEXT,
        schema_version=1,
        timestamp=_ts(),
        source_adapter="src-adapter",
        source_transport_id="matrix",
        source_channel_id="!room:test",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": "cap suppression test"},
        metadata=EventMetadata(),
    )


def _cap_suppressed_receipt(
    *,
    receipt_id: str = "rcpt-cap-001",
    event_id: str = "ev-cap-sup-001",
    target_adapter: str = "radio",
    target_channel: str | None = "ch-0",
    route_id: str = "route-cap-1",
    delivery_plan_id: str = "dp-cap-001",
    error: str = "capability_suppressed: reactions unsupported by adapter (event has reaction relation)",
    failure_kind: str = "capability_suppressed",
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
        status="suppressed",
        error=error,
        failure_kind=failure_kind,
        attempt_number=1,
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
    try:
        await storage.initialize()
        event = _make_event(event_id=event_id)
        await storage.append(event)
        for r in receipts:
            await storage.append_receipt(r)
    finally:
        await storage.close()


async def _get_incident_summary(
    db_path: str,
    event_id: str,
) -> dict[str, Any]:
    """Collect evidence bundle and return incident_summary dict."""
    report = await collect_evidence_bundle(
        storage_path=db_path,
        event_id=event_id,
    )
    return report["sections"]["storage"]["data"]["incident_summary"]


class TestCapabilitySuppressedReportDict:
    """Verify delivery_receipt_to_report_dict produces structured capability
    fields for capability-suppressed receipts without requiring the operator
    to parse free-form error strings."""

    def test_capability_suppressed_extracts_reason(self) -> None:
        receipt = _cap_suppressed_receipt(
            error="capability_suppressed: reactions unsupported by adapter (event has reaction relation)",
        )
        report = delivery_receipt_to_report_dict(receipt)
        assert report["suppression_reason"] == (
            "reactions unsupported by adapter (event has reaction relation)"
        )

    def test_capability_suppressed_extracts_capability_field(self) -> None:
        receipt = _cap_suppressed_receipt(
            error="capability_suppressed: reactions unsupported by adapter (event has reaction relation)",
        )
        report = delivery_receipt_to_report_dict(receipt)
        assert report["capability_field"] == "reactions"

    def test_capability_suppressed_extracts_capability_level(self) -> None:
        receipt = _cap_suppressed_receipt(
            error="capability_suppressed: reactions unsupported by adapter (event has reaction relation)",
        )
        report = delivery_receipt_to_report_dict(receipt)
        assert report["capability_level"] == "unsupported"

    def test_capability_suppressed_extracts_delivery_strategy(self) -> None:
        receipt = _cap_suppressed_receipt(
            error="capability_suppressed: reactions unsupported by adapter (event has reaction relation)",
        )
        report = delivery_receipt_to_report_dict(receipt)
        assert report["delivery_strategy"] == "skip"

    def test_capability_suppressed_replay_run_id_present(self) -> None:
        receipt = _cap_suppressed_receipt(
            source="replay",
            replay_run_id="run-cap-42",
        )
        report = delivery_receipt_to_report_dict(receipt)
        assert report["replay_run_id"] == "run-cap-42"
        assert report["source"] == "replay"

    def test_capability_suppressed_live_source_default(self) -> None:
        receipt = _cap_suppressed_receipt()
        report = delivery_receipt_to_report_dict(receipt)
        assert report["source"] == "live"
        assert report["replay_run_id"] is None

    def test_plan_skip_extracts_reason_and_strategy(self) -> None:
        receipt = _cap_suppressed_receipt(
            error="plan_skip: delivery strategy is 'skip' (event_kind=message.reaction)",
            failure_kind="capability_suppressed",
        )
        report = delivery_receipt_to_report_dict(receipt)
        assert report["suppression_reason"] is not None
        assert "skip" in str(report["suppression_reason"])
        assert report["delivery_strategy"] == "skip"
        assert report["capability_level"] == "unsupported"

    def test_loop_suppressed_extracts_reason(self) -> None:
        receipt = _cap_suppressed_receipt(
            error="Self-loop guard",
            failure_kind="loop_suppressed",
        )
        report = delivery_receipt_to_report_dict(receipt)
        assert report["suppression_reason"] == "Self-loop guard"
        assert report["capability_level"] is None
        assert report["capability_field"] is None

    def test_sent_receipt_capability_from_rendering_evidence(self) -> None:
        """Sent receipts get capability_level/delivery_strategy from rendering_evidence."""
        receipt = DeliveryReceipt(
            receipt_id="rcpt-sent-001",
            event_id="ev-cap-sup-001",
            delivery_plan_id="dp-cap-001",
            target_adapter="radio",
            target_channel="ch-0",
            route_id="route-cap-1",
            status="sent",
            failure_kind=None,
            rendering_evidence='{"delivery_strategy": "direct", "capability_level": "native", "truncated": false}',
            source="live",
            created_at=_ts(second=1),
        )
        report = delivery_receipt_to_report_dict(receipt)
        assert report["capability_level"] == "native"
        assert report["delivery_strategy"] == "direct"
        # No suppression_reason for sent receipts.
        assert report["suppression_reason"] is None

    def test_fallback_reason_parsed(self) -> None:
        receipt = _cap_suppressed_receipt(
            error="capability_suppressed: replies fallback for adapter (event has reply relation)",
            failure_kind="capability_suppressed",
        )
        report = delivery_receipt_to_report_dict(receipt)
        assert report["capability_field"] == "replies"
        assert report["capability_level"] == "fallback"
        assert report["delivery_strategy"] == "fallback_text"

    def test_capability_field_from_event_kind_reason(self) -> None:
        receipt = _cap_suppressed_receipt(
            error="capability_suppressed: text unsupported by adapter (event_kind=message.telemetry)",
            failure_kind="capability_suppressed",
        )
        report = delivery_receipt_to_report_dict(receipt)
        assert report["capability_field"] == "text"
        assert report["capability_level"] == "unsupported"
        assert report["delivery_strategy"] == "skip"

    def test_core_fields_present_on_capability_suppressed(self) -> None:
        receipt = _cap_suppressed_receipt(
            receipt_id="rcpt-core-001",
            event_id="ev-core-001",
            target_adapter="meshtastic_adapter",
            target_channel="ch-mesh",
            route_id="route-core-1",
            delivery_plan_id="dp-core-001",
        )
        report = delivery_receipt_to_report_dict(receipt)
        assert report["route_id"] == "route-core-1"
        assert report["target_adapter"] == "meshtastic_adapter"
        assert report["target_channel"] == "ch-mesh"
        assert report["delivery_plan_id"] == "dp-core-001"
        assert report["status"] == "suppressed"
        assert report["failure_kind"] == "capability_suppressed"


class TestDeriveCapabilityEvidenceUnit:
    """Unit tests for _derive_capability_evidence helper."""

    def test_none_inputs_returns_all_none(self) -> None:
        result = _derive_capability_evidence(None, None, None, "sent")
        assert result["suppression_reason"] is None
        assert result["capability_field"] is None
        assert result["capability_level"] is None
        assert result["delivery_strategy"] is None

    def test_rendering_evidence_provides_capability_level(self) -> None:
        result = _derive_capability_evidence(
            error=None,
            rendering_evidence='{"capability_level": "native", "delivery_strategy": "direct"}',
            failure_kind=None,
            status="sent",
        )
        assert result["capability_level"] == "native"
        assert result["delivery_strategy"] == "direct"

    def test_rendering_evidence_invalid_json_ignored(self) -> None:
        result = _derive_capability_evidence(
            error=None,
            rendering_evidence="{broken",
            failure_kind=None,
            status="sent",
        )
        assert result["capability_level"] is None
        assert result["delivery_strategy"] is None

    def test_capability_suppressed_error_pattern(self) -> None:
        result = _derive_capability_evidence(
            error="capability_suppressed: reactions unsupported by adapter (event has reaction relation)",
            rendering_evidence=None,
            failure_kind="capability_suppressed",
            status="suppressed",
        )
        assert result["suppression_reason"] == (
            "reactions unsupported by adapter (event has reaction relation)"
        )
        assert result["capability_field"] == "reactions"
        assert result["capability_level"] == "unsupported"
        assert result["delivery_strategy"] == "skip"

    def test_plan_skip_error_pattern(self) -> None:
        result = _derive_capability_evidence(
            error="plan_skip: delivery strategy is 'skip' (event_kind=message.reaction)",
            rendering_evidence=None,
            failure_kind="capability_suppressed",
            status="suppressed",
        )
        assert result["suppression_reason"] is not None
        assert result["delivery_strategy"] == "skip"
        assert result["capability_level"] == "unsupported"

    def test_loop_suppressed_error(self) -> None:
        result = _derive_capability_evidence(
            error="Self-loop guard",
            rendering_evidence=None,
            failure_kind="loop_suppressed",
            status="suppressed",
        )
        assert result["suppression_reason"] == "Self-loop guard"
        assert result["capability_level"] is None
        assert result["capability_field"] is None
        assert result["delivery_strategy"] is None

    def test_capability_suppressed_no_field_match(self) -> None:
        """capability_suppressed error without field pattern still gets level."""
        result = _derive_capability_evidence(
            error="capability_suppressed: event kind not supported by target adapter(s)",
            rendering_evidence=None,
            failure_kind="capability_suppressed",
            status="suppressed",
        )
        assert result["suppression_reason"] == (
            "event kind not supported by target adapter(s)"
        )
        assert result["capability_field"] is None
        assert result["capability_level"] == "unsupported"
        assert result["delivery_strategy"] == "skip"

    def test_capability_suppressed_unrecognized_error_fallback(self) -> None:
        """capability_suppressed failure_kind with completely unrecognized error
        format still falls back to unsupported/skip via safety net."""
        result = _derive_capability_evidence(
            error="some completely unknown error message",
            rendering_evidence=None,
            failure_kind="capability_suppressed",
            status="suppressed",
        )
        assert result["capability_level"] == "unsupported"
        assert result["delivery_strategy"] == "skip"
        assert result["suppression_reason"] == "some completely unknown error message"
        assert result["capability_field"] is None

    def test_capability_suppressed_empty_error_fallback(self) -> None:
        """capability_suppressed with empty error still gets unsupported/skip."""
        result = _derive_capability_evidence(
            error="",
            rendering_evidence=None,
            failure_kind="capability_suppressed",
            status="suppressed",
        )
        # Empty error is falsy, so the suppressed block is skipped entirely;
        # safety net still kicks in.
        assert result["capability_level"] == "unsupported"
        assert result["delivery_strategy"] == "skip"
        assert result["suppression_reason"] is None

    def test_suppression_reason_sanitized_with_token(self) -> None:
        """suppression_reason is sanitized: tokens in error text are redacted."""
        result = _derive_capability_evidence(
            error="capability_suppressed: text unsupported (token=syt_deadbeef123abc)",
            rendering_evidence=None,
            failure_kind="capability_suppressed",
            status="suppressed",
        )
        assert "syt_deadbeef123abc" not in result["suppression_reason"]
        assert "[REDACTED]" in result["suppression_reason"]
        assert result["capability_field"] == "text"
        assert result["capability_level"] == "unsupported"

    def test_suppression_reason_sanitized_plan_skip_with_token(self) -> None:
        """plan_skip error with embedded token is sanitized in suppression_reason."""
        result = _derive_capability_evidence(
            error="plan_skip: delivery strategy is 'skip' (token=sk-abc123def456ghi789xyz)",
            rendering_evidence=None,
            failure_kind="capability_suppressed",
            status="suppressed",
        )
        assert "sk-abc123def456ghi789xyz" not in result["suppression_reason"]
        assert "[REDACTED]" in result["suppression_reason"]
        assert result["delivery_strategy"] == "skip"
        assert result["capability_level"] == "unsupported"

    def test_suppression_reason_sanitized_loop_suppressed_with_token(self) -> None:
        """loop_suppressed error with embedded token is sanitized."""
        result = _derive_capability_evidence(
            error="Self-loop guard detected (access_token=syt_looptoken123)",
            rendering_evidence=None,
            failure_kind="loop_suppressed",
            status="suppressed",
        )
        assert "syt_looptoken123" not in result["suppression_reason"]
        assert "[REDACTED]" in result["suppression_reason"]
        assert result["capability_level"] is None

    def test_rendering_evidence_overridden_by_capability_suppressed_safety_net(
        self,
    ) -> None:
        """When failure_kind is capability_suppressed but error doesn't match
        any known pattern, safety net overrides rendering_evidence values."""
        result = _derive_capability_evidence(
            error="unrecognized error text",
            rendering_evidence='{"capability_level": "native", "delivery_strategy": "direct"}',
            failure_kind="capability_suppressed",
            status="suppressed",
        )
        # Safety net fills unsupported/skip since error parsing didn't set them.
        assert result["capability_level"] == "unsupported"
        assert result["delivery_strategy"] == "skip"


class TestCapabilitySuppressedEvidenceBundle:
    """Integration tests: capability-suppressed receipts in evidence bundle
    delivery_state_by_target expose all required operator-visible fields."""

    @pytest.mark.asyncio
    async def test_capability_suppressed_dsbt_has_source(self, tmp_path: Path) -> None:
        """delivery_state_by_target includes 'source' for suppressed receipt."""
        event_id = "ev-cap-dsbt-src-001"
        db_path = str(tmp_path / "cap-dsbt-src.db")
        await _build_db(
            db_path,
            event_id,
            [
                _cap_suppressed_receipt(
                    receipt_id="rcpt-src-1",
                    event_id=event_id,
                    source="replay",
                    replay_run_id="run-dsbt-42",
                ),
            ],
        )
        summary = await _get_incident_summary(db_path, event_id)
        dsbt = summary["delivery_state_by_target"]
        assert len(dsbt) == 1
        entry = next(iter(dsbt.values()))
        assert entry["source"] == "replay"
        assert entry["replay_run_id"] == "run-dsbt-42"

    @pytest.mark.asyncio
    async def test_capability_suppressed_dsbt_has_reason(self, tmp_path: Path) -> None:
        """delivery_state_by_target includes suppression_reason for capability_suppressed."""
        event_id = "ev-cap-dsbt-reason-001"
        db_path = str(tmp_path / "cap-dsbt-reason.db")
        await _build_db(
            db_path,
            event_id,
            [
                _cap_suppressed_receipt(
                    receipt_id="rcpt-reason-1",
                    event_id=event_id,
                    error="capability_suppressed: reactions unsupported by adapter (event has reaction relation)",
                ),
            ],
        )
        summary = await _get_incident_summary(db_path, event_id)
        dsbt = summary["delivery_state_by_target"]
        entry = next(iter(dsbt.values()))
        assert entry["suppression_reason"] == (
            "reactions unsupported by adapter (event has reaction relation)"
        )

    @pytest.mark.asyncio
    async def test_capability_suppressed_dsbt_has_capability_field(
        self, tmp_path: Path
    ) -> None:
        """delivery_state_by_target includes capability_field."""
        event_id = "ev-cap-dsbt-cf-001"
        db_path = str(tmp_path / "cap-dsbt-cf.db")
        await _build_db(
            db_path,
            event_id,
            [
                _cap_suppressed_receipt(
                    receipt_id="rcpt-cf-1",
                    event_id=event_id,
                    error="capability_suppressed: reactions unsupported by adapter (event has reaction relation)",
                ),
            ],
        )
        summary = await _get_incident_summary(db_path, event_id)
        dsbt = summary["delivery_state_by_target"]
        entry = next(iter(dsbt.values()))
        assert entry["capability_field"] == "reactions"

    @pytest.mark.asyncio
    async def test_capability_suppressed_dsbt_has_capability_level(
        self, tmp_path: Path
    ) -> None:
        """delivery_state_by_target includes capability_level."""
        event_id = "ev-cap-dsbt-cl-001"
        db_path = str(tmp_path / "cap-dsbt-cl.db")
        await _build_db(
            db_path,
            event_id,
            [
                _cap_suppressed_receipt(
                    receipt_id="rcpt-cl-1",
                    event_id=event_id,
                    error="capability_suppressed: reactions unsupported by adapter (event has reaction relation)",
                ),
            ],
        )
        summary = await _get_incident_summary(db_path, event_id)
        dsbt = summary["delivery_state_by_target"]
        entry = next(iter(dsbt.values()))
        assert entry["capability_level"] == "unsupported"

    @pytest.mark.asyncio
    async def test_capability_suppressed_dsbt_has_delivery_strategy(
        self, tmp_path: Path
    ) -> None:
        """delivery_state_by_target includes delivery_strategy."""
        event_id = "ev-cap-dsbt-ds-001"
        db_path = str(tmp_path / "cap-dsbt-ds.db")
        await _build_db(
            db_path,
            event_id,
            [
                _cap_suppressed_receipt(
                    receipt_id="rcpt-ds-1",
                    event_id=event_id,
                ),
            ],
        )
        summary = await _get_incident_summary(db_path, event_id)
        dsbt = summary["delivery_state_by_target"]
        entry = next(iter(dsbt.values()))
        assert entry["delivery_strategy"] == "skip"

    @pytest.mark.asyncio
    async def test_capability_suppressed_dsbt_has_all_required_fields(
        self, tmp_path: Path
    ) -> None:
        """All operator-required fields are present in a single entry."""
        event_id = "ev-cap-dsbt-all-001"
        db_path = str(tmp_path / "cap-dsbt-all.db")
        await _build_db(
            db_path,
            event_id,
            [
                _cap_suppressed_receipt(
                    receipt_id="rcpt-all-1",
                    event_id=event_id,
                    target_adapter="meshtastic_adapter",
                    target_channel="ch-mesh",
                    route_id="route-all-1",
                    delivery_plan_id="dp-all-001",
                    source="replay",
                    replay_run_id="run-all-42",
                    error="capability_suppressed: reactions unsupported by adapter (event has reaction relation)",
                ),
            ],
        )
        summary = await _get_incident_summary(db_path, event_id)
        dsbt = summary["delivery_state_by_target"]
        assert len(dsbt) == 1
        entry = next(iter(dsbt.values()))

        # Required fields per the task spec.
        assert "route_id" in entry
        assert entry["route_id"] == "route-all-1"
        assert "target_adapter" in entry
        assert entry["target_adapter"] == "meshtastic_adapter"
        assert "target_channel" in entry
        assert entry["target_channel"] == "ch-mesh"
        assert "delivery_plan_id" in entry
        assert entry["delivery_plan_id"] == "dp-all-001"
        assert "status" in entry
        assert entry["status"] == "suppressed"
        assert "failure_kind" in entry
        assert entry["failure_kind"] == "capability_suppressed"
        assert "suppression_reason" in entry
        assert entry["suppression_reason"] is not None
        assert "capability_field" in entry
        assert entry["capability_field"] == "reactions"
        assert "capability_level" in entry
        assert entry["capability_level"] == "unsupported"
        assert "delivery_strategy" in entry
        assert entry["delivery_strategy"] == "skip"
        assert "source" in entry
        assert entry["source"] == "replay"
        assert "replay_run_id" in entry
        assert entry["replay_run_id"] == "run-all-42"
        assert "error" in entry

    @pytest.mark.asyncio
    async def test_loop_suppressed_dsbt_has_suppression_reason(
        self, tmp_path: Path
    ) -> None:
        """Loop-suppressed receipts have suppression_reason but no capability fields."""
        event_id = "ev-cap-dsbt-loop-001"
        db_path = str(tmp_path / "cap-dsbt-loop.db")
        await _build_db(
            db_path,
            event_id,
            [
                _cap_suppressed_receipt(
                    receipt_id="rcpt-loop-1",
                    event_id=event_id,
                    error="Self-loop guard",
                    failure_kind="loop_suppressed",
                ),
            ],
        )
        summary = await _get_incident_summary(db_path, event_id)
        dsbt = summary["delivery_state_by_target"]
        entry = next(iter(dsbt.values()))
        assert entry["suppression_reason"] == "Self-loop guard"
        assert entry["capability_level"] is None
        assert entry["capability_field"] is None
        assert entry["source"] == "live"

    @pytest.mark.asyncio
    async def test_sent_receipt_dsbt_capability_from_rendering_evidence(
        self, tmp_path: Path
    ) -> None:
        """Sent receipts expose capability_level/delivery_strategy from rendering_evidence."""
        event_id = "ev-cap-dsbt-sent-001"
        db_path = str(tmp_path / "cap-dsbt-sent.db")
        await _build_db(
            db_path,
            event_id,
            [
                DeliveryReceipt(
                    receipt_id="rcpt-sent-re-1",
                    event_id=event_id,
                    delivery_plan_id="dp-sent-001",
                    target_adapter="radio",
                    target_channel="ch-0",
                    route_id="route-sent-1",
                    status="sent",
                    source="live",
                    rendering_evidence='{"delivery_strategy": "direct", "capability_level": "native"}',
                    created_at=_ts(second=1),
                ),
            ],
        )
        summary = await _get_incident_summary(db_path, event_id)
        dsbt = summary["delivery_state_by_target"]
        entry = next(iter(dsbt.values()))
        assert entry["capability_level"] == "native"
        assert entry["delivery_strategy"] == "direct"
        assert entry["suppression_reason"] is None
        assert entry["source"] == "live"

    @pytest.mark.asyncio
    async def test_sent_receipt_dsbt_has_replay_run_id(self, tmp_path: Path) -> None:
        """Replay-origin sent receipts expose replay_run_id in dsbt."""
        event_id = "ev-cap-dsbt-replay-001"
        db_path = str(tmp_path / "cap-dsbt-replay.db")
        await _build_db(
            db_path,
            event_id,
            [
                DeliveryReceipt(
                    receipt_id="rcpt-replay-1",
                    event_id=event_id,
                    delivery_plan_id="dp-replay-001",
                    target_adapter="radio",
                    target_channel="ch-0",
                    route_id="route-replay-1",
                    status="sent",
                    source="replay",
                    replay_run_id="run-replay-99",
                    rendering_evidence='{"delivery_strategy": "direct", "capability_level": "native"}',
                    created_at=_ts(second=1),
                ),
            ],
        )
        summary = await _get_incident_summary(db_path, event_id)
        dsbt = summary["delivery_state_by_target"]
        entry = next(iter(dsbt.values()))
        assert entry["replay_run_id"] == "run-replay-99"
        assert entry["source"] == "replay"

    @pytest.mark.asyncio
    async def test_capability_suppressed_dsbt_json_roundtrip(
        self, tmp_path: Path
    ) -> None:
        """delivery_state_by_target with capability fields survives JSON round-trip."""
        event_id = "ev-cap-dsbt-json-001"
        db_path = str(tmp_path / "cap-dsbt-json.db")
        await _build_db(
            db_path,
            event_id,
            [
                _cap_suppressed_receipt(
                    receipt_id="rcpt-json-1",
                    event_id=event_id,
                    error="capability_suppressed: reactions unsupported by adapter (event has reaction relation)",
                ),
            ],
        )
        summary = await _get_incident_summary(db_path, event_id)
        raw = json.dumps(summary, sort_keys=True)
        reloaded = json.loads(raw)
        dsbt = reloaded["delivery_state_by_target"]
        entry = next(iter(dsbt.values()))
        assert entry["capability_field"] == "reactions"
        assert entry["capability_level"] == "unsupported"
        assert entry["delivery_strategy"] == "skip"


# ===================================================================
# 15. Resolver-reason round-trip: coupling guard
# ===================================================================


class TestResolverReasonRoundTrip:
    """Regression tests guarding the coupling between
    CapabilityDecisionResolver.reason and
    _derive_capability_evidence / delivery_receipt_to_report_dict.

    Every test uses the **real resolver** to produce a decision, then
    constructs the ``"capability_suppressed: {reason}"`` error string
    that the delivery pipeline would emit, and feeds it through
    ``_derive_capability_evidence`` (or
    ``delivery_receipt_to_report_dict``) to assert that the
    capability_field and capability_level are correctly derived.

    If a reason-format change in capability_decision.py breaks the
    parser in reporting.py, these tests fail first — before the change
    reaches production.
    """

    @staticmethod
    def _error_from_decision(decision: "CapabilityDecision") -> str:
        """Build the error string the pipeline would emit for a suppressed decision."""
        assert decision.reason is not None, "decision.reason must not be None"
        return f"capability_suppressed: {decision.reason}"

    def test_event_kind_unsupported_round_trip(self) -> None:
        """Event-kind unsupported reason → _derive_capability_evidence."""
        from medre.core.planning.capability_decision import resolver

        caps = AdapterCapabilities(reactions="unsupported")
        event = make_event(event_kind="message.reacted")
        decision = resolver.decide(event, caps)

        error = self._error_from_decision(decision)
        result = _derive_capability_evidence(
            error=error,
            rendering_evidence=None,
            failure_kind="capability_suppressed",
            status="suppressed",
        )
        assert result["capability_field"] == decision.capability_field
        assert result["capability_level"] == "unsupported"
        assert result["delivery_strategy"] == "skip"

    def test_event_kind_fallback_round_trip(self) -> None:
        """Event-kind fallback reason → _derive_capability_evidence."""
        from medre.core.planning.capability_decision import resolver

        caps = AdapterCapabilities(reactions="fallback")
        event = make_event(event_kind="message.reacted")
        decision = resolver.decide(event, caps)

        error = self._error_from_decision(decision)
        result = _derive_capability_evidence(
            error=error,
            rendering_evidence=None,
            failure_kind="capability_suppressed",
            status="suppressed",
        )
        assert result["capability_field"] == decision.capability_field
        assert result["capability_level"] == "fallback"
        assert result["delivery_strategy"] == "fallback_text"

    def test_relation_unsupported_round_trip(self) -> None:
        """Relation unsupported reason → _derive_capability_evidence."""
        from medre.core.events.canonical import EventRelation, NativeRef
        from medre.core.planning.capability_decision import resolver

        caps = AdapterCapabilities(replies="unsupported")
        rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-parent",
            target_native_ref=NativeRef(
                adapter="test_adapter",
                native_channel_id="ch-0",
                native_message_id="native-001",
            ),
            key=None,
            fallback_text="original",
        )
        event = make_event(
            event_kind="plugin.custom",
            relations=(rel,),
        )
        decision = resolver.decide(event, caps)

        error = self._error_from_decision(decision)
        result = _derive_capability_evidence(
            error=error,
            rendering_evidence=None,
            failure_kind="capability_suppressed",
            status="suppressed",
        )
        assert result["capability_field"] == "replies"
        assert result["capability_level"] == "unsupported"
        assert result["delivery_strategy"] == "skip"

    def test_relation_fallback_round_trip(self) -> None:
        """Relation fallback reason → _derive_capability_evidence."""
        from medre.core.events.canonical import EventRelation, NativeRef
        from medre.core.planning.capability_decision import resolver

        caps = AdapterCapabilities(replies="fallback")
        rel = EventRelation(
            relation_type="reply",
            target_event_id="evt-parent",
            target_native_ref=NativeRef(
                adapter="test_adapter",
                native_channel_id="ch-0",
                native_message_id="native-001",
            ),
            key=None,
            fallback_text="original",
        )
        event = make_event(
            event_kind="plugin.custom",
            relations=(rel,),
        )
        decision = resolver.decide(event, caps)

        error = self._error_from_decision(decision)
        result = _derive_capability_evidence(
            error=error,
            rendering_evidence=None,
            failure_kind="capability_suppressed",
            status="suppressed",
        )
        assert result["capability_field"] == "replies"
        assert result["capability_level"] == "fallback"
        assert result["delivery_strategy"] == "fallback_text"

    def test_boolean_field_unsupported_round_trip(self) -> None:
        """Boolean capability field (text=False) unsupported → round trip."""
        from medre.core.planning.capability_decision import resolver

        caps = AdapterCapabilities(text=False)
        event = make_event(event_kind="message.text")
        decision = resolver.decide(event, caps)

        error = self._error_from_decision(decision)
        result = _derive_capability_evidence(
            error=error,
            rendering_evidence=None,
            failure_kind="capability_suppressed",
            status="suppressed",
        )
        assert result["capability_field"] == "text"
        assert result["capability_level"] == "unsupported"
        assert result["delivery_strategy"] == "skip"

    def test_edits_event_kind_round_trip(self) -> None:
        """Edits field (string 3-level) event-kind round trip."""
        from medre.core.planning.capability_decision import resolver

        caps = AdapterCapabilities(edits="fallback")
        event = make_event(event_kind="message.edited")
        decision = resolver.decide(event, caps)

        error = self._error_from_decision(decision)
        result = _derive_capability_evidence(
            error=error,
            rendering_evidence=None,
            failure_kind="capability_suppressed",
            status="suppressed",
        )
        assert result["capability_field"] == "edits"
        assert result["capability_level"] == "fallback"
        assert result["delivery_strategy"] == "fallback_text"

    def test_full_receipt_round_trip(self) -> None:
        """Full delivery_receipt_to_report_dict round trip via resolver."""
        from medre.core.planning.capability_decision import resolver

        caps = AdapterCapabilities(reactions="unsupported")
        event = make_event(event_kind="message.reacted")
        decision = resolver.decide(event, caps)

        error = self._error_from_decision(decision)
        receipt = DeliveryReceipt(
            receipt_id="rcpt-roundtrip-001",
            event_id=event.event_id,
            delivery_plan_id="dp-roundtrip-001",
            target_adapter="radio",
            target_channel="ch-0",
            route_id="route-roundtrip-1",
            status="suppressed",
            error=error,
            failure_kind="capability_suppressed",
            attempt_number=1,
            source="live",
            created_at=_ts(second=1),
        )
        report = delivery_receipt_to_report_dict(receipt)
        assert report["capability_field"] == decision.capability_field
        assert report["capability_level"] == "unsupported"
        assert report["delivery_strategy"] == "skip"
        assert report["suppression_reason"] == decision.reason
