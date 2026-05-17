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
9. Adapters — constructed from enabled adapter configs, in deterministic
   ``(transport, adapter_id)`` sorted order
10. Routes — validated, expanded, and registered in config declaration order
11. :class:`asyncio.Event` — shutdown signal
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, replace
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
    from medre.core.planning.delivery_plan import RetryPolicy

__all__ = ["RuntimeBuilder", "AdapterBuildFailure"]

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Build result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
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

    transport: str
    adapter_id: str
    error: Exception


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
        dependency_module: str | None = None,
        dependency_availability_flag: str | None = None,
    ) -> None:
        self._module = module
        self._cls_name = cls_name
        self._dependency_module = dependency_module
        self._dependency_availability_flag = dependency_availability_flag

    def build(self, config: Any) -> BaseAdapter | None:
        """Construct the adapter, returning ``None`` on missing deps."""
        # Check optional dependency flag if applicable.
        if self._dependency_module and self._dependency_availability_flag:
            try:
                mod = __import__(
                    self._dependency_module, fromlist=[self._dependency_availability_flag]
                )
                if not getattr(mod, self._dependency_availability_flag, True):
                    _logger.warning(
                        "Optional dependency not available for %s — skipping",
                        self._cls_name,
                    )
                    return None
            except ImportError:
                _logger.warning(
                    "Dependency module %s not found — skipping %s",
                    self._dependency_module,
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
        dependency_module="medre.adapters.matrix.compat",
        dependency_availability_flag="HAS_NIO",
    ),
    "meshtastic": _AdapterFactory(
        module="medre.adapters.meshtastic.adapter",
        cls_name="MeshtasticAdapter",
        dependency_module="medre.adapters.meshtastic.compat",
        dependency_availability_flag="HAS_MESHTASTIC",
    ),
    "meshcore": _AdapterFactory(
        module="medre.adapters.meshcore.adapter",
        cls_name="MeshCoreAdapter",
        dependency_module="medre.adapters.meshcore.compat",
        dependency_availability_flag="HAS_MESHCORE",
    ),
    "lxmf": _AdapterFactory(
        module="medre.adapters.lxmf.adapter",
        cls_name="LxmfAdapter",
        dependency_module="medre.adapters.lxmf.compat",
        dependency_availability_flag="HAS_LXMF",
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
        return FakeMeshtasticAdapter(adapter_id=adapter_id)
    if transport == "meshcore":
        from medre.adapters.fake_meshcore import FakeMeshCoreAdapter
        return FakeMeshCoreAdapter(adapter_id=adapter_id)
    if transport == "lxmf":
        from medre.adapters.fake_lxmf import FakeLxmfAdapter
        return FakeLxmfAdapter(adapter_id=adapter_id)
    raise RuntimeConfigError(
        f"Unknown transport type {transport!r} for fake adapter "
        f"{adapter_id!r}. Known types: {', '.join(sorted(_ADAPTER_BUILDERS))}"
    )


# ---------------------------------------------------------------------------
# Adapter renderer registration
# ---------------------------------------------------------------------------

_ADAPTER_RENDERER_SPECS: list[tuple[str, str]] = [
    ("medre.adapters.matrix.renderer", "MatrixRenderer"),
    ("medre.adapters.meshtastic.renderer", "MeshtasticRenderer"),
    ("medre.adapters.meshcore.renderer", "MeshCoreRenderer"),
    ("medre.adapters.lxmf.renderer", "LxmfRenderer"),
]
"""(module_path, class_name) pairs for transport-specific renderers."""


def _register_adapter_renderers(pipeline: RenderingPipeline, config: RuntimeConfig | None = None) -> None:
    """Register all transport-specific renderers at priority 50.

    Uses dynamic imports to avoid static coupling between the builder
    and concrete adapter packages, preserving the architectural boundary
    enforced by the test suite.

    When *config* is provided, transport-specific renderer config is
    extracted and passed to renderers that accept it (e.g.
    ``MeshtasticRenderer`` receives the first ``MeshtasticConfig`` so
    that ``relay_prefix`` is available).
    """
    # Collect MeshtasticConfig for MeshtasticRenderer, if available.
    meshtastic_config: Any = None
    if config is not None:
        for _transport, _adapter_id, rtc in config.adapters.all_configs():
            if _transport == "meshtastic" and getattr(rtc, "config", None) is not None:
                meshtastic_config = rtc.config
                break

    for module_path, class_name in _ADAPTER_RENDERER_SPECS:
        try:
            mod = __import__(module_path, fromlist=[class_name])
            renderer_cls = getattr(mod, class_name)
            # Pass MeshtasticConfig when constructing MeshtasticRenderer.
            if class_name == "MeshtasticRenderer" and meshtastic_config is not None:
                pipeline.register(renderer_cls(config=meshtastic_config), priority=50)
            elif class_name == "MatrixRenderer" and meshtastic_config is not None:
                pipeline.register(renderer_cls(meshtastic_config=meshtastic_config), priority=50)
            else:
                pipeline.register(renderer_cls(), priority=50)
        except ImportError:
            _logger.debug(
                "Skipping renderer %s.%s (import failed)",
                module_path, class_name,
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
        # Adapter-specific renderers at priority 50 (before TextRenderer's
        # 100) so they match their platform first.  Each renderer's
        # can_render() checks target_platform, so registering all four
        # is safe — only the matching one will accept.
        _register_adapter_renderers(rendering_pipeline, config=self._config)
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
            pipeline=pipeline_runner,  # type: ignore[arg-type]
            capacity_controller=capacity_controller,
            diagnostician=diagnostician,
            accounting=runtime_accounting,
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
        #       Validates adapter references against configured enabled IDs
        #       for config correctness; degrades routes referencing adapters
        #       that failed to build rather than aborting the entire runtime.
        from medre.runtime.route_engine import register_routes
        configured_enabled_ids = frozenset(
            aid for aid, _ in self._config.adapters.all_enabled()
        )
        built_adapter_ids = frozenset(adapters.keys())
        route_result = register_routes(
            router,
            self._config.routes,
            configured_enabled_ids,
            built_adapter_ids,
        )

        # 10.6. Build route-level retry policies mapping.
        #       Maps expanded route IDs to RetryPolicy instances for routes
        #       that have retry enabled.  Uses the provenance mapping to
        #       resolve config route IDs to expanded route IDs.
        route_retry_policies = self._build_route_retry_policies(
            route_result.provenance,
        )
        pipeline_config.route_retry_policies = route_retry_policies

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
        app._route_eligibility = route_result.eligibility
        app._route_provenance = route_result.provenance
        app._registered_routes = route_result.registered_routes
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

    # -- Route retry policies ---------------------------------------------------

    def _build_route_retry_policies(
        self,
        provenance: dict[str, str],
    ) -> dict[str, RetryPolicy]:
        """Build a mapping from expanded route ID to :class:`RetryPolicy`.

        Iterates all enabled route configs that declare a retry section
        with ``enabled=True``, converts each :class:`RouteRetryConfig` to
        a :class:`RetryPolicy`, and maps every expanded route ID that
        originated from that config route.

        Parameters
        ----------
        provenance:
            Mapping from expanded route ID to config route ID, as returned
            by :func:`register_routes`.

        Returns
        -------
        dict[str, RetryPolicy]
            Mapping from expanded route ID to RetryPolicy for routes with
            retry enabled.
        """
        from medre.core.planning.delivery_plan import RetryPolicy

        # Build config_route_id → RetryPolicy for enabled retry configs.
        config_policies: dict[str, RetryPolicy] = {}
        for rc in self._config.routes.routes:
            if not rc.enabled or rc.retry is None or not rc.retry.enabled:
                continue
            config_policies[rc.route_id] = RetryPolicy(
                max_attempts=rc.retry.max_attempts,
                backoff_base=rc.retry.backoff_base,
                max_delay_seconds=rc.retry.max_delay_seconds,
                jitter=rc.retry.jitter,
            )

        if not config_policies:
            return {}

        # Expand config_route_id → all expanded route IDs via provenance.
        result: dict[str, RetryPolicy] = {}
        for expanded_id, config_id in provenance.items():
            policy = config_policies.get(config_id)
            if policy is not None:
                result[expanded_id] = policy

        return result

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
                f"not be built: the optional SDK dependency is not installed. "
                f"Install it with: pip install medre[{transport}]"
            )
        return adapter
