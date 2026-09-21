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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping, cast

from medre.adapter_registry import (
    AdapterSpec,
    get_adapter_spec,
    iter_adapter_specs,
)
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
# Registry-driven adapter assembly
# ---------------------------------------------------------------------------


def _build_source_attribution(
    config: RuntimeConfig | None,
) -> dict[str, SourceAttributionConfig]:
    """Build the platform-neutral source-attribution map for enabled adapters."""
    result: dict[str, SourceAttributionConfig] = {}
    if config is None:
        return result
    for transport, adapter_id, rtc in config.adapters.all_configs():
        if not rtc.enabled:
            continue
        adapter_config = getattr(rtc, "config", None)
        result[adapter_id] = SourceAttributionConfig(
            adapter_id=adapter_id,
            platform=transport,
            origin_label=getattr(adapter_config, "origin_label", "")
            if adapter_config is not None
            else "",
        )
    return result


def _register_adapter_renderers(
    pipeline: RenderingPipeline, config: RuntimeConfig | None = None
) -> dict[str, SourceAttributionConfig]:
    """Register transport renderers from the authoritative adapter registry.

    Renderer constructor differences remain adapter-owned: each registered
    renderer factory receives that transport's runtime configs, the full
    transport-group mapping for cross-transport rendering context, and the
    generic source-attribution map.  The runtime builder contains no
    transport-specific constructor branches.
    """
    source_attribution = _build_source_attribution(config)
    if config is None:
        return source_attribution

    all_runtime_configs: dict[str, Mapping[str, Any]] = {
        transport: group for transport, group in config.adapters.groups()
    }
    for spec in iter_adapter_specs():
        if spec.renderer_factory is None:
            continue
        try:
            factory = spec.renderer_factory.load()
            renderer = factory(
                runtime_configs=all_runtime_configs[spec.transport],
                all_runtime_configs=all_runtime_configs,
                source_attribution=source_attribution,
            )
        except ImportError as exc:
            _logger.debug(
                "Skipping renderer factory for %s (import failed: %s)",
                spec.transport,
                exc,
            )
            continue
        if renderer is not None:
            pipeline.register(renderer, priority=50)
    return source_attribution


def _dependency_available(spec: AdapterSpec) -> bool:
    """Return whether the optional SDK dependency declared by *spec* exists."""
    if spec.dependency_probe is None:
        return True
    try:
        return bool(spec.dependency_probe.load())
    except ImportError:
        return False


def _build_fake_adapter(spec: AdapterSpec, adapter_id: str) -> AdapterContract:
    """Construct the registered fake adapter without importing live SDKs."""
    try:
        fake_cls = spec.fake_adapter.load()
        return cast(AdapterContract, fake_cls(adapter_id=adapter_id))
    except (ImportError, AttributeError, TypeError) as exc:
        raise RuntimeConfigError(
            f"Could not construct fake adapter {adapter_id!r} "
            f"for transport {spec.transport!r}: {exc}"
        ) from exc


def _build_real_adapter(spec: AdapterSpec, config: Any) -> AdapterContract | None:
    """Construct the registered real adapter, or ``None`` when its SDK is absent.

    Once the explicit dependency probe succeeds, import/construction failures
    are real adapter defects and must propagate instead of being mislabeled as
    an optional dependency that is not installed.
    """
    if not _dependency_available(spec):
        return None
    adapter_cls = spec.adapter.load()
    return cast(AdapterContract, adapter_cls(config))


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
        self._adapter_preparation_routes: tuple[Any, ...] = ()

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
        # can_render() checks target_platform, so registering all built-ins
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

        # 10.1 Expand routes once for adapter-owned runtime preparation hooks.
        #      The generic builder does not interpret transport-specific route
        #      semantics; registered adapters may opt into a preparation hook.
        from medre.runtime.route_engine import build_runtime_routes

        self._adapter_preparation_routes = tuple(
            build_runtime_routes(self._config.routes, adapter_platforms)
        )
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
        """Construct one enabled adapter from its registered specification."""
        spec = get_adapter_spec(transport)
        if spec is None:
            known = ", ".join(s.transport for s in iter_adapter_specs())
            raise RuntimeConfigError(
                f"Unknown transport type {transport!r} for adapter "
                f"{adapter_id!r}. Known types: {known}"
            )

        adapter_kind = getattr(rtc, "adapter_kind", "real")
        if adapter_kind == "fake":
            return _build_fake_adapter(spec, adapter_id)

        config = rtc.config
        if config is None:
            raise RuntimeConfigError(
                f"Adapter {adapter_id!r} ({transport}) is enabled but has no config"
            )

        if spec.runtime_config_preparer is not None:
            try:
                prepare = spec.runtime_config_preparer.load()
                config = prepare(
                    config,
                    adapter_id=adapter_id,
                    paths=self._paths,
                    routes=self._adapter_preparation_routes,
                )
            except Exception as exc:
                raise RuntimeConfigError(
                    f"Failed to prepare adapter {adapter_id!r} ({transport}): {exc}"
                ) from exc

        try:
            adapter = _build_real_adapter(spec, config)
        except Exception as exc:
            raise RuntimeConfigError(
                f"Failed to build adapter {adapter_id!r} ({transport}): {exc}"
            ) from exc
        if adapter is None:
            raise RuntimeConfigError(
                f"Adapter {adapter_id!r} ({transport}) is enabled but could "
                f"not be built: the optional SDK dependency is not installed. "
                f"Install it with: pip install medre[{spec.install_extra}]"
            )
        return adapter
