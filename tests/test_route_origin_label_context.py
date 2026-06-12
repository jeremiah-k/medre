"""Tests for route-level source-context ``origin_label`` plumbing.

Verifies the minimal source-side origin-label foundation:

* :class:`RouteConfig.origin_label` is threaded through the route engine
  into :attr:`RouteSource.origin_label`.
* :meth:`RenderingPipeline.render` threads ``source_origin_label`` into
  the frozen :class:`RenderingContext` available to renderers.
* Precedence intent: when a route label is set it appears on the context
  (overriding the adapter origin label, which renderers resolve in a
  later wave); when unset the context field is ``None`` so renderers
  fall back to the adapter origin label; missing label is always safe.

These tests use a test-double renderer so they do not depend on adapter
renderer changes (owned by a separate wave).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from medre.config.routes import RouteConfig, RouteDirectionality
from medre.core.events import CanonicalEvent, EventMetadata
from medre.core.rendering.renderer import (
    RenderingContext,
    RenderingPipeline,
    RenderingResult,
)
from medre.core.routing.models import RouteSource
from medre.runtime.route_engine import _expand_route_config

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event() -> CanonicalEvent:
    return CanonicalEvent(
        event_id="evt-1",
        event_kind="message.created",
        schema_version=1,
        timestamp=datetime.now(timezone.utc),
        source_adapter="src-a",
        source_transport_id="node-1",
        source_channel_id="0",
        parent_event_id=None,
        lineage=(),
        relations=(),
        payload={"body": "hello"},
        metadata=EventMetadata(),
    )


@dataclass
class _CapturingRenderer:
    """Test-double renderer that records the RenderingContext it receives.

    Implements the structural :class:`Renderer` protocol without any
    adapter dependencies.  The captured context lets tests assert which
    ``source_origin_label`` was made available to renderers.
    """

    captured: RenderingContext | None = None
    _name: str = "capturing"

    @property
    def name(self) -> str:
        return self._name

    def can_render(self, event: CanonicalEvent, ctx: RenderingContext) -> bool:
        return True

    async def render(
        self, event: CanonicalEvent, ctx: RenderingContext
    ) -> RenderingResult:
        object.__setattr__(self, "captured", ctx)
        return RenderingResult(
            event_id=event.event_id,
            target_adapter=ctx.target_adapter,
            target_channel=ctx.target_channel,
            payload={"text": event.payload.get("body", "")},
        )


# ---------------------------------------------------------------------------
# Route engine: RouteConfig.origin_label -> RouteSource.origin_label
# ---------------------------------------------------------------------------


class TestRouteEngineOriginLabelThreading:
    """The route engine carries origin_label onto the source descriptor."""

    def test_standard_expansion_threads_origin_label(self) -> None:
        rc = RouteConfig.from_toml_dict(
            "r1",
            {
                "source_adapters": ["src-a"],
                "dest_adapters": ["dst-b"],
                "origin_label": "East Relay",
            },
        )
        routes = _expand_route_config(rc)
        assert len(routes) == 1
        assert routes[0].source.origin_label == "East Relay"

    def test_standard_expansion_none_when_unset(self) -> None:
        rc = RouteConfig.from_toml_dict(
            "r2",
            {"source_adapters": ["src-a"], "dest_adapters": ["dst-b"]},
        )
        routes = _expand_route_config(rc)
        assert len(routes) == 1
        assert routes[0].source.origin_label is None

    def test_swap_direction_keeps_origin_label(self) -> None:
        """dest_to_source reverse leg still carries the route label."""
        rc = RouteConfig.from_toml_dict(
            "r3",
            {
                "source_adapters": ["src-a"],
                "dest_adapters": ["dst-b"],
                "directionality": RouteDirectionality.DEST_TO_SOURCE.value,
                "origin_label": "West Relay",
            },
        )
        routes = _expand_route_config(rc, swap_direction=True)
        assert len(routes) == 1
        assert routes[0].source.origin_label == "West Relay"

    def test_multi_source_threads_label_to_each_leg(self) -> None:
        rc = RouteConfig.from_toml_dict(
            "r4",
            {
                "source_adapters": ["src-a", "src-c"],
                "dest_adapters": ["dst-b"],
                "origin_label": "Shared Label",
            },
        )
        routes = _expand_route_config(rc)
        assert len(routes) == 2
        assert all(r.source.origin_label == "Shared Label" for r in routes)


# ---------------------------------------------------------------------------
# RouteSource model
# ---------------------------------------------------------------------------


class TestRouteSourceOriginLabel:
    """RouteSource defaults origin_label to None and is frozen."""

    def test_default_none(self) -> None:
        rs = RouteSource(adapter="a", event_kinds=(), channel=None)
        assert rs.origin_label is None

    def test_set_value(self) -> None:
        rs = RouteSource(adapter="a", event_kinds=(), channel=None, origin_label="L")
        assert rs.origin_label == "L"


# ---------------------------------------------------------------------------
# RenderingPipeline: source_origin_label -> RenderingContext
# ---------------------------------------------------------------------------


class TestPipelineSourceOriginLabelContext:
    """The pipeline threads source_origin_label into the render context."""

    async def test_route_label_appears_on_context(self) -> None:
        """A route-level label reaches the renderer's context."""
        pipeline = RenderingPipeline()
        renderer = _CapturingRenderer()
        pipeline.register(renderer, priority=10)

        await pipeline.render(
            _make_event(),
            "dst-b",
            source_origin_label="Route Override Label",
        )

        assert renderer.captured is not None
        assert renderer.captured.source_origin_label == "Route Override Label"

    async def test_no_route_label_is_none_on_context(self) -> None:
        """When no route label is set, the context field is None (safe)."""
        pipeline = RenderingPipeline()
        renderer = _CapturingRenderer()
        pipeline.register(renderer, priority=10)

        await pipeline.render(_make_event(), "dst-b")

        assert renderer.captured is not None
        assert renderer.captured.source_origin_label is None

    async def test_route_label_overrides_adapter_label_intent(self) -> None:
        """Precedence proof at the context level.

        The route/context label is the highest-precedence source for
        origin attribution.  When present on the context, a renderer
        (later wave) MUST prefer it over the adapter origin label from
        the source-attribution registry.  This test asserts the context
        carries the route label so that the override is observable; the
        adapter-label fallback case is covered by the None test above.
        """
        pipeline = RenderingPipeline()
        renderer = _CapturingRenderer()
        pipeline.register(renderer, priority=10)

        # Simulate the delivery pipeline handing the route label through.
        await pipeline.render(
            _make_event(),
            "dst-b",
            source_origin_label="Route Label Wins",
        )

        assert renderer.captured is not None
        # The context exposes the route label; renderers resolve precedence
        # as: context label > adapter origin_label > native > empty.
        assert renderer.captured.source_origin_label == "Route Label Wins"
