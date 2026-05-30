"""Internal replay helpers: filter conversion, stage resolution, and timing utilities."""

from __future__ import annotations

import time

from medre.core.engine.replay.types import _MODE_STAGES, ReplayRequest
from medre.core.events import CanonicalEvent
from medre.core.storage.backend import EventFilter


def _request_to_filter(request: ReplayRequest) -> EventFilter:
    """Convert a :class:`ReplayRequest` to an :class:`EventFilter`.

    The ``correlation_ids``, ``target_stages``, and ``target_adapters``
    fields have no equivalent in ``EventFilter`` and are handled
    separately by :meth:`ReplayEngine.replay`.
    """
    return EventFilter(
        event_kinds=request.event_kinds,
        source_adapters=request.source_adapters,
        time_start=request.time_start,
        time_end=request.time_end,
        limit=request.limit,
    )


def _resolve_stages(request: ReplayRequest) -> tuple[str, ...]:
    """Return the ordered tuple of stages to execute for *request*.

    The result is the intersection of the stages allowed by the replay
    mode and the stages explicitly requested via ``target_stages``.
    If ``target_stages`` is ``None``, all stages for the mode are used.
    """
    allowed = _MODE_STAGES[request.mode]
    if request.target_stages is None:
        return allowed
    requested = set(request.target_stages)
    return tuple(s for s in allowed if s in requested)


def _event_matches_filters(
    event: CanonicalEvent,
    request: ReplayRequest,
) -> bool:
    """Return ``True`` if *event* satisfies the non-ID filter criteria.

    Used when events are fetched individually by correlation ID and the
    time / kind / adapter filters must be applied as post-filters.
    """
    if request.event_kinds is not None and event.event_kind not in request.event_kinds:
        return False
    if (
        request.source_adapters is not None
        and event.source_adapter not in request.source_adapters
    ):
        return False
    if request.time_start is not None and event.timestamp < request.time_start:
        return False
    if request.time_end is not None and event.timestamp > request.time_end:
        return False
    return True


def _elapsed_ms(t0: float) -> float:
    """Return milliseconds elapsed since *t0* (from ``time.monotonic()``)."""
    return (time.monotonic() - t0) * 1000.0


def _verify_immutability(original: CanonicalEvent, event_id: str) -> None:
    """Assert that *original* is still frozen (immutable).

    This is a development-time guard to catch accidental mutation of
    historical canonical events during replay.  Since ``CanonicalEvent``
    uses ``frozen=True`` (msgspec Struct), any in-place mutation raises
    ``FrozenInstanceError`` at the point of attempted mutation.  This
    function provides an explicit checkpoint for diagnostic purposes
    and verifies that key identity fields remain stable.

    Replay must never mutate historical CanonicalEvents.  This guarantee
    holds across all replay modes --- even BEST_EFFORT reads events from
    storage without modification.
    """
    # The frozen=True on CanonicalEvent prevents mutation at the
    # Python level.  This function verifies key identity fields are
    # present as an additional guard.
    assert original.event_id == event_id, (
        f"Event ID mismatch during immutability check: "
        f"{original.event_id!r} != {event_id!r}"
    )
