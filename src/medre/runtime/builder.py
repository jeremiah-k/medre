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
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Mapping, cast

from medre.config.model import (
    RuntimeConfig,
    StorageConfig,
)
from medre.config.paths import MedrePaths, MedrePathsError
from medre.core.contracts.adapter import AdapterContract
from medre.core.engine.pipeline import PipelineConfig, PipelineRunner
from medre.core.engine.replay.engine import ReplayEngine
from medre.core.events.bus import EventBus
from medre.core.observability.metrics import Diagnostician
from medre.core.planning.fallback_resolution import FallbackResolver
from medre.core.planning.relation_resolution import RelationResolver
from medre.core.rendering.renderer import RenderingPipeline
from medre.core.rendering.text import TextRenderer
from medre.core.routing.router import Router
from medre.core.routing.stats import RouteStats
from medre.core.storage.sqlite.storage import SQLiteStorage
from medre.core.supervision.accounting import RuntimeAccounting
from medre.core.supervision.capacity import CapacityController
from medre.runtime.app import MedreApp
from medre.runtime.errors import RuntimeConfigError

if TYPE_CHECKING:
    from medre.core.events.canonical import CanonicalEvent
    from medre.core.planning.delivery_plan import RetryPolicy
    from medre.core.planning.relation_enricher import SenderProjectionFn

__all__ = ["AdapterBuildFailure", "RuntimeBuilder", "SourceAttributionConfig"]

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Build result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceAttributionConfig:
    """Platform-neutral source attribution for prefix formatting.

    Built by :class:`RuntimeBuilder` from adapter configs.  Passed to
    renderers so they can look up source adapter ``origin_label`` and
    platform info when formatting relay prefixes.

    Attributes
    ----------
    adapter_id:
        Unique adapter identifier within the runtime.
    platform:
        Transport name (``"meshtastic"``, ``"meshcore"``, ``"lxmf"``,
        ``"matrix"``).
    origin_label:
        Human-readable label for the source adapter.  Empty string when
        not configured.
    """

    adapter_id: str
    platform: str
    origin_label: str = ""


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

    def build(self, config: Any) -> AdapterContract | None:
        """Construct the adapter, returning ``None`` on missing deps."""
        # Check optional dependency flag if applicable.
        if self._dependency_module and self._dependency_availability_flag:
            try:
                mod = __import__(
                    self._dependency_module,
                    fromlist=[self._dependency_availability_flag],
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


def _build_fake_adapter(transport: str, adapter_id: str) -> AdapterContract:
    """Construct a fake adapter for the given transport.

    Fake adapters are always importable from core — they do not depend on
    optional live SDKs.  This function raises :class:`RuntimeConfigError`
    if *transport* is not recognised.
    """
    if transport == "matrix":
        from medre.adapters.fakes.matrix import FakeMatrixAdapter

        return FakeMatrixAdapter(adapter_id=adapter_id)
    if transport == "meshtastic":
        from medre.adapters.fakes.meshtastic import FakeMeshtasticAdapter

        return FakeMeshtasticAdapter(adapter_id=adapter_id)
    if transport == "meshcore":
        from medre.adapters.fakes.meshcore import FakeMeshCoreAdapter

        return FakeMeshCoreAdapter(adapter_id=adapter_id)
    if transport == "lxmf":
        from medre.adapters.fakes.lxmf import FakeLxmfAdapter

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


def _register_adapter_renderers(
    pipeline: RenderingPipeline, config: RuntimeConfig | None = None
) -> dict[str, "SourceAttributionConfig"]:
    """Register all transport-specific renderers at priority 50.

    Uses dynamic imports to avoid static coupling between the builder
    and concrete adapter packages, preserving the architectural boundary
    enforced by the test suite.

    When *config* is provided, transport-specific renderer config is
    extracted and passed to renderers that accept it:

    * ``MeshtasticRenderer`` receives a mapping of ALL Meshtastic adapter
      configs (``adapter_id → MeshtasticConfig``) so that rendering is
      target-adapter-aware in multi-radio setups.
    * ``MatrixRenderer`` receives Matrix adapter configs via ``configs``
      for target-local ``relay_prefix`` resolution.  It registers whenever
      Matrix configs exist.  Unknown sources render plain Matrix output
      without prefix or metadata contamination.
    * ``LxmfRenderer`` receives a mapping of ALL LXMF adapter configs
      (``adapter_id → LxmfConfig``) so that rendering is target-adapter-aware
      in multi-LXMF setups.  The prefix template is resolved from the
      target adapter's ``lxmf_relay_prefix`` at render time.
    * ``MeshCoreRenderer`` receives a mapping of ALL MeshCore adapter
      configs (``adapter_id → MeshCoreConfig``) so that rendering is
      target-adapter-aware in multi-node setups.

    Returns the mapping of ``adapter_id → SourceAttributionConfig`` built
    while inspecting adapter configs.  Callers (notably
    :meth:`RuntimeBuilder.build`) reuse this mapping to wire other
    attribution-sensitive subsystems such as sender-identity projection
    for relation enrichment.
    """
    # Collect ALL MeshtasticConfigs for target-aware rendering.
    meshtastic_configs: dict[str, Any] = {}
    # Collect ALL MeshCoreConfigs for target-aware rendering.
    meshcore_configs: dict[str, Any] = {}
    # Collect ALL LxmfConfigs for prefix extraction.
    lxmf_configs: dict[str, Any] = {}
    if config is not None:
        for _transport, _adapter_id, rtc in config.adapters.all_configs():
            if not rtc.enabled:
                continue
            if _transport == "meshtastic" and getattr(rtc, "config", None) is not None:
                meshtastic_configs[_adapter_id] = rtc.config
            if _transport == "meshcore" and getattr(rtc, "config", None) is not None:
                meshcore_configs[_adapter_id] = rtc.config
            if _transport == "lxmf" and getattr(rtc, "config", None) is not None:
                lxmf_configs[_adapter_id] = rtc.config
        # Fallback: synthesize default MeshtasticConfigs for adapters that
        # lack a real config (e.g. fake adapters in mixed configs).
        if config.adapters.meshtastic:
            for adapter_id in config.adapters.meshtastic:
                if adapter_id not in meshtastic_configs:
                    import importlib

                    _meshtastic_mod = importlib.import_module(
                        "medre.config.adapters.meshtastic"
                    )
                    _MConfig = _meshtastic_mod.MeshtasticConfig
                    meshtastic_configs[adapter_id] = _MConfig(
                        adapter_id=adapter_id,
                        radio_relay_prefix="",
                    )
        # Fallback: synthesize default MeshCoreConfigs for adapters that
        # lack a real config (e.g. fake adapters in mixed configs).
        if config.adapters.meshcore:
            for adapter_id in config.adapters.meshcore:
                if adapter_id not in meshcore_configs:
                    import importlib

                    _meshcore_mod = importlib.import_module(
                        "medre.config.adapters.meshcore"
                    )
                    _MCConfig = _meshcore_mod.MeshCoreConfig
                    meshcore_configs[adapter_id] = _MCConfig(
                        adapter_id=adapter_id,
                    )
        # Fallback: synthesize default LxmfConfigs for adapters that
        # lack a real config (e.g. fake adapters in mixed configs).
        if config.adapters.lxmf:
            for adapter_id in config.adapters.lxmf:
                if adapter_id not in lxmf_configs:
                    import importlib

                    _lxmf_mod = importlib.import_module("medre.config.adapters.lxmf")
                    _LConfig = _lxmf_mod.LxmfConfig
                    lxmf_configs[adapter_id] = _LConfig(
                        adapter_id=adapter_id,
                        connection_type="fake",
                    )

    # Build source attribution registry from all adapter configs.
    # Maps adapter_id → SourceAttributionConfig for every enabled adapter
    # across all transports.  Uses duck-typing (getattr) to avoid importing
    # adapter config classes into core.
    source_attribution: dict[str, SourceAttributionConfig] = {}
    _matrix_configs: dict[str, Any] = {}
    if config is not None:
        for _transport, _adapter_id, _rtc in config.adapters.all_configs():
            if (
                _transport == "matrix"
                and _rtc.enabled
                and getattr(_rtc, "config", None) is not None
            ):
                _matrix_configs[_adapter_id] = _rtc.config
        # Fallback: synthesize default MatrixConfigs for adapters that
        # lack a real config (e.g. fake adapters in mixed configs).
        if config.adapters.matrix:
            for adapter_id in config.adapters.matrix:
                if adapter_id not in _matrix_configs:
                    import importlib

                    _matrix_mod = importlib.import_module(
                        "medre.config.adapters.matrix"
                    )
                    _MXConfig = _matrix_mod.MatrixConfig
                    _matrix_configs[adapter_id] = _MXConfig(
                        adapter_id=adapter_id,
                        homeserver="",
                        user_id="",
                    )
    _all_config_maps: list[tuple[str, dict[str, Any]]] = [
        ("meshtastic", meshtastic_configs),
        ("meshcore", meshcore_configs),
        ("lxmf", lxmf_configs),
        ("matrix", _matrix_configs),
    ]
    for _platform, _cfg_map in _all_config_maps:
        for _aid, _cfg in _cfg_map.items():
            source_attribution[_aid] = SourceAttributionConfig(
                adapter_id=_aid,
                platform=_platform,
                origin_label=getattr(_cfg, "origin_label", ""),
            )

    for module_path, class_name in _ADAPTER_RENDERER_SPECS:
        try:
            mod = __import__(module_path, fromlist=[class_name])
            renderer_cls = getattr(mod, class_name)
            # Pass all MeshtasticConfigs when constructing MeshtasticRenderer.
            # Only register when configs are available — the renderer rejects
            # an empty mapping at construction.
            if class_name == "MeshtasticRenderer":
                if not meshtastic_configs:
                    continue
                pipeline.register(
                    renderer_cls(
                        configs=meshtastic_configs,
                        source_attribution=source_attribution,
                    ),
                    priority=50,
                )
            elif class_name == "MeshCoreRenderer":
                if not meshcore_configs:
                    continue
                pipeline.register(
                    renderer_cls(
                        configs=meshcore_configs,
                        source_attribution=source_attribution,
                    ),
                    priority=50,
                )
            elif class_name == "MatrixRenderer":
                # MatrixRenderer uses target-local MatrixConfig.relay_prefix
                # for relay prefix resolution.  Register when Matrix configs
                # exist.  Meshtastic configs are passed as source_configs
                # for mmrelay wire compatibility only — they do not trigger
                # registration.
                if not _matrix_configs:
                    continue
                pipeline.register(
                    renderer_cls(
                        source_configs=meshtastic_configs,
                        source_attribution=source_attribution,
                        configs=_matrix_configs,
                    ),
                    priority=50,
                )
            elif class_name == "LxmfRenderer":
                pipeline.register(
                    renderer_cls(
                        configs=lxmf_configs,
                        source_attribution=source_attribution,
                    ),
                    priority=50,
                )
            else:
                pipeline.register(renderer_cls(), priority=50)
        except ImportError:
            _logger.debug(
                "Skipping renderer %s.%s (import failed)",
                module_path,
                class_name,
            )

    return source_attribution


# ---------------------------------------------------------------------------
# Sender-identity projection wiring (runtime -> core relation enrichment)
# ---------------------------------------------------------------------------


def _build_project_sender_metadata_fn(
    source_attribution: dict[str, SourceAttributionConfig],
) -> "SenderProjectionFn":
    """Build a sender-identity projection callback for relation enrichment.

    Returns a closure that adapts a target :class:`CanonicalEvent` into
    the JSON-safe generic field dict consumed by
    :class:`~medre.core.planning.relation_enricher.RelationEnricher`.  The
    closure delegates to the adapter-local attribution dispatch
    (:func:`medre.adapters._attribution_dispatch.project_source_fields`),
    passing the target event's native metadata, source adapter,
    source transport id, and a platform hint resolved from
    *source_attribution*.

    Layering: this helper lives in :mod:`medre.runtime` so core never
    imports adapter packages.  Core receives only the generic dict.

    Parameters
    ----------
    source_attribution:
        Mapping from adapter ID to :class:`SourceAttributionConfig`,
        built earlier in :meth:`RuntimeBuilder.build`.  Used to resolve
        a per-adapter ``platform_hint`` when the source adapter is
        registered there.
    """
    # Imported here (not at module top) to keep the static dependency
    # graph obvious: only the runtime wires adapter dispatch into core.
    from medre.adapters._attribution_dispatch import project_source_fields

    def _project(event: CanonicalEvent) -> Mapping[str, str | None]:
        native_data: dict[str, Any] = {}
        meta = getattr(event, "metadata", None)
        native = getattr(meta, "native", None) if meta is not None else None
        native_data_obj = getattr(native, "data", None) if native is not None else None
        if isinstance(native_data_obj, dict):
            native_data = dict(native_data_obj)

        source_info = source_attribution.get(getattr(event, "source_adapter", ""))
        platform_hint = getattr(source_info, "platform", None) if source_info else None

        return project_source_fields(
            native_data,
            source_adapter=getattr(event, "source_adapter", "") or "",
            source_transport_id=getattr(event, "source_transport_id", None),
            platform_hint=platform_hint,
        )

    return cast("SenderProjectionFn", _project)


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
        self._matrix_auto_join: dict[str, tuple[str, ...]] = {}

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
        source_attribution = _register_adapter_renderers(
            rendering_pipeline, config=self._config
        )
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
        adapters: dict[str, AdapterContract] = {}

        # 9. PipelineConfig + PipelineRunner
        route_stats = RouteStats()
        # Build the sender-identity projection callback that relation
        # enrichment uses to populate original_sender_displayname /
        # original_sender from generic projected fields.  Core planning
        # never imports adapter projection helpers; the runtime injects
        # this closure so layering is preserved.  The callback reads the
        # target event's native metadata, source_adapter, and
        # source_transport_id, then delegates to the adapter-local
        # dispatch (``project_source_fields``) which routes to the
        # appropriate per-transport projection helper.
        project_sender_metadata_fn = _build_project_sender_metadata_fn(
            source_attribution,
        )
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
            project_sender_metadata_fn=project_sender_metadata_fn,
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
        # 10.0 Build adapter_id → transport mapping for route expansion.
        adapter_platforms: dict[str, str] = {}
        for transport, adapter_id, _rtc in self._config.adapters.all_configs():
            adapter_platforms[adapter_id] = transport

        # 10.1 Derive Matrix auto-join rooms from route configuration
        #      before constructing adapters.
        self._matrix_auto_join = self._derive_matrix_auto_join_rooms(adapter_platforms)
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
            adapter_platforms=adapter_platforms,
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

    # -- Matrix auto-join room derivation ----------------------------------------

    def _derive_matrix_auto_join_rooms(
        self,
        adapter_platforms: dict[str, str],
    ) -> dict[str, tuple[str, ...]]:
        """Derive Matrix auto-join rooms from route configuration.

        For each Matrix adapter, collect canonical room IDs from:

        1. Route sources where the source adapter is a Matrix adapter
           and the source channel is a non-empty string starting with ``!``.
        2. Route targets where the target adapter is a Matrix adapter
           and the target channel is a non-empty string starting with ``!``.
        3. Explicit ``MatrixConfig.auto_join_rooms`` set by the operator.

        Also validates that if ``room_allowlist`` is explicitly set on a
        Matrix config, it must include every source-derived room for that
        adapter.

        Returns
        -------
        dict[str, tuple[str, ...]]
            Mapping from Matrix adapter ID to the merged tuple of room IDs
            to auto-join.

        Raises
        ------
        RuntimeConfigError
            If ``room_allowlist`` is explicitly set but omits a route-derived
            source room.
        """
        from medre.runtime.route_engine import build_runtime_routes

        # Build adapter_id → transport mapping for Matrix adapters.
        matrix_adapter_ids: set[str] = set()
        for transport, adapter_id, _rtc in self._config.adapters.all_configs():
            if transport == "matrix":
                matrix_adapter_ids.add(adapter_id)

        if not matrix_adapter_ids:
            return {}

        # Expand routes to get Route objects with channels.
        expanded_routes = build_runtime_routes(self._config.routes, adapter_platforms)

        # Collect rooms per adapter, tracking source vs all.
        source_rooms: dict[str, set[str]] = {aid: set() for aid in matrix_adapter_ids}
        all_rooms: dict[str, set[str]] = {aid: set() for aid in matrix_adapter_ids}

        for route in expanded_routes:
            if not route.enabled:
                continue

            # Source channel rooms.
            src = route.source.adapter
            src_channel = route.source.channel
            if (
                src is not None
                and src in matrix_adapter_ids
                and isinstance(src_channel, str)
                and src_channel.startswith("!")
            ):
                source_rooms[src].add(src_channel)
                all_rooms[src].add(src_channel)

            # Target channel rooms.
            for target in route.targets:
                tgt = target.adapter
                tgt_channel = target.channel
                if (
                    tgt is not None
                    and tgt in matrix_adapter_ids
                    and isinstance(tgt_channel, str)
                    and tgt_channel.startswith("!")
                ):
                    all_rooms[tgt].add(tgt_channel)

        # Merge with explicit auto_join_rooms from operator config.
        for transport, adapter_id, rtc in self._config.adapters.all_configs():
            if transport != "matrix" or rtc.config is None:
                continue
            explicit = getattr(rtc.config, "auto_join_rooms", ())
            if explicit:
                all_rooms[adapter_id].update(explicit)

        # Validate room_allowlist covers source rooms.
        for transport, adapter_id, rtc in self._config.adapters.all_configs():
            if transport != "matrix" or rtc.config is None:
                continue
            allowlist = getattr(rtc.config, "room_allowlist", None)
            if allowlist is not None:
                missing = source_rooms.get(adapter_id, set()) - allowlist
                if missing:
                    raise RuntimeConfigError(
                        f"Matrix adapter {adapter_id!r} has room_allowlist "
                        f"that omits source rooms from routes: "
                        f"{sorted(missing)}. Either add these rooms to "
                        f"room_allowlist or set room_allowlist to None to "
                        f"accept all rooms."
                    )

        # Build result: adapter_id → sorted tuple of merged rooms.
        return {
            aid: tuple(sorted(rooms))
            for aid, rooms in all_rooms.items()
            if rooms  # only include adapters that have rooms to join
        }

    # -- Adapter construction ----------------------------------------------------

    def _build_adapters(
        self, adapters: dict[str, AdapterContract]
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
                _logger.info("Constructed adapter %r (%s)", adapter_id, transport)
            except Exception as exc:
                wrapped = RuntimeConfigError(
                    f"Failed to build adapter {adapter_id!r} " f"({transport}): {exc}"
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
    ) -> AdapterContract:
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
            derived_store = (
                self._paths.adapter_transport_state_dir(adapter_id, "matrix") / "store"
            )
            config = replace(config, store_path=str(derived_store))

        # Inject auto-join rooms derived from route configuration.
        if transport == "matrix":
            extra_rooms = self._matrix_auto_join.get(adapter_id, ())
            if extra_rooms:
                existing = getattr(config, "auto_join_rooms", ())
                merged = tuple(sorted(set(existing) | set(extra_rooms)))
                config = replace(config, auto_join_rooms=merged)

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
