"""Startup scope integration: readiness enforcement vs stored-work routing.

Covers the three startup integration boundaries that the blanket
startup-SKIPPED-route removal (and retry-worker activation order) must
respect:

1. **Replay is not gated by live source connectivity.**  A best_effort
   replay of a stored canonical event routes by the event's stored
   source adapter; the old INPUT radio being offline (source adapter
   start failure) must not turn the requested execution into a
   "No routes matched" outcome.  Readiness still *reports* the skip.
2. **Due retry work cannot execute against adapters still STARTING.**
   The retry worker's first claim cycle must not run before adapters
   have reached their terminal startup state.
3. **Already-admitted durable ingress is not fresh source availability.**
   On a normal LIVE restart, pending admitted rows from a source whose
   adapter now fails to start must still route to surviving targets;
   blanket source-based pruning would silently acknowledge stored input
   as no-route.

All transport interaction uses the existing fake adapter boundary and the
real CLI / runtime / storage seams; no live radios are involved.
"""

from __future__ import annotations

import asyncio
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from medre.adapters.fakes.matrix import FakeMatrixAdapter
from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter
from medre.cli import main
from medre.config.model import (
    AdapterConfigSet,
    LoggingConfig,
    MatrixRuntimeConfig,
    MeshtasticRuntimeConfig,
    RetryConfig,
    RuntimeConfig,
    RuntimeOptions,
    StorageConfig,
)
from medre.config.paths import MedrePaths, resolve
from medre.config.routes import RouteConfig, RouteConfigSet
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.runtime.app import RuntimeState, StartupScope
from medre.runtime.errors import RuntimeStartupError
from medre.runtime.builder import RuntimeBuilder
from medre.runtime.route_engine import RouteOperationalState
from tests.helpers.fake_runtime import wait_until
from tests.helpers.storage_outbox import make_outbox_item
from tests.helpers.walkthrough import seed_via_smoke_cli, write_replay_config

# ---------------------------------------------------------------------------
# Fixtures and config builders
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_path_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "MEDRE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def tmp_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MedrePaths:
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path / "medre_home"))
    return resolve()


def _mx_to_mesh_config(
    db_path: Path,
    *,
    retry: RetryConfig | None = None,
) -> RuntimeConfig:
    """Matrix source (mx_src) → Meshtastic target (mesh_tgt), SQLite."""
    return RuntimeConfig(
        runtime=RuntimeOptions(name="startup-scope-integration"),
        logging=LoggingConfig(level="WARNING"),
        storage=StorageConfig(backend="sqlite", path=str(db_path)),
        retry=retry or RetryConfig(enabled=False),
        adapters=AdapterConfigSet(
            matrix={
                "mx_src": MatrixRuntimeConfig(
                    adapter_id="mx_src",
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
            meshtastic={
                "mesh_tgt": MeshtasticRuntimeConfig(
                    adapter_id="mesh_tgt",
                    enabled=True,
                    adapter_kind="fake",
                ),
            },
        ),
        routes=RouteConfigSet(
            routes=(
                RouteConfig(
                    route_id="src-to-tgt",
                    source_adapters=("mx_src",),
                    dest_adapters=("mesh_tgt",),
                    enabled=True,
                ),
            )
        ),
    )


def _mx_src_event(
    event_id: str, text: str = "stored historical message"
) -> CanonicalEvent:
    """A routable canonical event from the mx_src source adapter."""
    return CanonicalEvent(
        event_id=event_id,
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(UTC),
        source_adapter="mx_src",
        source_transport_id=f"t-{event_id}",
        source_channel_id="!room:fake.local",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"text": text},
        metadata=EventMetadata(),
    )


def _routable_retry_metadata() -> dict[str, Any]:
    """Route-decision metadata a real outbox row carries (retry contract)."""
    return {
        "delivery_strategy": "direct",
        "capability_level": None,
        "capability_field": None,
        "capability_reason": None,
        "deadline": None,
    }


def _patch_start_failure(adapter: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make this adapter instance fail start like a genuinely dead radio."""

    async def _fail_start(ctx: Any) -> None:
        raise RuntimeError("simulated adapter start failure: radio offline")

    monkeypatch.setattr(adapter, "start", _fail_start)


# ---------------------------------------------------------------------------
# Case 1: replay is not gated by live source connectivity
# ---------------------------------------------------------------------------


class TestReplayNotGatedByLiveSource:
    """Historical replay routes by stored evidence, not live source state."""

    def test_best_effort_replay_delivers_with_source_adapter_dead(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``medre replay --mode best_effort --event`` reaches the healthy
        target even though the source adapter fails to start.

        Pre-condition failure must not be misreported as "No routes
        matched": the route is configured and the target is up; only the
        old input radio is offline.  dry_run stays side-effect free, the
        selected event carries replay receipts, and unrelated pending
        durable ingress stays untouched (live authority).
        """
        selected_id, db_path = seed_via_smoke_cli(tmp_path)
        config_path = write_replay_config(tmp_path, db_path)

        # A crash-style bystander: pending durable ingress from the same
        # source, belonging to the live authority, never selected.
        bystander_id = "evt-bystander-pending"

        async def _seed_bystander() -> None:
            storage = SQLiteStorage(db_path=str(db_path))
            try:
                await storage.initialize()
                event = CanonicalEvent(
                    event_id=bystander_id,
                    event_kind="message.created",
                    schema_version=1,
                    timestamp=datetime.now(UTC),
                    source_adapter="fake_matrix",
                    source_transport_id="t-bystander",
                    source_channel_id="!room:fake.local",
                    parent_event_id=None,
                    lineage=(),
                    relations=(),
                    payload={"text": "bystander pending ingress"},
                    metadata=EventMetadata(),
                )
                result = await storage.admit_ingress(event, None, "live")
                assert result.created and result.work_status == "pending"
            finally:
                await storage.close()

        asyncio.run(_seed_bystander())

        # The source radio is now offline: every runtime this CLI builds
        # fails the fake_matrix start.  Seed already happened (healthy).
        async def _fail_matrix_start(self: Any, ctx: Any) -> None:
            raise RuntimeError("simulated matrix start failure: session refused")

        monkeypatch.setattr(FakeMatrixAdapter, "start", _fail_matrix_start)

        async def _receipt_count() -> int:
            storage = SQLiteStorage(db_path=str(db_path))
            try:
                await storage.initialize()
                return len(await storage.list_receipts_for_event(selected_id))
            finally:
                await storage.close()

        receipts_before = asyncio.run(_receipt_count())

        # dry_run must stay read-only even with the source offline.
        dry_buf = io.StringIO()
        with redirect_stdout(dry_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "replay",
                    "--config",
                    config_path,
                    "--mode",
                    "dry_run",
                    "--event",
                    selected_id,
                    "--json",
                ]
            )
        assert asyncio.run(_receipt_count()) == receipts_before

        # The real side-effect replay must deliver to the healthy target.
        out_buf = io.StringIO()
        with redirect_stdout(out_buf), redirect_stderr(io.StringIO()):
            main(
                [
                    "replay",
                    "--config",
                    config_path,
                    "--mode",
                    "best_effort",
                    "--event",
                    selected_id,
                    "--json",
                ]
            )

        summary = json.loads(out_buf.getvalue())
        assert (
            summary["by_status"].get("failed", 0) == 0
        ), f"replay failed with the source offline: {summary}"
        assert summary["skip_reasons"].get("No routes matched", 0) == 0, (
            "replay misreported a configured route as no-route because its "
            f"source adapter failed to start: {summary['skip_reasons']}"
        )
        assert summary["by_status"].get("passed", 0) >= 1

        async def _check_storage() -> tuple[list[Any], Any, int]:
            storage = SQLiteStorage(db_path=str(db_path))
            try:
                await storage.initialize()
                receipts = await storage.list_receipts_for_event(selected_id)
                replay_receipts = [r for r in receipts if r.source == "replay"]
                bystander_receipts = await storage.list_receipts_for_event(bystander_id)
                counts = await storage.count_ingress_work_by_status()
                return replay_receipts, bystander_receipts, counts.get("pending", 0)
            finally:
                await storage.close()

        replay_receipts, bystander_receipts, pending_rows = asyncio.run(
            _check_storage()
        )
        assert any(
            r.target_adapter == "fake_meshtastic" and r.status == "sent"
            for r in replay_receipts
        ), f"replay never reached the healthy target: {replay_receipts}"
        assert len(bystander_receipts) == 0, "replay dispatched unrelated work"
        assert pending_rows >= 1, "unrelated pending durable ingress vanished"

    def test_replay_scope_keeps_source_skipped_route_registered(
        self,
        tmp_paths: MedrePaths,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """REPLAY start keeps a source-failed route routable and truthful.

        The readiness assessment still reports SKIPPED for the route (the
        source adapter genuinely failed), but the REPLAY scope must not
        remove the route from the router: replay selection routes stored
        events, and pruning it would turn requested executions into
        no-route outcomes.
        """
        from medre.core.engine.pipeline.runner import PipelineRunner

        db = tmp_path / "replay_scope.db"
        config = _mx_to_mesh_config(db)
        app = RuntimeBuilder(config, tmp_paths).build()
        source = app.adapters["mx_src"]
        assert isinstance(source, FakeMatrixAdapter)
        _patch_start_failure(source, monkeypatch)

        async def _scenario() -> None:
            await app.start(scope=StartupScope.REPLAY)
            try:
                assert app.state is RuntimeState.RUNNING
                assert app.boot_summary is not None
                assert app.boot_summary.runtime_health == "degraded"
                assert "mx_src" in app.boot_summary.failed_adapter_ids

                # Truthful reporting: the route is assessed SKIPPED...
                readiness = app.startup_readiness
                assert readiness is not None
                assert (
                    readiness.route_states["src-to-tgt"]
                    is RouteOperationalState.SKIPPED
                )

                # ...but the REPLAY scope must not prune it from the
                # router: stored events from this source still route.
                event = _mx_src_event("evt-replay-scope-probe")
                matched = app.router.match(event)
                assert [r.id for r in matched] == ["src-to-tgt"], (
                    "REPLAY scope pruned a source-failed route; historical "
                    "replay into the healthy target is now impossible"
                )

                # Scope boundaries intact: no live workers in REPLAY.
                assert app._retry_worker is None
                assert app._ingress_worker is None
                assert isinstance(app.pipeline_runner, PipelineRunner)
            finally:
                await app.stop()
                assert app.state is RuntimeState.STOPPED

        asyncio.run(_scenario())


# ---------------------------------------------------------------------------
# Case 2: due retry work cannot execute against adapters still STARTING
# ---------------------------------------------------------------------------


class TestRetryActivationAfterAdapterBoundary:
    """The retry worker's first claim cycle runs after adapters settle."""

    def test_due_retry_work_held_until_target_adapter_ready(
        self,
        tmp_paths: MedrePaths,
        tmp_path: Path,
    ) -> None:
        """A due pending row stays untouched while the target start is held.

        Consumer-visible contract: during a deterministically held adapter
        start the due work is not claimed, no attempt is consumed, no
        receipt appears, and nothing is delivered; once the start
        completes, the existing retry authority delivers exactly once.
        """
        from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter

        db = tmp_path / "retry_hold.db"
        config = _mx_to_mesh_config(
            db,
            retry=RetryConfig(enabled=True, interval_seconds=0.05),
        )
        event_id = "evt-due-during-startup"

        async def _seed() -> str:
            storage = SQLiteStorage(db_path=str(db))
            try:
                await storage.initialize()
                await storage.append(_mx_src_event(event_id))
                item = make_outbox_item(
                    delivery_plan_id="plan-due-hold",
                    target_adapter="mesh_tgt",
                    target_channel=None,
                    status="pending",
                    event_id=event_id,
                )
                item.metadata = _routable_retry_metadata()
                created = await storage.create_outbox_item(item)
                return created.outbox_id
            finally:
                await storage.close()

        outbox_id = asyncio.run(_seed())

        app = RuntimeBuilder(config, tmp_paths).build()
        target = app.adapters["mesh_tgt"]
        assert isinstance(target, FakeMeshtasticAdapter)

        original_start = target.start
        start_entered = asyncio.Event()
        release_start = asyncio.Event()

        async def _held_start(ctx: Any) -> None:
            start_entered.set()
            await release_start.wait()
            await original_start(ctx)

        target.start = _held_start  # type: ignore[assignment]

        async def _scenario() -> None:
            start_task = asyncio.create_task(app.start())
            try:
                await wait_until(start_entered.is_set, timeout=5.0)

                # Deterministic boundary: while target.start() is blocked,
                # the retry worker must not yet be running.  This directly
                # proves startup ordering without a timing window.
                assert app.retry_state.running is False

                async def _row() -> Any:
                    storage = SQLiteStorage(db_path=str(db))
                    try:
                        await storage.initialize()
                        return await storage.get_outbox_item(outbox_id)
                    finally:
                        await storage.close()

                row = await _row()
                assert row is not None
                assert row.status == "pending", (
                    f"due work was touched while its adapter was still "
                    f"starting: status={row.status!r} "
                    f"attempt={row.attempt_number}"
                )
                assert row.attempt_number == 1, "attempt consumed before adapter ready"
                assert (
                    target.delivered_payloads == []
                ), "delivered into an adapter whose start never completed"

                async def _receipts() -> int:
                    storage = SQLiteStorage(db_path=str(db))
                    try:
                        await storage.initialize()
                        return len(await storage.list_receipts_for_event(event_id))
                    finally:
                        await storage.close()

                assert (
                    await _receipts() == 0
                ), "receipt written while the target adapter was still starting"

                # Release the start; the runtime finishes starting and the
                # due work proceeds exactly once through the retry authority.
                release_start.set()
                await wait_until(start_task.done, timeout=10.0)
                start_task.result()  # startup must not have failed
                assert app.state is RuntimeState.RUNNING

                await wait_until(
                    lambda: len(target.delivered_payloads) == 1, timeout=5.0
                )

                final_row = await _row()
                assert final_row is not None
                assert (
                    final_row.status == "sent"
                ), f"released due work did not complete: {final_row.status!r}"
                assert (
                    len(target.delivered_payloads) == 1
                ), "due work delivered more than once across the boundary"
            finally:
                release_start.set()
                if not start_task.done():
                    start_task.cancel()
                    try:
                        await start_task
                    except BaseException:
                        pass
                if app.state not in (
                    RuntimeState.STOPPED,
                    RuntimeState.FAILED,
                    RuntimeState.INITIALIZED,
                ):
                    try:
                        await app.stop()
                    except Exception:
                        pass

        try:
            asyncio.run(_scenario())
        finally:
            target.start = original_start  # type: ignore[assignment]


def test_due_retry_for_startup_failed_target_is_deferred_without_attempt(
    tmp_paths: MedrePaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Partial startup preserves retry work for an unavailable target."""
    db = tmp_path / "retry_failed_target.db"
    config = _mx_to_mesh_config(
        db,
        retry=RetryConfig(enabled=True, interval_seconds=300.0),
    )
    event_id = "evt-retry-target-start-failed"

    async def _seed() -> str:
        storage = SQLiteStorage(db_path=str(db))
        try:
            await storage.initialize()
            await storage.append(_mx_src_event(event_id))
            item = make_outbox_item(
                delivery_plan_id="plan-target-start-failed",
                target_adapter="mesh_tgt",
                target_channel=None,
                status="pending",
                event_id=event_id,
            )
            item.metadata = _routable_retry_metadata()
            created = await storage.create_outbox_item(item)
            return created.outbox_id
        finally:
            await storage.close()

    outbox_id = asyncio.run(_seed())
    app = RuntimeBuilder(config, tmp_paths).build()
    target = app.adapters["mesh_tgt"]
    assert isinstance(target, FakeMeshtasticAdapter)
    _patch_start_failure(target, monkeypatch)

    async def _scenario() -> None:
        await app.start()
        try:
            assert app.state is RuntimeState.RUNNING
            assert app.boot_summary is not None
            assert "mesh_tgt" in app.boot_summary.failed_adapter_ids
            assert app._retry_worker is not None

            # Stop the background loop from interfering: the 300 s retry
            # interval guarantees no background claim cycle during this
            # test.  The worker must stay started — stop() sets the
            # shutdown event, and _process_due claims rows but refuses to
            # process them once that event is set, leaving rows claimed
            # in_progress.  Driving a cycle directly is deterministic.
            await app._retry_worker._process_due(datetime.now(UTC) + timedelta(days=1))

            storage = SQLiteStorage(db_path=str(db))
            try:
                await storage.initialize()
                row = await storage.get_outbox_item(outbox_id)
                receipts = await storage.list_receipts_for_event(event_id)
            finally:
                await storage.close()

            assert row is not None
            assert row.status == "retry_wait"
            assert row.attempt_number == 1
            assert row.failure_kind == "adapter_transient"
            assert row.error_summary is not None
            assert "adapter_unavailable_startup" in row.error_summary
            assert receipts == []
            assert target.delivered_payloads == []
        finally:
            if app.state not in (RuntimeState.STOPPED, RuntimeState.FAILED):
                await app.stop()

    asyncio.run(_scenario())


# ---------------------------------------------------------------------------
# Case 3: admitted durable ingress is not fresh source availability
# ---------------------------------------------------------------------------


class TestAdmittedIngressSurvivesSourceOutage:
    """LIVE restart processes stored input even when its producer is down."""

    def test_admitted_ingress_delivered_when_source_start_fails(
        self,
        tmp_paths: MedrePaths,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A previously admitted pending row is routed, not acknowledged
        as no-route, on a LIVE restart whose source adapter fails to start.

        A second admitted row from a source with no configured route at
        all still completes without delivery: legitimate no-route policy
        is preserved and only the source startup failure is under test.
        """
        db = tmp_path / "ingress_outage.db"
        config = _mx_to_mesh_config(db)
        admitted_id = "evt-admitted-survivor"
        noroute_id = "evt-admitted-noroute"

        async def _seed() -> None:
            storage = SQLiteStorage(db_path=str(db))
            try:
                await storage.initialize()
                survivor = _mx_src_event(admitted_id, "admitted before outage")
                result = await storage.admit_ingress(survivor, None, "live")
                assert result.created and result.work_status == "pending"
                orphan = CanonicalEvent(
                    event_id=noroute_id,
                    event_kind="message.created",
                    schema_version=1,
                    timestamp=datetime.now(UTC),
                    source_adapter="unknown_source",
                    source_transport_id="t-noroute",
                    source_channel_id="ch-0",
                    parent_event_id=None,
                    lineage=(),
                    relations=(),
                    payload={"text": "no route configured for this source"},
                    metadata=EventMetadata(),
                )
                result = await storage.admit_ingress(orphan, None, "live")
                assert result.created
            finally:
                await storage.close()

        asyncio.run(_seed())

        app = RuntimeBuilder(config, tmp_paths).build()
        source = app.adapters["mx_src"]
        assert isinstance(source, FakeMatrixAdapter)
        _patch_start_failure(source, monkeypatch)
        target = app.adapters["mesh_tgt"]
        assert isinstance(target, FakeMeshtasticAdapter)

        async def _scenario() -> None:
            await app.start()
            try:
                assert app.boot_summary is not None
                assert app.boot_summary.runtime_health == "degraded"
                assert "mx_src" in app.boot_summary.failed_adapter_ids

                # Truthful readiness reporting is unchanged...
                readiness = app.startup_readiness
                assert readiness is not None
                assert (
                    readiness.route_states["src-to-tgt"]
                    is RouteOperationalState.SKIPPED
                )

                # ...but the stored event still routes to the healthy
                # target: its producer being offline does not erase
                # already-admitted input.
                await wait_until(
                    lambda: len(target.delivered_payloads) >= 1, timeout=5.0
                )
                assert any(
                    result.payload.get("text") == "admitted before outage"
                    for result in target.delivered_payloads
                )

                async def _counts() -> tuple[int, int]:
                    storage = SQLiteStorage(db_path=str(db))
                    try:
                        await storage.initialize()
                        noroute_receipts = len(
                            await storage.list_receipts_for_event(noroute_id)
                        )
                        counts = await storage.count_ingress_work_by_status()
                        return noroute_receipts, counts.get("pending", 0)
                    finally:
                        await storage.close()

                async def _ingress_drained() -> bool:
                    _receipts, pending = await _counts()
                    return pending == 0

                assert await wait_until(_ingress_drained, timeout=5.0), (
                    "durable ingress worker did not finish admitted rows"
                )
                noroute_receipts, pending_rows = await _counts()
                assert (
                    noroute_receipts == 0
                ), "source-less no-route bystander was delivered"
                assert pending_rows == 0, f"admitted rows left pending: {pending_rows}"

                survivor_receipts: list[Any] = []

                async def _survivor_receipts() -> None:
                    storage = SQLiteStorage(db_path=str(db))
                    try:
                        await storage.initialize()
                        survivor_receipts.extend(
                            await storage.list_receipts_for_event(admitted_id)
                        )
                    finally:
                        await storage.close()

                await _survivor_receipts()
                assert any(r.status == "sent" for r in survivor_receipts), (
                    f"admitted input was consumed without delivery: "
                    f"{survivor_receipts}"
                )
            finally:
                await app.stop()
                assert app.state is RuntimeState.STOPPED

        asyncio.run(_scenario())


async def test_retry_worker_activation_failure_cleans_up_started_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Post-adapter worker activation is still inside startup ownership."""
    db = tmp_path / "retry-activation-failure.db"
    config = _mx_to_mesh_config(
        db,
        retry=RetryConfig(enabled=True, interval_seconds=1.0),
    )
    paths = MedrePaths(
        config_dir=tmp_path / "config",
        config_file=tmp_path / "config" / "config.yaml",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        log_dir=tmp_path / "logs",
        database_path=db,
    )
    app = RuntimeBuilder(config, paths).build()

    async def _fail_retry_start(self: Any) -> None:
        raise RuntimeError("retry startup evidence failed")

    monkeypatch.setattr("medre.runtime.retry.RetryWorker.start", _fail_retry_start)

    with pytest.raises(RuntimeStartupError, match="Failed to activate runtime workers"):
        await app.start()

    assert app.state is RuntimeState.FAILED
    assert all(not adapter.is_started for adapter in app.adapters.values())
