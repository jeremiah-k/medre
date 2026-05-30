"""Replay rendering stage: transform and render event output."""

from __future__ import annotations

import logging
import time

from medre.core.engine.replay.helpers import _elapsed_ms
from medre.core.engine.replay.types import ReplayMode, ReplayResult
from medre.core.events import CanonicalEvent

_logger = logging.getLogger(__name__)


class _ReplayRenderingMixin:
    """Mixin providing the render stage for replay execution."""

    async def _stage_render(
        self,
        event: CanonicalEvent,
        mode: ReplayMode,
    ) -> ReplayResult:
        """Re-run transforms and rendering on *event*.

        Applies transforms first (via ``pipeline.transform_event``) and
        then renders the transformed event (via ``pipeline.render_event``).
        Captures the rendering output without delivering it.  Read-only.
        """
        t0 = time.monotonic()
        if self._pipeline is None:
            return ReplayResult(
                event_id=event.event_id,
                stage="render",
                status="error",
                error="No pipeline configured; rendering requires a pipeline",
                duration_ms=_elapsed_ms(t0),
            )
        try:
            if hasattr(self._pipeline, "transform_event"):
                transformed = await self._pipeline.transform_event(event)
            else:
                _logger.debug(
                    "Pipeline has no transform_event; skipping transform "
                    "for event_id=%s",
                    event.event_id,
                )
                transformed = event
            if hasattr(self._pipeline, "render_event"):
                rendered = await self._pipeline.render_event(transformed)
            else:
                _logger.debug(
                    "Pipeline has no render_event; skipping render " "for event_id=%s",
                    event.event_id,
                )
                rendered = transformed
            return ReplayResult(
                event_id=event.event_id,
                stage="render",
                status="passed",
                output=rendered,
                duration_ms=_elapsed_ms(t0),
            )
        except Exception as exc:
            if self._diagnostician is not None:
                self._diagnostician.record_renderer_failure(
                    event.event_id,
                    "replay",
                    str(exc),
                )
            return ReplayResult(
                event_id=event.event_id,
                stage="render",
                status="error",
                error=str(exc),
                duration_ms=_elapsed_ms(t0),
            )
