"""Runtime builder that constructs a :class:`MedreApp` from configuration.

:class:`RuntimeBuilder` wires together every subsystem (storage, event bus,
rendering pipeline, router, adapters, etc.) using a :class:`RuntimeConfig`
and :class:`MedrePaths` as inputs.  The returned :class:`MedreApp` is fully
constructed but **not yet started** — call :meth:`MedreApp.start` to begin
processing.

Construction order
------------------
1. :class:`EventBus` — central async pub/sub
2. :class:`RenderingPipeline` — with a default :class:`TextRenderer`
3. :class:`Router` — empty route table
4. :class:`FallbackResolver` — capability degradation
5. :class:`SQLiteStorage` — using resolved database path
6. :class:`Diagnostician` — metrics and diagnostics
7. :class:`RelationResolver` — cross-adapter event linking
8. :class:`PipelineConfig` / :class:`PipelineRunner` — orchestration
9. Adapters — constructed from enabled adapter configs
10. :class:`asyncio.Event` — shutdown signal
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from medre.adapters.base import BaseAdapter
from medre.config.model import (
    AdapterConfigSet,
    LxmfRuntimeConfig,
    MatrixRuntimeConfig,
    MeshCoreRuntimeConfig,
    MeshtasticRuntimeConfig,
    RuntimeConfig,
    StorageConfig,
)
from medre.config.paths import MedrePaths, MedrePathsError
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.events.bus import EventBus
from medre.core.observability.metrics import Diagnostician
from medre.core.planning.fallback_resolution import FallbackResolver
from medre.core.planning.relation_resolution import RelationResolver
from medre.core.rendering.renderer import RenderingPipeline
from medre.core.rendering.text import TextRenderer
from medre.core.routing.router import Router
from medre.core.routing.stats import RouteStats
from medre.core.runtime.accounting import RuntimeAccounting
from medre.core.storage.sqlite import SQLiteStorage
from medre.core.storage.replay import ReplayEngine
from medre.runtime.app import MedreApp
from medre.runtime.capacity import CapacityController
from medre.runtime.errors import RuntimeConfigError

if TYPE_CHECKING:
    pass

__all__ = ["RuntimeBuilder", "AdapterBuildFailure"]

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Build result types
# ---------------------------------------------------------------------------


class AdapterBuildFailure:
    """Records a single adapter that failed during construction.

    Attributes
    ----------
    transport:
        Transport type (e.g. ``"matrix"``).
    adapter_id:
        Adapter identifier.
    error:
        The exception that caused the failure.
    """

    __slots__ = ("transport", "adapter_id", "error")

    def __init__(
        self, transport: str, adapter_id: str, error: Exception
    ) -> None:
        self.transport = transport
        self.adapter_id = adapter_id
        self.error = error

    def __repr__(self) -> str:
        return (
            f"AdapterBuildFailure(transport={self.transport!r}, "
            f"adapter_id={self.adapter_id!r}, error={self.error!r})"
        )


# ---------------------------------------------------------------------------
# Adapter factory dispatch
# ---------------------------------------------------------------------------

_ADAPTER_BUILDERS: dict[str, _AdapterFactory]  # forward declaration


class _AdapterFactory:
    """Descriptor that lazily imports an adapter class and constructs it."""

    def __init__(
        self,
        module: str,
        cls_name: str,
        compat_module: str | None = None,
        compat_flag: str | None = None,
    ) -> None:
        self._module = module
        self._cls_name = cls_name
        self._compat_module = compat_module
        self._compat_flag = compat_flag

    def build(self, config: Any) -> BaseAdapter | None:
        """Construct the adapter, returning ``None`` on missing deps."""
        # Check optional dependency flag if applicable.
        if self._compat_module and self._compat_flag:
            try:
                mod = __import__(
                    self._compat_module, fromlist=[self._compat_flag]
                )
                if not getattr(mod, self._compat_flag, True):
                    _logger.warning(
                        "Optional dependency not available for %s — skipping",
                        self._cls_name,
                    )
                    return None
            except ImportError:
                _logger.warning(
                    "Compat module %s not found — skipping %s",
                    self._compat_module,
                    self._cls_name,
                )
                return None

        # Import and construct the adapter.
        try:
            mod = __import__(self._module, fromlist=[self._cls_name])
            cls = getattr(mod, self._cls_name)
            return cls(config)
        except ImportError as exc:
            _logger.warning(
                "Cannot import %s from %s: %s — skipping",
                self._cls_name,
                self._module,
                exc,
            )
            return None


_ADAPTER_BUILDERS: dict[str, _AdapterFactory] = {
    "matrix": _AdapterFactory(
        module="medre.adapters.matrix.adapter",
        cls_name="MatrixAdapter",
        compat_module="medre.adapters.matrix.compat",
        compat_flag="HAS_NIO",
    ),
    "meshtastic": _AdapterFactory(
        module="medre.adapters.meshtastic.adapter",
        cls_name="MeshtasticAdapter",
        compat_module="medre.adapters.meshtastic.compat",
        compat_flag="HAS_MESHTASTIC",
    ),
    "meshcore": _AdapterFactory(
        module="medre.adapters.meshcore.adapter",
        cls_name="MeshCoreAdapter",
        compat_module="medre.adapters.meshcore.compat",
        compat_flag="HAS_MESHCORE",
    ),
    "lxmf": _AdapterFactory(
        module="medre.adapters.lxmf.adapter",
        cls_name="LxmfAdapter",
        compat_module="medre.adapters.lxmf.compat",
        compat_flag="HAS_LXMF",
    ),
}


def _build_fake_adapter(transport: str, adapter_id: str) -> BaseAdapter:
    """Construct a fake adapter for the given transport.

    Fake adapters are always importable from core — they do not depend on
    optional live SDKs.  This function raises :class:`RuntimeConfigError`
    if *transport* is not recognised.
    """
    if transport == "matrix":
        from medre.adapters.fake_matrix import FakeMatrixAdapter
        return FakeMatrixAdapter(adapter_id=adapter_id)
    if transport == "meshtastic":
        from medre.adapters.fake_meshtastic import FakeMeshtasticAdapter
        return FakeMeshtasticAdapter()
    if transport == "meshcore":
        from medre.adapters.fake_meshcore import FakeMeshCoreAdapter
        return FakeMeshCoreAdapter()
    if transport == "lxmf":
        from medre.adapters.fake_lxmf import FakeLxmfAdapter
        return FakeLxmfAdapter()
    raise RuntimeConfigError(
        f"Unknown transport type {transport!r} for fake adapter "
        f"{adapter_id!r}. Known types: {', '.join(sorted(_ADAPTER_BUILDERS))}"
    )


# ---------------------------------------------------------------------------
# RuntimeBuilder
# ---------------------------------------------------------------------------


class RuntimeBuilder:
    """Constructs a :class:`MedreApp` from :class:`RuntimeConfig` + :class:`MedrePaths`.

    Parameters
    ----------
    config:
        Fully-resolved runtime configuration.
    paths:
        Fully-resolved filesystem paths.
    """

    def __init__(self, config: RuntimeConfig, paths: MedrePaths) -> None:
        self._config = config
        self._paths = paths

    def build(self) -> MedreApp:
        """Build and return a :class:`MedreApp`, ready for :meth:`MedreApp.start`.

        Returns
        -------
        MedreApp
            Fully wired runtime container.  Callers must call
            :meth:`MedreApp.start` before use.

        Raises
        ------
        RuntimeConfigError
            If the configuration is invalid or inconsistent.
        """
        # 1. EventBus
        event_bus = EventBus()

        # 2. RenderingPipeline with default TextRenderer
        rendering_pipeline = RenderingPipeline()
        rendering_pipeline.register(TextRenderer(), priority=100)

        # 3. Router (empty — routes configured separately)
        router = Router()

        # 4. FallbackResolver
        fallback_resolver = FallbackResolver()

        # 5. Storage — honour StorageConfig.backend and optional path
        storage = self._build_storage()

        # 6. Diagnostician
        diagnostician = Diagnostician()

        # 6.5 RuntimeAccounting — process-local bounded event counters.
        runtime_accounting = RuntimeAccounting()

        # 7. RelationResolver (depends on storage)
        relation_resolver = RelationResolver(storage=storage)

        # 8. Build adapters dict (mutable — shared with PipelineConfig)
        adapters: dict[str, BaseAdapter] = {}

        # 9. PipelineConfig + PipelineRunner
        route_stats = RouteStats()
        pipeline_config = PipelineConfig(
            storage=storage,
            router=router,
            fallback_resolver=fallback_resolver,
            relation_resolver=relation_resolver,
            adapters=adapters,
            event_bus=event_bus,
            rendering_pipeline=rendering_pipeline,
            diagnostician=diagnostician,
            route_stats=route_stats,
            runtime_accounting=runtime_accounting,
        )
        pipeline_runner = PipelineRunner(pipeline_config)

        # 9.5 CapacityController — bounds in-flight delivery and replay.
        capacity_controller = CapacityController(self._config.limits)
        pipeline_runner.set_capacity_controller(capacity_controller)

        # 9.6 ReplayEngine — replay harness with capacity controller wired.
        replay_engine = ReplayEngine(
            storage=storage,
            pipeline=pipeline_runner,
            capacity_controller=capacity_controller,
            diagnostician=diagnostician,
        )

        # 10. Construct adapters from RuntimeConfig
        build_failures = self._build_adapters(adapters)

        if build_failures:
            failed_ids = ", ".join(
                f"{f.transport}.{f.adapter_id}" for f in build_failures
            )
            _logger.warning(
                "Adapter build failures (%d): %s", len(build_failures), failed_ids
            )

        # 10.5. Register configured routes on the Router.
        #       Validates adapter references against built adapters first.
        from medre.runtime.route_engine import register_routes
        adapter_ids = frozenset(adapters.keys())
        register_routes(router, self._config.routes, adapter_ids)

        # 11. Shutdown event
        shutdown_event = asyncio.Event()

        app = MedreApp(
            config=self._config,
            paths=self._paths,
            storage=storage,
            event_bus=event_bus,
            rendering_pipeline=rendering_pipeline,
            router=router,
            fallback_resolver=fallback_resolver,
            relation_resolver=relation_resolver,
            pipeline_runner=pipeline_runner,
            route_stats=route_stats,
            diagnostician=diagnostician,
            adapters=adapters,
            shutdown_event=shutdown_event,
            build_failures=build_failures,
        )
        # Wire capacity controller and replay engine onto the app.
        app._capacity_controller = capacity_controller
        app._replay_engine = replay_engine
        app._runtime_accounting = runtime_accounting
        return app

    # -- Storage construction ----------------------------------------------------

    def _build_storage(self) -> SQLiteStorage:
        """Construct storage based on :class:`StorageConfig`.

        The builder does **not** create directories — that responsibility
        belongs to :meth:`MedreApp.start`.
        """
        storage_config: StorageConfig = self._config.storage

        if storage_config.backend == "sqlite":
            if storage_config.path:
                try:
                    db_path = str(self._paths.expand_placeholder(storage_config.path))
                except MedrePathsError as exc:
                    raise RuntimeConfigError(
                        f"Invalid storage path {storage_config.path!r}: {exc}"
                    ) from exc
            else:
                db_path = str(self._paths.database_path)
            return SQLiteStorage(db_path)

        if storage_config.backend == "memory":
            return SQLiteStorage(":memory:")

        raise RuntimeConfigError(
            f"Unsupported storage backend {storage_config.backend!r}. "
            f"Supported: sqlite, memory"
        )

    # -- Adapter construction ----------------------------------------------------

    def _build_adapters(
        self, adapters: dict[str, BaseAdapter]
    ) -> list[AdapterBuildFailure]:
        """Populate *adapters* from the enabled adapter configs.

        Disabled adapters are silently skipped.  Individual adapter
        construction failures are **not fatal** — the failed adapter is
        recorded and the remaining adapters continue to build.

        Returns
        -------
        list[AdapterBuildFailure]
            Adapters that failed to build, with transport and adapter_id
            attribution.

        Ordering
        --------
        Adapters are built in deterministic order sorted by
        ``(transport, adapter_id)`` tuple.
        """
        failures: list[AdapterBuildFailure] = []

        # Gather all enabled adapters and sort deterministically.
        all_cfgs = self._config.adapters.all_configs()
        enabled = [
            (transport, adapter_id, rtc)
            for transport, adapter_id, rtc in all_cfgs
            if rtc.enabled
        ]
        enabled.sort(key=lambda t: (t[0], t[1]))

        for transport, adapter_id, rtc in enabled:
            try:
                adapter = self._build_single_adapter(transport, adapter_id, rtc)
                adapters[adapter_id] = adapter
                _logger.info(
                    "Constructed adapter %r (%s)", adapter_id, transport
                )
            except Exception as exc:
                wrapped = RuntimeConfigError(
                    f"Failed to build adapter {adapter_id!r} "
                    f"({transport}): {exc}"
                )
                wrapped.__cause__ = exc
                failures.append(
                    AdapterBuildFailure(
                        transport=transport,
                        adapter_id=adapter_id,
                        error=wrapped,
                    )
                )
                _logger.error(
                    "Failed to build adapter %r (%s): %s",
                    adapter_id,
                    transport,
                    exc,
                )

        return failures

    def _build_single_adapter(
        self,
        transport: str,
        adapter_id: str,
        rtc: Any,
    ) -> BaseAdapter:
        """Construct a single enabled adapter.

        Raises :class:`RuntimeConfigError` if the adapter is enabled but
        cannot be built (unknown transport, missing config, or missing
        optional dependencies).
        """
        adapter_kind = getattr(rtc, "adapter_kind", "real")

        # --- Fake adapter path (no optional SDK imports) ---
        if adapter_kind == "fake":
            return _build_fake_adapter(transport, adapter_id)

        # --- Real adapter path ---
        factory = _ADAPTER_BUILDERS.get(transport)
        if factory is None:
            raise RuntimeConfigError(
                f"Unknown transport type {transport!r} for adapter "
                f"{adapter_id!r}. "
                f"Known types: {', '.join(sorted(_ADAPTER_BUILDERS))}"
            )

        config = rtc.config
        if config is None:
            raise RuntimeConfigError(
                f"Adapter {adapter_id!r} ({transport}) is enabled but has no config"
            )

        # Derive Matrix E2EE store_path from resolved state directory when
        # not explicitly configured.  Per-adapter isolation:
        # {state}/adapters/{adapter_id}/matrix/store
        if transport == "matrix" and getattr(config, "store_path", None) is None:
            derived_store = self._paths.adapter_transport_state_dir(adapter_id, "matrix") / "store"
            config = replace(config, store_path=str(derived_store))

        try:
            adapter = factory.build(config)
        except Exception as exc:
            raise RuntimeConfigError(
                f"Failed to build adapter {adapter_id!r} ({transport}): {exc}"
            ) from exc
        if adapter is None:
            raise RuntimeConfigError(
                f"Adapter {adapter_id!r} ({transport}) is enabled but could "
                f"not be built (missing optional dependencies)"
            )
        return adapter
