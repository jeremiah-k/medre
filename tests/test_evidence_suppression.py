"""Suppression-evidence integration tests for delivery evidence.

Moved from test_delivery_evidence_unification.py to keep the main file under
the 1500-line limit.  These tests exercise the real PipelineRunner with fake
adapters and collect_evidence_bundle to produce operator-facing evidence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from medre.core.planning.delivery_plan import DeliveryFailureKind
from medre.runtime.evidence._bundle import collect_evidence_bundle

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
    ``delivery_state_by_adapter``.

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
        from medre.adapters.fake_matrix import FakeMatrixAdapter
        from medre.core.engine.pipeline import PipelineRunner
        from medre.core.events.kinds import EventKind
        from medre.core.routing import Route, Router, RouteSource, RouteTarget
        from medre.core.routing.stats import RouteStats
        from medre.core.runtime.accounting import RuntimeAccounting
        from medre.core.storage.sqlite import SQLiteStorage
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

        # -- Phase 5: delivery_state_by_adapter assertions -------------------
        dsba = summary["delivery_state_by_adapter"]
        assert adapter_id in dsba, (
            f"Expected adapter {adapter_id!r} in delivery_state_by_adapter, "
            f"got {sorted(dsba.keys())}"
        )

        adapter_state = dsba[adapter_id]
        assert adapter_state["status"] == "suppressed"
        assert adapter_state["failure_kind"] == "loop_suppressed"
        assert adapter_state["retryable"] is False
        assert (
            "target_channel" in adapter_state
        ), "delivery_state_by_adapter entry must include 'target_channel' key"
        assert adapter_state["target_channel"] == target_channel, (
            f"Expected target_channel {target_channel!r}, "
            f"got {adapter_state['target_channel']!r}"
        )

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
