"""ReplayEngine diagnostician wiring and schema-version compatibility.

Tests that the Diagnostician captures replay failures, render errors,
missing events, unregistered kinds, and no-route-matched conditions.
Also covers schema-version acceptance in STRICT mode.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

from medre.core.engine.replay import ReplayMode, ReplayRequest
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.routing import Router
from medre.core.storage.sqlite.storage import SQLiteStorage
from tests.helpers.replay import StubPipeline, make_engine

# ===================================================================
# Diagnostician wiring
# ===================================================================


class TestDiagnostician:
    """Verify Diagnostician captures replay issues."""

    async def test_records_missing_event(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Replaying a missing event records a replay_skip diagnostic."""
        from medre.core.observability.metrics import Diagnostician

        diag = Diagnostician()
        engine = make_engine(temp_storage)
        engine._diagnostician = diag

        request = ReplayRequest(
            correlation_ids=["nonexistent-001"],
            mode=ReplayMode.STRICT,
        )

        results = [r async for r in engine.replay(request)]
        assert len(results) == 1
        assert results[0].status == "failed"

        snap = diag.snapshot()
        assert "Event not found in storage" in snap["replay_skips"]

    async def test_records_unregistered_kind(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        """Unregistered event_kind records a replay_downgrade diagnostic."""
        from medre.core.observability.metrics import Diagnostician

        event = CanonicalEvent(
            event_id="bad-kind-002",
            event_kind="unknown.event_type",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="test",
            source_transport_id="node-1",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "test"},
            metadata=EventMetadata(),
        )
        await temp_storage.append(event)

        diag = Diagnostician()
        engine = make_engine(temp_storage)
        engine._diagnostician = diag

        request = ReplayRequest(
            event_kinds=["unknown.event_type"],
            mode=ReplayMode.STRICT,
        )

        results = [r async for r in engine.replay(request)]
        assert len(results) == 1
        assert results[0].status == "failed"

        snap = diag.snapshot()
        assert len(snap["replay_downgrades"]) > 0

    async def test_records_no_routes_matched(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """No matching routes records a replay_skip diagnostic."""
        from medre.core.observability.metrics import Diagnostician

        await temp_storage.append(sample_event)

        # Empty router -- no routes will match
        empty_router = Router(routes=[])
        pipeline = StubPipeline(router=empty_router)

        diag = Diagnostician()
        engine = make_engine(temp_storage, pipeline=pipeline)
        engine._diagnostician = diag

        request = ReplayRequest(mode=ReplayMode.RE_ROUTE)

        results = [r async for r in engine.replay(request)]
        # store + route (failed)
        assert len(results) == 3
        assert results[1].status == "failed"

        snap = diag.snapshot()
        assert "No routes matched" in snap["replay_skips"]

    async def test_render_failure_records_diagnostics(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """Render failure emits diagnostic via Diagnostician."""
        from medre.core.observability.metrics import Diagnostician

        await temp_storage.append(sample_event)

        pipeline = AsyncMock()
        pipeline.transform_event = AsyncMock(return_value=sample_event)
        pipeline.render_event = AsyncMock(
            side_effect=RuntimeError("No renderer for adapter"),
        )

        diag = Diagnostician()
        engine = make_engine(temp_storage, pipeline=pipeline)
        engine._diagnostician = diag

        request = ReplayRequest(
            event_kinds=["message.created"],
            mode=ReplayMode.RE_RENDER,
        )

        results = [r async for r in engine.replay(request)]
        assert len(results) == 2
        assert results[1].stage == "render"
        assert results[1].status == "error"
        assert "No renderer" in (results[1].error or "")

        # Diagnostician captured the renderer failure
        snap = diag.snapshot()
        assert len(snap["renderer_failures"]) > 0


# ===================================================================
# Schema-version compatibility
# ===================================================================


class TestSchemaVersionCompatibility:
    """Verify schema version handling during replay."""

    async def test_current_schema_version_passes(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """Events with current schema_version pass STRICT replay."""
        from medre.core.events.schema import CURRENT_SCHEMA_VERSION

        event = CanonicalEvent(
            event_id="schema-v1",
            event_kind="message.created",
            schema_version=CURRENT_SCHEMA_VERSION,
            timestamp=sample_event.timestamp,
            source_adapter="fake_transport",
            source_transport_id="node-123",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "schema test"},
            metadata=EventMetadata(),
        )
        await temp_storage.append(event)

        engine = make_engine(temp_storage)
        request = ReplayRequest(
            event_kinds=["message.created"],
            mode=ReplayMode.STRICT,
        )

        results = [r async for r in engine.replay(request)]
        assert len(results) == 1
        assert results[0].status == "passed"

    async def test_future_schema_version_accepted(
        self,
        temp_storage: SQLiteStorage,
        sample_event: CanonicalEvent,
    ) -> None:
        """Events with schema_version > CURRENT pass STRICT replay.

        The schema system accepts future versions at storage time.
        Replay should not reject them either.
        """
        from medre.core.events.schema import CURRENT_SCHEMA_VERSION

        event = CanonicalEvent(
            event_id="schema-future",
            event_kind="message.created",
            schema_version=CURRENT_SCHEMA_VERSION + 1,
            timestamp=sample_event.timestamp,
            source_adapter="fake_transport",
            source_transport_id="node-123",
            source_channel_id="ch-0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "future schema"},
            metadata=EventMetadata(),
        )
        await temp_storage.append(event)

        engine = make_engine(temp_storage)
        request = ReplayRequest(
            event_kinds=["message.created"],
            mode=ReplayMode.STRICT,
        )

        results = [r async for r in engine.replay(request)]
        assert len(results) == 1
        assert results[0].status == "passed"
