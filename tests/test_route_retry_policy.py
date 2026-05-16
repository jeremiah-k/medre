"""Route-level retry policy: parsing, validation, runtime attachment, and scheduling.

Tests cover:
1. RouteRetryConfig parsing and validation (also in test_routes.py; this file
   focuses on runtime integration).
2. RuntimeBuilder builds route_retry_policies mapping from RouteConfigSet.
3. PipelineRunner attaches RetryPolicy to DeliveryPlan when route has retry.
4. Transient failures produce retry receipts (next_retry_at) when route retry
   is enabled; no retry receipts when absent/disabled.
5. Metadata persistence: retry fields on the receipt match route config.
6. Global [retry] worker semantics: route retry schedules, worker executes.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import cast
from unittest.mock import MagicMock

import pytest

from medre.adapters.base import AdapterSendError
from medre.config.model import (
    AdapterConfigSet,
    LoggingConfig,
    MatrixRuntimeConfig,
    MeshtasticRuntimeConfig,
    RuntimeConfig,
    RuntimeOptions,
    StorageConfig,
)
from medre.config.paths import MedrePaths, resolve
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.events.bus import EventBus
from medre.core.planning import FallbackResolver, RelationResolver
from medre.core.planning.delivery_plan import DeliveryPlan, RetryPolicy
from medre.core.routing import Router
from medre.core.routing.models import Route, RouteSource, RouteTarget
from medre.core.storage import SQLiteStorage
from medre.core.storage.backend import StorageBackend
from medre.runtime.builder import RuntimeBuilder
from medre.runtime.routes import RouteConfig, RouteConfigSet, RouteRetryConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    event_id: str = "evt-retry-001",
    source_adapter: str = "src",
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
        payload={"text": "hello"},
        metadata=EventMetadata(),
    )


def _make_pipeline_config(
    storage: SQLiteStorage,
    router: Router,
    adapters: dict | None = None,
    route_retry_policies: dict[str, RetryPolicy] | None = None,
) -> PipelineConfig:
    return PipelineConfig(
        storage=cast(StorageBackend, storage),
        router=router,
        fallback_resolver=FallbackResolver(),
        relation_resolver=RelationResolver(storage=storage),
        adapters=adapters or {},
        event_bus=EventBus(),
        route_retry_policies=route_retry_policies or {},
    )


class _TransientFailAdapter:
    """Adapter that always raises a transient error."""

    adapter_id = "fail-target"

    def __init__(self) -> None:
        self.received_events: list[object] = []

    async def deliver(self, payload: object) -> None:
        self.received_events.append(payload)
        raise AdapterSendError("transient boom", transient=True)

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def capabilities(self) -> dict:
        return {}

    def diagnostics(self) -> dict:
        return {}


class _SuccessAdapter:
    """Adapter that always succeeds."""

    adapter_id = "ok-target"

    def __init__(self) -> None:
        self.received_events: list[object] = []

    async def deliver(self, payload: object) -> None:
        self.received_events.append(payload)

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def capabilities(self) -> dict:
        return {}

    def diagnostics(self) -> dict:
        return {}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("MEDRE_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME",
                "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def tmp_paths(tmp_path, monkeypatch: pytest.MonkeyPatch) -> MedrePaths:
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    return resolve()


@pytest.fixture()
async def mem_storage() -> AsyncGenerator[SQLiteStorage, None]:
    """SQLiteStorage backed by :memory:, initialized and cleaned up."""
    storage = SQLiteStorage(":memory:")
    await storage.initialize()
    yield storage
    await storage.close()


# ---------------------------------------------------------------------------
# 1. RuntimeBuilder builds retry policies mapping
# ---------------------------------------------------------------------------


class TestBuilderRetryPolicies:
    """RuntimeBuilder._build_route_retry_policies produces correct mapping."""

    def _make_config_with_routes(
        self,
        routes: RouteConfigSet,
    ) -> RuntimeConfig:
        return RuntimeConfig(
            runtime=RuntimeOptions(name="test-retry-builder"),
            logging=LoggingConfig(),
            storage=StorageConfig(backend="memory"),
            routes=routes,
        )

    def test_no_retry_routes_empty_mapping(self, tmp_paths: MedrePaths) -> None:
        routes = RouteConfigSet(routes=(
            RouteConfig.from_toml_dict("r1", {
                "source_adapters": ["a"],
                "dest_adapters": ["b"],
            }),
        ))
        config = self._make_config_with_routes(routes)
        builder = RuntimeBuilder(config, tmp_paths)
        result = builder._build_route_retry_policies({"r1": "r1"})
        assert result == {}

    def test_enabled_retry_produces_policy(self, tmp_paths: MedrePaths) -> None:
        routes = RouteConfigSet(routes=(
            RouteConfig.from_toml_dict("r1", {
                "source_adapters": ["a"],
                "dest_adapters": ["b"],
                "retry": {"enabled": True, "max_attempts": 5},
            }),
        ))
        config = self._make_config_with_routes(routes)
        builder = RuntimeBuilder(config, tmp_paths)
        result = builder._build_route_retry_policies({"r1": "r1"})
        assert "r1" in result
        assert result["r1"].max_attempts == 5

    def test_disabled_retry_not_in_mapping(self, tmp_paths: MedrePaths) -> None:
        routes = RouteConfigSet(routes=(
            RouteConfig.from_toml_dict("r1", {
                "source_adapters": ["a"],
                "dest_adapters": ["b"],
                "retry": {"enabled": False},
            }),
        ))
        config = self._make_config_with_routes(routes)
        builder = RuntimeBuilder(config, tmp_paths)
        result = builder._build_route_retry_policies({"r1": "r1"})
        assert result == {}

    def test_mixed_routes(self, tmp_paths: MedrePaths) -> None:
        routes = RouteConfigSet(routes=(
            RouteConfig.from_toml_dict("with_retry", {
                "source_adapters": ["a"],
                "dest_adapters": ["b"],
                "retry": {"enabled": True, "max_attempts": 3},
            }),
            RouteConfig.from_toml_dict("no_retry", {
                "source_adapters": ["c"],
                "dest_adapters": ["d"],
            }),
            RouteConfig.from_toml_dict("disabled_retry", {
                "source_adapters": ["e"],
                "dest_adapters": ["f"],
                "retry": {"enabled": False},
            }),
        ))
        config = self._make_config_with_routes(routes)
        builder = RuntimeBuilder(config, tmp_paths)
        provenance = {
            "with_retry": "with_retry",
            "no_retry": "no_retry",
            "disabled_retry": "disabled_retry",
        }
        result = builder._build_route_retry_policies(provenance)
        assert set(result.keys()) == {"with_retry"}
        assert result["with_retry"].max_attempts == 3

    def test_bidirectional_expands_provenance(self, tmp_paths: MedrePaths) -> None:
        routes = RouteConfigSet(routes=(
            RouteConfig.from_toml_dict("bidir", {
                "source_adapters": ["a"],
                "dest_adapters": ["b"],
                "directionality": "bidirectional",
                "retry": {"enabled": True, "max_attempts": 4},
            }),
        ))
        config = self._make_config_with_routes(routes)
        builder = RuntimeBuilder(config, tmp_paths)
        # Bidirectional expands to "bidir" + "bidir__rev_0"
        provenance = {"bidir": "bidir", "bidir__rev_0": "bidir"}
        result = builder._build_route_retry_policies(provenance)
        assert "bidir" in result
        assert "bidir__rev_0" in result
        assert result["bidir"].max_attempts == 4
        assert result["bidir__rev_0"].max_attempts == 4

    def test_disabled_route_not_in_mapping(self, tmp_paths: MedrePaths) -> None:
        routes = RouteConfigSet(routes=(
            RouteConfig.from_toml_dict("disabled", {
                "source_adapters": ["a"],
                "dest_adapters": ["b"],
                "enabled": False,
                "retry": {"enabled": True, "max_attempts": 3},
            }),
        ))
        config = self._make_config_with_routes(routes)
        builder = RuntimeBuilder(config, tmp_paths)
        result = builder._build_route_retry_policies({"disabled": "disabled"})
        assert result == {}

    def test_retry_policy_fields_match_config(self, tmp_paths: MedrePaths) -> None:
        routes = RouteConfigSet(routes=(
            RouteConfig.from_toml_dict("r1", {
                "source_adapters": ["a"],
                "dest_adapters": ["b"],
                "retry": {
                    "enabled": True,
                    "max_attempts": 7,
                    "backoff_base": 3.0,
                    "max_delay_seconds": 90.0,
                    "jitter": True,
                },
            }),
        ))
        config = self._make_config_with_routes(routes)
        builder = RuntimeBuilder(config, tmp_paths)
        result = builder._build_route_retry_policies({"r1": "r1"})
        policy = result["r1"]
        assert policy.max_attempts == 7
        assert policy.backoff_base == 3.0
        assert policy.max_delay_seconds == 90.0
        assert policy.jitter is True


# ---------------------------------------------------------------------------
# 2. PipelineRunner attaches retry policy from route_retry_policies
# ---------------------------------------------------------------------------


class TestPipelineRetryAttachment:
    """PipelineRunner.route_event attaches retry policy from config."""

    @pytest.mark.asyncio()
    async def test_retry_policy_attached_to_plan(
        self, mem_storage: SQLiteStorage,
    ) -> None:
        """When route_retry_policies maps a route, the plan gets the policy."""
        route = Route(
            id="retry-route",
            source=RouteSource(adapter="src", event_kinds=(), channel=None),
            targets=[RouteTarget(adapter="ok-target")],
        )
        router = Router(routes=[route])
        policy = RetryPolicy(max_attempts=3, backoff_base=2.0, jitter=False)
        config = _make_pipeline_config(
            storage=mem_storage,
            router=router,
            adapters={"ok-target": _SuccessAdapter()},
            route_retry_policies={"retry-route": policy},
        )
        runner = PipelineRunner(config)
        await runner.start()
        try:
            event = _make_event(source_adapter="src")
            _, plans = await runner.route_event(event)
            assert len(plans) == 1
            _, plan = plans[0]
            assert plan.retry_policy is not None
            assert plan.retry_policy.max_attempts == 3
            assert plan.retry_policy.backoff_base == 2.0
        finally:
            await runner.stop()

    @pytest.mark.asyncio()
    async def test_no_retry_policy_when_absent(
        self, mem_storage: SQLiteStorage,
    ) -> None:
        """When route is not in route_retry_policies, plan.retry_policy is None."""
        route = Route(
            id="no-retry",
            source=RouteSource(adapter="src", event_kinds=(), channel=None),
            targets=[RouteTarget(adapter="ok-target")],
        )
        router = Router(routes=[route])
        config = _make_pipeline_config(
            storage=mem_storage,
            router=router,
            adapters={"ok-target": _SuccessAdapter()},
        )
        runner = PipelineRunner(config)
        await runner.start()
        try:
            event = _make_event(source_adapter="src")
            _, plans = await runner.route_event(event)
            assert len(plans) == 1
            _, plan = plans[0]
            assert plan.retry_policy is None
        finally:
            await runner.stop()


# ---------------------------------------------------------------------------
# 3. Transient failure: retry receipt with next_retry_at
# ---------------------------------------------------------------------------


class TestTransientRetryScheduling:
    """Transient failures produce retry receipts when route retry is enabled."""

    @pytest.mark.asyncio()
    async def test_transient_failure_with_retry_schedules_retry(
        self, mem_storage: SQLiteStorage,
    ) -> None:
        """With route retry enabled, transient failure produces next_retry_at."""
        route = Route(
            id="retry-route",
            source=RouteSource(adapter="src", event_kinds=(), channel=None),
            targets=[RouteTarget(adapter="fail-target")],
        )
        router = Router(routes=[route])
        policy = RetryPolicy(max_attempts=3, backoff_base=2.0, jitter=False)
        config = _make_pipeline_config(
            storage=mem_storage,
            router=router,
            adapters={"fail-target": _TransientFailAdapter()},
            route_retry_policies={"retry-route": policy},
        )
        runner = PipelineRunner(config)
        await runner.start()
        try:
            event = _make_event(source_adapter="src")
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "transient_failure"

            receipts = await mem_storage.list_receipts_for_event(event.event_id)
            assert len(receipts) >= 1
            failed_rcpt = receipts[0]
            assert failed_rcpt.status == "failed"
            assert failed_rcpt.next_retry_at is not None
            assert failed_rcpt.retry_max_attempts == 3
            assert failed_rcpt.retry_backoff_base == 2.0
            assert failed_rcpt.retry_jitter is False
        finally:
            await runner.stop()

    @pytest.mark.asyncio()
    async def test_transient_failure_without_retry_no_schedule(
        self, mem_storage: SQLiteStorage,
    ) -> None:
        """Without route retry, transient failure has no next_retry_at."""
        route = Route(
            id="no-retry",
            source=RouteSource(adapter="src", event_kinds=(), channel=None),
            targets=[RouteTarget(adapter="fail-target")],
        )
        router = Router(routes=[route])
        config = _make_pipeline_config(
            storage=mem_storage,
            router=router,
            adapters={"fail-target": _TransientFailAdapter()},
        )
        runner = PipelineRunner(config)
        await runner.start()
        try:
            event = _make_event(source_adapter="src")
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1
            assert outcomes[0].status == "transient_failure"

            receipts = await mem_storage.list_receipts_for_event(event.event_id)
            assert len(receipts) >= 1
            failed_rcpt = receipts[0]
            assert failed_rcpt.status == "failed"
            assert failed_rcpt.next_retry_at is None
        finally:
            await runner.stop()

    @pytest.mark.asyncio()
    async def test_retry_metadata_persisted(
        self, mem_storage: SQLiteStorage,
    ) -> None:
        """Retry policy metadata is persisted on the receipt."""
        route = Route(
            id="meta-route",
            source=RouteSource(adapter="src", event_kinds=(), channel=None),
            targets=[RouteTarget(adapter="fail-target")],
        )
        router = Router(routes=[route])
        policy = RetryPolicy(
            max_attempts=5,
            backoff_base=1.5,
            max_delay_seconds=120.0,
            jitter=True,
        )
        config = _make_pipeline_config(
            storage=mem_storage,
            router=router,
            adapters={"fail-target": _TransientFailAdapter()},
            route_retry_policies={"meta-route": policy},
        )
        runner = PipelineRunner(config)
        await runner.start()
        try:
            event = _make_event(source_adapter="src")
            outcomes = await runner.handle_ingress(event)
            assert len(outcomes) == 1

            receipts = await mem_storage.list_receipts_for_event(event.event_id)
            failed_rcpt = receipts[0]
            assert failed_rcpt.retry_max_attempts == 5
            assert failed_rcpt.retry_backoff_base == 1.5
            assert failed_rcpt.retry_max_delay == 120.0
            assert failed_rcpt.retry_jitter is True
        finally:
            await runner.stop()


# ---------------------------------------------------------------------------
# 4. Global [retry] disabled semantics
# ---------------------------------------------------------------------------


class TestGlobalRetrySemantics:
    """Route retry schedules receipts even when global [retry] is disabled.

    The global [retry] controls whether the RetryWorker runs and picks up
    due retry receipts.  Route retry controls whether receipts get
    next_retry_at set in the first place.  When global retry is disabled
    but route retry is enabled, due retry receipts are persisted but not
    processed until the worker is enabled.
    """

    @pytest.mark.asyncio()
    async def test_route_retry_schedules_regardless_of_global(
        self, mem_storage: SQLiteStorage,
    ) -> None:
        """Route retry produces next_retry_at even without global RetryWorker."""
        route = Route(
            id="route-retry",
            source=RouteSource(adapter="src", event_kinds=(), channel=None),
            targets=[RouteTarget(adapter="fail-target")],
        )
        router = Router(routes=[route])
        policy = RetryPolicy(max_attempts=3, jitter=False)
        config = _make_pipeline_config(
            storage=mem_storage,
            router=router,
            adapters={"fail-target": _TransientFailAdapter()},
            route_retry_policies={"route-retry": policy},
        )
        runner = PipelineRunner(config)
        await runner.start()
        try:
            event = _make_event(source_adapter="src")
            outcomes = await runner.handle_ingress(event)
            assert outcomes[0].status == "transient_failure"
            receipts = await mem_storage.list_receipts_for_event(event.event_id)
            assert receipts[0].next_retry_at is not None
        finally:
            await runner.stop()
