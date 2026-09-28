"""Pre-admission inbound pressure aggregates for ``SQLiteStorage``.

Authority surface:
  - record_inbound_pressure: **upsert**.  One aggregate row per (window,
    source, outcome); each pre-admission rejection, timeout, or cursor-safe
    deferral increments the row for the current fixed window (or adds a
    batched ``count`` at once when the runtime flushes aggregated
    in-memory counters).  Counters and timestamps only — never payloads,
    sender identity, or transport-native content.  Writes are append-only
    upserts (no deletes, per the storage module's append-only invariant);
    growth is rate-bounded to one row per key per minute and only while
    pressure occurs.
  - list_inbound_pressure_observations: **list** (read-only), ordered by
    window; ``limit`` bounds the result in SQL to the newest windows.

Observations are operational evidence, not delivery evidence: they never
become canonical events, receipts, or outbox state, and they survive
runtime restarts by design — pressure history spans generations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

#: Fixed aggregation window size in seconds.  Windows are aligned to
#: multiples of this value (``ts - ts % WINDOW``), so aggregates are
#: deterministic and comparable across restarts.
PRESSURE_WINDOW_SECONDS: int = 60

_VALID_OUTCOMES = frozenset({"rejected", "timed_out", "deferred"})

_UPSERT_OBSERVATION = """
INSERT INTO inbound_pressure_observations (
    window_start, source, outcome, count, first_seen_at, last_seen_at
) VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(window_start, source, outcome) DO UPDATE SET
    count = count + excluded.count,
    last_seen_at = excluded.last_seen_at
"""


def pressure_window_start(unix_seconds: int) -> int:
    """Return the aligned window start for *unix_seconds*."""
    return unix_seconds - (unix_seconds % PRESSURE_WINDOW_SECONDS)


class _PressureMixin:
    """Pre-admission pressure aggregate methods for ``SQLiteStorage``."""

    if TYPE_CHECKING:

        async def _write(self, sql: str, params: tuple[Any, ...] = ()) -> None: ...

        async def _read_all(
            self, sql: str, params: tuple[Any, ...] = ()
        ) -> list[dict[str, Any]]: ...

    async def record_inbound_pressure(
        self,
        source: str,
        outcome: str,
        *,
        count: int = 1,
        unix_seconds: int | None = None,
        iso_timestamp: str | None = None,
    ) -> None:
        """Aggregate pressure events into durable storage.

        ``source`` is the adapter id (or the anonymous-source sentinel);
        ``outcome`` is one of ``rejected``, ``timed_out``, ``deferred``;
        ``count`` batches multiple events of one key into a single upsert
        (the runtime flushes aggregated in-memory counters this way).  The
        write is an append-only upsert onto the current window's aggregate
        row: no row is ever deleted (storage append-only invariant), and
        growth is rate-bounded to one row per key per window while
        pressure occurs.
        """
        if outcome not in _VALID_OUTCOMES:
            raise ValueError(
                f"invalid pressure outcome {outcome!r}; "
                f"expected one of {sorted(_VALID_OUTCOMES)}"
            )
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise ValueError(f"count must be a positive integer, got {count!r}")
        from datetime import datetime, timezone

        if unix_seconds is None:
            unix_seconds = int(datetime.now(timezone.utc).timestamp())
        if iso_timestamp is None:
            # Derive the observation timestamp from the same instant as
            # the window so one row never describes two different times.
            iso_timestamp = datetime.fromtimestamp(
                int(unix_seconds), tz=timezone.utc
            ).isoformat()
        window_start = pressure_window_start(int(unix_seconds))
        await self._write(
            _UPSERT_OBSERVATION,
            (window_start, source, outcome, count, iso_timestamp, iso_timestamp),
        )

    async def list_inbound_pressure_observations(
        self,
        *,
        source: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return pressure aggregates ordered by window start (ascending).

        Rows carry counters and timestamps only (window_start, source,
        outcome, count, first_seen_at, last_seen_at).  ``limit`` bounds the
        result in SQL to the newest windows (``ORDER BY ... DESC LIMIT``
        reversed back to ascending), so memory and read latency stay
        constant regardless of total pressure history; without it the
        listing is unbounded by design for full audits.
        """
        # A read-only open of a schema-version-1 database created before
        # this additive table skips DDL, so the table may legitimately not
        # exist — that is an empty pressure history, not corruption.
        from medre.core.storage.backend import StorageError

        base = (
            "SELECT window_start, source, outcome, count, "
            "first_seen_at, last_seen_at "
            "FROM inbound_pressure_observations "
        )
        where = "" if source is None else "WHERE source = ? "
        params: tuple[Any, ...] = () if source is None else (source,)
        if limit is None:
            try:
                return await self._read_all(
                    base + where + "ORDER BY window_start, source, outcome",
                    params,
                )
            except StorageError as exc:
                if "inbound_pressure_observations" in str(exc) and (
                    "no such table" in str(exc)
                ):
                    return []
                raise
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError(f"limit must be a positive integer, got {limit!r}")
        try:
            rows = await self._read_all(
                base + where + "ORDER BY window_start DESC, source DESC, outcome DESC "
                "LIMIT ?",
                (*params, limit),
            )
        except StorageError as exc:
            if "inbound_pressure_observations" in str(exc) and (
                "no such table" in str(exc)
            ):
                return []
            raise
        rows.reverse()
        return rows
