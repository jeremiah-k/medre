"""Native-ref dedup suppression contract tests.

Proves that the MEDRE pipeline's deduplication (source_native_ref) is
deterministic across every meaningful combination of adapter, channel,
and message ID inputs.  Also verifies persistence of dedup behavior
across runner restarts using the same SQLite storage.

Contract rules
--------------
1. Same (adapter, native_channel_id, native_message_id) → suppressed.
2. Different adapters → different dedup keys → both accepted.
3. Different channels → different dedup keys → both accepted.
4. Falsy native_message_id ("") → bypasses dedup.
5. All behaviors persist after runner restart with same storage.

No Docker, no live transports, no SDK dependencies required.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import cast

from medre.adapters.fakes.presentation import FakePresentationAdapter
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.events import CanonicalEvent, EventMetadata, NativeRef
from medre.core.events.bus import EventBus
from medre.core.planning import FallbackResolver, RelationResolver
from medre.core.rendering.renderer import RenderingPipeline
from medre.core.rendering.text import TextRenderer
from medre.core.routing import Route, Router, RouteSource, RouteTarget
from medre.core.storage import SQLiteStorage
from medre.core.storage.backend import StorageBackend
from medre.core.supervision.accounting import RuntimeAccounting

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event_with_ref(
    event_id: str,
    native_ref: NativeRef,
    source_adapter: str = "src",
    payload: dict | None = None,
) -> CanonicalEvent:
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter=source_adapter,
        source_transport_id="node-1",
        source_channel_id="ch-0",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload=payload or {"text": "hello"},
        metadata=EventMetadata(),
        source_native_ref=native_ref,
    )


def _build_runner(
    storage: SQLiteStorage,
    accounting: RuntimeAccounting,
    source_adapter: str = "src",
    target_adapter: str = "target",
) -> PipelineRunner:
    target = FakePresentationAdapter(adapter_id=target_adapter)
    route = Route(
        id="dedup-route",
        source=RouteSource(
            adapter=source_adapter, event_kinds=("message.created",), channel=None
        ),
        targets=[RouteTarget(adapter=target_adapter)],
    )
    rp = RenderingPipeline()
    rp.register(TextRenderer(), priority=100)
    config = PipelineConfig(
        storage=cast(StorageBackend, storage),
        router=Router(routes=[route]),
        fallback_resolver=FallbackResolver(),
        relation_resolver=RelationResolver(storage=storage),
        adapters={target_adapter: target},
        event_bus=EventBus(),
        rendering_pipeline=rp,
        runtime_accounting=accounting,
    )
    return PipelineRunner(config)


async def _restart_runner(
    storage: SQLiteStorage,
    accounting: RuntimeAccounting,
    source_adapter: str = "src",
    target_adapter: str = "target",
) -> PipelineRunner:
    runner = _build_runner(
        storage,
        accounting,
        source_adapter=source_adapter,
        target_adapter=target_adapter,
    )
    await runner.start()
    return runner


async def _count_events(storage: SQLiteStorage) -> int:
    rows = await storage._read_all("SELECT event_id FROM canonical_events")
    return len(rows)


# ===================================================================
# TEST 1: null channel suppressed
# ===================================================================


class TestNullChannelSuppressed:
    async def test_null_channel_suppressed(self, temp_storage: SQLiteStorage) -> None:
        accounting = RuntimeAccounting()
        runner = await _restart_runner(temp_storage, accounting)

        ref = NativeRef(
            adapter="src", native_channel_id=None, native_message_id="null-ch-001"
        )
        try:
            out1 = await runner.handle_ingress(
                _make_event_with_ref(f"nc-{uuid.uuid4()}", ref)
            )
            assert len(out1) == 1

            out2 = await runner.handle_ingress(
                _make_event_with_ref(f"nc-dup-{uuid.uuid4()}", ref)
            )
            assert out2 == []

            assert await _count_events(temp_storage) == 1
            snap = accounting.snapshot()
            assert snap["inbound_accepted"] == 1
            assert snap["loop_prevented"] == 1
        finally:
            await runner.stop()


# ===================================================================
# TEST 2: explicit channel suppressed
# ===================================================================


class TestExplicitChannelSuppressed:
    async def test_explicit_channel_suppressed(
        self, temp_storage: SQLiteStorage
    ) -> None:
        accounting = RuntimeAccounting()
        runner = await _restart_runner(temp_storage, accounting)

        ref = NativeRef(
            adapter="src", native_channel_id="ch-0", native_message_id="explicit-001"
        )
        try:
            out1 = await runner.handle_ingress(
                _make_event_with_ref(f"ec-{uuid.uuid4()}", ref)
            )
            assert len(out1) == 1

            out2 = await runner.handle_ingress(
                _make_event_with_ref(f"ec-dup-{uuid.uuid4()}", ref)
            )
            assert out2 == []

            assert await _count_events(temp_storage) == 1
            snap = accounting.snapshot()
            assert snap["inbound_accepted"] == 1
            assert snap["loop_prevented"] == 1
        finally:
            await runner.stop()


# ===================================================================
# TEST 3: different adapters, same message_id → both accepted
# ===================================================================


class TestDifferentAdaptersSameMessageId:
    async def test_different_adapters_same_message_id(
        self, temp_storage: SQLiteStorage
    ) -> None:
        accounting = RuntimeAccounting()
        runner = await _restart_runner(temp_storage, accounting)

        ref_a = NativeRef(
            adapter="adapter-A", native_channel_id="ch-0", native_message_id="shared-id"
        )
        ref_b = NativeRef(
            adapter="adapter-B", native_channel_id="ch-0", native_message_id="shared-id"
        )
        try:
            out_a = await runner.handle_ingress(
                _make_event_with_ref(
                    f"da-{uuid.uuid4()}", ref_a, source_adapter="adapter-A"
                )
            )
            assert out_a == []  # no route matches adapter-A/adapter-B source

            out_b = await runner.handle_ingress(
                _make_event_with_ref(
                    f"db-{uuid.uuid4()}", ref_b, source_adapter="adapter-B"
                )
            )
            assert out_b == []

            assert await _count_events(temp_storage) == 2
            snap = accounting.snapshot()
            assert snap["inbound_accepted"] == 2
            assert snap["loop_prevented"] == 0
        finally:
            await runner.stop()


# ===================================================================
# TEST 4: same adapter, different channels → both accepted
# ===================================================================


class TestSameAdapterDifferentChannel:
    async def test_same_adapter_different_channel(
        self, temp_storage: SQLiteStorage
    ) -> None:
        accounting = RuntimeAccounting()
        runner = await _restart_runner(temp_storage, accounting)

        ref_0 = NativeRef(
            adapter="src", native_channel_id="ch-0", native_message_id="shared-mid"
        )
        ref_1 = NativeRef(
            adapter="src", native_channel_id="ch-1", native_message_id="shared-mid"
        )
        try:
            out_0 = await runner.handle_ingress(
                _make_event_with_ref(f"sch-{uuid.uuid4()}", ref_0)
            )
            assert len(out_0) == 1

            out_1 = await runner.handle_ingress(
                _make_event_with_ref(f"sch2-{uuid.uuid4()}", ref_1)
            )
            assert (
                len(out_1) == 1
            ), "Different channel should produce different dedup key"

            assert await _count_events(temp_storage) == 2
            snap = accounting.snapshot()
            assert snap["inbound_accepted"] == 2
            assert snap["loop_prevented"] == 0
        finally:
            await runner.stop()


# ===================================================================
# TEST 5: empty string message_id bypasses suppression
# ===================================================================


class TestEmptyStringIdBypassesSuppression:
    async def test_empty_string_id_bypasses_suppression(
        self, temp_storage: SQLiteStorage
    ) -> None:
        accounting = RuntimeAccounting()
        runner = await _restart_runner(temp_storage, accounting)

        ref_empty = NativeRef(
            adapter="src", native_channel_id="ch-0", native_message_id=""
        )
        try:
            for i in range(2):
                out = await runner.handle_ingress(
                    _make_event_with_ref(f"empty-{i}-{uuid.uuid4()}", ref_empty)
                )
                assert (
                    len(out) == 1
                ), f"Event {i} with empty message_id should be accepted"

            assert await _count_events(temp_storage) == 2
            snap = accounting.snapshot()
            assert snap["inbound_accepted"] == 2
            assert snap["loop_prevented"] == 0
        finally:
            await runner.stop()


# ===================================================================
# TEST 6: Restart persistence — each scenario survives runner restart
# ===================================================================


class TestRestartPersistence:
    """For each suppression scenario, prove behavior persists after:
    Stop runner, create new runner with same SQLite storage, inject
    same native ref again, same suppression behavior."""

    async def test_null_channel_persists_after_restart(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        ref = NativeRef(
            adapter="src", native_channel_id=None, native_message_id="restart-null"
        )
        accounting = RuntimeAccounting()
        runner = await _restart_runner(temp_storage, accounting)
        try:
            await runner.handle_ingress(_make_event_with_ref(f"rn-{uuid.uuid4()}", ref))
        finally:
            await runner.stop()

        runner2 = await _restart_runner(temp_storage, accounting)
        try:
            out = await runner2.handle_ingress(
                _make_event_with_ref(f"rn-dup-{uuid.uuid4()}", ref)
            )
            assert out == [], "Suppression should persist after restart"
            assert await _count_events(temp_storage) == 1
            snap = accounting.snapshot()
            assert snap["inbound_accepted"] == 1
            assert snap["loop_prevented"] == 1
        finally:
            await runner2.stop()

    async def test_explicit_channel_persists_after_restart(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        ref = NativeRef(
            adapter="src",
            native_channel_id="ch-0",
            native_message_id="restart-explicit",
        )
        accounting = RuntimeAccounting()
        runner = await _restart_runner(temp_storage, accounting)
        try:
            await runner.handle_ingress(_make_event_with_ref(f"re-{uuid.uuid4()}", ref))
        finally:
            await runner.stop()

        runner2 = await _restart_runner(temp_storage, accounting)
        try:
            out = await runner2.handle_ingress(
                _make_event_with_ref(f"re-dup-{uuid.uuid4()}", ref)
            )
            assert out == []
            snap = accounting.snapshot()
            assert snap["inbound_accepted"] == 1
            assert snap["loop_prevented"] == 1
        finally:
            await runner2.stop()

    async def test_different_adapters_still_accepted_after_restart(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        accounting = RuntimeAccounting()
        runner = await _restart_runner(temp_storage, accounting)
        ref_a = NativeRef(
            adapter="adapter-A",
            native_channel_id="ch-0",
            native_message_id="restart-shared",
        )
        try:
            await runner.handle_ingress(
                _make_event_with_ref(
                    f"rda-{uuid.uuid4()}", ref_a, source_adapter="adapter-A"
                )
            )
        finally:
            await runner.stop()

        runner2 = await _restart_runner(temp_storage, accounting)
        ref_b = NativeRef(
            adapter="adapter-B",
            native_channel_id="ch-0",
            native_message_id="restart-shared",
        )
        try:
            out = await runner2.handle_ingress(
                _make_event_with_ref(
                    f"rdb-{uuid.uuid4()}", ref_b, source_adapter="adapter-B"
                )
            )
            assert (
                out == []
            ), "No route matches adapter-B source; event accepted via storage"
            assert await _count_events(temp_storage) == 2
            snap = accounting.snapshot()
            assert snap["inbound_accepted"] == 2
            assert snap["loop_prevented"] == 0
        finally:
            await runner2.stop()

    async def test_different_channels_still_accepted_after_restart(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        accounting = RuntimeAccounting()
        runner = await _restart_runner(temp_storage, accounting)
        ref_0 = NativeRef(
            adapter="src", native_channel_id="ch-0", native_message_id="restart-ch-mid"
        )
        try:
            await runner.handle_ingress(
                _make_event_with_ref(f"rch-{uuid.uuid4()}", ref_0)
            )
        finally:
            await runner.stop()

        runner2 = await _restart_runner(temp_storage, accounting)
        ref_1 = NativeRef(
            adapter="src", native_channel_id="ch-1", native_message_id="restart-ch-mid"
        )
        try:
            out = await runner2.handle_ingress(
                _make_event_with_ref(f"rch2-{uuid.uuid4()}", ref_1)
            )
            assert len(out) == 1, "Different channel should be accepted after restart"
            assert await _count_events(temp_storage) == 2
            snap = accounting.snapshot()
            assert snap["inbound_accepted"] == 2
            assert snap["loop_prevented"] == 0
        finally:
            await runner2.stop()

    async def test_empty_string_still_bypasses_after_restart(
        self,
        temp_storage: SQLiteStorage,
    ) -> None:
        ref_empty = NativeRef(
            adapter="src", native_channel_id="ch-0", native_message_id=""
        )
        accounting = RuntimeAccounting()
        runner = await _restart_runner(temp_storage, accounting)
        try:
            await runner.handle_ingress(
                _make_event_with_ref(f"rem-{uuid.uuid4()}", ref_empty)
            )
        finally:
            await runner.stop()

        runner2 = await _restart_runner(temp_storage, accounting)
        try:
            out = await runner2.handle_ingress(
                _make_event_with_ref(f"rem2-{uuid.uuid4()}", ref_empty)
            )
            assert len(out) == 1, "Empty message_id should bypass dedup after restart"
            assert await _count_events(temp_storage) == 2
            snap = accounting.snapshot()
            assert snap["inbound_accepted"] == 2
            assert snap["loop_prevented"] == 0
        finally:
            await runner2.stop()
