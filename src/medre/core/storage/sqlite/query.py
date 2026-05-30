"""Query builder for the SQLite storage layer.

Pure functions that construct parameterised SQL from filter objects.
No dependency on sibling submodules.
"""

from __future__ import annotations

from typing import Any

from medre.core.storage.backend import EventFilter


def _build_query_sql(filt: EventFilter) -> tuple[str, tuple[Any, ...]]:
    """Build a parameterised ``SELECT`` for ``canonical_events``.

    Raises
    ------
    ValueError
        If ``filt.limit`` is not a non-negative integer.
    """
    if not isinstance(filt.limit, int) or filt.limit < 0:
        raise ValueError(
            f"EventFilter.limit must be a non-negative integer, got {filt.limit!r}"
        )

    clauses: list[str] = []
    params: list[Any] = []

    if filt.event_kinds:
        holders = ",".join("?" for _ in filt.event_kinds)
        clauses.append(f"event_kind IN ({holders})")
        params.extend(filt.event_kinds)

    if filt.source_adapters:
        holders = ",".join("?" for _ in filt.source_adapters)
        clauses.append(f"source_adapter IN ({holders})")
        params.extend(filt.source_adapters)

    if filt.time_start:
        clauses.append("timestamp >= ?")
        params.append(filt.time_start.isoformat())

    if filt.time_end:
        clauses.append("timestamp <= ?")
        params.append(filt.time_end.isoformat())

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"SELECT * FROM canonical_events{where} ORDER BY timestamp ASC, event_id ASC LIMIT ?"  # nosec: clauses are hardcoded field names, values via ? parameters
    params.append(filt.limit)
    return sql, tuple(params)
