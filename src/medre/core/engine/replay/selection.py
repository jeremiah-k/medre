"""Replay selection: event iteration, counting, and missing-event handling."""

from __future__ import annotations

from collections.abc import AsyncIterator

from medre.core.engine.replay.helpers import _event_matches_filters, _request_to_filter
from medre.core.engine.replay.types import ReplayRequest, ReplayResult
from medre.core.events import CanonicalEvent


class _ReplaySelectionMixin:
    """Mixin providing event selection helpers for the replay engine.

    Relies on ``self._storage``, ``self._diagnostician``, and
    ``self._accounting`` being set by the concrete base class via MRO.
    """

    async def count_matching(self, request: ReplayRequest) -> int:
        """Return the number of events matching *request* without replaying.

        Follows the same dual-path strategy as :meth:`replay`: individual
        gets when ``correlation_ids`` is set, storage query otherwise.

        **Contract**: When ``correlation_ids`` is set, the count includes
        IDs that do not exist in storage (missing events).  This is
        consistent with :meth:`replay`, which emits ``"failed"`` results
        for missing correlation IDs.  Use the count as a work estimate,
        not a "exists-in-storage" count.

        Parameters
        ----------
        request:
            Filter specification.

        Returns
        -------
        int
            Count of matching events (including missing IDs).
        """
        count = 0

        if request.correlation_ids is not None:
            for eid in request.correlation_ids:
                if count >= request.limit:
                    break
                event = await self._storage.get(eid)
                if event is None:
                    # Missing ID — count it to stay consistent with
                    # _iter_by_ids and replay().
                    count += 1
                elif _event_matches_filters(event, request):
                    count += 1
        else:
            event_filter = _request_to_filter(request)
            async for _ in self._storage.query(event_filter):  # type: ignore[union-attr]
                count += 1

        return count

    async def _iter_by_ids(
        self,
        request: ReplayRequest,
    ) -> AsyncIterator[tuple[str, CanonicalEvent | None]]:
        """Yield ``(event_id, event | None)`` tuples for correlation IDs.

        For each requested ID, fetches the event from storage.  If the
        event does not exist, ``(event_id, None)`` is yielded so that
        the caller can report the failure.  If the event exists but does
        not match the filter criteria (time, kind, adapter), the pair is
        skipped entirely.

        Respects the ``limit`` on *request*.
        """
        yielded = 0
        ids = request.correlation_ids
        if ids is None:
            return
        for eid in ids:
            if yielded >= request.limit:
                break
            event = await self._storage.get(eid)
            if event is None:
                yielded += 1
                yield (eid, None)
                continue
            if not _event_matches_filters(event, request):
                continue
            yielded += 1
            yield (eid, event)

    async def _replay_missing(
        self,
        event_id: str,
        stages: tuple[str, ...],
    ) -> AsyncIterator[ReplayResult]:
        """Yield results for an event that could not be found in storage.

        The first stage (``store``) receives ``"failed"`` status; all
        subsequent stages receive ``"skipped"``.
        """
        if self._diagnostician is not None:
            self._diagnostician.record_replay_skip(
                event_id,
                "Event not found in storage",
            )
        if self._accounting is not None:
            self._accounting.record_replay_rejected()
        for stage in stages:
            if stage == "store":
                yield ReplayResult(
                    event_id=event_id,
                    stage="store",
                    status="failed",
                    error="Event not found in storage",
                )
            else:
                yield ReplayResult(
                    event_id=event_id,
                    stage=stage,
                    status="skipped",
                    error="Event not found in storage; upstream stages failed",
                )
