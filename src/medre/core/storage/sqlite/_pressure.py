"""Pre-admission inbound pressure aggregates for ``SQLiteStorage``.

Authority surface:
  - record_inbound_pressure: **upsert**.  One aggregate row per (window,
    source, outcome); each pre-admission admission-gate refusal
    (``rejected``, ``timed_out``, ``deferred``) or producer-side staging
    fact (``scheduled``, ``overflow``, ``cancelled_before_admission``)
    increments the row for the current fixed window (or adds a batched
    ``count`` at once when the runtime flushes aggregated in-memory
    counters).  Counters and timestamps only — never payloads, sender
    identity, or transport-native content.  Writes are append-only
    upserts (no deletes, per the storage module's append-only invariant);
    growth is rate-bounded to one row per key per minute whenever that
    outcome occurs, including healthy producer-side `scheduled` staging.
  - list_inbound_pressure_observations: **list** (read-only), ordered by
    window; ``limit`` bounds the result in SQL to the newest windows.
  - record_inbound_pressure_outage_gap: **append-only insert**.  One
    accounting-gap row per detected storage outage: the content-free
    interval bounds plus saturating refusal counters (observed, retained,
    discarded).  The row is the durable distinction between "zero
    refusals occurred during the outage" and "refusals occurred and some
    retained facts were discarded".
  - list_inbound_pressure_outage_gaps: **list** (read-only), ordered by
    insertion; ``limit`` bounds the result in SQL to the newest records.

The outcome vocabulary is closed with two halves.  Admission-gate
outcomes are core facts observed at the bounded admission boundary;
producer outcomes are adapter-reported pre-admission facts observed where
messages are produced.  Neither half ever claims durable admission.

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

#: Closed pressure outcome vocabulary: the three admission-gate outcomes
#: recorded by the runtime gate plus the three producer-side outcomes
#: reported by adapters through the producer pressure sink.
_VALID_OUTCOMES = frozenset(
    {
        "rejected",
        "timed_out",
        "deferred",
        "scheduled",
        "overflow",
        "cancelled_before_admission",
    }
)

# SQLite stores nonnegative pressure counts as signed 64-bit integers.  A
# repeated upsert must never promote an overflowing sum to REAL or fail a
# subsequent writer; saturation is the explicit extreme-count boundary.
SQLITE_MAX_PRESSURE_COUNT: int = (1 << 63) - 1

_UPSERT_OBSERVATION = f"""
INSERT INTO inbound_pressure_observations (
    window_start, source, outcome, count, first_seen_at, last_seen_at
) VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(window_start, source, outcome) DO UPDATE SET
    count = CASE
        WHEN count > {SQLITE_MAX_PRESSURE_COUNT} - excluded.count
        THEN {SQLITE_MAX_PRESSURE_COUNT}
        ELSE count + excluded.count
    END,
    first_seen_at = MIN(first_seen_at, excluded.first_seen_at),
    last_seen_at = MAX(last_seen_at, excluded.last_seen_at)
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
        last_unix_seconds: int | None = None,
        iso_timestamp: str | None = None,
    ) -> None:
        """Aggregate pressure events into durable storage.

        ``source`` is the adapter id (or the anonymous-source sentinel);
        ``outcome`` is one of the admission-gate outcomes ``rejected``,
        ``timed_out``, ``deferred`` or one of the producer-side outcomes
        ``scheduled``, ``overflow``, ``cancelled_before_admission``;
        ``count`` batches multiple events of one key into a single upsert
        (the runtime flushes aggregated in-memory counters this way).
        ``unix_seconds`` and ``last_unix_seconds`` bound that batch's actual
        observation times and must remain inside one fixed window.  The
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
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or not 1 <= count <= SQLITE_MAX_PRESSURE_COUNT
        ):
            raise ValueError(
                f"count must be a positive integer within SQLite range, got {count!r}"
            )
        from datetime import datetime, timezone

        if unix_seconds is None:
            unix_seconds = int(datetime.now(timezone.utc).timestamp())
        if last_unix_seconds is None:
            last_unix_seconds = unix_seconds
        first_seconds = int(unix_seconds)
        last_seconds = int(last_unix_seconds)
        if last_seconds < first_seconds:
            raise ValueError("last pressure timestamp must not precede first")
        window_start = pressure_window_start(first_seconds)
        if pressure_window_start(last_seconds) != window_start:
            raise ValueError(
                "batched pressure timestamps must belong to the same window"
            )
        if iso_timestamp is None:
            # Derive the observation timestamp from the same instant as
            # the event so one row never describes two different times.
            iso_timestamp = datetime.fromtimestamp(
                first_seconds, tz=timezone.utc
            ).isoformat()
        last_iso_timestamp = (
            iso_timestamp
            if last_seconds == first_seconds
            else datetime.fromtimestamp(last_seconds, tz=timezone.utc).isoformat()
        )
        await self._write(
            _UPSERT_OBSERVATION,
            (
                window_start,
                source,
                outcome,
                count,
                iso_timestamp,
                last_iso_timestamp,
            ),
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
        base = (
            "SELECT window_start, source, outcome, count, "
            "first_seen_at, last_seen_at "
            "FROM inbound_pressure_observations "
        )
        where = "" if source is None else "WHERE source = ? "
        params: tuple[Any, ...] = () if source is None else (source,)
        if limit is None:
            return await self._read_all(
                base + where + "ORDER BY window_start, source, outcome", params
            )
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError(f"limit must be a positive integer, got {limit!r}")
        rows = await self._read_all(
            base + where + "ORDER BY window_start DESC, source DESC, outcome DESC "
            "LIMIT ?",
            (*params, limit),
        )
        rows.reverse()
        return rows

    async def record_inbound_pressure_outage_gap(
        self,
        *,
        opened_unix_seconds: int,
        recovered_unix_seconds: int,
        refusals_observed: int,
        refusals_retained: int,
        refusals_discarded: int,
    ) -> None:
        """Append one accounting-gap record for a detected storage outage.

        The record is content-free accounting: ``opened_unix_seconds`` and
        ``recovered_unix_seconds`` bound the detected outage interval,
        and the three counters carry saturating refusal facts — how many
        gate refusals were observed while the outage was open, how many
        refusal facts were still retained when recovery persisted (the
        recovery batch's gate-half total), and how many were discarded
        from retention under the documented bound.  No adapter identity,
        window, payload, sender, or transport-native content is stored.
        The row lets a reader distinguish "zero refusals occurred during
        the outage" (``refusals_observed = 0``) from "refusals occurred
        and some retained facts were discarded"; it never becomes a
        canonical event, receipt, or outbox entry, and it is never
        updated or deleted (append-only invariant).
        """
        counters = {
            "refusals_observed": refusals_observed,
            "refusals_retained": refusals_retained,
            "refusals_discarded": refusals_discarded,
        }
        for name, value in counters.items():
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 0 <= value <= SQLITE_MAX_PRESSURE_COUNT
            ):
                raise ValueError(
                    f"{name} must be a nonnegative integer within SQLite "
                    f"range, got {value!r}"
                )
        if not isinstance(opened_unix_seconds, int) or isinstance(
            opened_unix_seconds, bool
        ):
            raise ValueError(
                f"opened_unix_seconds must be an integer, got {opened_unix_seconds!r}"
            )
        if not isinstance(recovered_unix_seconds, int) or isinstance(
            recovered_unix_seconds, bool
        ):
            raise ValueError(
                f"recovered_unix_seconds must be an integer, "
                f"got {recovered_unix_seconds!r}"
            )
        if recovered_unix_seconds < opened_unix_seconds:
            raise ValueError("recovery timestamp must not precede outage open")
        from datetime import datetime, timezone

        opened_at = datetime.fromtimestamp(
            opened_unix_seconds, tz=timezone.utc
        ).isoformat()
        recovered_at = datetime.fromtimestamp(
            recovered_unix_seconds, tz=timezone.utc
        ).isoformat()
        await self._write(
            "INSERT INTO inbound_pressure_outage_gaps ("
            "opened_at, recovered_at, refusals_observed, refusals_retained, "
            "refusals_discarded) VALUES (?, ?, ?, ?, ?)",
            (
                opened_at,
                recovered_at,
                refusals_observed,
                refusals_retained,
                refusals_discarded,
            ),
        )

    async def list_inbound_pressure_outage_gaps(
        self,
        *,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return outage-gap records ordered by insertion (ascending).

        Rows carry the content-free accounting shape only (gap_id,
        opened_at, recovered_at, and the three saturating counters).
        ``limit`` bounds the result in SQL to the newest records
        (``ORDER BY ... DESC LIMIT`` reversed back to ascending).
        """
        base = (
            "SELECT gap_id, opened_at, recovered_at, refusals_observed, "
            "refusals_retained, refusals_discarded "
            "FROM inbound_pressure_outage_gaps "
        )
        if limit is None:
            return await self._read_all(base + "ORDER BY gap_id")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError(f"limit must be a positive integer, got {limit!r}")
        rows = await self._read_all(base + "ORDER BY gap_id DESC LIMIT ?", (limit,))
        rows.reverse()
        return rows
